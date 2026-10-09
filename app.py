import json
import hashlib
import hmac
import logging
import os
import sqlite3
import secrets
import struct
import threading
import time
import uuid
import re
import base64
import csv
import io
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session
from werkzeug.security import check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
except ImportError:  # optional dependency, degrade gracefully
    Limiter = None

BASE_DIR = Path(__file__).resolve().parent
if load_dotenv:
    load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.environ.get("WIFI_DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "billing.db"
IS_PRODUCTION = os.environ.get("RENDER", "") != "" or os.environ.get("FLASK_ENV") == "production"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("wifi-billing")

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if IS_PRODUCTION and (len(SECRET_KEY) < 32 or SECRET_KEY == "local-development-key-change-me"):
    raise RuntimeError("Production requires a random SECRET_KEY of at least 32 characters.")
app.secret_key = SECRET_KEY or "local-development-key-change-me"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
)

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
ADMIN_TOTP_SECRET = os.environ.get("ADMIN_TOTP_SECRET", "").replace(" ", "").upper()
ROUTER_MAC_SIGNING_SECRET = os.environ.get("ROUTER_MAC_SIGNING_SECRET", "")
PAYMENT_WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "")
if IS_PRODUCTION and not ADMIN_PASSWORD_HASH:
    raise RuntimeError("Production requires ADMIN_PASSWORD_HASH; plaintext admin passwords are not accepted.")
if IS_PRODUCTION and (not ADMIN_USERNAME or ADMIN_USERNAME == "admin"):
    raise RuntimeError("Production requires a private ADMIN_USERNAME other than 'admin'.")
if IS_PRODUCTION and not ADMIN_TOTP_SECRET:
    raise RuntimeError("Production requires ADMIN_TOTP_SECRET for admin two-factor authentication.")
if ADMIN_TOTP_SECRET and not re.fullmatch(r"[A-Z2-7]{16,64}", ADMIN_TOTP_SECRET):
    raise RuntimeError("ADMIN_TOTP_SECRET must be a valid Base32 secret of 16-64 characters.")
if ADMIN_TOTP_SECRET:
    try:
        padded_totp_secret = ADMIN_TOTP_SECRET + "=" * (-len(ADMIN_TOTP_SECRET) % 8)
        base64.b32decode(padded_totp_secret, casefold=True)
    except (ValueError, base64.binascii.Error) as error:
        raise RuntimeError("ADMIN_TOTP_SECRET must be valid Base32.") from error

if os.environ.get("TRUST_PROXY_HEADERS", "false").lower() == "true":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

if Limiter:
    limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri=os.environ.get("RATE_LIMIT_STORAGE_URL", "memory://"))
else:
    class _NoopLimiter:
        def limit(self, *_args, **_kwargs):
            def decorator(fn):
                return fn

            return decorator

    limiter = _NoopLimiter()
if IS_PRODUCTION and Limiter is None:
    raise RuntimeError("Flask-Limiter is required in production; install requirements.txt.")
ALLOWED_PLANS = {"halfHour", "hourly", "shortSession", "fullDay", "weekly", "monthly", "quarterly"}
ALLOWED_STATUSES = {"pending", "paid", "expired"}
PLAN_PRICES = {
    "halfHour": 100, "hourly": 200, "shortSession": 500, "fullDay": 1000, "weekly": 4000,
    "monthly": 25000, "quarterly": 70000,
}
PLAN_DURATIONS_DAYS = {
    "halfHour": 0.5 / 24, "hourly": 1 / 24, "shortSession": 0.25, "fullDay": 1, "weekly": 7,
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
if IS_PRODUCTION and not ROUTER_API_KEY:
    raise RuntimeError("Production requires ROUTER_API_KEY to protect router access endpoints.")
if IS_PRODUCTION and len(ROUTER_MAC_SIGNING_SECRET) < 32:
    raise RuntimeError("Production requires ROUTER_MAC_SIGNING_SECRET with at least 32 characters.")
MAX_DEVICES_PER_VOUCHER = max(1, min(10, int(os.environ.get("MAX_DEVICES_PER_VOUCHER", "1"))))
VOUCHER_ACTIVATION_TTL_DAYS = max(1, int(os.environ.get("VOUCHER_ACTIVATION_TTL_DAYS", "30")))
SESSION_REAPER_INTERVAL_SECONDS = max(30, int(os.environ.get("SESSION_REAPER_INTERVAL_SECONDS", "30")))
DEVICE_TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60
VOUCHER_LOGIN_FAILURE_LIMIT = 5
VOUCHER_LOGIN_WINDOW_SECONDS = 10 * 60
# Fraud detection: block an intent/phone after repeated failed provider callbacks.
MAX_FAILED_PAYMENT_ATTEMPTS = int(os.environ.get("MAX_FAILED_PAYMENT_ATTEMPTS", "5"))
FAILED_ATTEMPT_WINDOW_MINUTES = int(os.environ.get("FAILED_ATTEMPT_WINDOW_MINUTES", "30"))

_token_cache_lock = threading.Lock()
_token_cache = {}


@app.before_request
def _block_sensitive_static_files():
    """The app serves its own folder statically; only public web assets may be fetched."""
    path = request.path
    if path.startswith(("/api/", "/health", "/qr.png")) or path == "/":
        return None
    if Path(path).suffix.lower() not in {".html", ".js", ".css", ".png", ".svg", ".webmanifest", ".ico"} or Path(path).name.startswith("."):
        if path not in _CAPTIVE_PORTAL_PROBE_PATHS:
            return jsonify({"error": "Not found."}), 404
    return None


@app.before_request
def _enforce_https():
    if IS_PRODUCTION and request.headers.get("X-Forwarded-Proto", "https") == "http":
        url = request.url.replace("http://", "https://", 1)
        return redirect(url, code=301)


@app.before_request
def _enforce_admin_csrf():
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"} or not session.get("admin_authenticated"):
        return None
    if request.path.startswith(("/api/access/", "/api/payments/")):
        return None
    expected = str(session.get("csrf_token", ""))
    supplied = request.headers.get("X-CSRF-Token", "")
    if not expected or not secrets.compare_digest(expected, supplied):
        return jsonify({"error": "CSRF validation failed. Refresh the admin page and try again."}), 403
    return None


@app.after_request
def _audit_admin_mutation(response):
    if (request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}
            and session.get("admin_authenticated")
            and request.path.startswith("/api/")
            and response.status_code < 400):
        log_activity("admin_mutation", f"method={request.method} path={request.path} status={response.status_code}")
    return response


def utc_now():
    return datetime.now(timezone.utc)


def mask_phone(phone):
    phone = str(phone or "")
    return f"{phone[:4]}***{phone[-2:]}" if len(phone) > 6 else "***"


def _phone_hash(phone):
    return hashlib.sha256(str(phone).encode()).hexdigest()


def _client_ip_hash():
    address = request.remote_addr or "unknown"
    return hmac.new(str(app.secret_key).encode(), address.encode(), hashlib.sha256).hexdigest()


def _voucher_code_hash(code):
    normalized = str(code or "").strip().upper()
    return hmac.new(str(app.secret_key).encode(), normalized.encode(), hashlib.sha256).hexdigest()


def voucher_login_retry_after(code, now=None):
    now = now or utc_now()
    cutoff = (now - timedelta(seconds=VOUCHER_LOGIN_WINDOW_SECONDS)).isoformat()
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT created_at FROM voucher_login_failures WHERE ip_hash = ? AND code_hash = ? AND created_at > ? ORDER BY created_at",
        (_client_ip_hash(), _voucher_code_hash(code), cutoff),
    ).fetchall()
    conn.close()
    if len(rows) < VOUCHER_LOGIN_FAILURE_LIMIT:
        return 0
    first_failure = datetime.fromisoformat(rows[-VOUCHER_LOGIN_FAILURE_LIMIT]["created_at"])
    return max(1, int((first_failure + timedelta(seconds=VOUCHER_LOGIN_WINDOW_SECONDS) - now).total_seconds()))


def record_voucher_login_failure(code):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO voucher_login_failures (id, ip_hash, code_hash, created_at) VALUES (?, ?, ?, ?)",
        (secrets.token_hex(12), _client_ip_hash(), _voucher_code_hash(code), utc_now().isoformat()),
    )
    conn.commit()
    conn.close()


def is_private_mac(device_mac):
    try:
        first_octet = int(str(device_mac).split(":", 1)[0], 16)
        return bool(first_octet & 0x02)
    except (TypeError, ValueError, IndexError):
        return False


def normalize_device_mac(device_mac):
    normalized = str(device_mac or "").strip().lower().replace("-", ":")
    if re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", normalized):
        return normalized
    return ""


def _device_token_hash(token):
    return hashlib.sha256(str(token).encode()).hexdigest()


