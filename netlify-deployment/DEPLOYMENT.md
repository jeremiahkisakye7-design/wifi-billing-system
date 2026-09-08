# 🚀 WiFi Billing System - Netlify Deployment Guide

## Your Frontend Files Ready for Deployment

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

## Automatic WiFi Popup

The service name is **Instant WiFi** and the captive-portal address is:

`https://quiet-chimera-4dae8a.netlify.app/`

To make this page pop up when a nearby customer joins the WiFi, configure the CPE/router hotspot or captive-portal setting to redirect unauthenticated clients to that URL. The website alone cannot detect a device joining WiFi or force a popup; that redirect must be enabled on the router.
