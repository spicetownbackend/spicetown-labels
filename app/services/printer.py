"""
app/services/printer.py — Printer transports for the Brother QL-810W.

A `PrinterTransport` takes a rendered PIL label image and gets it onto paper.
Four interchangeable backends, selected by config (STL_PRINT_TRANSPORT):

  * "cups"       — send to CUPS via `lp -d <printer>` (macOS Brother driver).
  * "brother_ql" — convert to Brother raster with the brother_ql library and
                   send over a brother_ql backend (USB/network).
  * "file"       — write the label PNG to a spool dir (hardware-free dev/test +
                   on-screen preview of exactly what would print).
  * "null"       — discard (log only); used in unit tests.

Switching transports is a config change only; the print queue/worker is agnostic.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from PIL import Image

logger = logging.getLogger("spicetown.printer")


class PrinterError(Exception):
    """Raised when a label could not be sent to the printer."""


class PrinterTransport(ABC):
    name: str = "base"

    @abstractmethod
    def send(self, image: "Image.Image", *, copies: int = 1, job_id: int | None = None) -> str:
        """Print `image` `copies` times. Returns a short result string.

        Raises PrinterError on failure (the worker decides about retries).
        """

    @abstractmethod
    def health_check(self) -> bool:
        """Best-effort readiness probe (printer reachable / spool writable)."""

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{self.__class__.__name__} name={self.name!r}>"


# ─────────────────────────────────────────────────────────────────────────────
class NullTransport(PrinterTransport):
    """Discards output; counts calls. For tests and 'printer disabled' mode."""

    name = "null"

    def __init__(self) -> None:
        self.sent = 0
        self.last_size: tuple[int, int] | None = None

    def send(self, image, *, copies: int = 1, job_id=None) -> str:
        self.sent += copies
        self.last_size = image.size
        logger.info("null-print: job=%s size=%s copies=%d (discarded)", job_id, image.size, copies)
        return f"null:{copies}"

    def health_check(self) -> bool:
        return True


# ─────────────────────────────────────────────────────────────────────────────
class FileTransport(PrinterTransport):
    """Writes the rendered label PNG to a spool directory.

    Lets you verify exactly what would print without any hardware — invaluable
    on the dev box and for the scanner-UI preview.
    """

    name = "file"

    def __init__(self, spool_dir: str | Path) -> None:
        self.spool_dir = Path(spool_dir)
        self.spool_dir.mkdir(parents=True, exist_ok=True)

    def send(self, image, *, copies: int = 1, job_id=None) -> str:
        ts = time.strftime("%Y%m%d-%H%M%S")
        stem = f"label_{ts}_job{job_id if job_id is not None else 'x'}"
        path = self.spool_dir / f"{stem}.png"
        # Avoid clobbering rapid-fire jobs in the same second.
        n = 0
        while path.exists():
            n += 1
            path = self.spool_dir / f"{stem}_{n}.png"
        try:
            image.save(path, format="PNG")
        except Exception as exc:
            raise PrinterError(f"failed writing spool file: {exc}") from exc
        logger.info("file-print: job=%s copies=%d -> %s", job_id, copies, path)
        return str(path)

    def health_check(self) -> bool:
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            return os.access(self.spool_dir, os.W_OK)
        except Exception:
            return False


# ─────────────────────────────────────────────────────────────────────────────
class CupsTransport(PrinterTransport):
    """Print via CUPS using the `lp` command (default for the Mac Mini)."""

    name = "cups"

    def __init__(
        self,
        printer_name: str,
        *,
        label_size: str = "62",
        lp_options: list[str] | None = None,
        fit_to_page: bool = True,
        timeout: float = 20.0,
    ) -> None:
        self.printer_name = printer_name
        self.label_size = label_size
        self.lp_options = lp_options or []
        # fit-to-page makes the Brother driver scale the image to the tape.
        # Disable it when you control sizing yourself via an explicit `scaling=`
        # lp option (e.g. landscape + scaling=123 for a 29x62 die-cut label).
        self.fit_to_page = fit_to_page
        self.timeout = timeout

    def _build_lp_cmd(self, file_path: str, copies: int) -> list[str]:
        """Assemble the `lp` argv. Separated out so it is unit-testable.

        fit-to-page is omitted when fit_to_page is False OR when the caller has
        supplied an explicit `scaling=` option (the two conflict).
        """
        has_scaling = any(o.strip().startswith("scaling") for o in self.lp_options)
        cmd = ["lp", "-d", self.printer_name, "-n", str(copies)]
        if self.fit_to_page and not has_scaling:
            cmd += ["-o", "fit-to-page"]
        for opt in self.lp_options:
            cmd += ["-o", opt]
        cmd.append(file_path)
        return cmd

    def send(self, image, *, copies: int = 1, job_id=None) -> str:
        if shutil.which("lp") is None:
            raise PrinterError("`lp` not found — is CUPS installed?")

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            image.save(tmp_path, format="PNG")
            cmd = self._build_lp_cmd(tmp_path, copies)

            logger.info("cups-print: job=%s cmd=%s", job_id, " ".join(cmd))
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
            if proc.returncode != 0:
                raise PrinterError(
                    f"lp failed (rc={proc.returncode}): {proc.stderr.strip()}"
                )
            return (proc.stdout or "queued").strip()
        except subprocess.TimeoutExpired as exc:
            raise PrinterError(f"lp timed out after {self.timeout}s") from exc
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def health_check(self) -> bool:
        if shutil.which("lpstat") is None:
            return False
        try:
            proc = subprocess.run(
                ["lpstat", "-p", self.printer_name],
                capture_output=True, text=True, timeout=5,
            )
            return proc.returncode == 0
        except Exception:
            return False


# ─────────────────────────────────────────────────────────────────────────────
class BrotherQLTransport(PrinterTransport):
    """Print via the brother_ql library (raster) over USB/network.

    brother_ql is imported lazily so the app runs on machines where it isn't
    installed (e.g. when using the CUPS or file transports).
    """

    name = "brother_ql"

    def __init__(
        self,
        *,
        model: str = "QL-810W",
        backend: str = "linux_kernel",
        device: str = "/dev/usb/lp0",
        label_size: str = "62",
        red: bool = False,
        rotate: str = "auto",
    ) -> None:
        self.model = model
        self.backend = backend
        self.device = device
        self.label_size = label_size
        self.red = red  # QL-810W supports black/red on DK-22251
        self.rotate = rotate

    def send(self, image, *, copies: int = 1, job_id=None) -> str:
        try:
            from brother_ql.backends.helpers import send as ql_send
            from brother_ql.conversion import convert
            from brother_ql.raster import BrotherQLRaster
        except Exception as exc:  # pragma: no cover - optional dep
            raise PrinterError(
                "brother_ql not installed; `pip install brother_ql` or use the "
                "cups transport"
            ) from exc

        qlr = BrotherQLRaster(self.model)
        qlr.exception_on_warning = True
        instructions = convert(
            qlr=qlr,
            images=[image] * max(1, copies),
            label=self.label_size,
            rotate=self.rotate,
            threshold=70.0,
            dither=False,
            red=self.red,
            cut=True,
        )
        try:
            result = ql_send(
                instructions=instructions,
                printer_identifier=self.device,
                backend_identifier=self.backend,
                blocking=True,
            )
        except Exception as exc:
            raise PrinterError(f"brother_ql send failed: {exc}") from exc

        if isinstance(result, dict) and not result.get("did_print", True):
            raise PrinterError(f"brother_ql reported failure: {result}")
        logger.info("brother_ql-print: job=%s copies=%d result=%s", job_id, copies, result)
        return "printed"

    def health_check(self) -> bool:
        try:
            import brother_ql  # noqa: F401
        except Exception:
            return False
        # USB device node present? (network backends skip this check)
        if self.backend == "linux_kernel":
            return os.path.exists(self.device)
        return True


# ─────────────────────────────────────────────────────────────────────────────
def build_printer(config) -> PrinterTransport:
    """Instantiate the transport named by config.PRINT_TRANSPORT."""
    kind = (getattr(config, "PRINT_TRANSPORT", "cups") or "cups").strip().lower()

    if kind == "null":
        logger.info("PrinterTransport=null")
        return NullTransport()

    if kind == "file":
        spool = getattr(config, "PRINT_SPOOL_DIR")
        logger.info("PrinterTransport=file spool=%s", spool)
        return FileTransport(spool)

    if kind == "cups":
        logger.info(
            "PrinterTransport=cups printer=%s size=%s",
            config.CUPS_PRINTER_NAME,
            config.LABEL_SIZE,
        )
        return CupsTransport(
            config.CUPS_PRINTER_NAME,
            label_size=config.LABEL_SIZE,
            lp_options=list(getattr(config, "CUPS_LP_OPTIONS", []) or []),
            fit_to_page=getattr(config, "CUPS_FIT_TO_PAGE", True),
            timeout=getattr(config, "PRINT_JOB_TIMEOUT_SECONDS", 20.0),
        )

    if kind == "brother_ql":
        logger.info(
            "PrinterTransport=brother_ql model=%s backend=%s device=%s size=%s",
            config.PRINTER_MODEL,
            config.PRINTER_BACKEND,
            getattr(config, "PRINTER_DEVICE", "/dev/usb/lp0"),
            config.LABEL_SIZE,
        )
        return BrotherQLTransport(
            model=config.PRINTER_MODEL,
            backend=config.PRINTER_BACKEND,
            device=getattr(config, "PRINTER_DEVICE", "/dev/usb/lp0"),
            label_size=config.LABEL_SIZE,
        )

    raise ValueError(
        f"unknown STL_PRINT_TRANSPORT={kind!r} "
        "(expected 'cups' | 'brother_ql' | 'file' | 'null')"
    )
