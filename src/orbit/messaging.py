"""Real SMS and email delivery clients used by campaign dispatch."""

import base64
import json
import os
import urllib.parse
import urllib.request

class MessageDelivery:
    def __init__(self):
        self.resend_key = os.getenv("RESEND_API_KEY")
        self.email_from = os.getenv("ORBIT_EMAIL_FROM")
        self.twilio_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.twilio_token = os.getenv("TWILIO_AUTH_TOKEN")
        self.twilio_from = os.getenv("TWILIO_FROM_NUMBER")
        self.telnyx_key = os.getenv("TELNYX_API_KEY")
        self.telnyx_from = os.getenv("TELNYX_FROM_NUMBER")
        self.telnyx_profile = os.getenv("TELNYX_MESSAGING_PROFILE_ID")

    def send(self, channel, to, subject, body):
        if channel == "email": return self._email(to, subject, body)
        if channel == "sms": return self._sms(to, body)
        raise ValueError("unsupported message channel")

    def _email(self, to, subject, body):
        if not self.resend_key or not self.email_from: raise RuntimeError("Resend sending is not configured")
        payload = json.dumps({"from": self.email_from, "to": [to], "subject": subject, "text": body}).encode()
        request = urllib.request.Request("https://api.resend.com/emails", payload, {"Authorization": f"Bearer {self.resend_key}", "Content-Type": "application/json", "User-Agent": "OrbitGuest/0.1"})
        with urllib.request.urlopen(request, timeout=30) as response: return json.load(response).get("id")

    def _sms(self, to, body):
        if self.telnyx_key and self.telnyx_from:
            payload_data = {"to": to, "from": self.telnyx_from, "text": body}
            if self.telnyx_profile:
                payload_data["messaging_profile_id"] = self.telnyx_profile
            payload = json.dumps(payload_data).encode()
            request = urllib.request.Request(
                "https://api.telnyx.com/v2/messages",
                payload,
                {
                    "Authorization": f"Bearer {self.telnyx_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "OrbitGuest/0.2",
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)["data"]["id"]
        if not all((self.twilio_sid, self.twilio_token, self.twilio_from)): raise RuntimeError("Twilio is not configured")
        payload = urllib.parse.urlencode({"To": to, "From": self.twilio_from, "Body": body}).encode()
        auth = base64.b64encode(f"{self.twilio_sid}:{self.twilio_token}".encode()).decode()
        request = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{self.twilio_sid}/Messages.json", payload, {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "OrbitGuest/0.1"})
        with urllib.request.urlopen(request, timeout=30) as response: return json.load(response).get("sid")
