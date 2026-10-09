# Wi-Fi Voucher Billing System

This repository extends the existing Flask/SQLite billing portal. Python was retained instead of introducing a second Node.js service because payment intents, admin routes, router metadata, and the current UI already run on Flask. The additions are incremental and preserve existing client/payment records.

## Project structure

```text
app.py                         Flask portal, APIs, SQLite migrations, router callbacks
outview.html                   Main customer captive portal and admin dashboard
script.js                      Customer, payment, voucher, and admin interactions
style.css                      Responsive portal/dashboard styles
schema.sql                     Reference DDL for voucher/session/security tables
reaper.py                      30-second background voucher/session expiry worker
Dockerfile                     Python production container
docker-compose.yml             Production web + reaper services with persistent storage
docker-compose.staging.yml     Isolated staging web + reaper and separate volume
.env.staging.example           Staging-only credential template
.env.example                   Environment variable template
requirements.txt               Python dependencies
tests/test_voucher_access.py   Voucher lifecycle and API regression tests
netlify-deployment/             Older static/browser-only demo (not shared billing)
```

`app.init_db()` creates and migrates the SQLite schema on startup. The `clients` table remains the source of paid voucher records; lifecycle columns are added in place. `voucher_devices` records MAC aliases grouped by device slot, `voucher_device_tokens` stores only hashes of short-lived handoff cookies, `voucher_login_failures` stores keyed hashes, and `active_sessions` tracks reconnectable router sessions. `schema.sql` is a reference, not a replacement for the idempotent app migration.

## Phases

### Phase 1: Core voucher access

Runnable now. Paid or admin-issued voucher time starts on the first successful `/api/access/login` or voucher `/api/access/validate` call, not when payment completes. Presets include 30 minutes, 1 hour, 1 day, and 7 days; bulk issue also supports a custom duration. One `expires_at` is shared across the voucher's devices. A returning MAC reactivates its session without resetting expiry. Unused vouchers expire after `VOUCHER_ACTIVATION_TTL_DAYS`. The reaper checks on the configured interval (minimum 30 seconds), marks vouchers/sessions expired, and calls the router disconnect webhook for active MACs. The customer portal supports voucher login, balance checks, expiry countdown, payment, and captive-portal probe routes for common OS checks.

Production requires HTTPS, a random `SECRET_KEY`, a non-default admin username, a Werkzeug `ADMIN_PASSWORD_HASH`, an `ADMIN_TOTP_SECRET`, `ROUTER_API_KEY`, and `ROUTER_MAC_SIGNING_SECRET`; startup fails if any are missing or invalid. Admin mutations require a session CSRF token and successful writes are audit logged. Wrong voucher attempts are rate limited and locked per hashed client-IP/code. Development may use `ADMIN_PASSWORD` without TOTP; production does not accept plaintext-only admin passwords.

Device identity is the MAC supplied by the hotspot/router. Phones, laptops, consoles, and smart TVs use the same rule; the app does not identify a device by brand. Voucher login requires a valid MAC. In production, login also requires a 120-second HMAC-signed MAC ticket from the trusted router adapter so users cannot edit the URL MAC to evade device limits. A short-lived HttpOnly handoff cookie lets a changed private/randomized MAC reuse its existing device slot. The portal detects locally administered MACs and advises disabling Private Wi-Fi Address if reconnects are unstable. `MAX_DEVICES_PER_VOUCHER` defaults to one; admins can issue vouchers with another limit up to ten.

### Phase 2: Business and network operations

Runnable now where a router adapter is configured. The admin dashboard has searchable voucher inventory, bulk issuance, CSV export, QR ticket printing, live session listing, disable/enable, pause/resume, delete, and per-device disconnect. Admin changes are audited in `activity_log`. Router-reported byte deltas are deduplicated and aggregated across every device on a voucher; reaching a data cap marks and disconnects its active sessions. Optional speed limits are stored on vouchers and returned to the router adapter. `/api/admin/pricing` supports bounded default device-count configuration.

