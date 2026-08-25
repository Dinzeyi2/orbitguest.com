"""Signature verification for Telnyx messaging webhooks."""

import base64
import os
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature


class TelnyxWebhook:
    def __init__(self, public_key=None, tolerance_seconds=300):
        self.public_key = public_key or os.getenv("TELNYX_PUBLIC_KEY")
        self.tolerance_seconds = tolerance_seconds

    def _key(self):
        if not self.public_key:
            raise ValueError("TELNYX_PUBLIC_KEY is not configured")
        value = self.public_key.strip()
        if value.startswith("-----BEGIN"):
            key = serialization.load_pem_public_key(value.encode())
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError("TELNYX_PUBLIC_KEY is not an Ed25519 key")
            return key
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError:
            raw = bytes.fromhex(value)
        return Ed25519PublicKey.from_public_bytes(raw)

    def verify(self, raw_body, headers, current_time=None):
        headers = {key.lower(): value for key, value in headers.items()}
        timestamp = headers.get("telnyx-timestamp")
        signature = headers.get("telnyx-signature-ed25519")
        if not timestamp or not signature:
            return False
        try:
            timestamp_number = int(timestamp)
            if abs((current_time or int(time.time())) - timestamp_number) > self.tolerance_seconds:
                return False
            signed = timestamp.encode() + b"|" + raw_body
            self._key().verify(base64.b64decode(signature), signed)
            return True
        except (ValueError, TypeError, InvalidSignature):
            return False
