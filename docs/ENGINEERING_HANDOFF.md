# Spice Town Labels — Engineering Handoff & Hosting Guide

**Audience:** the engineer who will host and operate this application for the store.
**Goal:** get the label-printing web app running reliably so staff can scan a
product and print a shelf/price label on the Brother QL-810W.

---

## 0. TL;DR (read this first)

- It's a **single-process Python (Flask) web app** with an embedded **SQLite**
  database, an **in-process print-worker thread**, and a **nightly scheduler**.
  Ship it as the provided **Docker image** and run **one** instance.
- **GitHub Actions does not host the app.** It's CI/CD: it runs the tests,
  builds the Docker image, and (optionally) triggers a deploy. The running app
  lives on **AWS** or an **on-prem box**.
- **A printer is a local device.** Whatever hosts the app can only print if it
  can reach the Brother QL-810W. AWS cannot reach a USB printer at the store
  unless you bridge to an on-prem CUPS server over a VPN/tunnel. **The simplest,
  most reliable setup for a single store is to run the app on a small box at the
  store, next to the printer.** AWS is appropriate if you want central hosting
  and you bridge printing back to the store (see §5/§7).
- Run it as **one process / one replica** (`gunicorn -w 1`). The print queue,
  the APScheduler nightly job, and SQLite all assume a single instance.

---

## 1. What the application does

Store flow:

1. Staff open the web app on a phone/tablet/PC and **scan a barcode** (camera,
   QuaggaJS) or type a UPC / product name.
2. The app **looks up the product** (name, price, sale/clearance status) from a
   local SQLite cache that is bulk-loaded from a product feed.
3. It shows a **label preview** and lets staff pick a variant (standard / sale /
   clearance) and number of copies.
4. **Print** sends the job to a single worker thread that renders the label
   (Pillow + barcode) and submits it to the printer via CUPS. Target: a label
   out within ~2 seconds of scanning.

Resilience / "AI" features: fuzzy search for unreadable barcodes, automatic
shortening of long product names to fit the label, duplicate-UPC detection, and
flagging of suspicious (>20%) price changes.

---

## 2. Tech stack

| Layer | Choice |
|------|--------|
| Language / runtime | **Python 3.12** (3.11+ required) |
| Web framework | Flask (app factory in `app/__init__.py`) |
| WSGI server | gunicorn (`wsgi:app`) |
| Datastore | **SQLite** (file on local disk; WAL mode) |
| Scheduling | APScheduler (nightly catalog refresh) |
| Label image | Pillow + python-barcode (Code128) |
| Printing | `brother_ql` **or** CUPS via `lp` (configurable) |
| Scanner UI | QuaggaJS (vanilla JS, served by Flask) |
| Fuzzy search | rapidfuzz |
| Tests | pytest (97 tests, all passing) |

Repository layout and per-module notes are in **README.md**. Deployment options
for a non-engineer are in **DEPLOY.md**; this document is the engineering-depth
version.

---

## 3. Architecture & data flow

```
                ┌──────────────────────────── one process (gunicorn -w 1) ───────────────────────────┐
  Browser  ──►  │  Flask routes                                                                        │
 (scanner UI)   │   /  /api/lookup  /api/search  /api/preview  /api/print  /api/refresh  /api/health   │
                │        │                 │              │            │                                │
                │        ▼                 ▼              ▼            ▼                                │
                │   CacheService      SearchService   LabelRenderer  PrintQueue ──► PrinterTransport ──┼──► CUPS / brother_ql ──► Brother QL-810W
                │   (SQLite-first)    (rapidfuzz)     (Pillow)       (queue.Queue,                      │
                │        │                                            single worker thread)             │
                │        ▼                                                                              │
                │     SQLite  ◄──── bulk loader (startup + APScheduler nightly) ◄──── DataProvider ─────┼──► products.csv / .json (or Toast API)
                └───────────────────────────────────────────────────────────────────────────────────┘
```

Key properties:
- **SQLite is the source of truth at runtime.** Lookups never hit the external
  feed on the hot path; the feed is bulk-loaded at startup and nightly.
- **The print worker is in-process and singular.** HTTP returns immediately
  after enqueue; the worker renders + prints. This is why we run one process.
