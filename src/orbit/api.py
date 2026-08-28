import argparse
import html
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
from .demo import BehaviorDemoSeeder, DemoSeedError

class Handler(BaseHTTPRequestHandler):
    service = None
    resend = None
    square = None
    def _send(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
    def _send_html(self, status, body):
        payload = body.encode()
        self.send_response(status); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"); self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload)
    def _enrollment_html(self, slug, page):
        merchant = html.escape(page["merchant_name"]); headline = html.escape(page["headline"])
        offer = page["offer"]; label = f"{offer['discount_value']}% off" if offer["discount_type"] == "percent" else f"${offer['discount_value']/100:.2f} off"
        terms = html.escape(offer["offer_terms"]); terms_version = html.escape(page["terms_version"])
        endpoint = f"/v1/public/enroll/{slug}/submit"
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{merchant} offer</title><style>
        :root{{font-family:Inter,ui-sans-serif,system-ui,sans-serif;color:#17211b;background:#f4f7f3}}*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px}}main{{width:min(100%,520px);background:#fff;border:1px solid #dce5dc;border-radius:24px;padding:32px;box-shadow:0 18px 55px #18351c18}}.brand{{font-weight:800;color:#247a3c;letter-spacing:.08em;text-transform:uppercase;font-size:.78rem}}h1{{font-size:2rem;line-height:1.08;margin:12px 0}}.offer{{font-size:1.35rem;font-weight:750;color:#247a3c;margin:18px 0}}label{{display:block;font-weight:650;margin:18px 0 8px}}input[type=tel]{{width:100%;font:inherit;padding:14px;border:1px solid #b8c6ba;border-radius:12px}}.consent{{display:flex;align-items:flex-start;gap:10px;font-size:.9rem;line-height:1.45;font-weight:400}}.consent input{{margin-top:4px;flex:none}}button{{width:100%;border:0;border-radius:12px;background:#247a3c;color:white;font:inherit;font-weight:750;padding:15px;margin-top:20px;cursor:pointer}}button:disabled{{opacity:.6}}small{{display:block;color:#647067;line-height:1.45;margin-top:12px}}a{{color:#195f31}}#result{{margin-top:16px;padding:12px;border-radius:10px;display:none}}.ok{{display:block!important;background:#edf8ef;color:#175c2c}}.error{{display:block!important;background:#fff0ef;color:#9b2c25}}</style></head><body><main>
        <div class="brand">{merchant}</div><h1>{headline}</h1><div class="offer">Get {html.escape(label)} your next purchase</div><form id="join"><label for="phone">Mobile phone number</label><input id="phone" type="tel" inputmode="tel" autocomplete="tel" placeholder="+1 555 123 4567" required><p class="consent">By entering your phone number and selecting “Text me the promo code,” you expressly agree to receive recurring marketing and promotional text messages from {merchant}, sent using OrbitGuest, including this welcome offer. Message frequency varies. Message and data rates may apply. Reply HELP for help or STOP to opt out. Consent is not a condition of purchase. You also accept OrbitGuest’s <a href="/terms" target="_blank" rel="noopener">Terms</a> and acknowledge its <a href="/privacy" target="_blank" rel="noopener">Privacy Policy</a>.</p><small>Your mobile information will not be sold or shared with third parties for promotional or marketing purposes. {terms}</small><button id="submit" type="submit">Text me the promo code</button></form><div id="result" role="status"></div>
        <script>const form=document.getElementById('join'),result=document.getElementById('result'),button=document.getElementById('submit');form.addEventListener('submit',async(e)=>{{e.preventDefault();button.disabled=true;result.className='';result.textContent='';const claim=new URLSearchParams(location.search).get('claim_token');try{{const response=await fetch({json.dumps(endpoint)},{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{phone:document.getElementById('phone').value,accept_terms:true,sms_consent:true,terms_version:{json.dumps(terms_version)},claim_token:claim||undefined}})}});const data=await response.json();if(!response.ok)throw new Error(data.error||'Unable to process request');result.className='ok';result.textContent=data.duplicate?'Your offer was already requested. Check your messages.':'Your promo code is on its way.';form.hidden=true}}catch(error){{result.className='error';result.textContent=error.message;button.disabled=false}}}});</script></main></body></html>"""

    def _policy_html(self, policy):
        if policy == "privacy":
            title = "OrbitGuest Privacy Policy"
            content = """<p>OrbitGuest processes contact information for restaurants that use OrbitGuest to provide requested offers and, when separately authorized, recurring marketing messages.</p><h2>Mobile information</h2><p>Mobile information will not be sold or shared with third parties for promotional or marketing purposes. We may share it only with service providers that deliver OrbitGuest’s services, when legally required, or with the customer’s direction.</p><h2>Choices</h2><p>Reply STOP to stop marketing texts. Reply HELP for help. Contact privacy@orbitguest.com for privacy requests.</p>"""
        else:
            title = "OrbitGuest Terms"
            content = """<p>These terms govern use of OrbitGuest enrollment pages. Restaurant offers are subject to the terms displayed on the applicable restaurant page.</p><h2>Text messages</h2><p>Selecting a clearly labeled text-message request button after entering a mobile number expressly requests and consents to the disclosed messages. Consent is not a condition of purchase. Message frequency varies. Message and data rates may apply. Reply HELP for help or STOP to opt out.</p><h2>Contact</h2><p>Questions may be sent to support@orbitguest.com.</p>"""
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>body{{font:16px/1.6 system-ui,sans-serif;color:#17211b;max-width:760px;margin:40px auto;padding:0 20px}}h1,h2{{line-height:1.2}}a{{color:#195f31}}</style></head><body><main><h1>{title}</h1><p><strong>Effective date:</strong> August 27, 2026</p>{content}<p><a href="/">OrbitGuest</a></p></main></body></html>"""
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
                if data.get("type") == "email.received":
                    threading.Thread(target=self._process_resend, args=(data,), daemon=True).start()
                    return self._send(202, {"status": "accepted"})
                event_data = data.get("data") or {}
                message_id = event_data.get("email_id") or event_data.get("id")
                if not message_id: return self._send(200, {"status": "ignored"})
                return self._send(200, self.service.record_provider_event("resend", self.headers.get("svix-id", data.get("id", message_id)), data.get("type", "unknown"), message_id, event_data.get("created_at"), event_data))
            if path == "/v1/webhooks/telnyx":
                headers = {key.lower(): value for key, value in self.headers.items()}
                if not self.service.delivery or not self.service.delivery.verify_telnyx(raw, headers):
                    return self._send(401, {"error": "invalid Telnyx signature"})
                envelope, payload = data.get("data") or {}, (data.get("data") or {}).get("payload") or {}
                event_type, event_id = envelope.get("event_type", "unknown"), envelope.get("id") or payload.get("id")
                if event_type == "message.received":
                    sender = (payload.get("from") or {}).get("phone_number")
                    return self._send(200, self.service.handle_inbound_sms({"from": sender, "text": payload.get("text", "")}))
                if not event_id or not payload.get("id"): return self._send(200, {"status": "ignored"})
                if event_type == "message.finalized":
                    statuses = [entry.get("status") for entry in payload.get("to", [])]
                    event_type = "message.delivered" if "delivered" in statuses else "message.failed"
                return self._send(200, self.service.record_provider_event("telnyx", event_id, event_type, payload.get("id"), envelope.get("occurred_at"), payload))
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
            if path == "/v1/identity/claim":
                return self._send(200, self.service.accept_identity_claim(data.get("claim_token"), data))
            if path.startswith("/v1/public/enroll/"):
                parts = path.strip("/").split("/")
                if len(parts) == 5 and parts[4] == "submit":
                    data["_request_ip"] = self.client_address[0]
                    return self._send(200, self.service.enroll_in_offer(parts[3], data))
            if path == "/v1/merchants": return self._send(201, self.service.create_merchant(data["name"]))
            merchant = self._merchant()
            if not merchant: return self._send(401, {"error": "unauthorized"})
            if path == "/v1/guests/identify": result = self.service.capture_identity(merchant, data)
            elif path == "/v1/identity/claims": result = self.service.create_identity_claim(merchant, data)
            elif path == "/v1/webhooks/pos/orders": result = self.service.ingest_order(merchant, data)
            elif path == "/v1/pos/connections": result = self.service.create_pos_connection(merchant, data)
            elif path == "/v1/integrations/square/authorize": result = self.square.authorize(merchant)
            elif path == "/v1/integrations/square/sync": result = self.square.historical_sync(merchant, data["begin_at"], data.get("end_at"))
            elif path == "/v1/integrations/square/locations/sync": result = self.square.sync_locations(merchant)
            elif path == "/v1/integrations/square/catalog/sync": result = self.square.sync_catalog(merchant)
            elif path.startswith("/v1/integrations/square/events/") and path.endswith("/retry"): result = self.square.retry_event(merchant, path.split("/")[5])
            elif path == "/v1/menu/items": result = self.service.upsert_menu_item(merchant, data)
            elif path == "/v1/recipes/links": result = self.service.link_recipe(merchant, data)
            elif path == "/v1/recipes/proposals": result = self.service.propose_recipes(merchant)
            elif path.startswith("/v1/recipes/proposals/") and path.endswith("/review"): result = self.service.review_recipe_proposal(merchant, path.split("/")[4], data)
            elif path == "/v1/inventory/conversions": result = self.service.set_unit_conversion(merchant, data)
            elif path == "/v1/inventory/adjustments": result = self.service.adjust_inventory(merchant, data)
            elif path == "/v1/campaign-policy": result = self.service.set_campaign_policy(merchant, data)
            elif path == "/v1/evaluations/backtest": result = self.service.run_backtest(merchant, data)
            elif path == "/v1/evaluations/messages": result = self.service.run_message_evaluation(merchant)
            elif path.startswith("/v1/campaigns/") and path.endswith("/approve"): result = self.service.approve_campaign(merchant, path.split("/")[3], data.get("approved_by", "restaurant_manager"))
            elif path == "/v1/operations/state": result = self.service.update_operational_state(merchant, data)
            elif path == "/v1/offers": result = self.service.configure_offer(merchant, data)
            elif path == "/v1/offers/redeem": result = self.service.redeem_offer(merchant, data)
            elif path == "/v1/behavior/context": result = self.service.record_behavior_context(merchant, data)
            elif path == "/v1/behavior/interactions": result = self.service.record_behavior_interaction(merchant, data)
            elif path == "/v1/webhooks/invoices": result = self.service.ingest_invoice(merchant, data)
            elif path.startswith("/v1/campaigns/") and path.endswith("/sent"): result = self.service.mark_sent(merchant, path.split("/")[3])
            elif path == "/v1/engine/run": result = self.service.run_behavior_engine(merchant)
            elif path == "/v1/campaigns/dispatch": result = self.service.dispatch_campaigns(merchant, data.get("limit", 100))
            elif path == "/v1/messages/events": result = self.service.record_message_event(merchant, data)
            elif path == "/v1/messaging/settings": result = self.service.update_messaging_settings(merchant, data)
            elif path.startswith("/v1/messaging/dead-letters/") and path.endswith("/retry"): result = self.service.retry_dead_letter(merchant, path.split("/")[4])
            elif path == "/v1/demo/behavior/seed": result = BehaviorDemoSeeder(self.service).seed(merchant)
            elif path.startswith("/v1/guests/") and path.endswith("/suppress"): result = self.service.suppress(merchant, path.split("/")[3], data["channel"], data.get("reason", "customer_opt_out"))
            else: return self._send(404, {"error": "not_found"})
            self._send(200, result)
        except (KeyError, ValueError, SquareError, DemoSeedError) as error: self._send(400, {"error": str(error)})
        except Exception: self._send(500, {"error": "internal_error"})
    def do_GET(self):
        parsed = urlparse(self.path); path, merchant = parsed.path, self._merchant()
        if path == "/health": return self._send(200, {"status": "ok"})
        if path in ("/privacy", "/terms"): return self._send_html(200, self._policy_html(path[1:]))
        if path.startswith("/join/"):
            slug = path.strip("/").split("/")[1]
            try: return self._send_html(200, self._enrollment_html(slug, self.service.public_enrollment_page(slug)))
            except KeyError: return self._send_html(404, "<!doctype html><title>Offer unavailable</title><p>This offer is not currently available.</p>")
        if path.startswith("/v1/public/enroll/"):
            try: return self._send(200, self.service.public_enrollment_page(path.strip("/").split("/")[3]))
            except KeyError as error: return self._send(404, {"error": str(error)})
        if path == "/v1/integrations/square/callback":
            query = parse_qs(parsed.query)
            try: return self._send(200, self.square.callback(query["code"][0], query["state"][0]))
            except (KeyError, SquareError) as error: return self._send(400, {"error": str(error)})
        if not merchant: return self._send(401, {"error": "unauthorized"})
        try:
            if path.startswith("/v1/guests/"): result = self.service.guest_profile(merchant, path.split("/")[3])
            elif path == "/v1/campaigns": result = self.service.list_campaigns(merchant)
            elif path == "/v1/metrics": result = self.service.metrics(merchant)
            elif path == "/v1/offers": result = self.service.offer_dashboard(merchant)
            elif path == "/v1/dashboard/invoices": result = self.service.invoice_dashboard(merchant)
            elif path == "/v1/dashboard/products": result = self.service.product_dashboard(merchant)
            elif path.startswith("/v1/products/") and path.endswith("/history"): result = self.service.product_history(merchant, path.split("/")[3])
            elif path == "/v1/dashboard/behaviors": result = self.service.behavior_dashboard(merchant)
            elif path == "/v1/dashboard/psychology": result = self.service.psychology_dashboard(merchant)
            elif path == "/v1/dashboard/predictions": result = self.service.prediction_dashboard(merchant)
            elif path == "/v1/dashboard/recipes": result = self.service.recipe_dashboard(merchant)
            elif path == "/v1/dashboard/inventory": result = self.service.inventory_dashboard(merchant)
            elif path == "/v1/dashboard/evaluations": result = self.service.evaluation_dashboard(merchant)
            elif path == "/v1/messaging/settings": result = self.service.messaging_settings(merchant)
            elif path == "/v1/messaging/dead-letters": result = self.service.dead_letters(merchant)
            elif path == "/v1/operations/state": result = self.service.operational_dashboard(merchant)
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
    behavior_worker_stop = threading.Event()
    behavior_interval = max(60, int(os.getenv("ORBIT_BEHAVIOR_INTERVAL_SECONDS", "900")))
    threading.Thread(target=Handler.service.behavior_worker_loop, args=(behavior_worker_stop, behavior_interval), daemon=True).start()
    message_worker_stop = threading.Event()
    message_interval = max(10, int(os.getenv("ORBIT_MESSAGE_INTERVAL_SECONDS", "30")))
    threading.Thread(target=Handler.service.message_worker_loop, args=(message_worker_stop, message_interval), daemon=True).start()
    resend_key, resend_secret = os.getenv("RESEND_API_KEY"), os.getenv("RESEND_WEBHOOK_SECRET")
    Handler.resend = ResendInboundClient(resend_key, resend_secret) if resend_key and resend_secret else None
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", args.port))), Handler).serve_forever()

if __name__ == "__main__": main()
