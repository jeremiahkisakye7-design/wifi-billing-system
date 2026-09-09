import json
import hashlib
import hmac
import logging
import os
import sqlite3
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
except ImportError:  # optional dependency, degrade gracefully
    Limiter = None

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("WIFI_DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "billing.db"
IS_PRODUCTION = os.environ.get("RENDER", "") != "" or os.environ.get("FLASK_ENV") == "production"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("wifi-billing")

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "local-development-key-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
PAYMENT_WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "")

if Limiter:
    limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri=os.environ.get("RATE_LIMIT_STORAGE_URL", "memory://"))
else:
    class _NoopLimiter:
        def limit(self, *_args, **_kwargs):
            def decorator(fn):
                return fn

            return decorator

    limiter = _NoopLimiter()
ALLOWED_PLANS = {"hourly", "shortSession", "fullDay", "weekly", "monthly", "quarterly"}
ALLOWED_STATUSES = {"pending", "paid", "expired"}
PLAN_PRICES = {
    "hourly": 200, "shortSession": 500, "fullDay": 1000, "weekly": 4000,
    "monthly": 25000, "quarterly": 70000,
}
PLAN_DURATIONS_DAYS = {
    "hourly": 1 / 24, "shortSession": 0.25, "fullDay": 1, "weekly": 7,
    "monthly": 30, "quarterly": 90,
}
# Bundle deal: a monthly/quarterly plan includes a bonus week at no extra cost.
BUNDLE_BONUS_DAYS = {"monthly": 7, "quarterly": 7}
# Monetization: speed tiers change price, throughput, and how many devices a voucher can authorize.
SPEED_TIER_MULTIPLIER = {"basic": 1.0, "premium": 1.6}
SPEED_TIER_DEVICE_LIMIT = {"basic": 1, "premium": 3}
SPEED_TIER_MBPS = {"basic": 2, "premium": 10}
# Loyalty reward: every Nth paid purchase from the same phone gets a percentage discount.
LOYALTY_PURCHASES_INTERVAL = 5
LOYALTY_DISCOUNT_PERCENT = 10
# "Buy 5 daily vouchers, get 1 free": every 6th fullDay purchase from the same phone is free.
DAILY_LOYALTY_PLAN = "fullDay"
DAILY_LOYALTY_BUY_COUNT = 5
# Referral reward: a successful referred purchase grants the referrer a free hourly voucher.
REFERRAL_REWARD_PLAN = "hourly"

PAYMENT_PROVIDER = os.environ.get("PAYMENT_PROVIDER", "manual").lower()

MTN_MERCHANT_CODE = os.environ.get("MTN_MERCHANT_CODE", "")
MTN_COLLECTION_PRIMARY_KEY = os.environ.get("MTN_COLLECTION_PRIMARY_KEY", "")
MTN_API_USER = os.environ.get("MTN_API_USER", "")
MTN_API_KEY = os.environ.get("MTN_API_KEY", "")
MTN_TARGET_ENVIRONMENT = os.environ.get("MTN_TARGET_ENVIRONMENT", "mtnuganda")
MTN_BASE_URL = os.environ.get(
    "MTN_BASE_URL",
    "https://sandbox.momodeveloper.mtn.com" if MTN_TARGET_ENVIRONMENT == "sandbox" else "https://proxy.momoapi.mtn.com",
)

AIRTEL_MERCHANT_CODE = os.environ.get("AIRTEL_MERCHANT_CODE", "")
AIRTEL_CLIENT_ID = os.environ.get("AIRTEL_CLIENT_ID", "")
AIRTEL_CLIENT_SECRET = os.environ.get("AIRTEL_CLIENT_SECRET", "")
AIRTEL_COUNTRY = os.environ.get("AIRTEL_COUNTRY", "UG")
AIRTEL_CURRENCY = os.environ.get("AIRTEL_CURRENCY", "UGX")
AIRTEL_TARGET_ENVIRONMENT = os.environ.get("AIRTEL_TARGET_ENVIRONMENT", "sandbox")
AIRTEL_BASE_URL = os.environ.get(
    "AIRTEL_BASE_URL",
    "https://openapiuat.airtel.africa" if AIRTEL_TARGET_ENVIRONMENT == "sandbox" else "https://openapi.airtel.africa",
)

PAYMENT_CALLBACK_BASE_URL = os.environ.get("PAYMENT_CALLBACK_BASE_URL", "")

# Kept server-side only; never sent to the public frontend source or static pages.
MANUAL_PAYMENT_RECIPIENT = os.environ.get("MANUAL_PAYMENT_RECIPIENT", "0741808601")
# Safe to display publicly as the customer support contact.
SUPPORT_INQUIRY_NUMBER = os.environ.get("SUPPORT_INQUIRY_NUMBER", "0704270565")
# Optional shared secret a router/captive-portal integration must send to query access status.
ROUTER_API_KEY = os.environ.get("ROUTER_API_KEY", "")
# Fraud detection: block an intent/phone after repeated failed provider callbacks.
MAX_FAILED_PAYMENT_ATTEMPTS = int(os.environ.get("MAX_FAILED_PAYMENT_ATTEMPTS", "5"))
FAILED_ATTEMPT_WINDOW_MINUTES = int(os.environ.get("FAILED_ATTEMPT_WINDOW_MINUTES", "30"))

_token_cache_lock = threading.Lock()
_token_cache = {}


@app.before_request
def _enforce_https():
    if IS_PRODUCTION and request.headers.get("X-Forwarded-Proto", "https") == "http":
        url = request.url.replace("http://", "https://", 1)
        return redirect(url, code=301)


def utc_now():
    return datetime.now(timezone.utc)


def mask_phone(phone):
    phone = str(phone or "")
    return f"{phone[:4]}***{phone[-2:]}" if len(phone) > 6 else "***"


def _phone_hash(phone):
    return hashlib.sha256(str(phone).encode()).hexdigest()


def is_payment_blocked(phone):
    """Basic fraud guard: block a phone number after repeated recent failed payment attempts."""
    cutoff = (utc_now() - timedelta(minutes=FAILED_ATTEMPT_WINDOW_MINUTES)).isoformat()
    conn = get_db_connection()
    count = conn.execute(
        "SELECT COUNT(*) AS total FROM payment_failures WHERE phone_hash = ? AND created_at > ?",
        (_phone_hash(phone), cutoff),
    ).fetchone()["total"]
    conn.close()
    return count >= MAX_FAILED_PAYMENT_ATTEMPTS