The app records voucher/session state and requests disconnects, but does not speak MikroTik's proprietary API or RADIUS directly. It requires the hotspot/RADIUS adapter to call the access API and enforce the returned decision, expiry, data limit, and speed limit. Point `ROUTER_DISCONNECT_WEBHOOK_URL` at an authenticated adapter that disconnects a MAC when it receives `{ "deviceMac": "...", "voucherCode": "..." }`. Configure `ROUTER_API_KEY` on that adapter and app for router-only endpoints.

### Phase 3: Advanced extras

Existing pieces include mobile-money checkout/polling, signed payment webhook validation, loyalty/referral rewards, a PWA/offline shell, payment failure throttling, admin TOTP, and scheduled expiring-voucher notification lookup. External integrations are deliberately opt-in. This repository does not currently provide native RADIUS/MikroTik control, WebSockets, SMS/WhatsApp delivery, email/Telegram reports, multiple admin roles, PDF reports, multi-hotspot tenancy, category filtering, fair-use shaping, automatic database restore UI, or server-down alert delivery. The static Netlify folder is a local-browser demo and must not be treated as a production shared voucher service. The Compose reaper is a local, independently runnable expiry worker; on hosted platforms configure an equivalent worker/service with the same persistent database.

## Run locally

Requirements: Python 3.11+ and the packages in `requirements.txt`.

PowerShell (local development):

```powershell
Copy-Item .env.example .env
# Edit .env and set a random SECRET_KEY plus private local ADMIN_USERNAME / ADMIN_PASSWORD.
$env:WIFI_DATA_DIR = "$PWD\data"
c:/python314/python.exe -m pip install -r requirements.txt
c:/python314/python.exe app.py
```

Open `http://127.0.0.1:5002`. In a second terminal, run the expiry worker with the same `WIFI_DATA_DIR`:

```powershell
$env:WIFI_DATA_DIR = "$PWD\data"
c:/python314/python.exe reaper.py
```

For a production process manager, run the web app with Gunicorn and run `reaper.py` as a separate service. Both processes must use the same writable persistent `WIFI_DATA_DIR`.

## Docker

1. Copy `.env.example` to `.env`. Production requires a random 32+ character `SECRET_KEY`, a private `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`, `ADMIN_TOTP_SECRET`, `ROUTER_API_KEY`, and `ROUTER_MAC_SIGNING_SECRET`. Generate a password hash locally with `python -c "from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass()))"`. Generate independent secrets locally with `python -c "import base64,secrets; print('ADMIN_TOTP_SECRET='+base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')); print('ROUTER_MAC_SIGNING_SECRET='+secrets.token_urlsafe(48)); print('ROUTER_API_KEY='+secrets.token_urlsafe(32))"`; enroll the TOTP secret in an authenticator. Do not commit `.env`.
2. Build and start both services:

```powershell
docker compose up --build -d
```

3. Open `http://localhost:5002`, inspect health with `http://localhost:5002/health`, and inspect service output:

```powershell
docker compose ps
docker compose logs -f web voucher-reaper
```

4. Stop services with `docker compose down`. The named `wifi-data` volume is retained; `docker compose down -v` deletes the database volume and should only be used when intentionally wiping local data.

### Isolated staging stack

Copy `.env.staging.example` to `.env.staging`, replace every placeholder with staging-only secrets, then run:

```powershell
docker compose -f docker-compose.staging.yml up --build -d
```

Staging uses `http://localhost:5003` and its own `wifi-data-staging` volume. This profile is for isolated local/staging testing only; put staging behind HTTPS before exposing it to other networks. Stop it with `docker compose -f docker-compose.staging.yml down`.

For public deployment, put the services behind an HTTPS reverse proxy, restrict admin/API access as appropriate, back up the volume, and set `SESSION_COOKIE_SECURE` behavior through production configuration. The supplied Gunicorn configuration uses one worker because the default Flask-Limiter backend is per-process memory; use a shared Redis `RATE_LIMIT_STORAGE_URL` before increasing workers or instances. Use strong secrets and never use development admin defaults.

