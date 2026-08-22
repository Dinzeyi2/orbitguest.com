import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from .db import Database
from .service import OrbitService
from .extraction import OpenAIInvoiceExtractor

class Handler(BaseHTTPRequestHandler):
    service = None
    def _send(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
    def _raw_body(self):
        size = int(self.headers.get("Content-Length", "0")); return self.rfile.read(size) or b"{}"
    def _merchant(self):
        auth = self.headers.get("Authorization", "")
        return self.service.authenticate(auth[7:]) if auth.startswith("Bearer ") else None
    def do_POST(self):
        try:
            path, raw = urlparse(self.path).path, self._raw_body()
            data = json.loads(raw)
            if path == "/v1/inbound/email":
                if not self.service.verify_inbound_signature(raw, self.headers.get("X-Orbit-Signature")):
                    return self._send(401, {"error": "invalid inbound signature"})
                return self._send(202, self.service.receive_email(data))
            if path == "/v1/merchants": return self._send(201, self.service.create_merchant(data["name"]))
            merchant = self._merchant()
            if not merchant: return self._send(401, {"error": "unauthorized"})
            if path == "/v1/guests/identify": result = self.service.capture_identity(merchant, data)
            elif path == "/v1/webhooks/pos/orders": result = self.service.ingest_order(merchant, data)
            elif path == "/v1/webhooks/invoices": result = self.service.ingest_invoice(merchant, data)
            elif path.startswith("/v1/campaigns/") and path.endswith("/sent"): result = self.service.mark_sent(merchant, path.split("/")[3])
            else: return self._send(404, {"error": "not_found"})
            self._send(200, result)
        except (KeyError, ValueError) as error: self._send(400, {"error": str(error)})
        except Exception: self._send(500, {"error": "internal_error"})
    def do_GET(self):
        path, merchant = urlparse(self.path).path, self._merchant()
        if path == "/health": return self._send(200, {"status": "ok"})
        if not merchant: return self._send(401, {"error": "unauthorized"})
        try:
            if path.startswith("/v1/guests/"): result = self.service.guest_profile(merchant, path.split("/")[3])
            elif path == "/v1/campaigns": result = self.service.list_campaigns(merchant)
            elif path == "/v1/metrics": result = self.service.metrics(merchant)
            elif path == "/v1/dashboard/invoices": result = self.service.invoice_dashboard(merchant)
            else: return self._send(404, {"error": "not_found"})
            self._send(200, result)
        except KeyError as error: self._send(404, {"error": str(error)})
    def log_message(self, *_): pass

def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--db", default="orbit.db"); parser.add_argument("--port", type=int, default=8080); args = parser.parse_args()
    Handler.service = OrbitService(Database(args.db), OpenAIInvoiceExtractor(), os.getenv("ORBIT_STORAGE_DIR")); ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", args.port))), Handler).serve_forever()

if __name__ == "__main__": main()
