import base64
import hashlib
import hmac
import os
import struct
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from werkzeug.security import generate_password_hash


_bootstrap_dir = tempfile.TemporaryDirectory(prefix="wifi-voucher-tests-")
os.environ.setdefault("WIFI_DATA_DIR", _bootstrap_dir.name)

import app as wifi_app


class VoucherAccessTests(unittest.TestCase):
    def setUp(self):
        self._database_dir = tempfile.TemporaryDirectory(prefix="wifi-voucher-case-")
        self._original_database_path = wifi_app.DB_PATH
        wifi_app.DB_PATH = Path(self._database_dir.name) / "billing.db"
        wifi_app.init_db()

    def tearDown(self):
        wifi_app.DB_PATH = self._original_database_path
        self._database_dir.cleanup()

    def add_voucher(self, code="TVTEST1234", duration_seconds=3600, max_devices=1, data_limit_mb=None):
        now = wifi_app.utc_now()
        conn = wifi_app.get_db_connection()
        conn.execute(
            "INSERT INTO clients (id, customer_name, phone, plan, payment_method, device_count, date, amount, "
            "discount, status, addons, created_at, voucher_code, duration_seconds, activation_deadline, max_devices, data_limit_mb) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"test-{code}", "Test customer", "+256700000000", "hourly", "test", 1,
                now.strftime("%Y-%m-%d"), 200, 0, "paid", "[]", now.isoformat(), code,
                duration_seconds, (now + timedelta(days=30)).isoformat(), max_devices, data_limit_mb,
            ),
        )
        conn.commit()
        conn.close()

    def test_first_use_starts_shared_timer_and_tv_reconnects(self):
        self.add_voucher()
        first_use = wifi_app.utc_now()

        first = wifi_app.authorize_voucher("TVTEST1234", "aa:bb:cc:dd:ee:01", "smart-tv", first_use)
        reconnect = wifi_app.authorize_voucher(
            "TVTEST1234", "aa:bb:cc:dd:ee:01", "smart-tv", first_use + timedelta(minutes=5)
        )

        self.assertTrue(first["active"])
        self.assertEqual(first["expiresAt"], (first_use + timedelta(seconds=3600)).isoformat())
        self.assertTrue(reconnect["active"])
        self.assertEqual(reconnect["expiresAt"], first["expiresAt"])
        self.assertTrue(reconnect["sessionTracked"])

    def test_half_hour_plan_has_a_thirty_minute_duration(self):
        self.assertIn("halfHour", wifi_app.ALLOWED_PLANS)
        self.assertEqual(wifi_app.PLAN_DURATIONS_DAYS["halfHour"] * 86400, 1800)

    def test_device_limit_applies_equally_to_tv_and_phone_macs(self):
        self.add_voucher()
        first = wifi_app.authorize_voucher("TVTEST1234", "aa:bb:cc:dd:ee:01", "phone")
        second = wifi_app.authorize_voucher("TVTEST1234", "aa:bb:cc:dd:ee:02", "smart-tv")

        self.assertTrue(first["active"])
        self.assertFalse(second["active"])
        self.assertEqual(second["reason"], "device_limit_reached")

    def test_private_mac_change_reuses_cookie_bound_device_slot(self):
        self.add_voucher(max_devices=1)
        client = wifi_app.app.test_client()
        first_mac = "02:11:22:33:44:55"
        first = client.post(
            "/api/access/login",
            json={"voucher": "TVTEST1234", "mac": first_mac, "deviceType": "smart-tv"},
        )
        self.assertTrue(first.get_json()["active"])
        self.assertTrue(first.get_json()["privateMac"])
        self.assertIn("HttpOnly", first.headers.get("Set-Cookie", ""))

        changed_mac = "06:11:22:33:44:66"
        handoff = client.post(
            "/api/access/login",
            json={"voucher": "TVTEST1234", "mac": changed_mac, "deviceType": "smart-tv"},
        )
        self.assertTrue(handoff.get_json()["active"])
        conn = wifi_app.get_db_connection()
        slots = conn.execute(
            "SELECT COUNT(DISTINCT device_slot_id) AS total FROM voucher_devices WHERE voucher_code = ?",
            ("TVTEST1234",),
        ).fetchone()["total"]
        conn.close()
        self.assertEqual(slots, 1)

        other_device = wifi_app.app.test_client().post(
            "/api/access/login",
            json={"voucher": "TVTEST1234", "mac": "08:11:22:33:44:77", "deviceType": "phone"},
        )
        self.assertEqual(other_device.get_json()["reason"], "device_limit_reached")

    def test_voucher_login_requires_a_valid_device_mac(self):
        client = wifi_app.app.test_client()
        missing = client.post("/api/access/login", json={"voucher": "TVTEST1234"})
        malformed = client.post("/api/access/login", json={"voucher": "TVTEST1234", "mac": "not-a-mac"})
        self.assertEqual(missing.get_json()["reason"], "invalid_mac")
        self.assertEqual(malformed.get_json()["reason"], "invalid_mac")

    def test_signed_mac_ticket_is_required_and_binds_the_claimed_mac(self):
        self.add_voucher()
        router_key = "router-test-key"
        signing_secret = "s" * 48
        mac = "02:aa:bb:cc:dd:33"
        client = wifi_app.app.test_client()
        with patch.object(wifi_app, "ROUTER_API_KEY", router_key), \
                patch.object(wifi_app, "ROUTER_MAC_SIGNING_SECRET", signing_secret):
            unsigned = client.post("/api/access/login", json={"voucher": "TVTEST1234", "mac": mac})
            self.assertEqual(unsigned.status_code, 401)

            unauthorized_ticket = client.post("/api/access/mac-ticket", json={"mac": mac})
            self.assertEqual(unauthorized_ticket.status_code, 401)
            ticket_response = client.post(
                "/api/access/mac-ticket",
                headers={"X-Router-Key": router_key},
                json={"mac": mac},
            )
            self.assertEqual(ticket_response.status_code, 200)
            ticket = ticket_response.get_json()["macTicket"]

            changed_mac = "02:aa:bb:cc:dd:34"
            spoofed = client.post(
                "/api/access/login",
                json={"voucher": "TVTEST1234", "mac": changed_mac, "macTicket": ticket},
            )
            self.assertEqual(spoofed.get_json()["reason"], "invalid_mac_ticket")

            accepted = client.post(
                "/api/access/login",
                json={"voucher": "TVTEST1234", "mac": mac, "macTicket": ticket},
            )
            self.assertTrue(accepted.get_json()["active"])

    def test_bad_voucher_attempts_lock_out_code_and_ip(self):
        client = wifi_app.app.test_client()
        for _ in range(wifi_app.VOUCHER_LOGIN_FAILURE_LIMIT):
            response = client.post("/api/access/login", json={"voucher": "NOPE234567", "mac": "02:aa:bb:cc:dd:01"})
            self.assertEqual(response.get_json()["reason"], "invalid_voucher")
        locked = client.post("/api/access/login", json={"voucher": "NOPE234567", "mac": "02:aa:bb:cc:dd:01"})
        self.assertEqual(locked.status_code, 429)
        self.assertGreater(locked.get_json()["retryAfterSeconds"], 0)

    def test_totp_verifier_accepts_rfc6238_six_digit_code(self):
        secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
        padded_secret = secret + "=" * (-len(secret) % 8)
        digest = hmac.new(base64.b32decode(padded_secret), struct.pack(">Q", 1), hashlib.sha1).digest()
        index = digest[-1] & 0x0F
        value = struct.unpack(">I", digest[index:index + 4])[0] & 0x7FFFFFFF
        expected = f"{value % 1_000_000:06d}"
        with patch.object(wifi_app, "ADMIN_TOTP_SECRET", secret):
            self.assertTrue(wifi_app.verify_admin_totp(expected, now=59))
            self.assertFalse(wifi_app.verify_admin_totp("000000", now=59))

    def test_admin_login_requires_hash_and_totp_then_issues_csrf_token(self):
        secret = base64.b32encode(b"another-test-secret!!").decode().rstrip("=")
        now = time.time()
        counter = int(now // 30)
        padded_secret = secret + "=" * (-len(secret) % 8)
        digest = hmac.new(base64.b32decode(padded_secret), struct.pack(">Q", counter), hashlib.sha1).digest()
        index = digest[-1] & 0x0F
        value = struct.unpack(">I", digest[index:index + 4])[0] & 0x7FFFFFFF
        code = f"{value % 1_000_000:06d}"
        client = wifi_app.app.test_client()
        with patch.object(wifi_app, "ADMIN_USERNAME", "phase1-owner"), \
                patch.object(wifi_app, "ADMIN_PASSWORD_HASH", generate_password_hash("correct-password")), \
                patch.object(wifi_app, "ADMIN_PASSWORD", "unused"), \
                patch.object(wifi_app, "ADMIN_TOTP_SECRET", secret):
            denied = client.post("/api/auth/login", json={
                "username": "phase1-owner", "password": "correct-password", "totpCode": "000000",
            })
            self.assertEqual(denied.status_code, 401)
            accepted = client.post("/api/auth/login", json={
                "username": "phase1-owner", "password": "correct-password", "totpCode": code,
            })
            self.assertEqual(accepted.status_code, 200)
            token = accepted.get_json()["csrfToken"]
            self.assertGreaterEqual(len(token), 32)
            auth_state = client.get("/api/auth/session").get_json()
            self.assertEqual(auth_state["csrfToken"], token)
            blocked_logout = client.post("/api/auth/logout")
            self.assertEqual(blocked_logout.status_code, 403)
            allowed_logout = client.post("/api/auth/logout", headers={"X-CSRF-Token": token})
            self.assertEqual(allowed_logout.status_code, 200)

    def test_csrf_is_required_for_admin_mutations(self):
        client = wifi_app.app.test_client()
        with client.session_transaction() as session:
            session["admin_authenticated"] = True
            session["csrf_token"] = "session-token"
        blocked = client.post("/api/admin/vouchers/bulk", json={"quantity": 0})
        self.assertEqual(blocked.status_code, 403)
        accepted_by_csrf = client.post(
            "/api/admin/vouchers/bulk",
            headers={"X-CSRF-Token": "session-token"},
            json={"quantity": 0},
        )
        self.assertEqual(accepted_by_csrf.status_code, 400)

    def test_data_cap_is_shared_idempotent_and_disconnects_all_devices(self):
        self.add_voucher(max_devices=2, data_limit_mb=1)
        first_mac = "aa:bb:cc:dd:ee:11"
        second_mac = "aa:bb:cc:dd:ee:12"
        self.assertTrue(wifi_app.authorize_voucher("TVTEST1234", first_mac, "smart-tv")["active"])
        self.assertTrue(wifi_app.authorize_voucher("TVTEST1234", second_mac, "phone")["active"])
        client = wifi_app.app.test_client()
        payload = {"voucher": "TVTEST1234", "mac": first_mac, "eventId": "event-first", "bytesDelta": 800000}

        first_report = client.post("/api/access/usage", json=payload)
        retry_report = client.post("/api/access/usage", json=payload)
        self.assertTrue(first_report.get_json()["active"])
        self.assertTrue(retry_report.get_json()["duplicate"])
        self.assertEqual(retry_report.get_json()["usedBytes"], 800000)

        with patch.object(wifi_app, "notify_router_disconnect") as disconnect:
            cap_report = client.post(
                "/api/access/usage",
                json={"voucher": "TVTEST1234", "mac": second_mac, "eventId": "event-second", "bytesDelta": 300000},
            )
        self.assertFalse(cap_report.get_json()["active"])
        self.assertEqual(cap_report.get_json()["reason"], "data_limit_reached")
        self.assertEqual(disconnect.call_count, 2)

        denied = client.post("/api/access/login", json={"voucher": "TVTEST1234", "mac": first_mac})
        self.assertEqual(denied.get_json()["reason"], "data_limit_reached")

    def test_payment_confirmation_issues_unused_first_use_voucher(self):
        now = wifi_app.utc_now()
        conn = wifi_app.get_db_connection()
        conn.execute(
            "INSERT INTO clients (id, customer_name, phone, plan, payment_method, device_count, date, amount, "
            "discount, status, addons, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("payment-client", "Test customer", "+256700000000", "hourly", "momo", 1,
             now.strftime("%Y-%m-%d"), 210, 0, "pending", "[]", now.isoformat()),
        )
        conn.execute(
            "INSERT INTO payment_intents (id, client_id, provider, amount, phone, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("payment-test", "payment-client", "momo", 210, "+256700000000", now.isoformat()),
        )
        intent = conn.execute("SELECT * FROM payment_intents WHERE id = ?", ("payment-test",)).fetchone()
        result = wifi_app.mark_intent_paid(conn, intent, "provider-reference")
        conn.commit()
        code = result["voucherCode"]
        voucher = conn.execute("SELECT expires_at, activated_at, duration_seconds FROM clients WHERE id = ?", ("payment-client",)).fetchone()
        conn.close()

        self.assertEqual(result["status"], "paid")
        self.assertEqual(len(code), 10)
        self.assertIsNone(voucher["expires_at"])
        self.assertIsNone(voucher["activated_at"])
        self.assertEqual(voucher["duration_seconds"], 3600)

    def test_reaper_expires_sessions_and_requests_disconnect(self):
        self.add_voucher()
        mac = "aa:bb:cc:dd:ee:03"
        wifi_app.authorize_voucher("TVTEST1234", mac, "smart-tv")
        conn = wifi_app.get_db_connection()
        conn.execute(
            "UPDATE clients SET expires_at = ? WHERE voucher_code = ?",
            ((wifi_app.utc_now() - timedelta(seconds=1)).isoformat(), "TVTEST1234"),
        )
        conn.commit()
        conn.close()

        with patch.object(wifi_app, "notify_router_disconnect") as disconnect:
            result = wifi_app.reap_expired_vouchers()

        self.assertEqual(result, {"expiredVouchers": 1, "disconnectedDevices": 1})
        disconnect.assert_called_once_with(mac, "TVTEST1234")
        conn = wifi_app.get_db_connection()
        status = conn.execute("SELECT status FROM clients WHERE voucher_code = ?", ("TVTEST1234",)).fetchone()["status"]
        session_status = conn.execute(
            "SELECT status FROM active_sessions WHERE voucher_code = ?", ("TVTEST1234",)
        ).fetchone()["status"]
        conn.close()
        self.assertEqual(status, "expired")
        self.assertEqual(session_status, "expired")

    def test_bulk_vouchers_are_searchable_and_can_pause_resume_sessions(self):
        client = wifi_app.app.test_client()
        with client.session_transaction() as session:
            session["admin_authenticated"] = True
            session["csrf_token"] = "test-csrf-token"
        csrf_headers = {"X-CSRF-Token": "test-csrf-token"}
        created = client.post(
            "/api/admin/vouchers/bulk",
            headers=csrf_headers,
            json={
                "quantity": 2,
                "plan": "hourly",
                "durationSeconds": 1200,
                "maxDevices": 2,
                "prefix": "TV",
                "dataLimitMb": 500,
                "speedLimitMbps": 10,
            },
        )
        self.assertEqual(created.status_code, 201)
        vouchers = created.get_json()["created"]
        self.assertEqual(len(vouchers), 2)
        self.assertTrue(all(len(voucher["code"]) == 10 for voucher in vouchers))

        code = vouchers[0]["code"]
        login = client.post(
            "/api/access/login",
            json={"voucher": code, "mac": "aa:bb:cc:dd:ee:04", "deviceType": "smart-tv"},
        )
        self.assertTrue(login.get_json()["active"])
        inventory = client.get("/api/admin/vouchers", query_string={"q": code})
        self.assertEqual(inventory.get_json()["vouchers"][0]["voucher_code"], code)
        sessions = client.get("/api/admin/sessions").get_json()["sessions"]
        self.assertEqual(sessions[0]["device_type"], "smart-tv")

        paused = client.post(f"/api/admin/vouchers/{code}/pause", headers=csrf_headers)
        self.assertTrue(paused.get_json()["paused"])
        denied = client.post(
            "/api/access/login",
            json={"voucher": code, "mac": "aa:bb:cc:dd:ee:04", "deviceType": "smart-tv"},
        )
        self.assertEqual(denied.get_json()["reason"], "paused")
        resumed = client.post(f"/api/admin/vouchers/{code}/resume", headers=csrf_headers)
        self.assertFalse(resumed.get_json()["paused"])


if __name__ == "__main__":
    unittest.main()