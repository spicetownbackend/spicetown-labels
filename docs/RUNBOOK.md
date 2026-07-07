# 🌶️ Spice Town Labels — Owner's Runbook

*Everything you need to check, test, fix, and operate the label-printing
system, end to end. Last updated: July 5, 2026.*

---

## 1. What the system is

```
 Toast POS ──nightly sync (3:30 AM ET)──▶ GitHub repo (products.csv)
                                            │  any push → auto-deploy (~2 min)
                                            ▼
 staff phone ──scan / name search──▶ https://spicetown-labels.onrender.com
                                            │  print → job queued in cloud DB
                                            ▼
 Samsung tablet (Termux bridge, polls every 2s) ──WiFi──▶ Brother QL-810W
```

| Component | Location | Credentials / access |
|-----------|----------|----------------------|
| Web app | https://spicetown-labels.onrender.com | public (staff use this) |
| Code + catalog | github.com/spicetownbackend/spicetown-labels (private) | your GitHub login `Sundar6264-ux` |
| Hosting | render.com dashboard → service `spicetown-labels` | your Render login |
| Toast API secrets | GitHub → repo Settings → Secrets and variables → Actions | `TOAST_CLIENT_ID`, `TOAST_CLIENT_SECRET`, `TOAST_RESTAURANT_GUID` |
| Bridge token | Render → service → Environment tab → `STL_BRIDGE_TOKEN` | same value lives on the tablet in `~/bridge/start.sh` |
| Bridge | Samsung tablet, Termux app | files: `~/bridge/print_bridge.py`, `~/bridge/start.sh`, log `~/bridge/bridge.log`; boot copy `~/.termux/boot/start-bridge.sh` |

**Normal daily operation requires touching NOTHING.** Staff scan and print;
prices flow from Toast overnight.

---

## 2. Health checks (do these anytime, from anywhere)

### 2.1 One-glance health

Open in any browser:

    https://spicetown-labels.onrender.com/api/health

Healthy looks like:

```json
{"db_ok": true, "print_mode": "remote", "print_queue_depth": 0,
 "product_count": 5025, "provider": "file", "status": "ok"}
```

What to read:
- `status: ok` and `db_ok: true` — app and database fine.
- `product_count` — roughly your catalog size (5,000+). If it's ~10, the app
  fell back to the sample file → check the last deploy (§5.3).
- `print_queue_depth: 0` — tablet is keeping up. A number that stays > 0 for
  minutes means the **tablet/bridge is down** (§5.1).
- First load after a quiet period can take 30–60 s (free tier waking). Normal.

### 2.2 Metrics (optional, more detail)

    https://spicetown-labels.onrender.com/api/stats

Shows cache hit/miss rates, flagged-price count, catalog size.

### 2.3 Job status by id

    https://spicetown-labels.onrender.com/api/print/123

`queued` → waiting for tablet · `printing` → tablet claimed it ·
`done` → printed · `error` → see the error text in the response.

---

## 3. End-to-end tests

### 3.1 Full test (proves EVERYTHING works) — 1 minute

1. On your phone, open https://spicetown-labels.onrender.com
2. Scan any product barcode with the camera (or type part of a name and pick
   from the results).
3. Check the preview looks right → tap **🖨️ Print label**.
4. Within ~5 seconds the label comes out of the QL-810W and the screen shows
   **“✓ Printed 1 label(s).”**

That single test exercises: cloud app → catalog → rendering → job queue →
tablet bridge → WiFi → printer. If it works, the whole system works.

### 3.2 Test without wasting a label

Preview rendering only (no print): open

    https://spicetown-labels.onrender.com/api/preview/<any-UPC>.png

You should see the label image. Add `?variant=sale` to see the red SALE style,
or `?variant=shelf` for the barcode-free 62×29 mm shelf tag (category, name,
price only — also available in the Variant dropdown on the scanner page).

### 3.3 Test the shared-barcode picker

Scan/type a UPC that two products share (e.g. a B1G1 pair). The screen must
show “2 products share this barcode” → tap one → its preview shows → print.
The printed label must match the one you picked.

### 3.4 Test the tablet recovers after reboot

Reboot the tablet, touch nothing, wait ~1 minute, then do test 3.1.
If it prints, Termux:Boot + battery settings are still good.

### 3.5 Test the offline-queue behaviour

Turn the tablet's WiFi off, print from your phone (status will sit at
`queued`), turn WiFi back on → the label prints within a few seconds.

---

## 4. Routine operations

### 4.1 Put an item on sale / clearance

1. GitHub → repo → `data/products.csv` → pencil (edit).
2. Find the row (Ctrl+F by UPC or name). Put the sale price in the
   `sale_price` column (e.g. `4.99`), or `true` in `clearance`.
3. Commit changes. ~2 minutes later the label prints with the red SALE /
   CLEARANCE styling automatically.
4. These hand edits **survive the nightly Toast sync** (it preserves
   `sale_price`/`clearance` per product). To end the sale, blank the cell.

### 4.2 Add / rename / reprice products

Do it **in Toast** — the nightly sync brings it over. For same-day urgency,
run the sync manually: GitHub → **Actions → “Toast catalog sync” → Run
workflow** (green button). Watch it go green, then check `product_count`.

