# WiFi Billing System Deployment Guide

## Recommended: one website with shared activity

Deploy the Flask app as one web service. This serves the customer portal, admin activity dashboard, API, SQLite database, and installable app from the same URL.

For local phased setup, Docker Compose, the voucher/session API, test commands, and router/captive-portal steps, see [SYSTEM_GUIDE.md](SYSTEM_GUIDE.md). The new Docker setup runs the web service and expiry worker against one persistent SQLite volume.

### Render deployment

1. Push this project to a GitHub repository.
2. In Render, choose **New + > Blueprint** and select the repository.
3. Render generates `SECRET_KEY`. Set a private `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`, `ADMIN_TOTP_SECRET`, `ROUTER_API_KEY`, and `ROUTER_MAC_SIGNING_SECRET`. Production startup intentionally fails if these are missing; plaintext `ADMIN_PASSWORD` is development-only.
4. Render will use `render.yaml` to install the dependencies and start the app.
5. Open the generated HTTPS URL. Log in through **Admin** to check billing activity from any device.

The free Render setup stores the SQLite database in the service filesystem. Records are shared while the service is running, but may reset after a redeploy or restart. Use persistent storage or PostgreSQL later when you need durable production records.

The app also supports installation from Chrome or Edge using the **Install app** button in the admin dashboard.

Router management is stored in the same shared database and is available online after admin login. It currently manages the router registry and connection details; live reboot, bandwidth, and connected-device control require credentials and an API supported by the specific router model.

## Automatic mobile-money setup

The application cannot generate a real merchant code. MTN or Airtel must issue the merchant code, collection primary key, API user, API key, client ID, and client secret after approving a merchant/collection account. Personal wallet numbers are not sufficient for automatic confirmation.

Render environment variables are listed in `.env.example` and are declared as private values in `render.yaml`. Enter the provider-issued values in Render; never commit them to GitHub or place them in frontend JavaScript.

The required callback base URL is:

`https://wifi-billing-system-e14d.onrender.com`

The exact payment webhook URL is:

`https://wifi-billing-system-e14d.onrender.com/api/payments/webhook`

Provider onboarding must register the payment callback URL with the provider before automatic payment confirmation can be enabled. Until those credentials and callback registration exist, the app intentionally uses the secure USSD/app handoff and does not mark a payment paid automatically.

When a valid signed callback is received, the app verifies the amount and provider reference, marks the matching bill as paid, creates a server-side Wi-Fi voucher, and exposes its status through the payment intent endpoint. The customer page polls that intent and displays the voucher after confirmation.

Once `MTN_API_USER`/`MTN_API_KEY`/`MTN_COLLECTION_PRIMARY_KEY` (or `AIRTEL_CLIENT_ID`/`AIRTEL_CLIENT_SECRET`) are set, `POST /api/payments/checkout` calls the provider's "request to pay" API directly, pushing a PIN prompt straight to the customer's phone. The frontend then polls `POST /api/payments/intents/<id>/sync`, which queries the provider's own transaction-status endpoint, so activation is automatic even before a webhook callback is registered.

## Security and access notes

- `MANUAL_PAYMENT_RECIPIENT` (the number that receives manual/USSD payments) is read only on the server and is never embedded in the committed frontend HTML/JS; the browser fetches a one-time dial/SMS link from `/api/payments/manual-instructions` at click time.
- `SUPPORT_INQUIRY_NUMBER` is public and shown at the bottom of the customer portal.
- Optional SMS receipts via Africa's Talking: set `AT_USERNAME` and `AT_API_KEY` (and `AT_SENDER_ID` if you have one). Without them no SMS is sent.
- Admin extras: revenue reports and CSV export, coupons, refunds (books only; return the money via mobile money yourself), reseller agents (`Authorization: Bearer <token>` on `/api/agent/*`), per-plan speed/data limits and an audit log.
- A voucher starts its shared timer on first successful access login. MAC sessions are tracked and limited by `MAX_DEVICES_PER_VOUCHER` (default one, configurable per voucher); the router must pass a stable MAC to enforce limits and reconnects. TVs are treated like any other device when their MAC is supplied. Router calls to `GET /api/access/validate` and `/api/access/reconnect` can be protected with `ROUTER_API_KEY` sent as `X-Router-Key`.
- Admin mutations require a CSRF token and admin login requires TOTP in production. The captive portal requires a valid, short-lived signed MAC ticket from a trusted router adapter; do not trust a customer-edited MAC query parameter.
- Repeated failed payments from the same phone are throttled via `MAX_FAILED_PAYMENT_ATTEMPTS` / `FAILED_ATTEMPT_WINDOW_MINUTES`.
- The app answers common OS/router captive-portal probe URLs (`/generate_204`, `/hotspot-detect.html`, etc.) with a redirect to the portal so a newly connected device is prompted automatically; the router/AP itself must still be configured (walled garden / hotspot mode) to intercept client traffic and point it at this app.
- Actually cutting a device's network access off at expiry requires your router/RADIUS/hotspot software to poll or receive from `/api/access/validate`; this app cannot control hardware network access directly.
- Run `reaper.py` as a separate worker with the same persistent `WIFI_DATA_DIR` to expire vouchers and request disconnects every 30 seconds. Render's current `render.yaml` only starts the web service; configure a worker service or use Docker Compose for the expiry worker.

## Optional: static Netlify frontend

The `netlify-deployment` folder remains available for a browser-only demo. It stores records in the current browser and should not be used as the shared activity system.

## Existing Netlify frontend

Location: `c:\Users\eco\Documents\wifi billing system\`

### Files to Upload to Netlify:
Upload the complete `netlify-deployment` folder. It contains the customer page, admin dashboard, payment logic, offline cache, routing configuration, and QR image.

---

## Step-by-Step Deployment (3 minutes)

### 1. Go to Netlify
Visit: **https://app.netlify.com**

### 2. Deploy Your Site
**Option A: Drag & Drop (Easiest)**
- Login to Netlify
- Drag the `netlify-deployment` folder into the deploy area
- Done! You'll get a live URL instantly

**Option B: GitHub + Auto-Deploy**
- Push files to GitHub
- Connect repo to Netlify
- Auto-deploys on every push

---

## Deploy This Exact Folder

Upload this folder to the Netlify site `quiet-chimera-4dae8a`:

`c:\Users\eco\Documents\wifi billing system\netlify-deployment`

The folder must contain `index.html`, `style.css`, `script.js`, `netlify.toml`, and `wifi-billing-qr.png`. A 404 at `https://quiet-chimera-4dae8a.netlify.app/` means this folder has not been deployed to that site yet.

## Current Live Site

The current public site is:

`https://quiet-chimera-4dae8a.netlify.app/`

The Netlify version works as a standalone frontend and stores its records in the current browser. Payment recipient settings are available only after admin login and are saved on that admin device.

---

## Payment Integration Note

USSD, app, web, SMS, and call methods are available. A fully automatic payment confirmation requires an official MTN or Airtel merchant API and a secure backend; the customer must always approve the wallet transaction with their PIN.
