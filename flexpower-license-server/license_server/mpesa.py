"""Safaricom Daraja (M-Pesa) client and helpers.

Used for:
  * STK push ("Lipa na M-Pesa Online"): the customer gets a PIN prompt on their phone.
  * STK query: ask Safaricom for the result if the callback never arrived, and to
    double-check a callback before crediting it.
  * C2B URL registration: so Paybill payments made from the phone menu are reported to us.

Daraja docs: https://developer.safaricom.co.ke
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

SANDBOX_URL = "https://sandbox.safaricom.co.ke"
PRODUCTION_URL = "https://api.safaricom.co.ke"
EAT = ZoneInfo("Africa/Nairobi")  # Daraja timestamps are Kenyan time

# Addresses Safaricom sends callbacks from, as published in the Daraja docs.
# Check the current list on the Daraja portal before relying on it.
SAFARICOM_CALLBACK_IPS = [
    "196.201.214.200", "196.201.214.206", "196.201.213.114", "196.201.214.207", "196.201.214.208",
    "196.201.213.44", "196.201.212.127", "196.201.212.138", "196.201.212.129", "196.201.212.136",
    "196.201.212.74", "196.201.212.69",
]

STILL_PROCESSING = "500.001.1001"  # STK query error code: the customer hasn't finished yet


class DarajaError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def normalize_phone(raw: str) -> str:
    """0712 345 678 / +254712345678 / 712345678 / 0112345678 -> 2547XXXXXXXX or 2541XXXXXXXX."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0"):
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in "71":
        digits = "254" + digits
    if not re.fullmatch(r"254[71]\d{8}", digits):
        raise ValueError("Enter a Safaricom number such as 0712 345 678")
    return digits


def mask_phone(phone: str) -> str:
    return phone[:5] + "****" + phone[-3:] if phone and len(phone) >= 9 else (phone or "")


def account_no(customer_id: int) -> str:
    return f"FLX{customer_id:04d}"


def parse_account_no(ref: str) -> int | None:
    m = re.fullmatch(r"\s*FLX[-\s]?0*(\d{1,9})\s*", ref or "", flags=re.I)
    return int(m.group(1)) if m else None


class MpesaSettings:
    def __init__(self, raw: dict[str, Any] | None):
        r = raw or {}
        self.enabled = bool(r.get("enabled", False))
        self.environment = str(r.get("environment", "sandbox")).lower()
        self.base_url = str(r.get("base_url") or (PRODUCTION_URL if self.environment == "production" else SANDBOX_URL)).rstrip("/")
        env = lambda name, default: os.environ.get(str(r.get(name) or default), "")  # noqa: E731
        self.consumer_key = env("consumer_key_env", "MPESA_CONSUMER_KEY")
        self.consumer_secret = env("consumer_secret_env", "MPESA_CONSUMER_SECRET")
        self.passkey = env("passkey_env", "MPESA_PASSKEY")
        self.callback_secret = env("callback_secret_env", "MPESA_CALLBACK_SECRET")
        self.shortcode = str(r.get("shortcode", ""))                       # STK: Paybill or Till (store) number
        self.transaction_type = str(r.get("transaction_type", "CustomerPayBillOnline"))
        self.party_b = str(r.get("party_b") or self.shortcode)              # Till: the till number
        self.c2b_shortcode = str(r.get("c2b_shortcode") or self.shortcode)  # Paybill customers pay to
        self.public_base_url = str(r.get("public_base_url", "")).rstrip("/")
        ips = r.get("allowed_ips")
        self.allowed_ips = SAFARICOM_CALLBACK_IPS if ips is None else list(ips)
        self.verify_with_query = bool(r.get("verify_stk_with_query", True))

    def problems(self) -> list[str]:
        p = []
        if not self.enabled:
            return ["M-Pesa is disabled in license-server.yaml"]
        for label, v in (("consumer key", self.consumer_key), ("consumer secret", self.consumer_secret),
                         ("passkey", self.passkey), ("shortcode", self.shortcode),
                         ("public_base_url", self.public_base_url)):
            if not v:
                p.append(f"missing {label}")
        if len(self.callback_secret) < 16:
            p.append("MPESA_CALLBACK_SECRET must be at least 16 random characters")
        return p

    def url(self, kind: str) -> str:
        return f"{self.public_base_url}/mpesa/{kind}/{self.callback_secret}"


