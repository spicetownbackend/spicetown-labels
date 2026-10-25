# KloudC sign-in for the labels site

Branch `kloudc-auth`. The full shared contract lives in the KloudC repo:
`docs/LABEL_PRINTING_FEATURE.md` (section 2 and section 3.4). This page
covers this site's side only.

## What it does

The labels site has no login of its own. Until now it only accepted the
dashboard cookie. This change adds a second way in, for people signed in to
KloudC:

1. **The KloudC app** sends its KloudC session token as
   `Authorization: Bearer <CODE>.<secret>`. Older KloudC app builds send the
   same token as the `session_token` cookie, and that works too.
2. **The KloudC website** never puts a token in a link. It asks KloudC for a
   one-time code (60 seconds, single use) and opens
   `/kloudc/enter?code=...`. This site trades the code with KloudC, then sets
   its own signed cookie `kc_labels` for 2 hours, or less when the person's
   KloudC session ends sooner (the cookie never outlives that session).

The dashboard cookie check still runs first and is unchanged. The KloudC
check runs only when that fails.

## How a KloudC token is checked

- A token for another store is refused at once, with no outside call.
- Otherwise the site asks KloudC `GET /api/labels/session` with the token
  and `X-Store`. It sends only `Authorization`, `X-Store`, `Accept` and
  `User-Agent`. No cookies, no forwarded-for headers, no proxy from the
  environment. Redirects are not followed. Timeouts are 2 s to connect and
  `STL_KLOUDC_TIMEOUT_SECONDS` to answer, with one attempt.
- A yes is remembered for 60 s (never past the session's end, and the
  server's `cache_seconds` can only make it shorter). A no is remembered for
  30 s.
- If KloudC can't be reached (timeout, connection error, 5xx, 429), a token
  that was a yes in the last 5 minutes still works. Otherwise the answer is
  503. A 401 or 403 from KloudC always wins at once.
- At most 120 uncached checks a minute; above that the answer is 429 and no
  call is made. One caller address may use at most half of them (60): behind
  the Cloudflare tunnel that is the `CF-Connecting-IP` address, read only
  when the request comes from this machine. So one stranger sending made-up
  tokens can't use up the checks for the store's staff. A token that was a
  yes in the last 5 minutes still works during such a flood.
- The cache key is `sha256(token)`. The raw token is never stored and never
  logged. Logs show the first 8 characters of the hash and the username.
- Nothing is written to any database for sign-in.

## Answers the app sees

| Status | `error` | `message` |
|---|---|---|
| 401 | `unauthorized` | Sign in again to use label printing. |
| 403 | `label_printing_off` | Label printing is turned off for this store. |
| 503 | `auth_unavailable` | Can't check your sign-in right now. Try again in a minute. |
| 429 | `rate_limited` | Too many sign-in checks. Try again in a minute. |

A page request (not `/api/*`) that is refused on a `*.kloudc.com` address
goes to `STL_KLOUDC_LOGIN_URL`. On any other address it goes to the
dashboard login, as before.

## Demo logins

Demo logins and the DEMO store can look things up, search, preview and see
history. They can't print, reprint, print price changes, add a product or
dismiss a price change (KloudC answers `can_print: false`). An answer
without a clear `demo: false` is treated as demo.

## Settings (`.env`)

| Setting | Default | Notes |
|---|---|---|
| `STL_KLOUDC_AUTH_ENABLED` | `false` | Master switch. |
| `STL_KLOUDC_API_BASE` | blank | Must be `https://app.kloudc.com`, `http://127.0.0.1:<port>`, `http://localhost:<port>` or `https://<name>.<tailnet>.ts.net[:<port>]`. Anything else logs one error and leaves KloudC sign-in off. |
| `STL_KLOUDC_STORE_CODE` | blank | Required. Upper case. |
| `STL_KLOUDC_LOGIN_URL` | `https://app.kloudc.com/#/label-printing/open` | Must be https. |
| `STL_KLOUDC_CACHE_SECONDS` | `60` | Capped at 120. |
| `STL_KLOUDC_NEGATIVE_CACHE_SECONDS` | `30` | |
| `STL_KLOUDC_STALE_GRACE_SECONDS` | `300` | |
| `STL_KLOUDC_TIMEOUT_SECONDS` | `3` | Read timeout. |
| `STL_KLOUDC_MAX_CHECKS_PER_MINUTE` | `120` | |

The KloudC path only matters when `STL_REQUIRE_DASHBOARD_LOGIN=true` (with
it off, the site is open to everyone, exactly as before). The website
hand-off also needs a real `STL_SECRET_KEY`; with the dev default it refuses
to set the cookie.

With every new setting blank, the site behaves exactly as before.

## Turning it on (owner, once, after merging and deploying this branch)

1. Cloudflare: add the public hostname `labels-spicetown.kloudc.com` →
   `http://127.0.0.1:8080` to the KloudC tunnel. Check
   `curl -s https://labels-spicetown.kloudc.com/healthz` says `ok`.
2. Add to `.env`:
   ```
   STL_KLOUDC_AUTH_ENABLED=true
   STL_KLOUDC_API_BASE=http://127.0.0.1:8900
   STL_KLOUDC_STORE_CODE=SPICETOWN
   STL_KLOUDC_LOGIN_URL=https://app.kloudc.com/#/label-printing/open
   ```
3. Restart the labels service's launchd job. The 15-minute auto-pull does not
   load new `.env` values. The log should show `kloudc_auth: on`.
4. Turn the feature on in KloudC (master console, or the `label-printing`
   CLI command).

**Undo:** set `STL_KLOUDC_AUTH_ENABLED=false` and restart, or turn the
feature off in KloudC (KloudC users are refused within 60 s).

## Known limit

The website's `kc_labels` cookie (2 hours at most, never past the end of the
KloudC session it came from) can outlive a KloudC sign-out or the feature
being turned off by up to 2 hours. The app path does not have this limit
(60 s).

Many different addresses sending made-up tokens together can still use up
the 120 checks a minute. Then staff who have not been checked in the last 5
minutes see "Too many sign-in checks" until the flood stops. The Spice Town
dashboard cookie is never affected (it is checked first, with no limit).

## Files

- `config.py`: the `STL_KLOUDC_*` settings.
- `app/services/kloudc_auth.py`: token shape, the KloudC call, the cache, the
  hand-off cookie.
- `app/__init__.py`: the gate (`_register_dashboard_auth_gate`).
- `app/routes/kloudc.py`: `GET /kloudc/enter`.
- `app/routes/api.py`: `_demo_status` reads the KloudC identity; prints by a
  KloudC user log `printed_by=<username>`.
- `tests/test_kloudc_auth.py`: a fake KloudC server on 127.0.0.1; no outside
  calls.