## Test

The tests use temporary SQLite databases and do not modify the app database:

```powershell
c:/python314/python.exe -m unittest discover -s tests -v
```

Coverage includes first-use activation and fixed durations, shared time across reconnects, device caps and private-MAC handoff, signed MAC claims, invalid-MAC denial, wrong-code lockout, hashed-password/TOTP login, CSRF, expiry/disconnect behavior, admin bulk issue/search, session visibility, and pause/resume.

## Router / captive portal setup

1. Create a guest/hotspot SSID and enable captive-portal interception. Permit the portal host and any payment-provider endpoints in the walled garden so new clients can load the portal before authentication.
2. Configure OS connectivity probes (Android `generate_204`, Apple `hotspot-detect.html`, Windows `connecttest.txt`/`ncsi.txt`) to redirect through the trusted adapter so every redirect includes a fresh signed MAC ticket. The app handles common probe paths, but a bare redirect to `/` will be rejected for voucher login in production because its MAC is not authenticated.
3. The exact MikroTik model and RouterOS version are still unknown, so verify hotspot macros and redirect syntax on the target device before go-live. A trusted RouterOS-side script or adapter calls `POST https://YOUR_HOST/api/access/mac-ticket` with `X-Router-Key` and JSON `{ "mac": "station-mac" }`; it receives a 120-second `macTicket`. Redirect the captive browser to `https://YOUR_HOST/?mac=station-mac&macTicket=SIGNED_TICKET`. The portal then submits `{ "voucher": "CODE", "mac": "station-mac", "macTicket": "SIGNED_TICKET", "deviceType": "smart-tv" }` to `/api/access/login`. Keep `ROUTER_API_KEY` and `ROUTER_MAC_SIGNING_SECRET` off the captive page and out of customer JavaScript. If RouterOS cannot make this authenticated server-side call directly, deploy a small trusted hotspot adapter; never fall back to trusting an unsigned customer-supplied MAC in production.
4. On reconnection, query `GET /api/access/reconnect?mac=station-mac` from the trusted adapter. A matching active session is renewed against the original voucher expiry.
5. Periodically revalidate authorized devices against `GET /api/access/validate?voucher=CODE&mac=station-mac`. The router must enforce `active: false` immediately and apply the returned `dataLimitMb`/`speedLimitMbps` if its platform supports them.
6. Report traffic with `POST /api/access/usage` and JSON `{ "voucher":"CODE", "mac":"station-mac", "eventId":"unique-event-id", "bytesDelta":12345 }`. Report each new byte delta once; on retry, reuse the same `eventId`. The service counts each event once, returns shared used/remaining data, and returns `active: false` at the cap.
7. Set `ROUTER_DISCONNECT_WEBHOOK_URL` to an adapter that accepts the expiry/admin disconnect callback and removes that station from the active hotspot session.

A browser-only captive portal cannot directly enable a radio connection or disconnect a station. The router must enforce authorization and session expiry; the app API is the source of voucher state.

## REST API