def create_mac_ticket(device_mac, now=None):
    now = now or utc_now()
    expires_at = int(now.timestamp()) + 120
    claim = f"{device_mac}|{expires_at}|{secrets.token_urlsafe(12)}"
    encoded = base64.urlsafe_b64encode(claim.encode()).rstrip(b"=").decode()
    signature = hmac.new(ROUTER_MAC_SIGNING_SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_mac_ticket(device_mac, ticket, now=None):
    if not ROUTER_MAC_SIGNING_SECRET or not device_mac or not ticket or "." not in ticket:
        return False
    encoded, supplied_signature = ticket.rsplit(".", 1)
    expected_signature = hmac.new(ROUTER_MAC_SIGNING_SECRET.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    if not secrets.compare_digest(expected_signature, supplied_signature):
        return False
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        claimed_mac, expires_at, nonce = decoded.split("|", 2)
        expiry = int(expires_at)
    except (ValueError, UnicodeDecodeError):
        return False
    current_timestamp = (now or utc_now()).timestamp()
    return claimed_mac == device_mac and current_timestamp <= expiry <= current_timestamp + 120 and bool(nonce)


def verify_admin_totp(code, now=None):
    if not ADMIN_TOTP_SECRET or not re.fullmatch(r"\d{6}", str(code or "")):
        return False
    try:
        padded_secret = ADMIN_TOTP_SECRET + "=" * (-len(ADMIN_TOTP_SECRET) % 8)
        secret = base64.b32decode(padded_secret, casefold=True)
    except (ValueError, base64.binascii.Error):
        return False
    counter = int((now if now is not None else time.time()) // 30)
    for offset in (-1, 0, 1):
        digest = hmac.new(secret, struct.pack(">Q", counter + offset), hashlib.sha1).digest()
        index = digest[-1] & 0x0F
        value = struct.unpack(">I", digest[index:index + 4])[0] & 0x7FFFFFFF
        expected = f"{value % 1_000_000:06d}"
        if secrets.compare_digest(expected, str(code)):
            return True
    return False


def generate_voucher_code(prefix=""):
    prefix = str(prefix or "").upper()
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    return prefix + "".join(secrets.choice(alphabet) for _ in range(10 - len(prefix)))


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
        "maxDevicesPerVoucher": MAX_DEVICES_PER_VOUCHER,
        "loyaltyPurchasesInterval": LOYALTY_PURCHASES_INTERVAL,
        "loyaltyDiscountPercent": LOYALTY_DISCOUNT_PERCENT,
        "dailyLoyaltyBuyCount": DAILY_LOYALTY_BUY_COUNT,
        "planSpeedLimitMbps": {},
        "planDataLimitMb": {},
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
    if "duration_seconds" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN duration_seconds INTEGER DEFAULT 0")
    if "activated_at" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN activated_at TEXT")
    if "activation_deadline" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN activation_deadline TEXT")
    if "max_devices" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN max_devices INTEGER DEFAULT 1")
    if "disabled_at" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN disabled_at TEXT")
    if "paused_at" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN paused_at TEXT")
    if "paused_seconds" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN paused_seconds INTEGER DEFAULT 0")
    if "data_limit_mb" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN data_limit_mb INTEGER")
    if "speed_limit_mbps" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN speed_limit_mbps INTEGER")
    if "agent_id" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN agent_id TEXT DEFAULT ''")
    if "commission" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN commission INTEGER DEFAULT 0")
    if "coupon_code" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN coupon_code TEXT DEFAULT ''")
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
    voucher_device_columns = {row[1] for row in conn.execute("PRAGMA table_info(voucher_devices)").fetchall()}
    if "device_slot_id" not in voucher_device_columns:
        conn.execute("ALTER TABLE voucher_devices ADD COLUMN device_slot_id TEXT")
    conn.execute("UPDATE voucher_devices SET device_slot_id = device_mac WHERE device_slot_id IS NULL OR device_slot_id = ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_voucher_devices_slot ON voucher_devices(voucher_code, device_slot_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voucher_device_tokens (
            token_hash TEXT PRIMARY KEY,
            voucher_code TEXT NOT NULL,
            device_slot_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_voucher_tokens_code ON voucher_device_tokens(voucher_code)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS voucher_login_failures (
            id TEXT PRIMARY KEY,
            ip_hash TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_voucher_failures_ip_created ON voucher_login_failures(ip_hash, created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS active_sessions (
            id TEXT PRIMARY KEY,
            voucher_code TEXT NOT NULL,
            device_mac TEXT NOT NULL,
            device_type TEXT DEFAULT '',
            connected_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            disconnected_at TEXT,
            UNIQUE(voucher_code, device_mac)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_active_sessions_status ON active_sessions(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_active_sessions_voucher ON active_sessions(voucher_code)")
    session_columns = {row[1] for row in conn.execute("PRAGMA table_info(active_sessions)").fetchall()}
    if "bytes_used" not in session_columns:
        conn.execute("ALTER TABLE active_sessions ADD COLUMN bytes_used INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_usage_events (
            event_id TEXT PRIMARY KEY,
            voucher_code TEXT NOT NULL,
            device_mac TEXT NOT NULL,
            bytes_delta INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_session_usage_voucher ON session_usage_events(voucher_code)")
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
        CREATE TABLE IF NOT EXISTS refunds (
            id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            voucher_code TEXT DEFAULT '',
            amount INTEGER NOT NULL,
            reason TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_refunds_created_at ON refunds(created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coupons (
            code TEXT PRIMARY KEY,
            percent_off INTEGER,
            amount_off INTEGER,
            max_uses INTEGER,
            used_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            commission_percent INTEGER NOT NULL DEFAULT 10,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
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
    voucher_code = generate_voucher_code()
    total_days = config["planDurationsDays"].get(plan, 1) + config["bundleBonusDays"].get(plan, 0)
    duration_seconds = round(total_days * 86400)
    activation_deadline = (now + timedelta(days=VOUCHER_ACTIVATION_TTL_DAYS)).isoformat()
    conn.execute(
        """
        INSERT INTO clients (id, customer_name, phone, plan, payment_method, device_count,
            date, amount, discount, status, addons, router_id, device_mac, speed_tier, auto_renew,
            voucher_code, expires_at, referral_code, created_at, duration_seconds,
            activated_at, activation_deadline, max_devices)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (client_id, "Reward customer", phone, plan, reason, 1, now.strftime("%Y-%m-%d"),
         0, 0, "paid", "[]", "", "", "basic", 0, voucher_code, None, secrets.token_hex(3).upper(),
         now.isoformat(), duration_seconds, None, activation_deadline, config["maxDevicesPerVoucher"]),
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
    voucher_code = generate_voucher_code()
    referral_code = secrets.token_hex(3).upper()
    total_days = config["planDurationsDays"].get(client["plan"], 30) + config["bundleBonusDays"].get(client["plan"], 0)
    duration_seconds = round(total_days * 86400)
    activation_deadline = (utc_now() + timedelta(days=VOUCHER_ACTIVATION_TTL_DAYS)).isoformat()
    conn.execute(
        "UPDATE payment_intents SET status = 'paid', provider_reference = ?, confirmed_at = ? WHERE id = ?",
        (reference, confirmed_at, intent["id"]),
    )
    conn.execute(
        "UPDATE clients SET status = 'paid', voucher_code = ?, expires_at = NULL, referral_code = ?, "
        "duration_seconds = ?, activated_at = NULL, activation_deadline = ?, max_devices = ?, "
        "data_limit_mb = ?, speed_limit_mbps = ? WHERE id = ?",
        (voucher_code, referral_code, duration_seconds, activation_deadline,
         config["maxDevicesPerVoucher"], config["planDataLimitMb"].get(client["plan"]),
         config["planSpeedLimitMbps"].get(client["plan"]), intent["client_id"]),
    )
    log_activity("payment_confirmed", f"intent={intent['id']} voucher={voucher_code}", conn=conn)

    if client["referred_by"]:
        referrer = conn.execute(
            "SELECT phone FROM clients WHERE referral_code = ? AND phone != ? LIMIT 1",
            (client["referred_by"], client["phone"]),
        ).fetchone()
        if referrer:
            grant_free_voucher(conn, referrer["phone"], REFERRAL_REWARD_PLAN, "referral_reward_granted")

    send_receipt_async(client["phone"], intent["amount"], client["plan"], voucher_code)
    return {
        "success": True,
        "status": "paid",
        "clientId": intent["client_id"],
        "voucherCode": voucher_code,
        "expiresAt": None,
        "durationSeconds": duration_seconds,
        "activationDeadline": activation_deadline,
    }


@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "outview.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/qr.png")
def portal_qr():
    """Always-current portal QR: encodes PORTAL_URL if set, otherwise the host serving this request."""
    import qrcode
    url = os.environ.get("PORTAL_URL", "").rstrip("/") or request.host_url.rstrip("/")
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    buffer = io.BytesIO()
    qr.make_image(fill_color="#075bb5", back_color="#ffffff").save(buffer, format="PNG")
    return Response(buffer.getvalue(), mimetype="image/png", headers={"Cache-Control": "no-cache"})


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
    totp_code = str(payload.get("totpCode", "")).strip()
    totp_ok = verify_admin_totp(totp_code) if ADMIN_TOTP_SECRET else not IS_PRODUCTION
    if not secrets.compare_digest(username, ADMIN_USERNAME) or not password_ok or not totp_ok:
        log_activity("login_failed", f"username={username}")
        return jsonify({"error": "Invalid username or password."}), 401
    session.clear()
    session["admin_authenticated"] = True
    session["csrf_token"] = secrets.token_urlsafe(32)
    log_activity("login_success", f"username={username}")
    return jsonify({"authenticated": True, "csrfToken": session["csrf_token"]})


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"authenticated": False})


@app.get("/api/auth/session")
def auth_session():
    authenticated = bool(session.get("admin_authenticated"))
    return jsonify({
        "authenticated": authenticated,
        "csrfToken": session.get("csrf_token") if authenticated else None,
    })


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
    coupon_code = str(payload.get("couponCode", "")).strip().upper()[:20]
    coupon_discount = 0
    if coupon_code:
        coupon_discount, coupon_error = redeem_coupon(conn, coupon_code, base_price)
        if coupon_error:
            conn.close()
            return jsonify({"error": coupon_error}), 400
        base_price -= coupon_discount
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
         amount, coupon_discount, "pending", "[]", "", device_mac, speed_tier, auto_renew, referred_by, now.isoformat()),
    )
    if coupon_code and coupon_discount:
        conn.execute("UPDATE clients SET coupon_code = ? WHERE id = ?", (coupon_code, client_id))
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
    """Router check/login endpoint. A first successful voucher login starts its shared timer."""
    if ROUTER_API_KEY and not secrets.compare_digest(request.headers.get("X-Router-Key", ""), ROUTER_API_KEY):
        return jsonify({"error": "Unauthorized router client."}), 401
    payload = request.get_json(silent=True) or {}
    voucher = str(request.args.get("voucher", payload.get("voucher", ""))).strip().upper()
    phone = str(request.args.get("phone", "")).strip()
    device_mac = normalize_device_mac(request.args.get("mac", payload.get("mac", "")))
    device_type = str(request.args.get("deviceType", payload.get("deviceType", ""))).strip()[:40]
    if not voucher and not phone:
        return jsonify({"error": "Provide a voucher code or phone number."}), 400

    if voucher:
        if not device_mac:
            return jsonify({"active": False, "reason": "invalid_mac", "message": "A valid device MAC address is required."}), 400
        retry_after = voucher_login_retry_after(voucher)
        if retry_after:
            response = jsonify({"active": False, "reason": "temporarily_locked", "retryAfterSeconds": retry_after})
            response.status_code = 429
            response.headers["Retry-After"] = str(retry_after)
            return response
        result = authorize_voucher(voucher, device_mac, device_type)
        if result.get("reason") in {"not_found", "not_paid"}:
            record_voucher_login_failure(voucher)
            return jsonify({"active": False, "reason": "invalid_voucher"})
        return jsonify(result)

    if not device_mac:
        return jsonify({"active": False, "reason": "invalid_mac", "message": "A valid device MAC address is required."}), 400
    conn = get_db_connection()
    row = conn.execute(
        "SELECT voucher_code FROM clients WHERE phone = ? AND status = 'paid' ORDER BY created_at DESC LIMIT 1",
        (phone,),
    ).fetchone()
    if row is None:
        conn.close()
        return jsonify({"active": False, "reason": "not_found"})
    conn.close()
    if not row["voucher_code"]:
        return jsonify({"active": False, "reason": "voucher_required"})
    return jsonify(authorize_voucher(row["voucher_code"], device_mac, device_type))


def authorize_voucher(voucher_code, device_mac="", device_type="", now=None, device_token="", issue_device_token=False):
    """Activate a paid voucher once, share its expiry across devices, and refresh its MAC session."""
    now = now or utc_now()
    now_iso = now.isoformat()
    conn = get_db_connection()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT id, plan, status, expires_at, duration_seconds, activated_at, activation_deadline, "
        "max_devices, disabled_at, paused_at, paused_seconds, data_limit_mb, speed_limit_mbps "
        "FROM clients WHERE voucher_code = ? ORDER BY created_at DESC LIMIT 1",
        (voucher_code,),
    ).fetchone()
    if row is None:
        conn.rollback()
        conn.close()
        return {"active": False, "reason": "not_found", "message": "Voucher code not found."}
    if row["status"] != "paid":
        conn.rollback()
        conn.close()
        return {"active": False, "reason": "not_paid", "message": "Payment is not confirmed for this voucher."}
    if row["disabled_at"]:
        conn.rollback()
        conn.close()
        return {"active": False, "reason": "disabled", "message": "This voucher has been disabled. Contact support."}
    if row["paused_at"]:
        remaining = max(0, int(row["paused_seconds"] or 0))
        conn.rollback()
        conn.close()
        return {"active": False, "reason": "paused", "remainingSeconds": remaining, "message": "This voucher is paused."}
    used_bytes = conn.execute(
        "SELECT COALESCE(SUM(bytes_used), 0) AS total FROM active_sessions WHERE voucher_code = ?",
        (voucher_code,),
    ).fetchone()["total"]
    data_cap_bytes = int(row["data_limit_mb"] or 0) * 1024 * 1024
    if data_cap_bytes and used_bytes >= data_cap_bytes:
        conn.rollback()
        conn.close()
        return {
            "active": False,
            "reason": "data_limit_reached",
            "dataLimitMb": row["data_limit_mb"],
            "usedBytes": used_bytes,
            "message": "This voucher has used its data allowance.",
        }

    expires_at = row["expires_at"]
    if not row["activated_at"] and not expires_at:
        deadline = datetime.fromisoformat(row["activation_deadline"]) if row["activation_deadline"] else None
        if deadline and deadline <= now:
            conn.execute("UPDATE clients SET status = 'expired' WHERE id = ?", (row["id"],))
            conn.commit()
            conn.close()
            return {"active": False, "reason": "activation_expired", "message": "This unused voucher has expired."}
        duration_seconds = int(row["duration_seconds"] or round(PLAN_DURATIONS_DAYS.get(row["plan"], 1) * 86400))
        expires_at = (now + timedelta(seconds=duration_seconds)).isoformat()
        conn.execute(
            "UPDATE clients SET activated_at = ?, expires_at = ?, duration_seconds = ? WHERE id = ?",
            (now_iso, expires_at, duration_seconds, row["id"]),
        )
        log_activity("voucher_activated", f"voucher={voucher_code}", conn=conn)
    elif expires_at and datetime.fromisoformat(expires_at) <= now:
        conn.execute("UPDATE clients SET status = 'expired' WHERE id = ?", (row["id"],))
        expired_sessions = conn.execute(
            "SELECT device_mac FROM active_sessions WHERE voucher_code = ? AND status = 'active'",
            (voucher_code,),
        ).fetchall()
        conn.execute(
            "UPDATE active_sessions SET status = 'expired', disconnected_at = ? "
            "WHERE voucher_code = ? AND status = 'active'",
            (now_iso, voucher_code),
        )
        conn.commit()
        conn.close()
        for session_row in expired_sessions:
            notify_router_disconnect(session_row["device_mac"], voucher_code)
        return {"active": False, "reason": "expired", "message": "Voucher expired."}

    device_limit = max(1, min(10, int(row["max_devices"] or MAX_DEVICES_PER_VOUCHER)))
    token_row = None
    if device_token:
        token_row = conn.execute(
            "SELECT device_slot_id FROM voucher_device_tokens WHERE token_hash = ? AND voucher_code = ? "
            "AND revoked_at IS NULL AND expires_at > ?",
            (_device_token_hash(device_token), voucher_code, now_iso),
        ).fetchone()
    if device_mac:
        bound = conn.execute(
            "SELECT device_slot_id FROM voucher_devices WHERE voucher_code = ? AND device_mac = ?",
            (voucher_code, device_mac),
        ).fetchone()
        device_slot_id = bound["device_slot_id"] if bound else token_row["device_slot_id"] if token_row else None
        if not bound:
            count = conn.execute(
                "SELECT COUNT(DISTINCT COALESCE(NULLIF(device_slot_id, ''), device_mac)) AS total "
                "FROM voucher_devices WHERE voucher_code = ?",
                (voucher_code,),
            ).fetchone()["total"]
            if device_slot_id is None and count >= device_limit:
                conn.rollback()
                conn.close()
                log_activity("voucher_device_limit_reached", f"voucher={voucher_code}")
                return {
                    "active": False,
                    "reason": "device_limit_reached",
                    "deviceLimit": device_limit,
                    "message": f"This voucher is already in use on {device_limit} device(s).",
                }
            if device_slot_id is None:
                device_slot_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO voucher_devices (id, voucher_code, device_mac, created_at, device_slot_id) VALUES (?, ?, ?, ?, ?)",
                (secrets.token_hex(8), voucher_code, device_mac, now_iso, device_slot_id),
            )
        elif not device_slot_id:
            device_slot_id = device_mac
            conn.execute(
                "UPDATE voucher_devices SET device_slot_id = ? WHERE voucher_code = ? AND device_mac = ?",
                (device_slot_id, voucher_code, device_mac),
            )
        conn.execute(
            "INSERT INTO active_sessions (id, voucher_code, device_mac, device_type, connected_at, last_seen_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active') ON CONFLICT(voucher_code, device_mac) DO UPDATE SET "
            "device_type = excluded.device_type, last_seen_at = excluded.last_seen_at, status = 'active', disconnected_at = NULL",
            (secrets.token_hex(12), voucher_code, device_mac, device_type, now_iso, now_iso),
        )
    raw_device_token = None
    if issue_device_token and device_mac:
        if token_row:
            raw_device_token = device_token
        else:
            binding = conn.execute(
                "SELECT device_slot_id FROM voucher_devices WHERE voucher_code = ? AND device_mac = ?",
                (voucher_code, device_mac),
            ).fetchone()
            raw_device_token = secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO voucher_device_tokens (token_hash, voucher_code, device_slot_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (_device_token_hash(raw_device_token), voucher_code, binding["device_slot_id"], now_iso,
                 (now + timedelta(seconds=DEVICE_TOKEN_TTL_SECONDS)).isoformat()),
            )
    conn.commit()
    remaining_seconds = max(0, int((datetime.fromisoformat(expires_at) - now).total_seconds()))
    conn.close()
    return {
        "active": remaining_seconds > 0,
        "reason": None if remaining_seconds > 0 else "expired",
        "plan": row["plan"],
        "activatedAt": row["activated_at"] or now_iso,
        "expiresAt": expires_at,
        "remainingSeconds": remaining_seconds,
        "deviceLimit": device_limit,
        "deviceMac": device_mac or None,
        "deviceType": device_type or None,
        "sessionTracked": bool(device_mac),
        "dataLimitMb": row["data_limit_mb"],
        "speedLimitMbps": row["speed_limit_mbps"],
        "privateMac": is_private_mac(device_mac),
        "_deviceToken": raw_device_token,
    }


@app.post("/api/access/login")
@limiter.limit("20 per minute")
def access_login():
    payload = request.get_json(silent=True) or {}
    voucher = str(payload.get("voucher", "")).strip().upper()
    device_mac = normalize_device_mac(payload.get("mac", ""))
    mac_ticket = str(payload.get("macTicket", ""))
    device_type = str(payload.get("deviceType", "")).strip()[:40]
    if not voucher:
        return jsonify({"active": False, "reason": "missing_voucher", "message": "Enter a voucher code."}), 400
    if not device_mac:
        return jsonify({
            "active": False,
            "reason": "invalid_mac",
            "message": "This Wi-Fi portal did not provide your device address. Reopen it from the hotspot sign-in page.",
        }), 400
    if ROUTER_MAC_SIGNING_SECRET and not verify_mac_ticket(device_mac, mac_ticket):
        return jsonify({"active": False, "reason": "invalid_mac_ticket", "message": "Reopen the portal from the Wi-Fi sign-in page."}), 401
    retry_after = voucher_login_retry_after(voucher)
    if retry_after:
        response = jsonify({
            "active": False,
            "reason": "temporarily_locked",
            "message": "Too many incorrect voucher attempts. Please wait before trying again.",
            "retryAfterSeconds": retry_after,
        })
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response
    result = authorize_voucher(
        voucher,
        device_mac,
        device_type,
        device_token=request.cookies.get("wifi_device_handoff", ""),
        issue_device_token=True,
    )
    if result.get("reason") in {"not_found", "not_paid"}:
        record_voucher_login_failure(voucher)
        result = {
            "active": False,
            "reason": "invalid_voucher",
            "message": "Voucher code is invalid. Check the code and try again.",
        }
    device_token = result.pop("_deviceToken", None)
    response = jsonify(result)
    if device_token:
        response.set_cookie(
            "wifi_device_handoff",
            device_token,
            max_age=DEVICE_TOKEN_TTL_SECONDS,
            httponly=True,
            secure=IS_PRODUCTION,
            samesite="Lax",
            path="/api/access",
        )
    return response


@app.post("/api/access/mac-ticket")
@limiter.limit("120 per minute")
def issue_mac_ticket():
    if not ROUTER_API_KEY or not secrets.compare_digest(request.headers.get("X-Router-Key", ""), ROUTER_API_KEY):
        return jsonify({"error": "Unauthorized router client."}), 401
    payload = request.get_json(silent=True) or {}
    device_mac = normalize_device_mac(payload.get("mac", ""))
    if not device_mac:
        return jsonify({"error": "A valid device MAC address is required."}), 400
    if len(ROUTER_MAC_SIGNING_SECRET) < 32:
        return jsonify({"error": "MAC ticket signing is not configured."}), 503
    return jsonify({"mac": device_mac, "macTicket": create_mac_ticket(device_mac), "expiresInSeconds": 120})


@app.post("/api/access/usage")
@limiter.limit("120 per minute")
def access_usage():
    """Record an idempotent byte delta from a trusted router/RADIUS adapter."""
    if ROUTER_API_KEY and not secrets.compare_digest(request.headers.get("X-Router-Key", ""), ROUTER_API_KEY):
        return jsonify({"error": "Unauthorized router client."}), 401
    payload = request.get_json(silent=True) or {}
    voucher_code = str(payload.get("voucher", "")).strip().upper()
    device_mac = normalize_device_mac(payload.get("mac", ""))
    event_id = str(payload.get("eventId", "")).strip()[:80]
    try:
        bytes_delta = int(payload.get("bytesDelta"))
    except (TypeError, ValueError):
        return jsonify({"error": "bytesDelta must be a whole number."}), 400
    if not voucher_code or not device_mac or not event_id:
        return jsonify({"error": "A voucher, valid MAC, and eventId are required."}), 400
    if not 1 <= bytes_delta <= 1024 * 1024 * 1024:
        return jsonify({"error": "bytesDelta must be between 1 and 1073741824."}), 400

    now = utc_now()
    conn = get_db_connection()
    conn.execute("BEGIN IMMEDIATE")
    voucher = conn.execute(
        "SELECT status, expires_at, disabled_at, paused_at, data_limit_mb, speed_limit_mbps "
        "FROM clients WHERE voucher_code = ? ORDER BY created_at DESC LIMIT 1",
        (voucher_code,),
    ).fetchone()
    if voucher is None:
        conn.rollback()
        conn.close()
        return jsonify({"active": False, "reason": "not_found"}), 404
    duplicate = conn.execute("SELECT 1 FROM session_usage_events WHERE event_id = ?", (event_id,)).fetchone()
    if duplicate:
        used_bytes = conn.execute(
            "SELECT COALESCE(SUM(bytes_used), 0) AS total FROM active_sessions WHERE voucher_code = ?",
            (voucher_code,),
        ).fetchone()["total"]
        conn.rollback()
        conn.close()
        cap_bytes = int(voucher["data_limit_mb"] or 0) * 1024 * 1024
        return jsonify({"active": not cap_bytes or used_bytes < cap_bytes, "duplicate": True,
                        "usedBytes": used_bytes, "dataLimitMb": voucher["data_limit_mb"]})
    if voucher["status"] != "paid" or voucher["disabled_at"] or voucher["paused_at"]:
        conn.rollback()
        conn.close()
        return jsonify({"active": False, "reason": "voucher_unavailable"}), 409
    if voucher["expires_at"] and datetime.fromisoformat(voucher["expires_at"]) <= now:
        conn.rollback()
        conn.close()
        return jsonify({"active": False, "reason": "expired"}), 409

    device = conn.execute(
        "SELECT id FROM active_sessions WHERE voucher_code = ? AND device_mac = ? AND status = 'active'",
        (voucher_code, device_mac),
    ).fetchone()
    if device is None:
        conn.rollback()
        conn.close()
        return jsonify({"active": False, "reason": "session_not_active"}), 409

    conn.execute(
        "INSERT INTO session_usage_events (event_id, voucher_code, device_mac, bytes_delta, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (event_id, voucher_code, device_mac, bytes_delta, now.isoformat()),
    )
    conn.execute(
        "UPDATE active_sessions SET bytes_used = bytes_used + ?, last_seen_at = ? WHERE id = ?",
        (bytes_delta, now.isoformat(), device["id"]),
    )
    usage = conn.execute(
        "SELECT COALESCE(SUM(bytes_used), 0) AS total FROM active_sessions WHERE voucher_code = ?",
        (voucher_code,),
    ).fetchone()["total"]
    cap_bytes = int(voucher["data_limit_mb"] or 0) * 1024 * 1024
    capped_sessions = []
    if cap_bytes and usage >= cap_bytes:
        capped_sessions = conn.execute(
            "SELECT device_mac FROM active_sessions WHERE voucher_code = ? AND status = 'active'",
            (voucher_code,),
        ).fetchall()
        conn.execute(
            "UPDATE active_sessions SET status = 'data_capped', disconnected_at = ? "
            "WHERE voucher_code = ? AND status = 'active'",
            (now.isoformat(), voucher_code),
        )
        log_activity("voucher_data_limit_reached", f"voucher={voucher_code} usedBytes={usage}", conn=conn)
    conn.commit()
    conn.close()

    if capped_sessions:
        for session_row in capped_sessions:
            notify_router_disconnect(session_row["device_mac"], voucher_code)
    remaining_bytes = max(0, cap_bytes - usage) if cap_bytes else None
    return jsonify({
        "active": not cap_bytes or usage < cap_bytes,
        "reason": "data_limit_reached" if cap_bytes and usage >= cap_bytes else None,
        "usedBytes": usage,
        "usedMb": round(usage / (1024 * 1024), 3),
        "dataLimitMb": voucher["data_limit_mb"],
        "remainingBytes": remaining_bytes,
        "speedLimitMbps": voucher["speed_limit_mbps"],
    })


@app.get("/api/access/reconnect")
@limiter.limit("60 per minute")
def access_reconnect():
    if ROUTER_API_KEY and not secrets.compare_digest(request.headers.get("X-Router-Key", ""), ROUTER_API_KEY):
        return jsonify({"error": "Unauthorized router client."}), 401
    device_mac = normalize_device_mac(request.args.get("mac", ""))
    if not device_mac:
        return jsonify({"active": False, "reason": "missing_mac", "message": "A device MAC address is required."}), 400
    conn = get_db_connection()
    sessions = conn.execute(
        "SELECT voucher_code FROM active_sessions WHERE device_mac = ? ORDER BY last_seen_at DESC",
        (device_mac,),
    ).fetchall()
    conn.close()
    for session_row in sessions:
        result = authorize_voucher(session_row["voucher_code"], device_mac)
        if result["active"]:
            result["reconnected"] = True
            return jsonify(result)
    return jsonify({"active": False, "reason": "no_active_voucher", "message": "No active voucher was found for this device."})


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
            "SELECT customer_name, plan, amount, status, voucher_code, expires_at, speed_tier, auto_renew, phone, referral_code, "
            "duration_seconds, activated_at, activation_deadline, disabled_at, paused_at, paused_seconds, max_devices, data_limit_mb "
            "FROM clients WHERE voucher_code = ? ORDER BY created_at DESC LIMIT 1",
            (voucher,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT customer_name, plan, amount, status, voucher_code, expires_at, speed_tier, auto_renew, phone, referral_code, "
            "duration_seconds, activated_at, activation_deadline, disabled_at, paused_at, paused_seconds, max_devices, data_limit_mb "
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
    data_usage = conn.execute(
        "SELECT COALESCE(SUM(bytes_used), 0) AS total FROM active_sessions WHERE voucher_code = ?",
        (row["voucher_code"],),
    ).fetchone()["total"]
    conn.close()

    item = dict(row)
    expires_at = item.get("expires_at")
    now = utc_now()
    remaining_seconds = 0
    pending_activation = False
    if item.get("paused_at"):
        remaining_seconds = max(0, int(item.get("paused_seconds") or 0))
    elif expires_at:
        remaining_seconds = max(0, int((datetime.fromisoformat(expires_at) - now).total_seconds()))
    elif item["status"] == "paid" and not item.get("disabled_at"):
        activation_deadline = item.get("activation_deadline")
        if not activation_deadline or datetime.fromisoformat(activation_deadline) > now:
            remaining_seconds = int(item.get("duration_seconds") or round(PLAN_DURATIONS_DAYS.get(item["plan"], 1) * 86400))
            pending_activation = True
    item["remainingSeconds"] = remaining_seconds
    item["pendingActivation"] = pending_activation
    item["isActive"] = item["status"] == "paid" and not item.get("disabled_at") and not item.get("paused_at") and remaining_seconds > 0
    item["deviceLimit"] = item.get("max_devices") or MAX_DEVICES_PER_VOUCHER
    item["dataLimitMb"] = item.pop("data_limit_mb", None)
    item["dataUsedBytes"] = data_usage
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
        "speedTierDeviceLimit", "maxDevicesPerVoucher", "loyaltyPurchasesInterval",
        "loyaltyDiscountPercent", "dailyLoyaltyBuyCount", "planSpeedLimitMbps", "planDataLimitMb",
    }
    updates = {key: value for key, value in payload.items() if key in allowed_keys}
    for limit_key in ("planSpeedLimitMbps", "planDataLimitMb"):
        limits = updates.get(limit_key)
        if limit_key in updates and (
            not isinstance(limits, dict)
            or any(plan not in ALLOWED_PLANS or (v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 1))
                   for plan, v in limits.items())
        ):
            return jsonify({"error": f"{limit_key} must map plan names to positive whole numbers or null."}), 400
    if not updates:
        return jsonify({"error": "No recognized pricing fields were provided."}), 400
    if "maxDevicesPerVoucher" in updates:
        try:
            updates["maxDevicesPerVoucher"] = int(updates["maxDevicesPerVoucher"])
        except (TypeError, ValueError):
            return jsonify({"error": "maxDevicesPerVoucher must be a whole number."}), 400
        if not 1 <= updates["maxDevicesPerVoucher"] <= 10:
            return jsonify({"error": "maxDevicesPerVoucher must be 1-10."}), 400

    current = get_pricing_config()
    for key, value in updates.items():
        if isinstance(current.get(key), dict) and isinstance(value, dict):
            current[key] = {**current[key], **value}
        else:
            current[key] = value
    save_pricing_overrides(current)
    log_activity("pricing_updated", f"keys={list(updates.keys())}")
    return jsonify(current)


@app.get("/api/admin/vouchers")
@admin_required
def admin_vouchers():
    query = str(request.args.get("q", "")).strip()[:80]
    status = str(request.args.get("status", "")).strip().lower()
    limit = max(1, min(500, request.args.get("limit", 100, type=int)))
    offset = max(0, request.args.get("offset", 0, type=int))
    conditions = ["c.voucher_code != ''"]
    params = []
    if query:
        conditions.append("(c.voucher_code LIKE ? OR c.phone LIKE ? OR c.customer_name LIKE ?)")
        pattern = f"%{query}%"
        params.extend((pattern, pattern, pattern))
    if status in {"paid", "pending", "expired", "disabled", "paused"}:
        if status == "disabled":
            conditions.append("c.disabled_at IS NOT NULL")
        elif status == "paused":
            conditions.append("c.paused_at IS NOT NULL")
        else:
            conditions.append("c.status = ?")
            params.append(status)
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT c.id, c.customer_name, c.phone, c.plan, c.status, c.voucher_code, c.created_at, "
        "c.expires_at, c.activated_at, c.activation_deadline, c.duration_seconds, c.max_devices, "
        "c.disabled_at, c.paused_at, c.paused_seconds, c.data_limit_mb, c.speed_limit_mbps, "
        "(SELECT COUNT(*) FROM voucher_devices d WHERE d.voucher_code = c.voucher_code) AS bound_devices, "
        "(SELECT COUNT(*) FROM active_sessions s WHERE s.voucher_code = c.voucher_code AND s.status = 'active') AS active_devices, "
        "(SELECT COALESCE(SUM(s.bytes_used), 0) FROM active_sessions s WHERE s.voucher_code = c.voucher_code) AS data_used_bytes "
        f"FROM clients c WHERE {' AND '.join(conditions)} ORDER BY c.created_at DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    conn.close()
    return jsonify({"vouchers": [dict(row) for row in rows], "limit": limit, "offset": offset})


@app.post("/api/admin/vouchers/bulk")
@admin_required
def admin_create_vouchers():
    payload = request.get_json(silent=True) or {}
    try:
        quantity = int(payload.get("quantity", 1))
        max_devices = int(payload.get("maxDevices", get_pricing_config()["maxDevicesPerVoucher"]))
        duration_seconds = int(payload.get("durationSeconds", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Quantity, duration, and device limit must be whole numbers."}), 400
    plan = str(payload.get("plan", "fullDay"))
    if not 1 <= quantity <= 500 or not 1 <= max_devices <= 10:
        return jsonify({"error": "Quantity must be 1-500 and maxDevices must be 1-10."}), 400
    if plan not in ALLOWED_PLANS:
        return jsonify({"error": "Unknown voucher plan."}), 400
    config = get_pricing_config()
    if duration_seconds <= 0:
        days = config["planDurationsDays"].get(plan, 1) + config["bundleBonusDays"].get(plan, 0)
        duration_seconds = round(days * 86400)
    if not 60 <= duration_seconds <= 365 * 86400:
        return jsonify({"error": "Voucher duration must be between one minute and one year."}), 400
    prefix = str(payload.get("prefix", "")).strip().upper()
    if len(prefix) > 4 or (prefix and not prefix.isalnum()):
        return jsonify({"error": "Prefix must be up to four letters or digits."}), 400
    data_limit_mb = payload.get("dataLimitMb")
    speed_limit_mbps = payload.get("speedLimitMbps")
    try:
        data_limit_mb = int(data_limit_mb) if data_limit_mb not in (None, "") else None
        speed_limit_mbps = int(speed_limit_mbps) if speed_limit_mbps not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "Data and speed limits must be whole numbers."}), 400
    if data_limit_mb is not None and data_limit_mb < 1 or speed_limit_mbps is not None and speed_limit_mbps < 1:
        return jsonify({"error": "Data and speed limits must be positive."}), 400

    now = utc_now()
    activation_deadline = (now + timedelta(days=VOUCHER_ACTIVATION_TTL_DAYS)).isoformat()
    conn = get_db_connection()
    conn.execute("BEGIN IMMEDIATE")
    created = []
    for _ in range(quantity):
        while True:
            code = generate_voucher_code(prefix)
            if not conn.execute("SELECT 1 FROM clients WHERE voucher_code = ?", (code,)).fetchone():
                break
        client_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO clients (id, customer_name, phone, plan, payment_method, device_count, date, amount, "
            "discount, status, addons, router_id, created_at, voucher_code, duration_seconds, activation_deadline, "
            "max_devices, data_limit_mb, speed_limit_mbps) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (client_id, str(payload.get("label", "Voucher batch"))[:120], "", plan, "admin", max_devices,
             now.strftime("%Y-%m-%d"), 0, 0, "paid", "[]", "", now.isoformat(), code,
             duration_seconds, activation_deadline, max_devices, data_limit_mb, speed_limit_mbps),
        )
        created.append({"code": code, "plan": plan, "durationSeconds": duration_seconds,
                        "activationDeadline": activation_deadline, "maxDevices": max_devices})
    log_activity("vouchers_bulk_created", f"quantity={quantity} plan={plan}", conn=conn)
    conn.commit()
    conn.close()
    return jsonify({"created": created, "count": len(created)}), 201


@app.patch("/api/admin/vouchers/<voucher_code>")
@admin_required
def admin_update_voucher(voucher_code):
    payload = request.get_json(silent=True) or {}
    conn = get_db_connection()
    row = conn.execute("SELECT id, disabled_at FROM clients WHERE voucher_code = ?", (voucher_code.upper(),)).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "Voucher not found."}), 404
    updates = []
    values = []
    if "disabled" in payload:
        updates.append("disabled_at = ?")
        values.append(utc_now().isoformat() if payload["disabled"] else None)
    if "maxDevices" in payload:
        try:
            max_devices = int(payload["maxDevices"])
        except (TypeError, ValueError):
            conn.close()
            return jsonify({"error": "maxDevices must be a whole number."}), 400
        if not 1 <= max_devices <= 10:
            conn.close()
            return jsonify({"error": "maxDevices must be 1-10."}), 400
        updates.append("max_devices = ?")
        values.append(max_devices)
    if not updates:
        conn.close()
        return jsonify({"error": "No supported voucher changes were provided."}), 400
    conn.execute(f"UPDATE clients SET {', '.join(updates)} WHERE id = ?", (*values, row["id"]))
    disconnected = []
    if payload.get("disabled"):
        conn.execute(
            "UPDATE voucher_device_tokens SET revoked_at = ? WHERE voucher_code = ? AND revoked_at IS NULL",
            (utc_now().isoformat(), voucher_code.upper()),
        )
        disconnected = conn.execute(
            "SELECT id, device_mac, voucher_code FROM active_sessions WHERE voucher_code = ? AND status = 'active'",
            (voucher_code.upper(),),
        ).fetchall()
        conn.execute(
            "UPDATE active_sessions SET status = 'disconnected', disconnected_at = ? "
            "WHERE voucher_code = ? AND status = 'active'",
            (utc_now().isoformat(), voucher_code.upper()),
        )
    log_activity("voucher_updated", f"voucher={voucher_code.upper()} fields={list(payload)}", conn=conn)
    conn.commit()
    conn.close()
    for session_row in disconnected:
        notify_router_disconnect(session_row["device_mac"], session_row["voucher_code"])
    return jsonify({"updated": True, "voucherCode": voucher_code.upper()})


@app.delete("/api/admin/vouchers/<voucher_code>")
@admin_required
def admin_delete_voucher(voucher_code):
    voucher_code = voucher_code.strip().upper()
    conn = get_db_connection()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT id FROM clients WHERE voucher_code = ?", (voucher_code,)).fetchone()
    if row is None:
        conn.rollback()
        conn.close()
        return jsonify({"error": "Voucher not found."}), 404
    conn.execute("DELETE FROM active_sessions WHERE voucher_code = ?", (voucher_code,))
    conn.execute("DELETE FROM voucher_devices WHERE voucher_code = ?", (voucher_code,))
    conn.execute("DELETE FROM voucher_device_tokens WHERE voucher_code = ?", (voucher_code,))
    conn.execute("DELETE FROM clients WHERE id = ?", (row["id"],))
    log_activity("voucher_deleted", f"voucher={voucher_code}", conn=conn)
    conn.commit()
    conn.close()
    return jsonify({"deleted": True, "voucherCode": voucher_code})


@app.post("/api/admin/clients/<client_id>/voucher")
@admin_required
def admin_issue_client_voucher(client_id):
    conn = get_db_connection()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if row is None:
        conn.rollback()
        conn.close()
        return jsonify({"error": "Client not found."}), 404
    if row["status"] != "paid":
        conn.rollback()
        conn.close()
        return jsonify({"error": "Mark the client as paid before issuing a voucher."}), 409
    if row["voucher_code"]:
        conn.rollback()
        conn.close()
        return jsonify({"voucherCode": row["voucher_code"], "existing": True})
    code = generate_voucher_code()
    config = get_pricing_config()
    duration_seconds = round((config["planDurationsDays"].get(row["plan"], 1) + config["bundleBonusDays"].get(row["plan"], 0)) * 86400)
    deadline = (utc_now() + timedelta(days=VOUCHER_ACTIVATION_TTL_DAYS)).isoformat()
    conn.execute(
        "UPDATE clients SET voucher_code = ?, expires_at = NULL, duration_seconds = ?, activated_at = NULL, "
        "activation_deadline = ?, max_devices = ?, data_limit_mb = ?, speed_limit_mbps = ? WHERE id = ?",
        (code, duration_seconds, deadline, config["maxDevicesPerVoucher"],
         config["planDataLimitMb"].get(row["plan"]), config["planSpeedLimitMbps"].get(row["plan"]), client_id),
    )
    log_activity("voucher_created", f"client={client_id} voucher={code}", conn=conn)
    conn.commit()
    conn.close()
    return jsonify({"voucherCode": code, "durationSeconds": duration_seconds, "activationDeadline": deadline}), 201


@app.post("/api/admin/vouchers/<voucher_code>/pause")
@admin_required
def admin_pause_voucher(voucher_code):
    now = utc_now()
    conn = get_db_connection()
    row = conn.execute(
        "SELECT id, expires_at, duration_seconds, activated_at, paused_at FROM clients WHERE voucher_code = ?",
        (voucher_code.upper(),),
    ).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "Voucher not found."}), 404
    if row["paused_at"]:
        conn.close()
        return jsonify({"paused": True, "message": "Voucher is already paused."})
    if row["activated_at"] and row["expires_at"]:
        remaining = max(0, int((datetime.fromisoformat(row["expires_at"]) - now).total_seconds()))
    else:
        remaining = int(row["duration_seconds"] or 0)
    conn.execute("UPDATE clients SET paused_at = ?, paused_seconds = ? WHERE id = ?", (now.isoformat(), remaining, row["id"]))
    sessions = conn.execute(
        "SELECT device_mac, voucher_code FROM active_sessions WHERE voucher_code = ? AND status = 'active'",
        (voucher_code.upper(),),
    ).fetchall()
    conn.execute(
        "UPDATE active_sessions SET status = 'paused', disconnected_at = ? WHERE voucher_code = ? AND status = 'active'",
        (now.isoformat(), voucher_code.upper()),
    )
    log_activity("voucher_paused", f"voucher={voucher_code.upper()}", conn=conn)
    conn.commit()
    conn.close()
    for session_row in sessions:
        notify_router_disconnect(session_row["device_mac"], session_row["voucher_code"])
    return jsonify({"paused": True, "remainingSeconds": remaining})


@app.post("/api/admin/vouchers/<voucher_code>/resume")
@admin_required
def admin_resume_voucher(voucher_code):
    now = utc_now()
    conn = get_db_connection()
    row = conn.execute(
        "SELECT id, activated_at, paused_at, paused_seconds FROM clients WHERE voucher_code = ?",
        (voucher_code.upper(),),
    ).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "Voucher not found."}), 404
    if not row["paused_at"]:
        conn.close()
        return jsonify({"paused": False, "message": "Voucher is not paused."})
    expires_at = (now + timedelta(seconds=row["paused_seconds"] or 0)).isoformat() if row["activated_at"] else None
    conn.execute(
        "UPDATE clients SET paused_at = NULL, paused_seconds = 0, expires_at = COALESCE(?, expires_at) WHERE id = ?",
        (expires_at, row["id"]),
    )
    log_activity("voucher_resumed", f"voucher={voucher_code.upper()}", conn=conn)
    conn.commit()
    conn.close()
    return jsonify({"paused": False, "expiresAt": expires_at})


@app.get("/api/admin/sessions")
@admin_required
def admin_sessions():
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT s.id, s.voucher_code, s.device_mac, s.device_type, s.connected_at, s.last_seen_at, s.bytes_used, "
        "s.status, c.plan, c.expires_at, c.paused_at FROM active_sessions s "
        "JOIN clients c ON c.voucher_code = s.voucher_code WHERE s.status IN ('active', 'paused', 'data_capped') "
        "ORDER BY s.last_seen_at DESC LIMIT 500"
    ).fetchall()
    conn.close()
    now = utc_now()
    sessions = []
    for row in rows:
        item = dict(row)
        item["remainingSeconds"] = max(0, int((datetime.fromisoformat(row["expires_at"]) - now).total_seconds())) if row["expires_at"] else None
        sessions.append(item)
    return jsonify({"sessions": sessions})


@app.post("/api/admin/sessions/<session_id>/disconnect")
@admin_required
def admin_disconnect_session(session_id):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT voucher_code, device_mac FROM active_sessions WHERE id = ? AND status = 'active'",
        (session_id,),
    ).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "Active session not found."}), 404
    conn.execute(
        "UPDATE active_sessions SET status = 'disconnected', disconnected_at = ? WHERE id = ?",
        (utc_now().isoformat(), session_id),
    )
    log_activity("session_disconnected", f"voucher={row['voucher_code']} mac={row['device_mac']}", conn=conn)
    conn.commit()
    conn.close()
    notify_router_disconnect(row["device_mac"], row["voucher_code"])
    return jsonify({"disconnected": True, "sessionId": session_id})


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


def reap_expired_vouchers(now=None):
    """Expire activated or never-used vouchers and notify the router for active MAC sessions."""
    now = now or utc_now()
    now_iso = now.isoformat()
    conn = get_db_connection()
    conn.execute("BEGIN IMMEDIATE")
    attempts_cutoff = (now - timedelta(days=1)).isoformat()
    conn.execute("DELETE FROM voucher_login_failures WHERE created_at < ?", (attempts_cutoff,))
    expired = conn.execute(
        "SELECT voucher_code FROM clients WHERE status = 'paid' AND disabled_at IS NULL AND paused_at IS NULL "
        "AND voucher_code != '' AND ((expires_at IS NOT NULL AND expires_at <= ?) OR "
        "(activated_at IS NULL AND activation_deadline IS NOT NULL AND activation_deadline <= ?))",
        (now_iso, now_iso),
    ).fetchall()
    disconnects = []
    for row in expired:
        voucher_code = row["voucher_code"]
        conn.execute(
            "UPDATE clients SET status = 'expired' WHERE voucher_code = ? AND status = 'paid'",
            (voucher_code,),
        )
        disconnects.extend(conn.execute(
            "SELECT device_mac, voucher_code FROM active_sessions WHERE voucher_code = ? AND status = 'active'",
            (voucher_code,),
        ).fetchall())
        conn.execute(
            "UPDATE active_sessions SET status = 'expired', disconnected_at = ? "
            "WHERE voucher_code = ? AND status = 'active'",
            (now_iso, voucher_code),
        )
        log_activity("voucher_expired", f"voucher={voucher_code}", conn=conn)
    conn.commit()
    conn.close()
    for session_row in disconnects:
        notify_router_disconnect(session_row["device_mac"], session_row["voucher_code"])
    return {"expiredVouchers": len(expired), "disconnectedDevices": len(disconnects)}



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


# ---------------------------------------------------------------------------
# SMS receipts (Africa's Talking)
# ---------------------------------------------------------------------------
AT_USERNAME = os.environ.get("AT_USERNAME", "")
AT_API_KEY = os.environ.get("AT_API_KEY", "")
AT_SENDER_ID = os.environ.get("AT_SENDER_ID", "")
AT_SMS_URL = os.environ.get("AT_SMS_URL", "https://api.africastalking.com/version1/messaging")


def _e164(phone):
    digits = re.sub(r"\D", "", str(phone))
    if digits.startswith("0") and len(digits) == 10:
        digits = "256" + digits[1:]
    return "+" + digits if digits else ""


def send_sms(phone, message):
    if not (AT_USERNAME and AT_API_KEY):
        return False
    to = _e164(phone)
    if not to:
        return False
    data = {"username": AT_USERNAME, "to": to, "message": message}
    if AT_SENDER_ID:
        data["from"] = AT_SENDER_ID
    try:
        response = requests.post(
            AT_SMS_URL, data=data, timeout=10,
            headers={"apiKey": AT_API_KEY, "Accept": "application/json"},
        )
        response.raise_for_status()
        return True
    except requests.RequestException:
        logger.warning("SMS send failed for %s", mask_phone(phone))
        return False


def send_receipt_async(phone, amount, plan, voucher_code):
    if not (AT_USERNAME and AT_API_KEY):
        return
    message = f"WiFi payment received: UGX {int(amount):,} ({plan}). Your voucher code: {voucher_code}. Thank you."
    threading.Thread(target=send_sms, args=(phone, message), daemon=True).start()


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------
MIN_PRICE_AFTER_COUPON = 100


def _coupon_discount(coupon, base_price):
    if coupon["percent_off"]:
        discount = base_price * coupon["percent_off"] // 100
    else:
        discount = int(coupon["amount_off"] or 0)
    return max(0, min(discount, base_price - MIN_PRICE_AFTER_COUPON))


def _find_valid_coupon(conn, code):
    coupon = conn.execute("SELECT * FROM coupons WHERE code = ?", (code,)).fetchone()
    if coupon is None or not coupon["active"]:
        return None, "Coupon code is not valid."
    if coupon["expires_at"] and datetime.fromisoformat(coupon["expires_at"]) <= utc_now():
        return None, "This coupon has expired."
    if coupon["max_uses"] is not None and coupon["used_count"] >= coupon["max_uses"]:
        return None, "This coupon has been fully used."
    return coupon, None


def redeem_coupon(conn, code, base_price):
    """Atomically reserve one use of a coupon. Returns (discount, error); caller commits."""
    coupon, error = _find_valid_coupon(conn, code)
    if error:
        return 0, error
    reserved = conn.execute(
        "UPDATE coupons SET used_count = used_count + 1 WHERE code = ? AND active = 1 "
        "AND (max_uses IS NULL OR used_count < max_uses)",
        (code,),
    ).rowcount
    if not reserved:
        return 0, "This coupon has been fully used."
    return _coupon_discount(coupon, base_price), None


@app.post("/api/coupons/preview")
@limiter.limit("20 per minute")
def coupon_preview():
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("couponCode", "")).strip().upper()[:20]
    plan = payload.get("plan")
    speed_tier = str(payload.get("speedTier", "basic")).lower()
    config = get_pricing_config()
    if plan not in config["planPrices"]:
        return jsonify({"error": "Unknown plan."}), 400
    if speed_tier not in config["speedTierMultiplier"]:
        speed_tier = "basic"
    conn = get_db_connection()
    coupon, error = _find_valid_coupon(conn, code)
    conn.close()
    if error:
        return jsonify({"valid": False, "error": error}), 200
    base_price = round(config["planPrices"][plan] * config["speedTierMultiplier"][speed_tier])
    return jsonify({"valid": True, "discount": _coupon_discount(coupon, base_price)})


@app.get("/api/admin/coupons")
@admin_required
def admin_list_coupons():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM coupons ORDER BY created_at DESC").fetchall()
    conn.close()
    return jsonify({"coupons": [dict(row) for row in rows]})


@app.post("/api/admin/coupons")
@admin_required
def admin_create_coupon():
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code", "")).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,20}", code):
        return jsonify({"error": "Code must be 3-20 letters or digits."}), 400
    try:
        percent_off = int(payload["percentOff"]) if payload.get("percentOff") not in (None, "") else None
        amount_off = int(payload["amountOff"]) if payload.get("amountOff") not in (None, "") else None
        max_uses = int(payload["maxUses"]) if payload.get("maxUses") not in (None, "") else None
        expires_at = payload.get("expiresAt") or None
        if expires_at:
            parsed = datetime.fromisoformat(str(expires_at))
            expires_at = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat()
    except (TypeError, ValueError):
        return jsonify({"error": "Discount, max uses, or expiry is invalid."}), 400
    if (percent_off is None) == (amount_off is None):
        return jsonify({"error": "Provide exactly one of percentOff or amountOff."}), 400
    if percent_off is not None and not 1 <= percent_off <= 90:
        return jsonify({"error": "percentOff must be 1-90."}), 400
    if amount_off is not None and amount_off < 1:
        return jsonify({"error": "amountOff must be positive."}), 400
    if max_uses is not None and max_uses < 1:
        return jsonify({"error": "maxUses must be positive."}), 400
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO coupons (code, percent_off, amount_off, max_uses, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (code, percent_off, amount_off, max_uses, expires_at, utc_now().isoformat()),
        )
        log_activity("coupon_created", f"code={code}", conn=conn)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "Coupon code already exists."}), 409
    conn.close()
    return jsonify({"created": True, "code": code}), 201


@app.patch("/api/admin/coupons/<code>")
@admin_required
def admin_update_coupon(code):
    payload = request.get_json(silent=True) or {}
    if "active" not in payload:
        return jsonify({"error": "Provide active true/false."}), 400
    conn = get_db_connection()
    updated = conn.execute(
        "UPDATE coupons SET active = ? WHERE code = ?", (1 if payload["active"] else 0, code.strip().upper())
    ).rowcount
    log_activity("coupon_updated", f"code={code.strip().upper()} active={bool(payload['active'])}", conn=conn)
    conn.commit()
    conn.close()
    return jsonify({"updated": True}) if updated else (jsonify({"error": "Coupon not found."}), 404)


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------
@app.post("/api/admin/clients/<client_id>/refund")
@admin_required
def admin_refund_client(client_id):
    """Record a refund, revoke the voucher and disconnect its devices. The money itself
    must be returned through the mobile money provider; this only updates the books."""
    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason", "")).strip()[:200]
    now_iso = utc_now().isoformat()
    conn = get_db_connection()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT id, status, amount, voucher_code FROM clients WHERE id = ?", (client_id,)).fetchone()
    if row is None:
        conn.rollback()
        conn.close()
        return jsonify({"error": "Client not found."}), 404
    if row["status"] != "paid" or row["amount"] <= 0:
        conn.rollback()
        conn.close()
        return jsonify({"error": "Only paid, non-free purchases can be refunded."}), 409
    code = row["voucher_code"] or ""
    conn.execute("UPDATE clients SET status = 'refunded', disabled_at = ? WHERE id = ?", (now_iso, client_id))
    conn.execute("UPDATE payment_intents SET status = 'refunded' WHERE client_id = ?", (client_id,))
    conn.execute(
        "INSERT INTO refunds (id, client_id, voucher_code, amount, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (secrets.token_hex(8), client_id, code, row["amount"], reason, now_iso),
    )
    disconnected = []
    if code:
        conn.execute(
            "UPDATE voucher_device_tokens SET revoked_at = ? WHERE voucher_code = ? AND revoked_at IS NULL",
            (now_iso, code),
        )
        disconnected = conn.execute(
            "SELECT device_mac FROM active_sessions WHERE voucher_code = ? AND status = 'active'", (code,)
        ).fetchall()
        conn.execute(
            "UPDATE active_sessions SET status = 'disconnected', disconnected_at = ? "
            "WHERE voucher_code = ? AND status = 'active'",
            (now_iso, code),
        )
    log_activity("client_refunded", f"client={client_id} amount={row['amount']} voucher={code}", conn=conn)
    conn.commit()
    conn.close()
    for session_row in disconnected:
        notify_router_disconnect(session_row["device_mac"], code)
    return jsonify({"refunded": True, "amount": row["amount"], "voucherCode": code})


# ---------------------------------------------------------------------------
# Reports, CSV export, audit log
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_GROUP_SQL = {"day": "date", "week": "strftime('%Y-W%W', date)", "month": "substr(date, 1, 7)"}


def _date_range():
    today = utc_now().date()
    start = request.args.get("from") or (today - timedelta(days=29)).isoformat()
    end = request.args.get("to") or today.isoformat()
    if not _DATE_RE.fullmatch(start) or not _DATE_RE.fullmatch(end):
        return None, None
    return start, end


def _csv_cell(value):
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@app.get("/api/admin/reports/revenue")
@admin_required
def admin_revenue_report():
    start, end = _date_range()
    group = request.args.get("group", "day")
    if start is None or group not in _GROUP_SQL:
        return jsonify({"error": "Use from/to as YYYY-MM-DD and group of day, week or month."}), 400
    conn = get_db_connection()
    periods = conn.execute(
        f"SELECT {_GROUP_SQL[group]} AS period, COUNT(*) AS sales, COALESCE(SUM(amount), 0) AS revenue, "
        "COALESCE(SUM(discount), 0) AS discounts, COALESCE(SUM(commission), 0) AS commissions "
        "FROM clients WHERE status = 'paid' AND amount > 0 AND date BETWEEN ? AND ? GROUP BY period ORDER BY period",
        (start, end),
    ).fetchall()
    by_plan = conn.execute(
        "SELECT plan, COUNT(*) AS sales, COALESCE(SUM(amount), 0) AS revenue FROM clients "
        "WHERE status = 'paid' AND amount > 0 AND date BETWEEN ? AND ? GROUP BY plan ORDER BY revenue DESC",
        (start, end),
    ).fetchall()
    refunds = conn.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS total FROM refunds "
        "WHERE substr(created_at, 1, 10) BETWEEN ? AND ?",
        (start, end),
    ).fetchone()
    conn.close()
    return jsonify({
        "from": start, "to": end, "group": group,
        "periods": [dict(r) for r in periods],
        "byPlan": [dict(r) for r in by_plan],
        "totalRevenue": sum(r["revenue"] for r in periods),
        "refunds": {"count": refunds["count"], "total": refunds["total"]},
    })


@app.get("/api/admin/reports/export.csv")
@admin_required
def admin_export_csv():
    start, end = _date_range()
    if start is None:
        return jsonify({"error": "Use from/to as YYYY-MM-DD."}), 400
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT id, date, plan, speed_tier, phone, payment_method, amount, discount, coupon_code, agent_id, "
        "commission, status, voucher_code FROM clients "
        "WHERE status IN ('paid', 'refunded') AND amount > 0 AND date BETWEEN ? AND ? ORDER BY date, created_at LIMIT 50000",
        (start, end),
    ).fetchall()
    conn.close()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "date", "plan", "speed_tier", "phone", "payment_method", "amount", "discount",
                     "coupon", "agent_id", "commission", "status", "voucher"])
    for row in rows:
        values = list(row)
        values[4] = mask_phone(values[4])
        writer.writerow([_csv_cell(v) for v in values])
    log_activity("report_exported", f"from={start} to={end} rows={len(rows)}")
    return Response(
        buffer.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=payments_{start}_{end}.csv"},
    )


@app.get("/api/admin/audit-log")
@admin_required
def admin_audit_log():
    event = str(request.args.get("event", "")).strip()[:60]
    limit = max(1, min(500, request.args.get("limit", 100, type=int)))
    offset = max(0, request.args.get("offset", 0, type=int))
    conn = get_db_connection()
    if event:
        rows = conn.execute(
            "SELECT * FROM activity_log WHERE event_type = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (event, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
    conn.close()
    return jsonify({"entries": [dict(r) for r in rows], "limit": limit, "offset": offset})


# ---------------------------------------------------------------------------
# Reseller agents
# ---------------------------------------------------------------------------
def agent_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        agent = None
        if token:
            conn = get_db_connection()
            agent = conn.execute(
                "SELECT * FROM agents WHERE token_hash = ? AND active = 1", (_device_token_hash(token),)
            ).fetchone()
            conn.close()
        if agent is None:
            return jsonify({"error": "Invalid agent token."}), 401
        return view(agent, *args, **kwargs)

    return wrapped


def _parse_commission(value):
    try:
        percent = int(value)
    except (TypeError, ValueError):
        return None
    return percent if 0 <= percent <= 50 else None


@app.post("/api/admin/agents")
@admin_required
def admin_create_agent():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()[:80]
    percent = _parse_commission(payload.get("commissionPercent", 10))
    if not name or percent is None:
        return jsonify({"error": "Agent name and a commissionPercent of 0-50 are required."}), 400
    token = secrets.token_urlsafe(32)
    agent_id = f"agent-{secrets.token_hex(6)}"
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO agents (id, name, token_hash, commission_percent, created_at) VALUES (?, ?, ?, ?, ?)",
        (agent_id, name, _device_token_hash(token), percent, utc_now().isoformat()),
    )
    log_activity("agent_created", f"agent={agent_id} name={name}", conn=conn)
    conn.commit()
    conn.close()
    return jsonify({"id": agent_id, "name": name, "commissionPercent": percent, "token": token,
                    "note": "Store this token now; it cannot be shown again."}), 201


@app.get("/api/admin/agents")
@admin_required
def admin_list_agents():
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT a.id, a.name, a.commission_percent, a.active, a.created_at, "
        "COUNT(CASE WHEN c.status = 'paid' THEN 1 END) AS sales, "
        "COALESCE(SUM(CASE WHEN c.status = 'paid' THEN c.amount END), 0) AS revenue, "
        "COALESCE(SUM(CASE WHEN c.status = 'paid' THEN c.commission END), 0) AS commission_owed "
        "FROM agents a LEFT JOIN clients c ON c.agent_id = a.id GROUP BY a.id ORDER BY a.created_at DESC"
    ).fetchall()
    conn.close()
    return jsonify({"agents": [dict(r) for r in rows]})


@app.patch("/api/admin/agents/<agent_id>")
@admin_required
def admin_update_agent(agent_id):
    payload = request.get_json(silent=True) or {}
    updates, values = [], []
    if "active" in payload:
        updates.append("active = ?")
        values.append(1 if payload["active"] else 0)
    if "commissionPercent" in payload:
        percent = _parse_commission(payload["commissionPercent"])
        if percent is None:
            return jsonify({"error": "commissionPercent must be 0-50."}), 400
        updates.append("commission_percent = ?")
        values.append(percent)
    if not updates:
        return jsonify({"error": "No supported agent changes were provided."}), 400
    conn = get_db_connection()
    updated = conn.execute(f"UPDATE agents SET {', '.join(updates)} WHERE id = ?", (*values, agent_id)).rowcount
    log_activity("agent_updated", f"agent={agent_id} fields={list(payload)}", conn=conn)
    conn.commit()
    conn.close()
    return jsonify({"updated": True}) if updated else (jsonify({"error": "Agent not found."}), 404)


@app.post("/api/agent/vouchers")
@limiter.limit("30 per minute")
@agent_required
def agent_sell_vouchers(agent):
    """An agent sells prepaid vouchers at list price and earns the agreed commission."""
    payload = request.get_json(silent=True) or {}
    plan = payload.get("plan")
    speed_tier = str(payload.get("speedTier", "basic")).lower()
    config = get_pricing_config()
    try:
        quantity = int(payload.get("quantity", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "Quantity must be a whole number."}), 400
    if plan not in config["planPrices"] or not 1 <= quantity <= 20:
        return jsonify({"error": "Valid plan and a quantity of 1-20 are required."}), 400
    if speed_tier not in config["speedTierMultiplier"]:
        speed_tier = "basic"
    amount = round(config["planPrices"][plan] * config["speedTierMultiplier"][speed_tier])
    commission = amount * agent["commission_percent"] // 100
    days = config["planDurationsDays"].get(plan, 1) + config["bundleBonusDays"].get(plan, 0)
    duration_seconds = round(days * 86400)
    now = utc_now()
    deadline = (now + timedelta(days=VOUCHER_ACTIVATION_TTL_DAYS)).isoformat()
    conn = get_db_connection()
    conn.execute("BEGIN IMMEDIATE")
    codes = []
    for _ in range(quantity):
        while True:
            code = generate_voucher_code()
            if not conn.execute("SELECT 1 FROM clients WHERE voucher_code = ?", (code,)).fetchone():
                break
        conn.execute(
            "INSERT INTO clients (id, customer_name, phone, plan, payment_method, device_count, date, amount, "
            "discount, status, addons, router_id, speed_tier, created_at, voucher_code, duration_seconds, "
            "activation_deadline, max_devices, data_limit_mb, speed_limit_mbps, agent_id, commission) "
            "VALUES (?, ?, '', ?, 'agent', 1, ?, ?, 0, 'paid', '[]', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), f"Agent: {agent['name']}"[:120], plan, now.strftime("%Y-%m-%d"), amount,
             speed_tier, now.isoformat(), code, duration_seconds, deadline, config["maxDevicesPerVoucher"],
             config["planDataLimitMb"].get(plan), config["planSpeedLimitMbps"].get(plan), agent["id"], commission),
        )
        codes.append(code)
    log_activity("agent_sale", f"agent={agent['id']} plan={plan} quantity={quantity}", conn=conn)
    conn.commit()
    conn.close()
    return jsonify({"vouchers": codes, "unitPrice": amount, "total": amount * quantity,
                    "commission": commission * quantity}), 201


@app.get("/api/agent/sales")
@limiter.limit("60 per minute")
@agent_required
def agent_sales(agent):
    conn = get_db_connection()
    totals = conn.execute(
        "SELECT COUNT(*) AS sales, COALESCE(SUM(amount), 0) AS revenue, COALESCE(SUM(commission), 0) AS commission "
        "FROM clients WHERE agent_id = ? AND status = 'paid'",
        (agent["id"],),
    ).fetchone()
    recent = conn.execute(
        "SELECT voucher_code, plan, amount, commission, status, date FROM clients WHERE agent_id = ? "
        "ORDER BY created_at DESC LIMIT 100",
        (agent["id"],),
    ).fetchall()
    conn.close()
    return jsonify({"agent": agent["name"], "commissionPercent": agent["commission_percent"],
                    "totals": dict(totals), "recent": [dict(r) for r in recent]})


init_db()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", "5002")))
