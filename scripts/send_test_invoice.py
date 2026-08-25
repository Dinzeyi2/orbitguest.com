"""Send one real PDF or receipt image through Orbit's inbound-email workflow."""

import argparse
import base64
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import urllib.error
import urllib.request
import uuid

def main():
    parser = argparse.ArgumentParser(description="Test Orbit with a real invoice attachment")
    parser.add_argument("file", type=Path, help="PDF or image invoice")
    parser.add_argument("--url", default=os.getenv("ORBIT_URL"), help="Railway URL, or set ORBIT_URL")
    parser.add_argument("--recipient", default=os.getenv("ORBIT_INVOICE_EMAIL"), help="Restaurant invoice address")
    parser.add_argument("--secret", default=os.getenv("INBOUND_EMAIL_SECRET"), help="Same secret configured in Railway")
    args = parser.parse_args()
    missing = [name for name, value in (("--url", args.url), ("--recipient", args.recipient), ("--secret", args.secret)) if not value]
    if missing: parser.error(f"missing {', '.join(missing)} or corresponding environment variables")
    if not args.file.is_file(): parser.error(f"file does not exist: {args.file}")
    content_type = mimetypes.guess_type(args.file.name)[0] or "application/octet-stream"
    payload = {
        "message_id": f"manual-test-{uuid.uuid4().hex}",
        "sender": "test-vendor@example.com",
        "recipient": args.recipient,
        "subject": f"Test invoice: {args.file.name}",
        "attachments": [{"filename": args.file.name, "content_type": content_type, "content_base64": base64.b64encode(args.file.read_bytes()).decode()}]
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(args.secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(args.url.rstrip("/") + "/v1/inbound/email", body, {"Content-Type": "application/json", "X-Orbit-Signature": signature}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            print(json.dumps(json.load(response), indent=2))
    except urllib.error.HTTPError as error:
        print(error.read().decode())
        raise SystemExit(f"Orbit returned HTTP {error.code}") from error

if __name__ == "__main__": main()