### 4.3 Check the nightly sync ran

GitHub → Actions tab. You should see “Toast catalog sync” runs nightly
(~07:30 UTC). Green = synced. A run that found no changes commits nothing —
that's normal, not a failure.

### 4.4 See what changed in the catalog

Every sync is a git commit — GitHub → repo → commit history → click a
“Nightly Toast catalog sync” commit to see exact price/product diffs.

---

## 5. Troubleshooting

### 5.1 Labels not printing (most common)

Symptom: prints stay `queued`; `print_queue_depth` climbing.

Cause is almost always the tablet. On the tablet:
1. Is it on, charged, on store WiFi?
2. Open Termux, run: `tail -20 ~/bridge/bridge.log`
   - `bridge up: …` + recent `job N: sent to …` lines → bridge fine; check
     the **printer** instead (power, tape roll, red error light, WiFi).
   - `server unreachable` repeating → tablet WiFi or Render is down.
   - Nothing recent / Termux not running → start it: `~/bridge/start.sh &`
     then re-check Android battery settings (Termux → Battery →
     **Unrestricted**; “Never sleeping apps”).
3. Queued jobs print automatically once the bridge reconnects — no reprints
   needed (each job re-queues itself if claimed but not completed in 5 min).

### 5.2 Printer prints garbage / wrong size

- Check the tape roll is 62 mm continuous (DK-2205 type).
- Power-cycle the printer.
- Confirm the printer kept its IP (router DHCP reservation). If the IP
  changed, update `BRIDGE_PRINTER` in `~/bridge/start.sh` **and**
  `~/.termux/boot/start-bridge.sh`, then restart Termux.

### 5.3 App shows wrong/old/tiny catalog

- Render dashboard → service → **Events**: did the latest deploy succeed?
- GitHub → Actions: is CI green on the latest commit?
- `product_count` ≈ 10 means the sample CSV shipped — usually a failed sync
  overwrote nothing (the sync has a safety stop) but a bad deploy happened;
  redeploy: Render → Manual Deploy → Deploy latest commit.

### 5.4 Scanner camera won't start on a phone

Camera needs HTTPS (the Render URL is) + permission. If denied once, re-allow
in the browser's site settings. The name-search box always works regardless.

### 5.5 Toast sync failing (red run in Actions)

Click the run → read the log:
- `401`/auth errors → Toast credentials expired/rotated → update the three
  repo secrets (Settings → Secrets → Actions).
- `safety stop: Toast returned only N item(s)` → Toast API hiccup or wrong
  restaurant GUID; catalog was intentionally left untouched. Re-run later.

### 5.6 Render says service suspended / sleeping constantly

Free tier gives 750 h/month — exactly one always-on service. If a second
service was added, remove it. The bridge's polling keeps the app awake;
if the tablet is off overnight the app sleeps (fine — it wakes on first use).

### 5.7 Complete disaster recovery

Everything important is in the GitHub repo:
- **New tablet**: follow `docs/BRIDGE.md` §3 (Termux setup, ~15 min). Token
  comes from Render → Environment → `STL_BRIDGE_TOKEN`.
- **Recreate hosting**: render.com → New → Blueprint → pick the repo →
  re-add the bridge token to the new tablet config.
- **Bad catalog/config change**: GitHub → commit history → Revert.

---

## 6. Security notes

- Never commit secrets to the repo. Toast creds live ONLY in GitHub Actions
  Secrets; the bridge token ONLY in Render env + the tablet's `start.sh`.
- To rotate the bridge token: change `STL_BRIDGE_TOKEN` in Render → update
  the same value in the tablet's `~/bridge/start.sh` and
  `~/.termux/boot/start-bridge.sh` → restart Termux.
- The repo is private; the web app is public (menu prices only). If you ever
  want staff-only access, ask for a simple PIN gate to be added.

## 7. Costs

| Item | Cost |
|------|------|
| Render free tier | $0 (the $1 card charge was a one-time verification hold — it auto-refunds) |
| GitHub (private repo + Actions) | $0 at this usage |
| Tablet + printer + labels | your hardware / consumables |

## 8. Reference — all endpoints

| URL | What |
|-----|------|
| `/` | scanner UI (staff page) |
| `/api/health` | liveness + queue depth + product count |
| `/api/stats` | cache/catalog/queue metrics |
| `/api/lookup/<upc>` | product lookup (all matches for shared barcodes) |
| `/api/search?q=…` | fuzzy search by name or misread UPC |
| `/api/preview/<upc>.png[?id=&variant=]` | label image, no printing |
| `/api/print` (POST) | queue a print job |
| `/api/print/<job_id>` | job status |
| `/api/refresh` (POST) | force catalog reload from the CSV |
| `/api/bridge/*` | tablet-only (requires the bridge token) |

Repo docs: `README.md` (overview) · `docs/BRIDGE.md` (tablet/bridge setup) ·
`docs/TOAST.md` (inventory sync) · `DEPLOY.md` (hosting options) ·
`docs/ENGINEERING_HANDOFF.md` (deep engineering guide).