def record_payment_failure(phone):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO payment_failures (id, phone_hash, created_at) VALUES (?, ?, ?)",
        (secrets.token_hex(8), _phone_hash(phone), utc_now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_pricing_config():
    """Admin-adjustable pricing/tiers, merged over the built-in defaults."""
    defaults = {
        "planPrices": PLAN_PRICES,
        "planDurationsDays": PLAN_DURATIONS_DAYS,
        "bundleBonusDays": BUNDLE_BONUS_DAYS,
        "speedTierMultiplier": SPEED_TIER_MULTIPLIER,
        "speedTierDeviceLimit": SPEED_TIER_DEVICE_LIMIT,
        "loyaltyPurchasesInterval": LOYALTY_PURCHASES_INTERVAL,
        "loyaltyDiscountPercent": LOYALTY_DISCOUNT_PERCENT,
        "dailyLoyaltyBuyCount": DAILY_LOYALTY_BUY_COUNT,
    }
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM settings WHERE key = 'pricing_config'").fetchone()
    conn.close()
    if not row:
        return defaults
    try:
        overrides = json.loads(row["value"])
    except (TypeError, ValueError):
        return defaults
    for key, value in overrides.items():
        if key in defaults and isinstance(defaults[key], dict) and isinstance(value, dict):
            defaults[key] = {**defaults[key], **value}
        elif key in defaults:
            defaults[key] = value
    return defaults


def save_pricing_overrides(overrides):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('pricing_config', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(overrides),),
    )
    conn.commit()
    conn.close()


