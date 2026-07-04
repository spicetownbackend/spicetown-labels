# Deploying Spice Town Labels

There are three ways to run this. **Which one you want depends on one question:
do you need to physically print labels?**

> ⚠️ **The most important thing to understand:** a label printer is a *local*
> device. The app can only send ink to your Brother QL-810W if it runs on a
> machine **on the same local network as the printer** (your Mac Mini). A
> cloud server — free or paid — has no path to a USB printer on your desk. So:
>
> | Goal | Use |
> |------|-----|
> | **Actually print labels** | **Option A (Docker)** or **Option B (Mac native)** on the Mac Mini |
> | Just see/demo the app live on the web (scan, lookup, search, label preview) | **Option C (free cloud)** — printing is simulated |

---

## Option A — Docker (easiest; no Python version headaches)

Docker bundles Python 3.12 + every dependency, so the version problems you hit
disappear. Works on the Mac Mini, a Linux box, or a NAS.

```bash
cd spicetown-labels
docker compose up --build
# open http://localhost:8080
```

By default it uses **file** printing (writes label PNGs to `./data/spool/`), so
it runs with no printer. To print to the real Brother on your LAN, edit the
commented `environment:` block in `docker-compose.yml` to use the `cups`
transport pointed at the machine running CUPS, then restart.

> On macOS, Docker runs in a Linux VM, so reaching the Mac's USB printer from a
> container needs CUPS network sharing. If your only goal is printing, **Option
> B is simpler.** Docker shines for the demo and for Linux servers.

---

## Option B — Native on the Mac Mini (recommended for real printing)

This is the reliable path to actually print labels, and it's free (your own
hardware). One script fixes the Python version and sets everything up.

```bash
cd spicetown-labels
bash scripts/setup_mac.sh        # installs Python 3.12, venv, deps, loads catalog
```

Then run it:

```bash
# quick test:
source .venv/bin/activate && python run.py      # http://localhost:8080

# production (auto-restart, single worker):
bash scripts/run_gunicorn.sh
```

### Auto-start on every reboot (launchd)

```bash
# 1) Edit deploy/com.spicetown.labels.plist — replace the example paths
#    (/Users/rk_macmini/projects/spicetown-labels) with YOUR real path.
# 2) Install + start:
cp deploy/com.spicetown.labels.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.spicetown.labels.plist
launchctl list | grep spicetown        # confirm it's running
```

### Printer configuration (29×62mm landscape @ 123%)

```bash
lpstat -p                                       # confirm the queue name
lpoptions -p Brother_QL_810W -l | grep -i PageSize   # exact media name
```

Put your settings in a `.env` file (copy `.env.example`):

```ini
STL_PRINT_TRANSPORT=cups
STL_CUPS_PRINTER_NAME=Brother_QL_810W
STL_LABEL_SIZE=29x62
STL_CUPS_LP_OPTIONS=media=29x62mm,landscape,scaling=123
```

Test a single label before going live:

```bash
python scripts/test_print.py --upc 711535509127 --print \
    --printer Brother_QL_810W --media 29x62mm --landscape --scaling 123
```

---

## Option C — Free cloud (live demo, no physical printing)

Host the web app on a free tier so you (or staff) can open it from anywhere.
Scanning, lookup, fuzzy search and label **preview** all work; printing is
**simulated** (no printer in the cloud).

Using **Render** (free, no credit card):
1. Push this folder to a GitHub repo.
2. render.com → **New → Blueprint** → select the repo. It reads `render.yaml`
   and deploys, giving you a public `https://…onrender.com` URL.

The same repo also includes a `Procfile` + `.python-version`, so it deploys on
**Railway**, **Fly.io**, or similar with no changes. Free tiers sleep when idle
and cold-start in a few seconds (the catalog reloads from `data/products.csv` on
boot).

---

## Using the scanner from a phone

Browsers only allow camera access over **HTTPS** or **localhost**. So:
- On the Mac Mini itself: `http://localhost:8080` — camera works.
- From a phone to the Mac's LAN IP over **http** → the camera is blocked by the
  browser, but **manual UPC/name entry still works**. For phone camera scanning,
  put the app behind HTTPS (a reverse proxy like Caddy/Tailscale, or the cloud
  demo URL which is already HTTPS).

---

## Which should you pick?

- **You want to print labels now, reliably, for free:** Option B on the Mac Mini.
- **You want zero Python/setup fuss and don't mind Docker:** Option A.
- **You want a shareable live URL to show people (no printing):** Option C.

You can run B (printing, on the Mac) and C (public demo) at the same time.