class Daraja:
    def __init__(self, s: MpesaSettings, opener: Callable = urllib.request.urlopen):
        self.s = s
        self.opener = opener
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def _call(self, method: str, path: str, body: dict | None = None, auth: str | None = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = auth
        req = urllib.request.Request(self.s.base_url + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.opener(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}")
            except ValueError:
                err = {}
            raise DarajaError(err.get("errorMessage") or err.get("ResponseDescription") or f"Daraja HTTP {e.code}",
                              err.get("errorCode")) from e
        except urllib.error.URLError as e:
            raise DarajaError(f"cannot reach Safaricom: {e.reason}") from e

    def token(self) -> str:
        with self._lock:
            if self._token and self._token[1] > time.time() + 60:
                return self._token[0]
            basic = base64.b64encode(f"{self.s.consumer_key}:{self.s.consumer_secret}".encode()).decode()
            r = self._call("GET", "/oauth/v1/generate?grant_type=client_credentials", auth=f"Basic {basic}")
            if "access_token" not in r:
                raise DarajaError("Daraja did not return an access token (check consumer key/secret)")
            self._token = (r["access_token"], time.time() + float(r.get("expires_in", 3599)))
            return self._token[0]

    def _password(self) -> tuple[str, str]:
        ts = datetime.now(EAT).strftime("%Y%m%d%H%M%S")
        return base64.b64encode(f"{self.s.shortcode}{self.s.passkey}{ts}".encode()).decode(), ts

    def stk_push(self, phone: str, amount: int, account_ref: str, description: str) -> dict[str, Any]:
        pw, ts = self._password()
        r = self._call("POST", "/mpesa/stkpush/v1/processrequest", {
            "BusinessShortCode": self.s.shortcode, "Password": pw, "Timestamp": ts,
            "TransactionType": self.s.transaction_type, "Amount": int(amount),
            "PartyA": phone, "PartyB": self.s.party_b, "PhoneNumber": phone,
            "CallBackURL": self.s.url("stk-callback"),
            "AccountReference": account_ref[:12], "TransactionDesc": description[:13],
        }, auth=f"Bearer {self.token()}")
        if str(r.get("ResponseCode")) != "0":
            raise DarajaError(r.get("ResponseDescription") or r.get("errorMessage") or "STK push rejected")
        return r

    def stk_query(self, checkout_id: str) -> dict[str, Any]:
        """Returns the Daraja answer; raises DarajaError(code=STILL_PROCESSING) while the customer is deciding."""
        pw, ts = self._password()
        return self._call("POST", "/mpesa/stkpushquery/v1/query", {
            "BusinessShortCode": self.s.shortcode, "Password": pw, "Timestamp": ts, "CheckoutRequestID": checkout_id,
        }, auth=f"Bearer {self.token()}")

    def register_c2b(self) -> dict[str, Any]:
        return self._call("POST", "/mpesa/c2b/v1/registerurl", {
            "ShortCode": self.s.c2b_shortcode, "ResponseType": "Completed",
            "ConfirmationURL": self.s.url("c2b/confirmation"), "ValidationURL": self.s.url("c2b/validation"),
        }, auth=f"Bearer {self.token()}")


def parse_stk_callback(body: dict[str, Any]) -> dict[str, Any]:
    cb = (body.get("Body") or {}).get("stkCallback") or {}
    items = {i.get("Name"): i.get("Value") for i in ((cb.get("CallbackMetadata") or {}).get("Item") or [])}
    return {"checkout_id": cb.get("CheckoutRequestID"), "result_code": str(cb.get("ResultCode")),
            "result_desc": cb.get("ResultDesc", ""), "amount": items.get("Amount"),
            "receipt": items.get("MpesaReceiptNumber"), "phone": str(items.get("PhoneNumber") or "")}