- **The data feed is pluggable** via a `DataProvider` adapter
  (`STL_DATA_PROVIDER=file|toast`); switching is config-only.

---

## 4. Hard constraints the deployment MUST respect

1. **Single instance / single worker.** Use `gunicorn -w 1 --threads 4 -k gthread`.
   Multiple worker *processes* or multiple replicas would each spawn their own
   print queue and nightly scheduler, and would contend on the SQLite file.
   Scale with threads, not processes. (Horizontal scale would require moving to
   Postgres + an external queue + a separate print agent — out of scope.)
2. **Persistent disk for SQLite** if you want the cache/print history to survive
   restarts. If the disk is ephemeral (e.g., Fargate without EFS), that's OK
   functionally — the catalog is reloaded from the product feed on every boot —
   but `PrintJob`/`PriceHistory` audit rows are lost on restart.
3. **The printer is local.** See §5/§7 for the three viable printing topologies.
4. **Camera scanning needs HTTPS** (browser `getUserMedia` rule). Manual
   UPC/name entry works over plain HTTP; the camera does not. Put the app behind
   TLS for phone scanning.
5. **No authentication is built in** (by store request). Do **not** expose it on
   the public internet without putting auth/IP-allowlisting in front of it
   (reverse proxy, VPN, Cloudflare Access, etc.).

---

## 5. Hosting options (pick one)

| # | Where the app runs | Printing works? | Best for | Effort |
|---|--------------------|-----------------|----------|--------|
| **A** | **On-prem box at the store** (Mac Mini or small Linux/NUC) next to the printer | ✅ directly | **One store — recommended** | Low |
| **B** | **AWS** (ECS/EC2) + printing bridged to an **on-prem CUPS** over VPN/Tailscale | ✅ via tunnel | Central hosting, several stores | Medium |
| **C** | **AWS** (or any cloud), printing disabled/simulated | ❌ (preview only) | Demo / dashboard, no labels | Low |

**Recommendation for a single store: Option A.** It's the most reliable and
cheapest (uses hardware already at the store), and avoids exposing a printer to
the internet. Use AWS + GitHub Actions to **build/test/ship** the image; the
store box pulls and runs it. If central hosting is a hard requirement, Option B
keeps printing working by tunneling to the store's CUPS.

---

## 6. CI/CD with GitHub Actions

GitHub Actions is included at **`.github/workflows/ci.yml`**. What it does:

1. **On every push / PR:** set up Python 3.12, install deps, run the full pytest
   suite (97 tests).
2. **On push to `main`:** build the Docker image and push it to the GitHub
   Container Registry (GHCR) as `ghcr.io/<owner>/spicetown-labels:latest` and
   `:<git-sha>`.

This gives you a tested, versioned image on every merge. **Deployment** then
means "run that image" in your chosen location:

- **On-prem (Option A):** the store box runs
  `docker compose pull && docker compose up -d` (point compose at the GHCR
  image), or a cron/watchtower auto-updates it.
- **AWS (Option B/C):** add a deploy job (template notes in §7) that updates an
  ECS service to the new image tag, or pushes to ECR first if you prefer ECR
  over GHCR.

> If you'd rather host the image in **ECR**: change the registry login + image
> name in `ci.yml` to ECR and add `aws-actions/amazon-ecr-login`. The build
> steps are identical.

---

## 7. AWS deployment specifics (Option B/C)

**Compute:** run the container as a **single long-running service**, not Lambda.
The app has a persistent worker thread, an in-process scheduler, and SQLite —
none of which fit Lambda's model. Use one of:

- **ECS Fargate**, service with **`desiredCount: 1`**, 0.25–0.5 vCPU / 0.5–1 GB.
- **EC2 (t4g.small/medium)** running `docker compose up -d`.
- **App Runner / Lightsail Container** (1 instance) — simplest managed option.

**Storage (SQLite):**
- For durability, attach **EFS** mounted at `/data` (the image already uses
  `/data` for the DB, logs, and spool — see `Dockerfile` env vars).
- If you skip EFS, the catalog still reloads from the product feed on boot; you
  just lose print/price-history audit rows on restart. Acceptable for many uses.

