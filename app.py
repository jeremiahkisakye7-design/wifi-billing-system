import json
import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, session

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("WIFI_DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "billing.db"

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "local-development-key-change-me")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ALLOWED_PLANS = {"shortSession", "fullDay", "weekly", "monthly"}
ALLOWED_STATUSES = {"pending", "paid", "expired"}


def utc_now():
    return datetime.now(timezone.utc)


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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clients_status ON clients(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clients_date ON clients(date)")
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


@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "outview.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/api/auth/login")
def login():
    payload = request.get_json(silent=True) or {}
    if payload.get("username") != ADMIN_USERNAME or payload.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "Invalid username or password."}), 401
    session["admin_authenticated"] = True
    return jsonify({"authenticated": True})


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"authenticated": False})


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
        "created_at": utc_now().isoformat(),
    }

    conn.execute(
        """
        INSERT INTO clients (
            id, customer_name, phone, plan, payment_method, device_count,
            date, amount, discount, status, addons, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            record["created_at"],
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "id": client_id}), 201


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
    conn.close()
    return jsonify(
        {
            "totalRevenue": total_revenue,
            "activeClients": active_clients,
            "pendingPayments": pending,
            "paidToday": paid_today,
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
