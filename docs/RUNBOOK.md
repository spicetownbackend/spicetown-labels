# 🌶️ Spice Town Labels — Owner's Runbook

*Everything you need to check, test, fix, and operate the label-printing
system, end to end. Last updated: October 21, 2026 (section 9: duplicate
label fix).*

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
| `/api/print-history[?reason=&start=&end=&product_id=]` | labels printed (by hand + by price update), with old → new price and counts |
| `/api/products/<id>/last-print` | "was this label already printed, at this price?" |
| `/api/refresh` (POST) | force catalog reload from the CSV |
| `/api/bridge/*` | tablet-only (requires the bridge token) |

Repo docs: `README.md` (overview) · `docs/BRIDGE.md` (tablet/bridge setup) ·
`docs/TOAST.md` (inventory sync) · `DEPLOY.md` (hosting options) ·
`docs/ENGINEERING_HANDOFF.md` (deep engineering guide).


---

## 9. Duplicate label fix ("label sync issue") — October 2026

### 9.1 The problem

When a price changes in Toast, the sync prints a new shelf label for it
**automatically**. Staff could not see that anywhere, so they scanned the
item and printed the same label **again by hand**. The live database showed
about 20 items printed by hand within hours of the automatic label, and one
item the sync itself printed twice in a single run (the same change had been
logged twice).

### 9.2 What changed

Printing is the same for staff - nothing to learn - except:

- **One automatic label per item.** However many times a price change was
  logged, the sync prints one label for the item, and never one that someone
  already printed after the change. Two syncs finishing at the same moment
  can no longer both print it.
- **"Already printed" note.** When staff scan an item that was already
  printed at today's price, the scanner page (and the apps, once updated)
  shows a green note: *"A label at this price was already printed
  automatically by the price update (Oct 20, 12:02 PM). Only print again if
  the shelf label is missing or damaged."* If the price has changed since the
  last label, the note is amber and says to print a new one.
- **Ask before a repeat.** Tapping Print on an item printed at the same
  price in the last 7 days asks "Print it again?" first (one extra tap, only
  in that case).
- **Print history shows prices.** Each row shows the price on the label;
  price-update rows show old → new (e.g. `$6.45 → $5.49`), and the Show
  filter shows how many of each kind were printed in the chosen range.
- Every label now records the price printed on it. Labels printed before
  this update show a best-guess price worked out from the price log.

For app developers (all additive - older app builds keep working):
`last_print` on every product in `/api/lookup` and `/api/search`;
`GET /api/products/<id>/last-print`; `previous_print` in the `/api/print`
reply; `price_at_print`, `old_price`, `new_price` on `/api/print-history`
rows plus `counts {price_change, manual, total}` and a `product_id` filter.
Details in the docstrings of `app/routes/api.py` and
`app/services/print_history.py`.

### 9.3 Deploy (owner, ~5 minutes, on the store Mac)

The code is on branch `label-dup-fix` (not on `main` yet). The Mac's
15-minute sync job pulls `main` with a fast-forward only, so put the change
on GitHub's `main` first instead of merging only on this Mac:

1. Back up the database (safe while the app runs):

       cd ~/spicetown-labels
       mkdir -p backups
       sqlite3 spicetown.db ".backup backups/spicetown-before-dupfix.db"

2. Push the branch and merge it on GitHub (Pull request → Merge):

       git push origin label-dup-fix

   (If the old Render service is still connected to the repo, merging
   redeploys it too; that is harmless.)
3. Bring this Mac's copy up to date (or just wait up to 15 minutes for the
   sync job to do it):

       git checkout main && git pull --ff-only origin main

4. Restart the label app so it loads the new code (asks for your Mac
   password):

       sudo bash scripts/restart_daemon.sh

   On start it adds two columns (`price_at_print`, `price_history_id`) and an
   index to the `print_jobs` table by itself - nothing to run by hand. The
   sync job (`com.spicetown.labels.sync`) does not need a restart.
5. Check: open `https://labels.spicetown.shop/api/health` (status ok), then
   scan an item that was printed today by a price change - the green
   "already printed" note should appear above Print label.

**Undo:** revert the merge on GitHub, `git pull --ff-only` here, and run
`sudo bash scripts/restart_daemon.sh` again. The two new columns can stay;
the old code ignores them.
