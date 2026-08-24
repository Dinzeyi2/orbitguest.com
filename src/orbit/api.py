import argparse
import json
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from .db import Database
from .service import OrbitService
from .extraction import OpenAIInvoiceExtractor
from .resend import ResendInboundClient, ResendError
from .prediction import OpenAIBehaviorPredictor
from .messaging import MessageDelivery
from .square import SquareIntegration, SquareError

class Handler(BaseHTTPRequestHandler):
    service = None
    resend = None
    square = None
    def _send(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
    def _raw_body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 32 * 1024 * 1024: raise ValueError("request body exceeds 32 MB")
        return self.rfile.read(size) or b"{}"
    def _merchant(self):
        auth = self.headers.get("Authorization", "")
        return self.service.authenticate(auth[7:]) if auth.startswith("Bearer ") else None
    def do_POST(self):
        try:
            path, raw = urlparse(self.path).path, self._raw_body()
            data = json.loads(raw)
            if path == "/v1/webhooks/resend":
                if not self.resend or not self.resend.verify(raw, {key.lower(): value for key, value in self.headers.items()}):
                    return self._send(401, {"error": "invalid Resend signature"})
                if data.get("type") != "email.received": return self._send(200, {"status": "ignored"})
                threading.Thread(target=self._process_resend, args=(data,), daemon=True).start()
                return self._send(202, {"status": "accepted"})
            if path == "/v1/webhooks/square":
                if not self.square or not self.square.verify_webhook(raw, self.headers.get("X-Square-Hmacsha256-Signature")):
                    return self._send(401, {"error": "invalid Square signature"})
                return self._send(202, self.square.enqueue_webhook(raw))
            if path.startswith("/v1/webhooks/pos/"):
                return self._send(202, self.service.ingest_pos_webhook(path.split("/")[4], raw, self.headers.get("X-Orbit-POS-Secret")))
            if path == "/v1/inbound/email":
                if not self.service.verify_inbound_signature(raw, self.headers.get("X-Orbit-Signature")):
                    return self._send(401, {"error": "invalid inbound signature"})
                return self._send(202, self.service.receive_email(data))
            if path == "/v1/merchants": return self._send(201, self.service.create_merchant(data["name"]))
            merchant = self._merchant()
            if not merchant: return self._send(401, {"error": "unauthorized"})
            if path == "/v1/guests/identify": result = self.service.capture_identity(merchant, data)
            elif path == "/v1/webhooks/pos/orders": result = self.service.ingest_order(merchant, data)
            elif path == "/v1/pos/connections": result = self.service.create_pos_connection(merchant, data)
            elif path == "/v1/integrations/square/authorize": result = self.square.authorize(merchant)
            elif path == "/v1/integrations/square/sync": result = self.square.historical_sync(merchant, data["begin_at"], data.get("end_at"))
            elif path == "/v1/integrations/square/locations/sync": result = self.square.sync_locations(merchant)
            elif path == "/v1/integrations/square/catalog/sync": result = self.square.sync_catalog(merchant)
            elif path.startswith("/v1/integrations/square/events/") and path.endswith("/retry"): result = self.square.retry_event(merchant, path.split("/")[5])
            elif path == "/v1/menu/items": result = self.service.upsert_menu_item(merchant, data)
            elif path == "/v1/recipes/links": result = self.service.link_recipe(merchant, data)
            elif path == "/v1/webhooks/invoices": result = self.service.ingest_invoice(merchant, data)
            elif path.startswith("/v1/campaigns/") and path.endswith("/sent"): result = self.service.mark_sent(merchant, path.split("/")[3])
            elif path == "/v1/engine/run": result = self.service.run_behavior_engine(merchant)
            elif path == "/v1/campaigns/dispatch": result = self.service.dispatch_campaigns(merchant, data.get("limit", 100))
            elif path.startswith("/v1/guests/") and path.endswith("/suppress"): result = self.service.suppress(merchant, path.split("/")[3], data["channel"], data.get("reason", "customer_opt_out"))
            else: return self._send(404, {"error": "not_found"})
            self._send(200, result)
        except (KeyError, ValueError, SquareError) as error: self._send(400, {"error": str(error)})
        except Exception: self._send(500, {"error": "internal_error"})
    def do_GET(self):
        parsed = urlparse(self.path); path, merchant = parsed.path, self._merchant()
        if path == "/health": return self._send(200, {"status": "ok"})
        if path == "/v1/integrations/square/callback":
            query = parse_qs(parsed.query)
            try: return self._send(200, self.square.callback(query["code"][0], query["state"][0]))
            except (KeyError, SquareError) as error: return self._send(400, {"error": str(error)})
        if not merchant: return self._send(401, {"error": "unauthorized"})
        try:
            if path.startswith("/v1/guests/"): result = self.service.guest_profile(merchant, path.split("/")[3])
            elif path == "/v1/campaigns": result = self.service.list_campaigns(merchant)
            elif path == "/v1/metrics": result = self.service.metrics(merchant)
            elif path == "/v1/dashboard/invoices": result = self.service.invoice_dashboard(merchant)
            elif path == "/v1/dashboard/products": result = self.service.product_dashboard(merchant)
            elif path.startswith("/v1/products/") and path.endswith("/history"): result = self.service.product_history(merchant, path.split("/")[3])
            elif path == "/v1/dashboard/behaviors": result = self.service.behavior_dashboard(merchant)
            elif path == "/v1/dashboard/predictions": result = self.service.prediction_dashboard(merchant)
            elif path == "/v1/dashboard/recipes": result = self.service.recipe_dashboard(merchant)
            elif path == "/v1/integrations/square/status": result = self.square.status(merchant)
            else: return self._send(404, {"error": "not_found"})
            self._send(200, result)
        except KeyError as error: self._send(404, {"error": str(error)})
    def log_message(self, *_): pass
    def _process_resend(self, event):
        try: self.service.receive_email(self.resend.normalize(event))
        except Exception as error: print(f"Resend email processing failed: {error}", flush=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    db_path = args.db or os.getenv("ORBIT_DB_PATH") or "/tmp/orbit/orbit.db"
    storage_path = os.getenv("ORBIT_STORAGE_DIR") or str(Path(db_path).parent / "documents")
    Handler.service = OrbitService(Database(db_path), OpenAIInvoiceExtractor(), storage_path, OpenAIBehaviorPredictor(), MessageDelivery())
    Handler.square = SquareIntegration(Handler.service.db, Handler.service)
    square_worker_stop = threading.Event()
    threading.Thread(target=Handler.square.worker_loop, args=(square_worker_stop,), daemon=True).start()
    resend_key, resend_secret = os.getenv("RESEND_API_KEY"), os.getenv("RESEND_WEBHOOK_SECRET")
    Handler.resend = ResendInboundClient(resend_key, resend_secret) if resend_key and resend_secret else None
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", args.port))), Handler).serve_forever()

if __name__ == "__main__": main()