**Networking / access:**
- Put it behind an **ALB** (or App Runner's built-in TLS) with **HTTPS** so the
  camera works, and restrict access (Cognito/ALB auth, security group, or a VPN)
  since the app has no built-in login.
- Health check path: **`/healthz`** (200 "ok"). The JSON `/api/health` gives
  richer status (DB, printer, worker, queue depth).

**Printing from AWS (the crux of Option B):**
- AWS cannot see the store's USB printer. Bridge it:
  1. Run **CUPS** on a box at the store with the Brother queue shared, and
     connect the store network to AWS via **Tailscale**, **WireGuard**, or
     **AWS Client VPN / Site-to-Site VPN**.
  2. Set the container to use the CUPS transport pointed at the store's CUPS:
     ```
     STL_PRINT_TRANSPORT=cups
     CUPS_SERVER=<store-cups-host-or-tailscale-ip>:631
     STL_CUPS_PRINTER_NAME=Brother_QL_810W
     STL_LABEL_SIZE=29x62
     STL_CUPS_LP_OPTIONS=media=29x62mm,landscape,scaling=123
     ```
  The image bundles `cups-client` (`lp`/`lpstat`) so it can talk to a remote
  CUPS over IPP. Do **not** expose CUPS to the public internet — keep it on the
  private tunnel.
- If you don't bridge, set `STL_PRINT_TRANSPORT=null` (Option C): scanning,
  lookup, search and preview work; printing is simulated.

**Example ECS task definition (essentials):**
```jsonc
{
  "family": "spicetown-labels",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "256", "memory": "512",
  "containerDefinitions": [{
    "name": "labels",
    "image": "ghcr.io/<owner>/spicetown-labels:<tag>",
    "portMappings": [{ "containerPort": 8080 }],
    "environment": [
      { "name": "STL_ENV", "value": "production" },
      { "name": "STL_PRINT_TRANSPORT", "value": "null" }   // or "cups" + CUPS_SERVER over VPN
    ],
    "mountPoints": [{ "sourceVolume": "data", "containerPath": "/data" }],
    "healthCheck": {
      "command": ["CMD-SHELL", "curl -fsS http://localhost:8080/healthz || exit 1"],
      "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 20
    }
  }],
  "volumes": [{ "name": "data", "efsVolumeConfiguration": { "fileSystemId": "fs-XXXX" } }]
}
```

---

## 8. Configuration reference (environment variables)

All config is env-driven (`config.py`); copy `.env.example` → `.env` for local,
or set these in the task/compose definition. Most-used:

| Variable | Default | Purpose |
|----------|---------|---------|
| `STL_ENV` | `development` | `production` / `development` / `testing` |
| `STL_DATA_PROVIDER` | `file` | `file` or `toast` (product source adapter) |
| `STL_PRODUCTS_FILE` | `./data/products.csv` | product feed (CSV or JSON) |
| `STL_DB_PATH` | `<repo>/spicetown.db` | SQLite file location |
| `STL_PRINT_TRANSPORT` | `cups` | `cups` / `brother_ql` / `file` / `null` |
| `STL_CUPS_PRINTER_NAME` | `Brother_QL_810W` | CUPS queue name (`lpstat -p`) |
| `CUPS_SERVER` | (unset) | remote CUPS host:port (AWS→store bridge) |
| `STL_LABEL_SIZE` | `62` | media; `29x62` for DK-1209 landscape |
| `STL_CUPS_LP_OPTIONS` | (none) | extra `lp -o` opts, e.g. `media=29x62mm,landscape,scaling=123` |
| `STL_CUPS_FIT_TO_PAGE` | `true` | auto-off when a `scaling=` option is set |
| `STL_ENABLE_PRINT_WORKER` | `true` | start the print worker thread |
| `STL_ENABLE_SCHEDULER` | `true` | nightly bulk refresh (set `false` in cloud if no feed updates) |
| `STL_LOG_DIR` | `./logs` | rotating logs |
| `STL_SECRET_KEY` | dev value | set a real secret in production |

Full list with comments is in `config.py` and `.env.example`.

---

## 9. Printer setup (Brother QL-810W via CUPS)

On the box that talks to the printer (store box, or the CUPS host you bridge to):

```bash
lpstat -p                                   # confirm the queue name
lpoptions -p Brother_QL_810W -l | grep -i PageSize   # exact media name
```

The store currently wants **29×62 mm (DK-1209) labels, landscape, 123% scale**.
That maps to:
```
STL_LABEL_SIZE=29x62
STL_CUPS_LP_OPTIONS=media=29x62mm,landscape,scaling=123
```
Test one label end-to-end before going live:
```bash
python scripts/test_print.py --upc 711535509127 --print \
    --printer Brother_QL_810W --media 29x62mm --landscape --scaling 123
```
> Note on orientation: `-o landscape` rotates the page, so the print file is
> pre-rotated to portrait on purpose (see `scripts/test_print.py` / DEPLOY.md).
> If a test print comes out sideways, drop `-o landscape` or use the upright
> preview file. Confirm the exact `PageSize`/media name from `lpoptions` — it may
> be `DK1209` or `Custom.29x62mm` rather than `29x62mm`.

---

## 10. Product data feed

- **Default (`file`):** drop `products.csv` or `products.json` at
  `STL_PRODUCTS_FILE`. The loader tolerates common header spellings (UPC,
  barcode, price, retail, sale_price, clearance, …). A 10k-row generator is in
  `scripts/generate_products.py`.
- **Toast POS (`toast`):** `app/providers/toast_provider.py` is a wired stub
  (OAuth2 + rate-limited HTTP scaffolding) with clear `TODO`s. Implement the
  token + item endpoints, set `STL_DATA_PROVIDER=toast` and the
  `STL_TOAST_*` credentials. No other code changes needed.
- Catalog is bulk-loaded at startup and refreshed nightly (`STL_REFRESH_HOUR`).
  Trigger a manual refresh with `POST /api/refresh` or `python manage.py load`.

---

## 11. Operations

- **Health:** `GET /healthz` (LB check) and `GET /api/health` (JSON: db, printer,
  worker, queue depth, product count). `GET /api/stats` adds cache hit/miss +
  print metrics.
- **Logs:** rotating files under `STL_LOG_DIR` (`spicetown.log`, `errors.log`).
  In containers they also go to stdout/stderr (gunicorn) for CloudWatch/`docker logs`.
- **Manual catalog refresh:** `curl -X POST .../api/refresh` or `manage.py load`.
- **Backups:** if you keep audit history, back up the SQLite file
  (`STL_DB_PATH`) — e.g., nightly copy of `/data/spicetown.db` to S3.
- **Restart:** stateless w.r.t. the catalog (reloads from feed on boot); safe to
  restart anytime.
- **CLI:** `manage.py init-db | load | stats | search "<q>" | provider-check`.

---

## 12. Security notes

- No login by design. Restrict access via network (VPN/tunnel), a reverse-proxy
  with auth, or ALB/Cognito. Don't put it on the open internet as-is.
- Use HTTPS (required for camera; good practice regardless).
- Set a real `STL_SECRET_KEY` in production.
- If bridging CUPS over a tunnel, keep CUPS bound to the private interface only.

---

## 13. Decisions / inputs needed

1. **Topology:** on-prem (A), AWS+tunnel (B), or cloud-no-print (C)? (Recommend A
   for one store.)
2. **Image registry:** GHCR (default in CI) or ECR?
3. **Printer facts to confirm on the store box:** exact CUPS **queue name** and
   exact **media/PageSize** string for the 29×62 label.
4. **Product feed:** keep CSV/JSON, or implement the Toast adapter?
5. **TLS + access control** approach for the chosen host.
6. **SQLite durability:** persistent volume (EFS) or accept reload-from-feed?

---

## 14. Quick reference

```bash
# Local / on-prem with Docker (bundles Python 3.12 + deps)
docker compose up --build               # http://localhost:8080

# Native on the store Mac Mini (fixes Python version automatically)
bash scripts/setup_mac.sh
bash scripts/run_gunicorn.sh

# Run tests (what CI runs)
pip install -r requirements.txt
STL_ENV=testing pytest -q               # 97 passing

# Production server (single worker is intentional)
gunicorn -w 1 --threads 4 -k gthread -b 0.0.0.0:8080 wsgi:app
```

Questions on any of this — the code is documented module-by-module in README.md,
and every endpoint/flag referenced here exists in the repo.
