# WiFi Billing System Deployment Guide

## Recommended: one website with shared activity

Deploy the Flask app as one web service. This serves the customer portal, admin activity dashboard, API, SQLite database, and installable app from the same URL.

### Render deployment

1. Push this project to a GitHub repository.
2. In Render, choose **New + > Blueprint** and select the repository.
3. When prompted, enter a private `ADMIN_USERNAME` and `ADMIN_PASSWORD`. Render generates the session `SECRET_KEY` automatically.
4. Render will use `render.yaml` to install the dependencies and start the app.
5. Open the generated HTTPS URL. Log in through **Admin** to check billing activity from any device.

The Render blueprint attaches a persistent disk at `/opt/render/project/src/data`, and the app stores its database there automatically. Keep the disk attached so redeployments do not erase billing records.

The app also supports installation from Chrome or Edge using the **Install app** button in the admin dashboard.

Router management is stored in the same shared database and is available online after admin login. It currently manages the router registry and connection details; live reboot, bandwidth, and connected-device control require credentials and an API supported by the specific router model.

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
