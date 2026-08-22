"""Resend inbound-email webhook verification and attachment retrieval."""

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

class ResendError(RuntimeError): pass

class ResendInboundClient:
    API = "https://api.resend.com"
    USER_AGENT = "OrbitGuest/0.1 (+https://orbitguest.com)"

    def __init__(self, api_key, webhook_secret, api_base=None):
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self.api_base = (api_base or self.API).rstrip("/")

    def verify(self, raw_body: bytes, headers, tolerance=300):
        """Verify Resend/Svix webhook headers without an external SDK."""
        message_id = headers.get("svix-id")
        timestamp = headers.get("svix-timestamp")
        signatures = headers.get("svix-signature", "").split()
        if not all((message_id, timestamp, signatures, self.webhook_secret)): return False
        try:
            if abs(time.time() - int(timestamp)) > tolerance: return False
            secret = self.webhook_secret[6:] if self.webhook_secret.startswith("whsec_") else self.webhook_secret
            key = base64.b64decode(secret + "=" * (-len(secret) % 4))
        except (ValueError, TypeError): return False
        signed = f"{message_id}.{timestamp}.".encode() + raw_body
        expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
        return any(part.startswith("v1,") and hmac.compare_digest(part[3:], expected) for part in signatures)

    def _json(self, path):
        request = urllib.request.Request(self.api_base + path, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": self.USER_AGENT,
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response: return json.load(response)
        except urllib.error.HTTPError as error:
            raise ResendError(f"Resend API failed ({error.code}): {error.read().decode()[:500]}") from error
        except (OSError, ValueError) as error: raise ResendError(f"Resend API failed: {error}") from error

    def _download(self, url):
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/octet-stream",
            "User-Agent": self.USER_AGENT,
        })
        try:
            with urllib.request.urlopen(request, timeout=30) as response: return response.read()
        except urllib.error.HTTPError as error: raise ResendError(f"Attachment download failed ({error.code})") from error

    def normalize(self, event):
        if event.get("type") != "email.received": raise ResendError("unsupported Resend event")
        event_data = event.get("data", {})
        email_id = event_data.get("email_id") or event_data.get("id")
        if not email_id: raise ResendError("Resend event has no email_id")
        details_response = self._json(f"/emails/receiving/{email_id}")
        details = details_response.get("data", details_response)
        attachments = details.get("attachments") or event_data.get("attachments") or []
        if attachments and any(not (item.get("content") or item.get("content_base64") or item.get("download_url") or item.get("url")) for item in attachments):
            listed_response = self._json(f"/emails/receiving/{email_id}/attachments")
            listed = listed_response.get("data", listed_response if isinstance(listed_response, list) else [])
            by_id = {item.get("id"): item for item in listed}
            by_name = {item.get("filename"): item for item in listed}
            attachments = [{**item, **(by_id.get(item.get("id")) or by_name.get(item.get("filename")) or {})} for item in attachments]
        normalized = []
        for item in attachments:
            content = item.get("content") or item.get("content_base64")
            if content:
                raw = base64.b64decode(content)
            else:
                url = item.get("download_url") or item.get("url")
                if not url and item.get("id"):
                    attachment_response = self._json(f"/emails/receiving/{email_id}/attachments/{item['id']}")
                    attachment = attachment_response.get("data", attachment_response)
                    url = attachment.get("download_url") or attachment.get("url")
                    content = attachment.get("content") or attachment.get("content_base64")
                if content: raw = base64.b64decode(content)
                elif url: raw = self._download(url)
                else: raise ResendError(f"Resend attachment {item.get('filename', '')} has no content URL")
            normalized.append({"filename": item.get("filename") or "invoice", "content_type": item.get("content_type") or "application/octet-stream", "content_base64": base64.b64encode(raw).decode()})
        recipients = details.get("to") or event_data.get("to") or []
        if isinstance(recipients, str): recipients = [recipients]
        if not recipients: raise ResendError("Resend email has no recipient")
        sender = details.get("from") or event_data.get("from") or "unknown@resend.dev"
        return {"message_id": details.get("message_id") or event_data.get("message_id") or email_id, "sender": sender, "recipient": recipients[0], "subject": details.get("subject") or event_data.get("subject"), "received_at": details.get("created_at") or event_data.get("created_at"), "attachments": normalized}
