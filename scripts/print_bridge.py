#!/usr/bin/env python3
"""
print_bridge.py — Store-side print bridge for Spice Town Labels.

Runs on ANY always-on device on the store WiFi (an Android tablet in Termux,
a Raspberry Pi, an old laptop). It connects the cloud-hosted app to the local
Brother QL-810W:

    cloud /api/bridge  --claim job-->  this script  --raster bytes-->  printer:9100

The heavy lifting (label rendering + Brother raster conversion) happens on the
SERVER, so this script needs NOTHING beyond the Python standard library.
On Android: install Termux, `pkg install python`, copy this file, run it.

Usage:
    python print_bridge.py \
        --server https://spicetown-labels.onrender.com \
        --token  <STL_BRIDGE_TOKEN> \
        --printer 192.168.1.50

Every flag can also come from the environment (BRIDGE_SERVER, BRIDGE_TOKEN,
BRIDGE_PRINTER, BRIDGE_PRINTER_PORT, BRIDGE_POLL_SECONDS, BRIDGE_SPOOL_DIR).

Test without a printer:
    python print_bridge.py --server ... --token ... --spool ./spool
(writes the raw raster files to ./spool instead of printing)

Find the printer's IP: print the QL-810W's network status page (hold the cut
button), check your router's client list, or give it a DHCP reservation so the
address never changes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request

log = logging.getLogger("print-bridge")

DEFAULT_POLL_SECONDS = 2.0
NETWORK_BACKOFF_CAP = 60.0
PRINT_RETRIES = 2  # local attempts per job before reporting an error


class BridgeConfig:
    def __init__(self, args: argparse.Namespace) -> None:
        self.server = (args.server or os.getenv("BRIDGE_SERVER", "")).rstrip("/")
        self.token = args.token or os.getenv("BRIDGE_TOKEN", "")
        self.printer_host = args.printer or os.getenv("BRIDGE_PRINTER", "")
        self.printer_port = int(args.port or os.getenv("BRIDGE_PRINTER_PORT", "9100"))
        self.poll_seconds = float(
            args.poll or os.getenv("BRIDGE_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))
        )
        self.spool_dir = args.spool or os.getenv("BRIDGE_SPOOL_DIR", "")

        if not self.server or not self.token:
            sys.exit("error: --server and --token are required (or BRIDGE_SERVER/BRIDGE_TOKEN)")
        if not self.printer_host and not self.spool_dir:
            sys.exit("error: give --printer <ip> to print, or --spool <dir> to test without one")


# ── Server API ────────────────────────────────────────────────────────────────
def _request(cfg: BridgeConfig, method: str, path: str, body: dict | None = None):
    """One HTTP call to the bridge API. Returns (status, bytes)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        cfg.server + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {cfg.token}",
            "Content-Type": "application/json",
            "User-Agent": "spicetown-print-bridge/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status, resp.read()


def claim_next_job(cfg: BridgeConfig) -> dict | None:
    status, payload = _request(cfg, "POST", "/api/bridge/jobs/next")
    if status == 204 or not payload:
        return None
    return json.loads(payload)["job"]


def fetch_raster(cfg: BridgeConfig, job_id: int) -> bytes:
    status, payload = _request(cfg, "GET", f"/api/bridge/jobs/{job_id}/raster")
    if status != 200 or not payload:
        raise RuntimeError(f"raster fetch failed (http {status})")
    return payload


def report(cfg: BridgeConfig, job_id: int, status: str, error: str | None = None) -> None:
    body = {"status": status}
    if error:
        body["error"] = error[:500]
    _request(cfg, "POST", f"/api/bridge/jobs/{job_id}/complete", body)


# ── Printer output ────────────────────────────────────────────────────────────
def send_to_printer(cfg: BridgeConfig, raster: bytes) -> None:
    """Stream Brother raster bytes to the QL-810W's raw print port (9100)."""
    with socket.create_connection((cfg.printer_host, cfg.printer_port), timeout=30) as s:
        s.sendall(raster)
        # Give the printer a moment to buffer before we drop the connection.
        s.shutdown(socket.SHUT_WR)
        s.settimeout(5)
        try:
            s.recv(32)  # some firmwares send a status block; ignore content
        except (socket.timeout, OSError):
            pass


def spool_to_file(cfg: BridgeConfig, raster: bytes, job_id: int) -> str:
    os.makedirs(cfg.spool_dir, exist_ok=True)
    path = os.path.join(cfg.spool_dir, f"job_{job_id}_{int(time.time())}.bin")
    with open(path, "wb") as fh:
        fh.write(raster)
    return path


def print_job(cfg: BridgeConfig, job: dict) -> None:
    job_id = job["id"]
    try:
        raster = fetch_raster(cfg, job_id)
    except urllib.error.HTTPError as exc:
        # Server refused to render (product vanished, conversion bug). The job
        # can never succeed — report it instead of leaving it claimed.
        log.error("job %s: raster fetch rejected (%s)", job_id, exc)
        report(cfg, job_id, "error", f"raster fetch failed: {exc}")
        return
    log.info(
        "job %s: upc=%s variant=%s copies=%s raster=%d bytes",
        job_id, job.get("upc"), job.get("variant"), job.get("copies"), len(raster),
    )

    last_err: Exception | None = None
    for attempt in range(PRINT_RETRIES + 1):
        try:
            if cfg.printer_host:
                send_to_printer(cfg, raster)
                log.info("job %s: sent to %s:%d", job_id, cfg.printer_host, cfg.printer_port)
            else:
                path = spool_to_file(cfg, raster, job_id)
                log.info("job %s: spooled to %s (no printer configured)", job_id, path)
            report(cfg, job_id, "done")
            return
        except Exception as exc:
            last_err = exc
            wait = min(2.0 * (attempt + 1), 10.0)
            log.warning("job %s: print attempt %d failed (%s); retry in %.0fs",
                        job_id, attempt + 1, exc, wait)
            time.sleep(wait)

    log.error("job %s: giving up: %s", job_id, last_err)
    report(cfg, job_id, "error", f"bridge print failed: {last_err}")


# ── Main loop ─────────────────────────────────────────────────────────────────
_running = True


def _stop(signum, frame):  # noqa: ANN001
    global _running
    _running = False
    log.info("shutting down…")


def main() -> None:
    parser = argparse.ArgumentParser(description="Spice Town print bridge")
    parser.add_argument("--server", help="cloud app base URL (https://…)")
    parser.add_argument("--token", help="STL_BRIDGE_TOKEN shared secret")
    parser.add_argument("--printer", help="Brother QL-810W IP on the store WiFi")
    parser.add_argument("--port", help="printer raw port (default 9100)")
    parser.add_argument("--poll", help="seconds between polls (default 2)")
    parser.add_argument("--spool", help="write raster to this dir instead of printing")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = BridgeConfig(args)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info("bridge up: server=%s printer=%s poll=%.1fs",
             cfg.server, cfg.printer_host or f"(spool:{cfg.spool_dir})", cfg.poll_seconds)

    backoff = cfg.poll_seconds
    while _running:
        try:
            job = claim_next_job(cfg)
            backoff = cfg.poll_seconds  # server reachable → reset backoff
            if job is None:
                time.sleep(cfg.poll_seconds)
                continue
            print_job(cfg, job)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            # Server unreachable (free-tier dyno waking, WiFi blip). Back off
            # and keep trying forever — the bridge must self-recover.
            log.warning("server unreachable (%s); retrying in %.0fs", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, NETWORK_BACKOFF_CAP)
        except Exception:
            log.exception("unexpected error; continuing")
            time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