def log_activity(event_type, detail="", request_obj=None, conn=None):
    """Record an activity entry. Pass an existing open `conn` to reuse it (avoids a
    second connection deadlocking against an uncommitted write on the same database)."""
    ip_address = ""
    try:
        req = request_obj or request
        ip_address = req.headers.get("X-Forwarded-For", req.remote_addr or "")[:64]
    except Exception:
        pass
    entry = (secrets.token_hex(8), event_type, detail, ip_address, utc_now().isoformat())
    if conn is not None:
        conn.execute(
            "INSERT INTO activity_log (id, event_type, detail, ip_address, created_at) VALUES (?, ?, ?, ?, ?)",
            entry,
        )
        return
    try:
        own_conn = get_db_connection()
        own_conn.execute(
            "INSERT INTO activity_log (id, event_type, detail, ip_address, created_at) VALUES (?, ?, ?, ?, ?)",
            entry,
        )
        own_conn.commit()
        own_conn.close()
    except Exception:
        logger.exception("Failed to write activity log for %s", event_type)


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return jsonify({"error": "Authentication required."}), 401
        return view(*args, **kwargs)

    return wrapped


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clients (
            id TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            plan TEXT NOT NULL,
            payment_method TEXT,
            device_count INTEGER DEFAULT 1,
            date TEXT NOT NULL,
            amount INTEGER NOT NULL,
            discount INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            addons TEXT DEFAULT '[]',
            created_at TEXT NOT NULL
        )
        """
    )
    client_columns = {row[1] for row in conn.execute("PRAGMA table_info(clients)").fetchall()}
    if "router_id" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN router_id TEXT DEFAULT ''")
    if "voucher_code" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN voucher_code TEXT DEFAULT ''")
    if "expires_at" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN expires_at TEXT")
    if "device_mac" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN device_mac TEXT DEFAULT ''")
    if "speed_tier" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN speed_tier TEXT DEFAULT 'basic'")
    if "auto_renew" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN auto_renew INTEGER DEFAULT 0")
    if "referral_code" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN referral_code TEXT DEFAULT ''")
    if "referred_by" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN referred_by TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clients_status ON clients(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clients_date ON clients(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clients_device_mac ON clients(device_mac)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clients_referral_code ON clients(referral_code)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voucher_devices (
            id TEXT PRIMARY KEY,
            voucher_code TEXT NOT NULL,
            device_mac TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(voucher_code, device_mac)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_voucher_devices_voucher ON voucher_devices(voucher_code)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_intents (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            amount INTEGER NOT NULL,
            phone TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            provider_reference TEXT UNIQUE,
            created_at TEXT NOT NULL,
            confirmed_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_intents_status ON payment_intents(status)")
    payment_intent_columns = {row[1] for row in conn.execute("PRAGMA table_info(payment_intents)").fetchall()}
    if "provider_status" not in payment_intent_columns:
        conn.execute("ALTER TABLE payment_intents ADD COLUMN provider_status TEXT DEFAULT ''")
    if "failure_reason" not in payment_intent_columns:
        conn.execute("ALTER TABLE payment_intents ADD COLUMN failure_reason TEXT DEFAULT ''")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_log (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            detail TEXT DEFAULT '',
            ip_address TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_log_created_at ON activity_log(created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_failures (
            id TEXT PRIMARY KEY,
            phone_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payment_failures_phone_hash ON payment_failures(phone_hash)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            model TEXT NOT NULL,
            ip TEXT NOT NULL,
            status TEXT DEFAULT 'online',
            clients INTEGER DEFAULT 0,
            details TEXT DEFAULT '{}'
        )
        """
    )
    if conn.execute("SELECT COUNT(*) AS total FROM routers").fetchone()["total"] == 0:
        conn.executemany(
            "INSERT INTO routers (id, name, model, ip, status, clients) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("router-1", "Main Router", "TP-Link Archer C7", "192.168.1.1", "online", 8),
                ("router-2", "Backup Router", "D-Link DIR-882", "192.168.1.2", "online", 3),
            ],
        )
    conn.execute(
        """
        INSERT OR IGNORE INTO routers (id, name, model, ip, status, clients, details)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "router-cpe-b07",
            "CPE_BE97",
            "B07",
            "192.168.100.1",
            "online",
            0,
            json.dumps({
                "mac": "74:F8:DB:63:BE:97",
                "imei": "353899266529310",
                "serialNumber": "GUVECM160224",
                "power": "5V / 2A",
                "wifiKey": "1234567890",
                "captivePortalUrl": "https://wifi-billing-system-e14d.onrender.com/",
            }),
        ),
    )
    conn.execute(
        "UPDATE routers SET details = ? WHERE id = 'router-cpe-b07'",
        (json.dumps({
            "mac": "74:F8:DB:63:BE:97",
            "imei": "353899266529310",
            "serialNumber": "GUVECM160224",
            "power": "5V / 2A",
            "wifiKey": "1234567890",
            "captivePortalUrl": "https://wifi-billing-system-e14d.onrender.com/",
        }),),
    )

    existing = conn.execute("SELECT COUNT(*) AS total FROM clients").fetchone()["total"]
    if existing == 0:
        sample_records = [
            (
                "demo-1",
                "Nansubuga Aisha",
                "+256700111222",
                "monthly",
                "Mobile Money",
                2,
                utc_now().strftime("%Y-%m-%d"),
                26625,
                500,
                "paid",
                json.dumps(["router"]),
                utc_now().isoformat(),
            ),
            (
                "demo-2",
                "Kato Daniel",
                "+256784555777",
                "weekly",
                "Cash",
                1,
                utc_now().strftime("%Y-%m-%d"),
                7450,
                0,
                "pending",
                json.dumps(["speed"]),
                utc_now().isoformat(),
            ),
        ]
        conn.executemany(
            """
            INSERT INTO clients (
                id, customer_name, phone, plan, payment_method, device_count,
                date, amount, discount, status, addons, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            sample_records,
        )

    conn.commit()
    conn.close()


def _mtn_get_token():
    cached = _token_cache.get("mtn")
    if cached and cached["expires_at"] > time.time():
        return cached["token"]
    with _token_cache_lock:
        cached = _token_cache.get("mtn")
        if cached and cached["expires_at"] > time.time():
            return cached["token"]
        response = requests.post(
            f"{MTN_BASE_URL}/collection/token/",
            auth=(MTN_API_USER, MTN_API_KEY),
            headers={"Ocp-Apim-Subscription-Key": MTN_COLLECTION_PRIMARY_KEY},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        token = data["access_token"]
        _token_cache["mtn"] = {"token": token, "expires_at": time.time() + int(data.get("expires_in", 3600)) - 60}
        return token


def mtn_request_to_pay(amount, phone, reference_id, external_id):
    token = _mtn_get_token()
    response = requests.post(
        f"{MTN_BASE_URL}/collection/v1_0/requesttopay",
        json={
            "amount": str(amount),
            "currency": "UGX" if MTN_TARGET_ENVIRONMENT != "sandbox" else "EUR",
            "externalId": external_id,
            "payer": {"partyIdType": "MSISDN", "partyId": phone.lstrip("+")},
            "payerMessage": "WiFi access payment",
            "payeeNote": "WiFi access payment",
        },
        headers={
            "Authorization": f"Bearer {token}",
            "X-Reference-Id": reference_id,
            "X-Target-Environment": MTN_TARGET_ENVIRONMENT,
            "Ocp-Apim-Subscription-Key": MTN_COLLECTION_PRIMARY_KEY,
            "Content-Type": "application/json",
        },
        timeout=20,
    )
    response.raise_for_status()
    return reference_id


def mtn_get_status(reference_id):
    token = _mtn_get_token()
    response = requests.get(
        f"{MTN_BASE_URL}/collection/v1_0/requesttopay/{reference_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Target-Environment": MTN_TARGET_ENVIRONMENT,
            "Ocp-Apim-Subscription-Key": MTN_COLLECTION_PRIMARY_KEY,
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("status", "").upper()


def _airtel_get_token():
    cached = _token_cache.get("airtel")
    if cached and cached["expires_at"] > time.time():
        return cached["token"]
    with _token_cache_lock:
        cached = _token_cache.get("airtel")
        if cached and cached["expires_at"] > time.time():
            return cached["token"]
        response = requests.post(
            f"{AIRTEL_BASE_URL}/auth/oauth2/token",
            json={
                "client_id": AIRTEL_CLIENT_ID,
                "client_secret": AIRTEL_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        token = data["access_token"]
        _token_cache["airtel"] = {"token": token, "expires_at": time.time() + int(data.get("expires_in", 3600)) - 60}
        return token


def airtel_request_to_pay(amount, phone, reference_id):
    token = _airtel_get_token()
    response = requests.post(
        f"{AIRTEL_BASE_URL}/merchant/v1/payments/",
        json={
            "reference": reference_id,
            "subscriber": {"country": AIRTEL_COUNTRY, "currency": AIRTEL_CURRENCY, "msisdn": phone.lstrip("+").lstrip("256")},
            "transaction": {"amount": amount, "country": AIRTEL_COUNTRY, "currency": AIRTEL_CURRENCY, "id": reference_id},
        },
        headers={
            "Authorization": f"Bearer {token}",
            "X-Country": AIRTEL_COUNTRY,
            "X-Currency": AIRTEL_CURRENCY,
            "Content-Type": "application/json",
        },
        timeout=20,
    )
    response.raise_for_status()
    return reference_id


def airtel_get_status(reference_id):
    token = _airtel_get_token()
    response = requests.get(
        f"{AIRTEL_BASE_URL}/standard/v1/payments/{reference_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Country": AIRTEL_COUNTRY,
            "X-Currency": AIRTEL_CURRENCY,
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("data", {}).get("transaction", {}).get("status", "").upper()


def grant_free_voucher(conn, phone, plan, reason):
    """Directly activate a free voucher (loyalty/referral reward) without a payment intent."""
    now = utc_now()
    config = get_pricing_config()
    client_id = f"reward-{now.strftime('%Y%m%d%H%M%S%f')}"
    voucher_code = f"WIFI-{secrets.token_hex(4).upper()}"
    total_days = config["planDurationsDays"].get(plan, 1) + config["bundleBonusDays"].get(plan, 0)
    expires_at = (now + timedelta(days=total_days)).isoformat()
    conn.execute(
        """
        INSERT INTO clients (id, customer_name, phone, plan, payment_method, device_count,
            date, amount, discount, status, addons, router_id, device_mac, speed_tier, auto_renew,
            voucher_code, expires_at, referral_code, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (client_id, "Reward customer", phone, plan, reason, 1, now.strftime("%Y-%m-%d"),
         0, 0, "paid", "[]", "", "", "basic", 0, voucher_code, expires_at, secrets.token_hex(3).upper(),
         now.isoformat()),
    )
    log_activity(reason, f"phone={mask_phone(phone)} voucher={voucher_code}", conn=conn)
    return voucher_code


def mark_intent_paid(conn, intent, reference):
    """Confirm a payment intent, activate the client's WiFi voucher, and log the event. Caller must commit/close conn."""
    if intent["status"] == "paid":
        return {"success": True, "duplicate": True}
    confirmed_at = utc_now().isoformat()
    client = conn.execute(
        "SELECT plan, phone, referred_by FROM clients WHERE id = ?", (intent["client_id"],)
    ).fetchone()
    config = get_pricing_config()
    voucher_code = f"WIFI-{secrets.token_hex(4).upper()}"
    referral_code = secrets.token_hex(3).upper()
    total_days = config["planDurationsDays"].get(client["plan"], 30) + config["bundleBonusDays"].get(client["plan"], 0)
    expires_at = (utc_now() + timedelta(days=total_days)).isoformat()
    conn.execute(
        "UPDATE payment_intents SET status = 'paid', provider_reference = ?, confirmed_at = ? WHERE id = ?",
        (reference, confirmed_at, intent["id"]),
    )
    conn.execute(
        "UPDATE clients SET status = 'paid', voucher_code = ?, expires_at = ?, referral_code = ? WHERE id = ?",
        (voucher_code, expires_at, referral_code, intent["client_id"]),
    )
    log_activity("payment_confirmed", f"intent={intent['id']} voucher={voucher_code}", conn=conn)

    if client["referred_by"]:
        referrer = conn.execute(
            "SELECT phone FROM clients WHERE referral_code = ? AND phone != ? LIMIT 1",
            (client["referred_by"], client["phone"]),
        ).fetchone()
        if referrer:
            grant_free_voucher(conn, referrer["phone"], REFERRAL_REWARD_PLAN, "referral_reward_granted")

    return {"success": True, "clientId": intent["client_id"], "voucherCode": voucher_code, "expiresAt": expires_at}


@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "outview.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/api/auth/login")
@limiter.limit("5 per minute")
def login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    if ADMIN_PASSWORD_HASH:
        password_ok = check_password_hash(ADMIN_PASSWORD_HASH, password)
    else:
        password_ok = secrets.compare_digest(password, ADMIN_PASSWORD)
    if not secrets.compare_digest(username, ADMIN_USERNAME) or not password_ok:
        log_activity("login_failed", f"username={username}")
        return jsonify({"error": "Invalid username or password."}), 401
    session.clear()
    session["admin_authenticated"] = True
    log_activity("login_success", f"username={username}")
    return jsonify({"authenticated": True})


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"authenticated": False})


@app.get("/api/auth/session")
def auth_session():
    return jsonify({"authenticated": bool(session.get("admin_authenticated"))})


@app.post("/api/payments/checkout")
@limiter.limit("10 per minute")
def checkout():
    """Create a payment intent and, when live credentials are configured, trigger an
    automatic MTN MoMo / Airtel Money 'request to pay' prompt on the customer's phone."""
    payload = request.get_json(silent=True) or {}
    plan = payload.get("plan")
    phone = str(payload.get("phone", "")).strip()
    provider = str(payload.get("provider", "")).lower()
    device_mac = str(payload.get("deviceMac", "")).strip()[:64]
    speed_tier = str(payload.get("speedTier", "basic")).lower()
    auto_renew = 1 if payload.get("autoRenew") else 0
    referred_by = str(payload.get("referredBy", "")).strip().upper()[:16]
    config = get_pricing_config()
    if plan not in config["planPrices"] or not phone or provider not in {"momo", "airtel"}:
        return jsonify({"error": "Plan, phone, and payment provider are required."}), 400
    if speed_tier not in config["speedTierMultiplier"]:
        speed_tier = "basic"
    digits_only = phone.lstrip("+")
    if not digits_only.isdigit() or len(digits_only) < 9 or len(digits_only) > 15:
        return jsonify({"error": "Enter a valid phone number."}), 400
    if is_payment_blocked(phone):
        log_activity("checkout_blocked_fraud", f"phone={mask_phone(phone)}")
        return jsonify({"error": "Too many failed payment attempts. Please try again later."}), 429

    conn = get_db_connection()
    prior_paid_count = conn.execute(
        "SELECT COUNT(*) AS total FROM clients WHERE phone = ? AND status = 'paid'", (phone,)
    ).fetchone()["total"]

    if plan == DAILY_LOYALTY_PLAN:
        prior_daily_count = conn.execute(
            "SELECT COUNT(*) AS total FROM clients WHERE phone = ? AND plan = ? AND status = 'paid' AND amount > 0",
            (phone, DAILY_LOYALTY_PLAN),
        ).fetchone()["total"]
        if prior_daily_count > 0 and prior_daily_count % config["dailyLoyaltyBuyCount"] == 0:
            voucher_code = grant_free_voucher(conn, phone, plan, "loyalty_free_daily_voucher")
            conn.commit()
            conn.close()
            return jsonify({
                "mode": "free_reward",
                "voucherCode": voucher_code,
                "message": "You earned a free daily voucher for your loyalty! It is already active.",
            }), 201

    loyalty_discount_applied = (
        plan != DAILY_LOYALTY_PLAN
        and prior_paid_count > 0
        and (prior_paid_count + 1) % config["loyaltyPurchasesInterval"] == 0
    )

    base_price = round(config["planPrices"][plan] * config["speedTierMultiplier"][speed_tier])
    if loyalty_discount_applied:
        base_price = round(base_price * (100 - config["loyaltyDiscountPercent"]) / 100)
    amount = base_price + (base_price * 5 + 99) // 100
    now = utc_now()
    client_id = f"public-{now.strftime('%Y%m%d%H%M%S%f')}"
    intent_id = f"pay-{now.strftime('%Y%m%d%H%M%S%f')}"
    reference_id = str(uuid.uuid4())

    conn.execute(
        """
        INSERT INTO clients (id, customer_name, phone, plan, payment_method, device_count,
            date, amount, discount, status, addons, router_id, device_mac, speed_tier, auto_renew, referred_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (client_id, "Public customer", phone, plan, provider, 1, now.strftime("%Y-%m-%d"),
         amount, 0, "pending", "[]", "", device_mac, speed_tier, auto_renew, referred_by, now.isoformat()),
    )
    conn.execute(
        "INSERT INTO payment_intents (id, client_id, provider, amount, phone, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (intent_id, client_id, provider, amount, phone, now.isoformat()),
    )
    conn.commit()

    live_enabled = (
        (provider == "momo" and MTN_API_USER and MTN_API_KEY and MTN_COLLECTION_PRIMARY_KEY)
        or (provider == "airtel" and AIRTEL_CLIENT_ID and AIRTEL_CLIENT_SECRET)
    )
    if live_enabled:
        try:
            if provider == "momo":
                mtn_request_to_pay(amount, digits_only, reference_id, intent_id)
            else:
                airtel_request_to_pay(amount, digits_only, reference_id)
            conn.execute(
                "UPDATE payment_intents SET provider_reference = ?, provider_status = 'pending' WHERE id = ?",
                (reference_id, intent_id),
            )
            conn.commit()
            log_activity("checkout_push_sent", f"intent={intent_id} provider={provider} phone={mask_phone(phone)}")
            conn.close()
            return jsonify({
                "paymentIntentId": intent_id,
                "clientId": client_id,
                "amount": amount,
                "mode": "automatic",
                "message": "Check your phone and enter your mobile money PIN to complete payment.",
            }), 201
        except requests.RequestException as error:
            logger.warning("Provider request-to-pay failed: %s", error)
            conn.execute(
                "UPDATE payment_intents SET provider_status = 'failed', failure_reason = ? WHERE id = ?",
                (str(error)[:200], intent_id),
            )
            conn.commit()
            conn.close()
            record_payment_failure(phone)
            log_activity("checkout_push_failed", f"intent={intent_id} provider={provider}")
            return jsonify({"error": "Could not reach the mobile money provider. Please try again."}), 502

    conn.close()
    log_activity("checkout_manual_mode", f"intent={intent_id} provider={provider} phone={mask_phone(phone)}")
    return jsonify({
        "paymentIntentId": intent_id,
        "clientId": client_id,
        "amount": amount,
        "mode": "manual",
        "message": "Provider credentials are not configured yet; complete payment using the USSD prompt.",
    }), 201


@app.post("/api/payments/webhook")
def payment_webhook():
    if not PAYMENT_WEBHOOK_SECRET:
        return jsonify({"error": "Payment webhook is not configured."}), 503
    signature = request.headers.get("X-Payment-Signature", "")
    expected = hmac.new(PAYMENT_WEBHOOK_SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        log_activity("webhook_invalid_signature")
        return jsonify({"error": "Invalid payment signature."}), 401

    payload = request.get_json(silent=True) or {}
    intent_id = str(payload.get("paymentIntentId", "")).strip()
    reference = str(payload.get("providerReference", "")).strip()
    status = str(payload.get("status", "")).lower()
    try:
        amount = int(payload.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid payment amount."}), 400
    if not intent_id or not reference or status not in {"successful", "paid"} or amount < 0:
        return jsonify({"error": "Incomplete payment callback."}), 400

    conn = get_db_connection()
    intent = conn.execute("SELECT * FROM payment_intents WHERE id = ?", (intent_id,)).fetchone()
    if intent is None:
        conn.close()
        return jsonify({"error": "Payment intent not found."}), 404
    if amount != intent["amount"]:
        conn.close()
        return jsonify({"error": "Payment amount does not match the intent."}), 400

    result = mark_intent_paid(conn, intent, reference)
    conn.commit()
    conn.close()
    return jsonify(result)


@app.post("/api/payments/intents/<intent_id>/sync")
@limiter.limit("30 per minute")
def sync_payment_intent(intent_id):
    """Poll the provider's transaction status directly; used when a webhook callback
    has not yet been registered with MTN/Airtel, keeping activation fully automatic."""
    conn = get_db_connection()
    intent = conn.execute("SELECT * FROM payment_intents WHERE id = ?", (intent_id,)).fetchone()
    if intent is None:
        conn.close()
        return jsonify({"error": "Payment intent not found."}), 404
    if intent["status"] == "paid" or not intent["provider_reference"]:
        row = conn.execute(
            """
            SELECT p.status, p.amount, p.provider_reference, c.voucher_code, c.expires_at
            FROM payment_intents p JOIN clients c ON c.id = p.client_id WHERE p.id = ?
            """,
            (intent_id,),
        ).fetchone()
        conn.close()
        return jsonify(dict(row))

    try:
        if intent["provider"] == "momo":
            provider_status = mtn_get_status(intent["provider_reference"])
        else:
            provider_status = airtel_get_status(intent["provider_reference"])
    except requests.RequestException as error:
        logger.warning("Provider status check failed: %s", error)
        conn.close()
        return jsonify({"status": intent["status"], "error": "Could not reach provider."}), 502

    if provider_status in {"SUCCESSFUL", "SUCCESS", "TS"}:
        result = mark_intent_paid(conn, intent, intent["provider_reference"])
        conn.commit()
        conn.close()
        return jsonify(result)
    if provider_status in {"FAILED", "REJECTED", "TF"}:
        conn.execute("UPDATE payment_intents SET provider_status = ? WHERE id = ?", (provider_status, intent_id))
        conn.commit()
        conn.close()
        record_payment_failure(intent["phone"])
        log_activity("payment_failed", f"intent={intent_id} providerStatus={provider_status}")
        return jsonify({"status": "failed", "providerStatus": provider_status})

    conn.execute("UPDATE payment_intents SET provider_status = ? WHERE id = ?", (provider_status, intent_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "pending", "providerStatus": provider_status})


@app.get("/api/payments/intents/<intent_id>")
def payment_intent_status(intent_id):
    conn = get_db_connection()
    row = conn.execute(
        """
        SELECT p.status, p.amount, p.provider_reference, c.voucher_code, c.expires_at
        FROM payment_intents p JOIN clients c ON c.id = p.client_id WHERE p.id = ?
        """,
        (intent_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return jsonify({"error": "Payment intent not found."}), 404
    return jsonify(dict(row))


@app.get("/api/access/validate")
@limiter.limit("60 per minute")
def validate_access():
    """Endpoint for a router/captive-portal integration to confirm whether a voucher
    or phone number currently has active, paid WiFi access. A voucher is bound to up to
    SPEED_TIER_DEVICE_LIMIT distinct device MACs, preventing unlimited reuse/sharing."""
    if ROUTER_API_KEY and not secrets.compare_digest(request.headers.get("X-Router-Key", ""), ROUTER_API_KEY):
        return jsonify({"error": "Unauthorized router client."}), 401
    voucher = str(request.args.get("voucher", "")).strip().upper()
    phone = str(request.args.get("phone", "")).strip()
    device_mac = str(request.args.get("mac", "")).strip()[:64]
    if not voucher and not phone:
        return jsonify({"error": "Provide a voucher code or phone number."}), 400

    conn = get_db_connection()
    if voucher:
        row = conn.execute(
            "SELECT plan, status, expires_at, phone, speed_tier FROM clients WHERE voucher_code = ? ORDER BY created_at DESC LIMIT 1",
            (voucher,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT plan, status, expires_at, phone, speed_tier FROM clients WHERE phone = ? AND status = 'paid' ORDER BY created_at DESC LIMIT 1",
            (phone,),
        ).fetchone()

    if row is None:
        conn.close()
        return jsonify({"active": False, "reason": "not_found"})

    if device_mac and voucher:
        device_limit = get_pricing_config()["speedTierDeviceLimit"].get(row["speed_tier"], 1)
        bound_devices = {r["device_mac"] for r in conn.execute(
            "SELECT device_mac FROM voucher_devices WHERE voucher_code = ?", (voucher,)
        ).fetchall()}
        if device_mac not in bound_devices:
            if len(bound_devices) >= device_limit:
                conn.close()
                log_activity("voucher_device_limit_reached", f"voucher={voucher}")
                return jsonify({"active": False, "reason": "device_limit_reached"})
            conn.execute(
                "INSERT INTO voucher_devices (id, voucher_code, device_mac, created_at) VALUES (?, ?, ?, ?)",
                (secrets.token_hex(8), voucher, device_mac, utc_now().isoformat()),
            )
            conn.commit()
    conn.close()

    expires_at = row["expires_at"]
    is_active = bool(row["status"] == "paid" and expires_at and datetime.fromisoformat(expires_at) > utc_now())
    if not is_active and row["status"] == "paid" and expires_at:
        notify_router_disconnect(device_mac, voucher)
    return jsonify({
        "active": is_active,
        "plan": row["plan"],
        "expiresAt": expires_at,
        "reason": None if is_active else "expired",
    })


@app.get("/api/dashboard")
@limiter.limit("60 per minute")
def customer_dashboard():
    """Public, read-only remaining-time dashboard keyed by voucher code or phone, with recent payment history."""
    voucher = str(request.args.get("voucher", "")).strip().upper()
    phone = str(request.args.get("phone", "")).strip()
    if not voucher and not phone:
        return jsonify({"error": "Provide a voucher code or phone number."}), 400

    conn = get_db_connection()
    if voucher:
        row = conn.execute(
            "SELECT customer_name, plan, amount, status, voucher_code, expires_at, speed_tier, auto_renew, phone, referral_code "
            "FROM clients WHERE voucher_code = ? ORDER BY created_at DESC LIMIT 1",
            (voucher,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT customer_name, plan, amount, status, voucher_code, expires_at, speed_tier, auto_renew, phone, referral_code "
            "FROM clients WHERE phone = ? ORDER BY created_at DESC LIMIT 1",
            (phone,),
        ).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "No account found."}), 404

    history_rows = conn.execute(
        "SELECT plan, amount, status, date, expires_at FROM clients WHERE phone = ? ORDER BY created_at DESC LIMIT 10",
        (row["phone"],),
    ).fetchall()
    conn.close()

    item = dict(row)
    expires_at = item.get("expires_at")
    now = utc_now()
    remaining_seconds = 0
    if expires_at:
        remaining_seconds = max(0, int((datetime.fromisoformat(expires_at) - now).total_seconds()))
    item["remainingSeconds"] = remaining_seconds
    item["isActive"] = item["status"] == "paid" and remaining_seconds > 0
    item["history"] = [dict(record) for record in history_rows]
    return jsonify(item)


@app.get("/api/support-info")
def support_info():
    """Public support contact only; the merchant receiving number is never exposed here."""
    return jsonify({"inquiryNumber": SUPPORT_INQUIRY_NUMBER})


@app.get("/api/admin/payment-recipient")
@admin_required
def admin_payment_recipient():
    """Admin-only lookup of the manual payment recipient, used for the walk-in dial flow."""
    return jsonify({"recipient": MANUAL_PAYMENT_RECIPIENT})


@app.get("/api/admin/pricing")
@admin_required
def admin_get_pricing():
    """Admin view of effective voucher pricing, speed tiers, bundles, and loyalty settings."""
    return jsonify(get_pricing_config())


@app.put("/api/admin/pricing")
@admin_required
def admin_update_pricing():
    """Admin control panel for voucher pricing, speed tiers, device limits, bundles, and loyalty rewards."""
    payload = request.get_json(silent=True) or {}
    allowed_keys = {
        "planPrices", "planDurationsDays", "bundleBonusDays", "speedTierMultiplier",
        "speedTierDeviceLimit", "loyaltyPurchasesInterval", "loyaltyDiscountPercent", "dailyLoyaltyBuyCount",
    }
    updates = {key: value for key, value in payload.items() if key in allowed_keys}
    if not updates:
        return jsonify({"error": "No recognized pricing fields were provided."}), 400

    current = get_pricing_config()
    for key, value in updates.items():
        if isinstance(current.get(key), dict) and isinstance(value, dict):
            current[key] = {**current[key], **value}
        else:
            current[key] = value
    save_pricing_overrides(current)
    log_activity("pricing_updated", f"keys={list(updates.keys())}")
    return jsonify(current)


@app.post("/api/payments/auto-renew-check")
def auto_renew_check():
    """Intended to be called by a scheduled job (e.g. Render Cron Job) every few minutes.
    Finds clients who opted into auto-renew and are expiring soon, and pushes a fresh
    request-to-pay for the same plan/phone so their access renews without manual action."""
    if not ROUTER_API_KEY or not secrets.compare_digest(request.headers.get("X-Router-Key", ""), ROUTER_API_KEY):
        return jsonify({"error": "Unauthorized."}), 401

    soon = (utc_now() + timedelta(minutes=15)).isoformat()
    conn = get_db_connection()
    due = conn.execute(
        "SELECT id, phone, plan, payment_method, device_mac, speed_tier FROM clients "
        "WHERE auto_renew = 1 AND status = 'paid' AND expires_at IS NOT NULL AND expires_at <= ?",
        (soon,),
    ).fetchall()
    conn.close()

    renewed = []
    for row in due:
        provider = row["payment_method"] if row["payment_method"] in {"momo", "airtel"} else "momo"
        with app.test_request_context(
            "/api/payments/checkout",
            method="POST",
            json={
                "plan": row["plan"], "phone": row["phone"], "provider": provider,
                "deviceMac": row["device_mac"], "speedTier": row["speed_tier"], "autoRenew": True,
            },
        ):
            checkout()
        renewed.append(row["id"])
    log_activity("auto_renew_check", f"renewed={len(renewed)}")
    return jsonify({"renewed": renewed, "count": len(renewed)})


@app.post("/api/notifications/expiring-soon")
def expiring_soon_notifications():
    """Intended to be called by a scheduled job. Returns clients whose voucher expires
    within the configured warning window, for an SMS/email provider to notify. This app
    does not send SMS/email itself; wire the returned list to your SMS/email API of choice."""
    if not ROUTER_API_KEY or not secrets.compare_digest(request.headers.get("X-Router-Key", ""), ROUTER_API_KEY):
        return jsonify({"error": "Unauthorized."}), 401

    window_minutes = int(request.args.get("windowMinutes", "30"))
    now = utc_now()
    soon = (now + timedelta(minutes=window_minutes)).isoformat()
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT id, phone, plan, voucher_code, expires_at FROM clients "
        "WHERE status = 'paid' AND expires_at IS NOT NULL AND expires_at > ? AND expires_at <= ?",
        (now.isoformat(), soon),
    ).fetchall()
    conn.close()
    notifications = [dict(row) for row in rows]
    log_activity("expiring_soon_check", f"count={len(notifications)}")
    return jsonify({"expiringSoon": notifications, "count": len(notifications)})


ROUTER_DISCONNECT_WEBHOOK_URL = os.environ.get("ROUTER_DISCONNECT_WEBHOOK_URL", "")


def notify_router_disconnect(device_mac, voucher_code):
    """Best-effort call to a router/RADIUS adapter to force-disconnect an expired device.
    Requires ROUTER_DISCONNECT_WEBHOOK_URL to point at your hotspot's own control API."""
    if not ROUTER_DISCONNECT_WEBHOOK_URL or not device_mac:
        return
    try:
        requests.post(
            ROUTER_DISCONNECT_WEBHOOK_URL,
            json={"deviceMac": device_mac, "voucherCode": voucher_code},
            headers={"X-Router-Key": ROUTER_API_KEY} if ROUTER_API_KEY else {},
            timeout=5,
        )
    except requests.RequestException:
        logger.warning("Router disconnect webhook failed for voucher %s", voucher_code)



@app.post("/api/payments/manual-instructions")
@limiter.limit("20 per minute")
def manual_payment_instructions():
    """Build a dial/SMS/app link server-side so the merchant recipient number is never
    present in the committed frontend source, only in a just-in-time API response."""
    payload = request.get_json(silent=True) or {}
    intent_id = str(payload.get("paymentIntentId", "")).strip()
    provider = str(payload.get("provider", "")).lower()
    method = str(payload.get("method", "ussd")).lower()
    if provider not in {"momo", "airtel"}:
        return jsonify({"error": "Invalid provider."}), 400

    conn = get_db_connection()
    intent = conn.execute("SELECT amount FROM payment_intents WHERE id = ?", (intent_id,)).fetchone()
    conn.close()
    if intent is None:
        return jsonify({"error": "Payment intent not found."}), 404
    total = intent["amount"]
    recipient = MANUAL_PAYMENT_RECIPIENT.replace(" ", "")

    if method == "call":
        return jsonify({"callUrl": f"tel:+256{recipient[1:]}"})
    if method == "sms":
        message = f"WiFi instant payment. Total: UGX {total:,}. Please confirm payment."
        return jsonify({"smsUrl": f"sms:+256{recipient[1:]}?body={message}"})
    if method == "web":
        web_url = "https://www.mtn.co.ug/momo/" if provider == "momo" else "https://www.airtel.co.ug/airtel-money"
        return jsonify({"webUrl": web_url})
    if method == "app":
        deep_link = "mtnmomo://send" if provider == "momo" else "airtelmoney://send"
        return jsonify({"appUrl": f"{deep_link}?recipient={recipient}&amount={total}"})

    ussd_prefix = "*165*1*1" if provider == "momo" else "*185*1*1"
    ussd_code = f"{ussd_prefix}*{recipient}*{total}#"
    return jsonify({"ussdDialUrl": f"tel:{ussd_code}"})


_CAPTIVE_PORTAL_PROBE_PATHS = [
    "/generate_204",
    "/gen_204",
    "/hotspot-detect.html",
    "/library/test/success.html",
    "/ncsi.txt",
    "/connecttest.txt",
    "/success.txt",
    "/canonical.html",
]


@app.route("/generate_204")
@app.route("/gen_204")
@app.route("/hotspot-detect.html")
@app.route("/library/test/success.html")
@app.route("/ncsi.txt")
@app.route("/connecttest.txt")
@app.route("/success.txt")
@app.route("/canonical.html")
def captive_portal_probe():
    """OS/router internet-check URLs: reply with a redirect instead of 204/success so the
    device's captive portal popup opens our billing page immediately after it connects."""
    return redirect("/", code=302)


@app.route("/api/clients", methods=["GET", "POST"])
@admin_required
def clients():
    if request.method == "GET":
        conn = get_db_connection()
        rows = conn.execute("SELECT * FROM clients ORDER BY created_at DESC").fetchall()
        records = []
        for row in rows:
            item = dict(row)
            item["addons"] = json.loads(item["addons"] or "[]")
            records.append(item)
        conn.close()
        return jsonify(records)

    payload = request.get_json(silent=True) or {}
    required = ["customerName", "phone", "plan", "amount", "date"]
    if not all(key in payload for key in required):
        return jsonify({"error": "Missing required fields."}), 400

    if not str(payload["customerName"]).strip() or not str(payload["phone"]).strip():
        return jsonify({"error": "Customer name and phone are required."}), 400
    if payload["plan"] not in ALLOWED_PLANS:
        return jsonify({"error": "Invalid plan."}), 400
    try:
        device_count = int(payload.get("deviceCount", 1))
        amount = int(payload["amount"])
        discount = int(payload.get("discount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Numeric billing fields are invalid."}), 400
    if not 1 <= device_count <= 10 or amount < 0 or discount < 0:
        return jsonify({"error": "Billing values are outside the allowed range."}), 400
    if payload.get("status", "pending") not in ALLOWED_STATUSES:
        return jsonify({"error": "Invalid payment status."}), 400

    conn = get_db_connection()
    client_id = payload.get("id") or f"client-{utc_now().strftime('%Y%m%d%H%M%S%f')}"
    record = {
        "id": client_id,
        "customer_name": payload["customerName"],
        "phone": payload["phone"],
        "plan": payload["plan"],
        "payment_method": payload.get("paymentMethod", "Cash"),
        "device_count": device_count,
        "date": payload["date"],
        "amount": amount,
        "discount": discount,
        "status": payload.get("status", "pending"),
        "addons": json.dumps(payload.get("addons", [])),
        "router_id": payload.get("routerId", ""),
        "created_at": utc_now().isoformat(),
    }

    conn.execute(
        """
        INSERT INTO clients (
            id, customer_name, phone, plan, payment_method, device_count,
            date, amount, discount, status, addons, router_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["id"],
            record["customer_name"],
            record["phone"],
            record["plan"],
            record["payment_method"],
            record["device_count"],
            record["date"],
            record["amount"],
            record["discount"],
            record["status"],
            record["addons"],
            record["router_id"],
            record["created_at"],
        ),
    )
    payment_intent_id = f"pay-{utc_now().strftime('%Y%m%d%H%M%S%f')}"
    conn.execute(
        """
        INSERT INTO payment_intents (id, client_id, provider, amount, phone, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            payment_intent_id,
            client_id,
            payload.get("provider", os.environ.get("PAYMENT_PROVIDER", "manual")),
            amount,
            record["phone"],
            record["created_at"],
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "id": client_id, "paymentIntentId": payment_intent_id}), 201


@app.route("/api/clients/<client_id>", methods=["PATCH", "DELETE"])
@admin_required
def client_detail(client_id):
    conn = get_db_connection()
    if request.method == "PATCH":
        payload = request.get_json(silent=True) or {}
        if payload.get("status") not in ALLOWED_STATUSES:
            return jsonify({"error": "Valid status required."}), 400
        conn.execute("UPDATE clients SET status = ? WHERE id = ?", (payload["status"], client_id))
        conn.commit()
        row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        conn.close()
        if row is None:
            return jsonify({"error": "Client not found."}), 404
        item = dict(row)
        item["addons"] = json.loads(item["addons"] or "[]")
        return jsonify(item)

    deleted = conn.execute("DELETE FROM clients WHERE id = ?", (client_id,)).rowcount
    conn.commit()
    conn.close()
    if deleted == 0:
        return jsonify({"error": "Client not found."}), 404
    return jsonify({"deleted": True, "id": client_id})


@app.route("/api/summary")
@admin_required
def summary():
    conn = get_db_connection()
    total_revenue = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM clients WHERE status = 'paid'"
    ).fetchone()["total"]
    active_clients = conn.execute("SELECT COUNT(*) AS total FROM clients").fetchone()["total"]
    pending = conn.execute("SELECT COUNT(*) AS total FROM clients WHERE status = 'pending'").fetchone()["total"]
    today = utc_now().strftime("%Y-%m-%d")
    paid_today = conn.execute(
        "SELECT COUNT(*) AS total FROM clients WHERE date = ? AND status = 'paid'",
        (today,),
    ).fetchone()["total"]
    now_active = conn.execute(
        "SELECT COUNT(*) AS total FROM clients WHERE status = 'paid' AND expires_at > ?", (utc_now().isoformat(),)
    ).fetchone()["total"]
    speed_tier_breakdown = {
        row["speed_tier"] or "basic": row["total"]
        for row in conn.execute(
            "SELECT speed_tier, COUNT(*) AS total FROM clients WHERE status = 'paid' GROUP BY speed_tier"
        ).fetchall()
    }
    fraud_window_start = (utc_now() - timedelta(hours=24)).isoformat()
    fraud_attempts_24h = conn.execute(
        "SELECT COUNT(*) AS total FROM payment_failures WHERE created_at > ?", (fraud_window_start,)
    ).fetchone()["total"]
    peak_hour_row = conn.execute(
        "SELECT strftime('%H', created_at) AS hour, COUNT(*) AS total FROM clients "
        "WHERE status = 'paid' GROUP BY hour ORDER BY total DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return jsonify(
        {
            "totalRevenue": total_revenue,
            "activeClients": active_clients,
            "pendingPayments": pending,
            "paidToday": paid_today,
            "currentlyActiveSessions": now_active,
            "speedTierBreakdown": speed_tier_breakdown,
            "fraudAttemptsLast24h": fraud_attempts_24h,
            "fraudAlert": fraud_attempts_24h >= MAX_FAILED_PAYMENT_ATTEMPTS,
            "peakUsageHourUTC": peak_hour_row["hour"] if peak_hour_row else None,
        }
    )


@app.route("/api/routers", methods=["GET", "POST"])
@admin_required
def routers():
    conn = get_db_connection()
    if request.method == "GET":
        rows = conn.execute("SELECT * FROM routers ORDER BY name").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"] or "{}")
            result.append(item)
        conn.close()
        return jsonify(result)

    payload = request.get_json(silent=True) or {}
    if not all(str(payload.get(key, "")).strip() for key in ("name", "model", "ip")):
        conn.close()
        return jsonify({"error": "Router name, model, and IP are required."}), 400
    router_id = payload.get("id") or f"router-{utc_now().strftime('%Y%m%d%H%M%S%f')}"
    conn.execute(
        "INSERT INTO routers (id, name, model, ip, status, clients, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (router_id, payload["name"].strip(), payload["model"].strip(), payload["ip"].strip(), "online", 0, json.dumps(payload.get("details", {}))),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "id": router_id}), 201


@app.route("/api/routers/<router_id>", methods=["PATCH", "DELETE"])
@admin_required
def router_detail(router_id):
    conn = get_db_connection()
    if request.method == "PATCH":
        payload = request.get_json(silent=True) or {}
        if not str(payload.get("name", "")).strip():
            conn.close()
            return jsonify({"error": "Router name is required."}), 400
        updated = conn.execute("UPDATE routers SET name = ? WHERE id = ?", (payload["name"].strip(), router_id)).rowcount
        conn.commit()
        conn.close()
        return (jsonify({"success": True}), 200) if updated else (jsonify({"error": "Router not found."}), 404)

    deleted = conn.execute("DELETE FROM routers WHERE id = ?", (router_id,)).rowcount
    conn.commit()
    conn.close()
    return (jsonify({"deleted": True}), 200) if deleted else (jsonify({"error": "Router not found."}), 404)


init_db()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", "5002")))
