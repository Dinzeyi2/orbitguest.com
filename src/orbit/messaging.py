"""Resend email and Telnyx SMS delivery with production safety controls."""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request


class DeliveryError(RuntimeError):
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class MessageDelivery:
    TELNYX_ENDPOINT = "https://api.telnyx.com/v2/messages"
    RESEND_ENDPOINT = "https://api.resend.com/emails"

    def __init__(self):
        self.resend_key = os.getenv("RESEND_API_KEY")
        self.email_from = os.getenv("ORBIT_EMAIL_FROM")
        self.telnyx_key = os.getenv("TELNYX_API_KEY")
        self.telnyx_from = os.getenv("TELNYX_FROM_NUMBER")
        self.telnyx_profile = os.getenv("TELNYX_MESSAGING_PROFILE_ID")
        self.telnyx_public_key = os.getenv("TELNYX_PUBLIC_KEY")
        self.allowed_prefixes = tuple(value.strip() for value in os.getenv("TELNYX_ALLOWED_COUNTRY_PREFIXES", "").split(",") if value.strip())

    def send(self, channel, to, subject, body, idempotency_key=None):
        if channel == "email": return self._email(to, subject, body, idempotency_key)
        if channel == "sms": return self._sms(to, body, idempotency_key)
        raise ValueError("unsupported message channel")

    def _request(self, request, provider):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise DeliveryError(f"{provider} delivery failed ({error.code}): {detail}", retryable=error.code == 429 or error.code >= 500) from error
        except (OSError, TimeoutError) as error:
            raise DeliveryError(f"{provider} delivery failed: {error}", retryable=True) from error

    def _email(self, to, subject, body, idempotency_key):
        if not self.resend_key or not self.email_from: raise DeliveryError("Resend sending is not configured", retryable=False)
        payload = json.dumps({"from": self.email_from, "to": [to], "subject": subject, "text": body}).encode()
        headers = {"Authorization": f"Bearer {self.resend_key}", "Content-Type": "application/json", "User-Agent": "OrbitGuest/0.2"}
        if idempotency_key: headers["Idempotency-Key"] = idempotency_key
        result = self._request(urllib.request.Request(self.RESEND_ENDPOINT, payload, headers), "Resend")
        if not result.get("id"): raise DeliveryError("Resend returned no message id", retryable=True)
        return result["id"]

    def _sms(self, to, body, idempotency_key):
        if not self.telnyx_key or not self.telnyx_from: raise DeliveryError("Telnyx is not configured", retryable=False)
        if not re.fullmatch(r"\+[1-9]\d{7,14}", to or ""): raise DeliveryError("SMS recipient must be E.164", retryable=False)
        if self.allowed_prefixes and not any(to.startswith(prefix) for prefix in self.allowed_prefixes):
            raise DeliveryError("SMS destination is outside registered country prefixes", retryable=False)
        payload = {"from": self.telnyx_from, "to": to, "text": body}
        if self.telnyx_profile: payload["messaging_profile_id"] = self.telnyx_profile
        headers = {"Authorization": f"Bearer {self.telnyx_key}", "Content-Type": "application/json", "User-Agent": "OrbitGuest/0.2"}
        if idempotency_key: headers["Idempotency-Key"] = idempotency_key
        result = self._request(urllib.request.Request(self.TELNYX_ENDPOINT, json.dumps(payload).encode(), headers), "Telnyx")
        message_id = (result.get("data") or {}).get("id")
        if not message_id: raise DeliveryError("Telnyx returned no message id", retryable=True)
        return message_id

    def verify_telnyx(self, raw_body, headers, tolerance=300):
        """Verify Telnyx Ed25519 webhook signatures and reject stale replays."""
        signature = headers.get("telnyx-signature-ed25519")
        timestamp = headers.get("telnyx-timestamp")
        if not self.telnyx_public_key or not signature or not timestamp: return False
        try:
            if abs(time.time() - int(timestamp)) > tolerance: return False
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            key_text = self.telnyx_public_key.strip()
            if "BEGIN PUBLIC KEY" in key_text:
                from cryptography.hazmat.primitives.serialization import load_pem_public_key
                public_key = load_pem_public_key(key_text.encode())
            else:
                key_bytes = bytes.fromhex(key_text) if re.fullmatch(r"[0-9a-fA-F]{64}", key_text) else base64.b64decode(key_text, validate=True)
                public_key = Ed25519PublicKey.from_public_bytes(key_bytes)
            public_key.verify(base64.b64decode(signature), timestamp.encode() + b"|" + raw_body)
            return True
        except Exception:
            return False
