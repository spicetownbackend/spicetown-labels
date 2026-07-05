# Toast POS integration — automatic inventory sync

With the Toast provider active, the label system pulls its catalog straight
from your Toast POS: **change a price in Toast and it's on the labels after
the nightly sync** (3:00 AM ET) — no more editing `products.csv`.

How it maps: each Toast menu item's **`sku` field is the barcode/UPC**, `name`
and `price` come across as-is, and the item's menu-group name becomes the
label's department line. Items with no sku or no positive price are skipped
(they can't be scanned anyway); the skip count appears in the logs after each
sync.

There are **two ways to run the sync** — pick one:

## Option A (recommended): GitHub Actions nightly sync

Credentials live in **GitHub Actions Secrets** (encrypted; never in repo
files). A scheduled workflow (`.github/workflows/toast-sync.yml`) runs nightly
at ~3 AM ET on GitHub's servers, pulls the catalog from Toast, and commits an
updated `data/products.csv` — Render auto-deploys it minutes later. The app
stays on the simple `file` provider and never holds Toast credentials.

Bonuses: the catalog is **version-controlled** (every night's prices are a
commit you can inspect or revert), and **hand-edited `sale_price` /
`clearance` values in the CSV survive the sync** — so SALE labels still work
even though Toast has no sale-price concept.

Setup:

1. **Get credentials from Toast** (standard API access): client ID, client
   secret, restaurant GUID.
2. On GitHub: repo → **Settings → Secrets and variables → Actions →
   New repository secret**, add the three:
   `TOAST_CLIENT_ID`, `TOAST_CLIENT_SECRET`, `TOAST_RESTAURANT_GUID`.
3. Test it immediately: **Actions tab → "Toast catalog sync" → Run
   workflow**. Watch the run; on success the new `products.csv` is committed
   and Render redeploys with your real catalog.

That's it — it repeats nightly by itself. (GitHub pauses schedules in repos
with no activity for 60 days, but the sync's own nightly commits count as
activity, so this doesn't bite in practice.)

## Option B: live provider on Render

The app itself talks to Toast at runtime (startup load, nightly in-app
refresh, per-scan miss lookups).

1. **Get credentials from Toast** (standard API access): a client ID, client
   secret, and your restaurant GUID.

2. **Set them on Render** — dashboard → `spicetown-labels` service →
   **Environment** tab → add:

   | Key | Value |
   |-----|-------|
   | `STL_TOAST_CLIENT_ID` | from Toast |
   | `STL_TOAST_CLIENT_SECRET` | from Toast |
   | `STL_TOAST_RESTAURANT_GUID` | from Toast |
   | `STL_DATA_PROVIDER` | `toast` |

   > ⚠️ Secrets go in Render env vars ONLY — never commit them to this repo.
   > Note: `STL_DATA_PROVIDER` is also set (to `file`) in `render.yaml`; a
   > future Blueprint sync could reset a dashboard-only change, so when you're
   > happy with Toast, also flip the value in `render.yaml` and push (the env
   > var itself is not secret).

3. **Save** — Render restarts the service, which bulk-loads the catalog from
   Toast on boot. Check it worked:

   ```
   https://spicetown-labels.onrender.com/api/health   -> "provider": "toast", product_count > 0
   https://spicetown-labels.onrender.com/api/stats    -> catalog + cache metrics
   ```

## What runs automatically after that

- **On every boot/deploy**: full catalog load from Toast.
- **Every night at 3:00 AM ET**: full re-sync (APScheduler; enabled in
  render.yaml). Price changes >20% get flagged + logged with a 1-hour
  re-check TTL, same as with the CSV provider.
- **On a scan of an unknown/stale UPC**: a single-item refresh from Toast,
  rate-limited by the shared token bucket (10 req/s, burst 20) with
  exponential backoff on 429/5xx, and automatic OAuth token refresh.

## Rollback

Set `STL_DATA_PROVIDER` back to `file` and the app instantly reverts to
`data/products.csv`. Keep the CSV roughly current if you want it as a fallback.

## Limitations

- Toast's standard menus API has no sale/clearance concept, so `on_sale` /
  `clearance` label variants don't trigger from Toast data. If you need a SALE
  label, that item's price simply changes in Toast (standard label), or switch
  back to the CSV where `sale_price`/`clearance` columns are supported.
- The nightly sync needs the app awake at 3 AM ET — the print bridge's
  polling keeps the free Render instance up, so leave the tablet running.
