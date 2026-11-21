"""Cashfree Payment Links integration for Karobar AI.

Credentials stay server-side. Payment status is refreshed from Cashfree before
any customer ledger entry is written. Cashfree is never allowed to mark an
invoice paid merely because a browser returned from checkout.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

import requests
import db

API_VERSION = (os.environ.get("CASHFREE_API_VERSION") or "2026-01-01").strip()


def _config():
    app_id = (os.environ.get("CASHFREE_APP_ID") or "").strip()
    secret = (os.environ.get("CASHFREE_SECRET_KEY") or "").strip()
    if not app_id or not secret:
        raise RuntimeError("Cashfree credentials are not configured. Add CASHFREE_APP_ID and CASHFREE_SECRET_KEY to the server environment.")
    env = (os.environ.get("CASHFREE_ENV") or "sandbox").strip().lower()
    if env not in ("sandbox", "production"):
        raise RuntimeError("CASHFREE_ENV must be sandbox or production.")
    base = "https://sandbox.cashfree.com/pg" if env == "sandbox" else "https://api.cashfree.com/pg"
    return app_id, secret, base


def _headers(app_id, secret):
    return {"x-client-id": app_id, "x-client-secret": secret,
            "x-api-version": API_VERSION, "Content-Type": "application/json"}


def _ensure_table():
    with db.connect() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS cashfree_payment_links (
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            link_id TEXT NOT NULL,
            customer_id TEXT,
            amount NUMERIC(12,2) NOT NULL,
            currency TEXT NOT NULL DEFAULT 'INR',
            purpose TEXT NOT NULL DEFAULT '',
            customer_name TEXT NOT NULL DEFAULT '',
            customer_phone TEXT NOT NULL DEFAULT '',
            link_url TEXT NOT NULL,
            cf_link_id TEXT,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            amount_paid NUMERIC(12,2) NOT NULL DEFAULT 0,
            ledger_payment_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, link_id)
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS cashfree_links_user_status ON cashfree_payment_links(user_id, status)")


def create_link(user: dict, *, amount, customer_name: str, customer_phone: str,
                purpose: str, customer_id: str | None = None,
                customer_email: str | None = None, return_url: str | None = None):
    app_id, secret, base = _config()
    try:
        value = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Enter a valid payment amount.")
    if value < Decimal("1.00") or value > Decimal("500000.00"):
        raise ValueError("Payment amount must be between ₹1 and ₹5,00,000.")
    phone = re.sub(r"\D", "", str(customer_phone or ""))
    if len(phone) == 12 and phone.startswith("91"):
        phone = phone[-10:]
    if len(phone) != 10:
        raise ValueError("A valid 10-digit Indian customer phone number is required.")
    name = str(customer_name or "Customer").strip()[:100]
    purpose = str(purpose or "Karobar AI payment").strip()[:500]
    link_id = "karobar_" + uuid.uuid4().hex[:24]
    _ensure_table()
    payload = {
        "link_id": link_id,
        "link_amount": float(value),
        "link_currency": "INR",
        "link_purpose": purpose,
        "customer_details": {"customer_name": name, "customer_phone": phone},
        "link_partial_payments": False,
        "link_auto_reminders": False,
        "link_notify": {"send_sms": False, "send_email": False, "send_whatsapp": False},
        "link_notes": {"source": "karobar_ai", "user_id": str(user["user_id"])[:100]},
    }
    if customer_email:
        payload["customer_details"]["customer_email"] = str(customer_email).strip()[:150]
    if return_url:
        payload["link_meta"] = {"return_url": return_url}
    try:
        response = requests.post(base + "/links", json=payload,
                                 headers=_headers(app_id, secret), timeout=20)
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach Cashfree: {exc.__class__.__name__}")
    if not response.ok:
        try:
            detail = response.json().get("message", "Cashfree rejected the request")
        except Exception:
            detail = "Cashfree rejected the request"
        raise RuntimeError(f"Cashfree error ({response.status_code}): {str(detail)[:180]}")
    data = response.json()
    link_url = data.get("link_url")
    if not link_url or not str(link_url).startswith("https://"):
        raise RuntimeError("Cashfree did not return a valid payment URL.")
    with db.connect() as conn:
        conn.execute("""
          INSERT INTO cashfree_payment_links
          (user_id,link_id,customer_id,amount,purpose,customer_name,customer_phone,
           link_url,cf_link_id,status,amount_paid)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (user["user_id"], link_id, customer_id, value, purpose, name,
              phone, link_url, str(data.get("cf_link_id") or ""),
              str(data.get("link_status") or "ACTIVE"),
              Decimal(str(data.get("link_amount_paid") or 0))))
    return {"created": True, "link_id": link_id, "cf_link_id": data.get("cf_link_id"),
            "link_url": link_url, "amount": float(value), "currency": "INR",
            "status": data.get("link_status", "ACTIVE"), "customer": name,
            "purpose": purpose, "message": "Payment link created. Payment is not yet received."}