JSON error responses include an `error` or a stable access `reason`. Admin routes require a prior `POST /api/auth/login` session cookie. Router routes require `X-Router-Key` when configured. Public voucher login is rate limited.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/access/login` | Validate and first-activate a voucher; body includes `voucher`, optional `mac`, `deviceType`. |
| `POST` | `/api/access/usage` | Record `{voucher, mac, eventId, bytesDelta}` from the trusted router; deduplicates retries and enforces shared data caps. |
| `GET` | `/api/access/validate` | Router revalidation by `voucher` or `phone`; supports `mac` and `deviceType`. |
| `GET` | `/api/access/reconnect?mac=...` | Find and refresh a previously authorized device session. |
| `GET` | `/api/dashboard?voucher=...` | Read-only voucher balance, pending activation, remaining seconds, and purchase history. |
| `POST` | `/api/access/mac-ticket` | Router-key-protected endpoint that signs a MAC claim for 120 seconds |
| `POST` | `/api/payments/checkout` | Start a payment intent; payment-provider credentials are required for automatic push. |
| `GET` | `/api/payments/intents/{id}` | Read payment intent state. |
| `POST` | `/api/payments/intents/{id}/sync` | Ask configured provider for current status. |
| `POST` | `/api/payments/webhook` | Verify a signed payment confirmation. |
| `POST` | `/api/auth/login` | Admin session login. |
| `POST` | `/api/auth/logout` | End admin session. |
| `GET` | `/api/admin/vouchers?q=&status=&limit=&offset=` | Search voucher inventory. |
| `POST` | `/api/admin/vouchers/bulk` | Generate 1-500 codes. JSON fields: `quantity`, `plan`, optional `durationSeconds`, `prefix`, `maxDevices`, `dataLimitMb`, `speedLimitMbps`, `label`. |
| `PATCH` | `/api/admin/vouchers/{code}` | Set `disabled` or `maxDevices`. |
| `DELETE` | `/api/admin/vouchers/{code}` | Delete voucher and its tracked sessions/bindings; audited. |
| `POST` | `/api/admin/vouchers/{code}/pause` | Freeze remaining duration and disconnect active device sessions. |
| `POST` | `/api/admin/vouchers/{code}/resume` | Resume with the frozen remaining duration. |
| `GET` | `/api/admin/sessions` | List recent active/paused device sessions. |
| `POST` | `/api/admin/sessions/{id}/disconnect` | Mark a session disconnected and notify the router adapter. |
| `POST` | `/api/admin/clients/{id}/voucher` | Issue a first-use voucher for an already-paid walk-in client. |
| `GET` / `PUT` | `/api/admin/pricing` | Read/update pricing and voucher defaults, including `maxDevicesPerVoucher` (1-10). |
| `GET` | `/health` | Container/orchestrator liveness check. |

## Configuration

See `.env.example`. Most important settings:

- Production: `SECRET_KEY`, non-default `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`, `ADMIN_TOTP_SECRET`
- `WIFI_DATA_DIR` for the shared persistent SQLite database
- `ROUTER_API_KEY`, `ROUTER_MAC_SIGNING_SECRET`, `ROUTER_DISCONNECT_WEBHOOK_URL`
- `MAX_DEVICES_PER_VOUCHER`, `VOUCHER_ACTIVATION_TTL_DAYS`, `SESSION_REAPER_INTERVAL_SECONDS`
- MTN/Airtel credentials and callback secret for automatic mobile-money confirmation

Never put router keys, payment-provider credentials, or admin passwords in frontend files. `SUPPORT_INQUIRY_NUMBER` is intentionally public.

## Phase 1 Go-Live Checklist

- [ ] Confirm the exact MikroTik model, RouterOS release, HotSpot macros, and captive redirect behavior on the installed device.
- [ ] Deploy behind HTTPS and verify the app redirects HTTP to HTTPS.
- [ ] Set independent random `SECRET_KEY`, `ROUTER_API_KEY`, and `ROUTER_MAC_SIGNING_SECRET` values.
- [ ] Set a private admin username, a Werkzeug password hash, and enroll `ADMIN_TOTP_SECRET` in an authenticator.
- [ ] Verify the trusted adapter mints 120-second MAC tickets and includes them on OS probe and captive-portal redirects.
- [ ] Confirm clients without a valid MAC/ticket are denied, and a second device is denied at the one-device default.
- [ ] Confirm a private-MAC change reuses its HttpOnly device slot only while the handoff cookie is valid.
- [ ] Run the test suite and test voucher creation, 30-minute/1-hour/1-day/7-day expiry, reconnection, wrong-code lockout, admin CSRF/TOTP, and router disconnect against a test SSID.
- [ ] Run web and expiry worker against the same persistent `WIFI_DATA_DIR`; test expiry disconnect while the portal remains open.
- [ ] Back up SQLite and test restoring to a separate staging volume before accepting payments.
- [ ] Obtain the exact router model/version and payment provider credentials; do not enable customer sales until the adapter and provider callbacks are verified end to end.
