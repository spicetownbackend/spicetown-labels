# Print Bridge — cloud app → store printer

This guide sets up **fully automated cloud printing**: the app runs free on
Render with a public URL, and a small **print bridge** at the store feeds the
Brother QL-810W. Staff scan on any phone, anywhere on any network; labels come
out of the printer at the store within seconds.

```
 staff phone ──scan──▶ cloud app (Render, free)          store WiFi
                          │  /api/print → job queued        │
                          │                                 │
                          ◀──poll /api/bridge/jobs/next── bridge agent
                          ──raster bytes───────────────▶  (tablet/Pi/laptop)
                                                            │ TCP :9100
                                                            ▼
                                                      Brother QL-810W
```

The bridge agent (`scripts/print_bridge.py`) is a single file that needs
**only stock Python** — the server renders the label and converts it to
Brother raster format, so the agent just moves bytes. It polls every ~2
seconds, which as a bonus keeps the free Render instance awake.

---

## 1. Deploy the cloud app (once)

1. Push this repo to GitHub.
2. On [render.com](https://render.com): **New → Blueprint → pick the repo**.
   `render.yaml` configures everything, including remote print mode and a
   generated bridge token.
3. After deploy, open the service's **Environment** tab and copy the value of
   `STL_BRIDGE_TOKEN` — the bridge needs it.
4. Note your public URL, e.g. `https://spicetown-labels.onrender.com`.

Updating prices later: edit `data/products.csv`, push to GitHub — Render
auto-deploys and the catalog reloads on boot. Nothing else to do.

## 2. Get the printer on the store WiFi (once)

1. Connect the QL-810W to the store WiFi (Brother's iPrint&Label app or WPS).
2. Find its IP address (router's client list, or the printer's network status
   printout) and **give it a DHCP reservation** in the router so it never
   changes. Example below assumes `192.168.1.50`.
3. Quick check from any laptop on the WiFi: `ping 192.168.1.50`.

## 3. Run the bridge on the store device

The agent runs on **any always-on device on the store WiFi**. Setups below for
an Android tablet (Termux) and a Pi/laptop.

### Option A — Android tablet (Termux)

1. Install **Termux** from [F-Droid](https://f-droid.org/packages/com.termux/)
   (the Play Store build is outdated) and **Termux:Boot** (same source) so the
   bridge survives reboots.
2. In Termux:

   ```bash
   pkg update && pkg install python termux-api
   mkdir -p ~/bridge && cd ~/bridge
   # copy print_bridge.py here, e.g.:
   curl -O https://raw.githubusercontent.com/<you>/<repo>/main/scripts/print_bridge.py
   ```

3. Create `~/bridge/start.sh`:

   ```bash
   #!/data/data/com.termux/files/usr/bin/sh
   termux-wake-lock          # stop Android from sleeping the process
   export BRIDGE_SERVER=https://spicetown-labels.onrender.com
   export BRIDGE_TOKEN=<paste STL_BRIDGE_TOKEN from Render>
   export BRIDGE_PRINTER=192.168.1.50
   while true; do
     python ~/bridge/print_bridge.py
     sleep 5                 # auto-restart if it ever exits
   done
   ```

   `chmod +x ~/bridge/start.sh`, then test it: `~/bridge/start.sh`
   (you should see `bridge up: server=…`).

4. Auto-start on boot: `mkdir -p ~/.termux/boot` and put a copy of (or a
   one-line call to) `start.sh` in `~/.termux/boot/`. Open Termux:Boot once so
   Android registers it.
5. Android settings for reliability: Settings → Apps → Termux →
   **Battery → Unrestricted** (disable optimization), and keep the tablet
   plugged in. On Samsung, also add Termux to "Never sleeping apps".

### Option B — Raspberry Pi / any Linux or Mac box

```bash
BRIDGE_SERVER=https://spicetown-labels.onrender.com \
BRIDGE_TOKEN=<token> \
BRIDGE_PRINTER=192.168.1.50 \
python3 spicetown-labels/scripts/print_bridge.py
```

For 24/7 duty, wrap it in a systemd unit (Linux) or launchd plist (macOS) with
`Restart=always` — the agent itself already survives network/server outages.

### Testing without the printer

```bash
python3 print_bridge.py --server https://… --token … --spool ./spool
```

Jobs are claimed and the raster files land in `./spool` instead of printing —
prove the whole cloud loop before the printer is even unboxed.

---

## 4. End-to-end check

1. Open `https://<your-app>.onrender.com` on a phone → scan a product barcode.
2. The preview appears; tap **Print**.
3. Within a few seconds the bridge log shows `job N: sent to 192.168.1.50:9100`
   and the label prints.
4. `GET /api/print/<job_id>` (or the UI) shows the job `done`.

## Operational notes

- **Security**: every `/api/bridge` call requires the shared token
  (constant-time compared). Rotate it by changing the env var on Render and in
  `start.sh`.
- **Recovery**: if the bridge dies mid-print, the claimed job re-queues
  automatically after `STL_BRIDGE_STALE_SECONDS` (default 5 min). If the
  server is unreachable (dyno waking, WiFi blip), the agent backs off and
  retries forever.
- **Ordering**: jobs print oldest-first, matching the local single-worker
  behaviour.
- **Free-tier caveat**: Render's free disk is ephemeral — queued-but-unprinted
  jobs are lost if the dyno restarts at that exact moment (staff just reprint).
  The product catalog is safe; it reloads from `products.csv` on every boot.
- The tablet **also runs the scanner UI** fine in its browser, so one device
  can be both the store's scan station and the print bridge.