def get_owned_link(user_id: str, link_id: str):
    _ensure_table()
    with db.connect() as conn:
        row = conn.execute("""SELECT user_id,link_id,customer_id,amount,purpose,
          customer_name,customer_phone,link_url,cf_link_id,status,amount_paid,
          ledger_payment_id FROM cashfree_payment_links
          WHERE user_id=%s AND link_id=%s""", (user_id, link_id)).fetchone()
        if not row:
            return None
        cols = ["user_id", "link_id", "customer_id", "amount", "purpose",
                "customer_name", "customer_phone", "link_url", "cf_link_id",
                "status", "amount_paid", "ledger_payment_id"]
        return dict(zip(cols, row))


def refresh_status(user: dict, repo, link_id: str):
    app_id, secret, base = _config()
    row = get_owned_link(user["user_id"], link_id)
    if not row:
        raise LookupError("Payment link not found.")
    try:
        response = requests.get(base + "/links/" + link_id,
                                headers=_headers(app_id, secret), timeout=20)
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach Cashfree: {exc.__class__.__name__}")
    if not response.ok:
        raise RuntimeError(f"Cashfree status check failed ({response.status_code}).")
    data = response.json()
    status = str(data.get("link_status") or row["status"]).upper()
    paid = Decimal(str(data.get("link_amount_paid") or 0)).quantize(Decimal("0.01"))
    amount = Decimal(str(row["amount"])).quantize(Decimal("0.01"))
    applied_id = row.get("ledger_payment_id")
    # Only a Cashfree-reported PAID status with the full expected amount can
    # create a receivable payment. Partial payment links are disabled here.
    if status == "PAID" and paid >= amount and row.get("customer_id") and not applied_id:
        customer = repo.customer(row["customer_id"])
        if not customer:
            raise RuntimeError("Customer linked to this payment no longer exists; payment verified but ledger reconciliation needs review.")
        _ensure_table()
        with db.connect() as conn:
            locked = conn.execute("""SELECT ledger_payment_id FROM cashfree_payment_links
                WHERE user_id=%s AND link_id=%s FOR UPDATE""",
                (user["user_id"], link_id)).fetchone()
            if locked and not locked[0]:
                last = conn.execute("""SELECT COALESCE(MAX(NULLIF(regexp_replace(payment_id,'\\D','','g'), '')::bigint),0)
                    FROM payments WHERE user_id=%s""", (user["user_id"],)).fetchone()[0]
                payment_id = f"pay_{int(last or 0)+1:04d}"
                conn.execute("""INSERT INTO payments
                    (user_id,payment_id,customer_id,amount,paid_on,note,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,now())""",
                    (user["user_id"], payment_id, row["customer_id"], float(amount),
                     date.today().isoformat(), f"Cashfree verified payment link {link_id}"))
                conn.execute("""UPDATE cashfree_payment_links SET ledger_payment_id=%s,
                    status=%s,amount_paid=%s,updated_at=now()
                    WHERE user_id=%s AND link_id=%s""",
                    (payment_id, status, paid, user["user_id"], link_id))
                applied_id = payment_id
    with db.connect() as conn:
        conn.execute("""UPDATE cashfree_payment_links SET status=%s,amount_paid=%s,updated_at=now()
            WHERE user_id=%s AND link_id=%s""",
            (status, paid, user["user_id"], link_id))
    if hasattr(repo, "refresh"):
        repo.refresh()
    return {"link_id": link_id, "status": status, "amount": float(amount),
            "amount_paid": float(paid), "link_url": row["link_url"],
            "ledger_updated": bool(applied_id), "ledger_payment_id": applied_id,
            "message": "Payment verified and customer ledger updated." if applied_id else
                       ("Payment is not yet fully paid. Ledger was not changed." if status != "PAID" else
                        "Payment verified. No customer ledger was linked to this payment.")}
