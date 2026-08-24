import tempfile
import unittest
import base64
from pathlib import Path
from datetime import datetime, timezone
from orbit.db import Database
from orbit.service import OrbitService
from orbit.resend import ResendInboundClient
from orbit.square import SquareIntegration, SquareClient
import hashlib
import hmac
import json
import time
import os
from unittest.mock import patch

class FakeExtractor:
    def extract(self, content, content_type, filename):
        return {"vendor": "Fresh Foods", "invoice_number": "INV-42", "invoice_date": "2026-08-22", "currency": "USD", "subtotal_cents": 9000, "tax_cents": 1000, "total_cents": 10000, "confidence": .97, "items": [{"sku": "RIB-1", "description": "Baby Back Ribs", "quantity": 40, "unit": "case", "unit_price_cents": 225, "line_total_cents": 9000}]}

class SequenceExtractor:
    def __init__(self, outputs): self.outputs = iter(outputs)
    def extract(self, *_): return next(self.outputs)

class FakePredictor:
    def predict(self, context):
        favorite = context["favorite_items"][0]["display_name"]
        return [{"type": "next_visit", "item": favorite, "score": .91, "reason": "Recurring two-week visit rhythm", "send_at": "2026-08-01T10:00:00+00:00", "subject": "Your usual", "message": f"Your {favorite} is available."}]

class FakeDelivery:
    def __init__(self): self.sent = []
    def send(self, channel, to, subject, body):
        self.sent.append((channel, to, subject, body)); return "provider-message-1"

class FakeResend(ResendInboundClient):
    def _json(self, path):
        if path.endswith("/attachments"):
            return {"data": [{"id": "att-1", "filename": "invoice.pdf", "content_type": "application/pdf", "content_base64": base64.b64encode(b"%PDF-live").decode()}]}
        return {"data": {"message_id": "mail-live", "from": "vendor@example.com", "to": ["restaurant@invoices.orbitguest.com"], "subject": "Invoice", "attachments": [{"id": "att-1", "filename": "invoice.pdf", "content_type": "application/pdf"}]}}

class OrbitFlowTest(unittest.TestCase):
    def setUp(self):
        self.file = tempfile.NamedTemporaryFile(suffix=".db")
        self.documents = tempfile.TemporaryDirectory()
        self.service = OrbitService(Database(self.file.name), FakeExtractor(), self.documents.name)
        merchant = self.service.create_merchant("Smokehouse")
        self.merchant = merchant["id"]
        self.key = merchant["api_key"]

    def tearDown(self):
        self.file.close()
        self.documents.cleanup()

    def order(self, external, when="2026-08-10T18:00:00+00:00"):
        return self.service.ingest_order(self.merchant, {"external_id": external, "source": "square", "payment_fingerprint": "tok_guest_1", "occurred_at": when, "total_cents": 3200, "items": [{"name": "Smoked Ribs", "quantity": 1, "unit_price_cents": 3200}]})

    def test_closed_loop_from_pos_to_invoice_campaign_and_revenue(self):
        self.order("order-1"); self.order("order-2")
        profile = self.service.capture_identity(self.merchant, {"payment_fingerprint": "tok_guest_1", "name": "Jamie", "phone": "+15550001111", "terms": {"accepted": True, "version": "terms-v1"}, "consent": {"sms": {"status": "granted", "disclosure_version": "v1"}}})
        self.assertEqual(profile["visits"], 2)
        invoice = self.service.ingest_invoice(self.merchant, {"external_id": "invoice-1", "vendor": "Foods Co", "invoice_date": "2026-08-20T10:00:00+00:00", "total_cents": 10000, "items": [{"ingredient": "Ribs", "quantity": 40, "unit": "case", "unit_cost_cents": 250}]})
        self.assertEqual(invoice["campaigns_created"], 1)
        campaign = self.service.list_campaigns(self.merchant)[0]
        self.assertEqual(campaign["channel"], "sms")
        inventory_prediction = self.service.prediction_dashboard(self.merchant)["predictions"][0]
        self.assertEqual(inventory_prediction["prediction_type"], "inventory_match")
        self.assertEqual(inventory_prediction["status"], "eligible")
        self.service.mark_sent(self.merchant, campaign["id"])
        self.order("order-3", datetime.now(timezone.utc).isoformat())
        self.assertEqual(self.service.metrics(self.merchant)["revenue_cents"], 3200)

    def test_opted_out_guest_is_not_messaged(self):
        self.order("one"); self.order("two")
        self.service.capture_identity(self.merchant, {"payment_fingerprint": "tok_guest_1", "phone": "+1555", "terms": {"accepted": True, "version": "terms-v1"}, "consent": {"sms": {"status": "denied", "disclosure_version": "v1"}}})
        result = self.service.ingest_invoice(self.merchant, {"external_id": "inv", "vendor": "V", "invoice_date": "2026-08-20", "total_cents": 1, "items": [{"ingredient": "Ribs", "quantity": 1, "unit": "case", "unit_cost_cents": 1}]})
        self.assertEqual(result["campaigns_created"], 0)

    def test_order_webhook_is_idempotent_and_raw_cards_rejected(self):
        self.assertFalse(self.order("same")["duplicate"])
        self.assertTrue(self.order("same")["duplicate"])
        with self.assertRaises(ValueError):
            self.service.ingest_order(self.merchant, {"external_id": "bad", "source": "x", "occurred_at": "now", "total_cents": 1, "card_number": "4111"})

    def test_supplier_email_creates_invoice_tracking_dashboard(self):
        with self.service.db.connect() as connection:
            address = connection.execute("SELECT inbound_alias FROM merchants WHERE id=?", (self.merchant,)).fetchone()["inbound_alias"]
        payload = {"message_id": "mail-42", "sender": "billing@fresh.test", "recipient": address, "subject": "Invoice", "attachments": [{"filename": "invoice.pdf", "content_type": "application/pdf", "content_base64": base64.b64encode(b"%PDF-test").decode()}]}
        result = self.service.receive_email(payload)
        self.assertEqual(result["status"], "processed")
        self.assertTrue(self.service.receive_email(payload)["duplicate"])
        dashboard = self.service.invoice_dashboard(self.merchant)
        self.assertEqual(dashboard["summary"]["invoice_count"], 1)
        self.assertEqual(dashboard["invoices"][0]["items"][0]["description"], "Baby Back Ribs")

    def test_database_creates_missing_parent_directory(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "new" / "nested" / "orbit.db"
            Database(str(path))
            self.assertTrue(path.is_file())

    def test_resend_svix_signature_verification(self):
        secret_bytes = b"test-webhook-secret"
        secret = "whsec_" + base64.b64encode(secret_bytes).decode()
        client = ResendInboundClient("re_test", secret)
        body = json.dumps({"type": "email.received"}).encode()
        timestamp, message_id = str(int(time.time())), "msg_test"
        signature = base64.b64encode(hmac.new(secret_bytes, f"{message_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()).decode()
        headers = {"svix-id": message_id, "svix-timestamp": timestamp, "svix-signature": f"v1,{signature}"}
        self.assertTrue(client.verify(body, headers))
        self.assertFalse(client.verify(body + b" ", headers))

    def test_resend_event_is_normalized_with_downloaded_attachment(self):
        email = FakeResend("re_test", "secret").normalize({"type": "email.received", "data": {"email_id": "email-1"}})
        self.assertEqual(email["recipient"], "restaurant@invoices.orbitguest.com")
        self.assertEqual(base64.b64decode(email["attachments"][0]["content_base64"]), b"%PDF-live")

    def test_resend_requests_use_explicit_user_agent(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return b'{}'
        with patch("urllib.request.urlopen", return_value=Response()) as urlopen:
            ResendInboundClient("re_test", "secret")._json("/emails/receiving/test")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), ResendInboundClient.USER_AGENT)

    def test_product_history_keeps_old_invoice_and_new_current_value(self):
        base = {"vendor": "Fresh Foods", "currency": "USD", "subtotal_cents": 100, "tax_cents": 0, "total_cents": 100, "confidence": .99}
        item = {"sku": "RIB-1", "description": "Baby Back Ribs", "quantity": 1, "unit": "case", "line_total_cents": 100}
        outputs = [
            {**base, "invoice_number": "NEW", "invoice_date": "2026-08-20", "items": [{**item, "unit_price_cents": 100}]},
            {**base, "invoice_number": "OLD", "invoice_date": "2025-01-10", "items": [{**item, "unit_price_cents": 70}]},
            {**base, "invoice_number": "LATEST", "invoice_date": "2026-09-01", "items": [{**item, "unit_price_cents": 120}, {**item, "sku": "SAUCE-1", "description": "BBQ Sauce", "unit_price_cents": 50, "line_total_cents": 50}]},
        ]
        self.service.extractor = SequenceExtractor(outputs)
        with self.service.db.connect() as c: address = c.execute("SELECT inbound_alias FROM merchants WHERE id=?", (self.merchant,)).fetchone()["inbound_alias"]
        def receive(message):
            return self.service.receive_email({"message_id": message, "sender": "vendor@test", "recipient": address, "attachments": [{"filename": f"{message}.pdf", "content_type": "application/pdf", "content_base64": base64.b64encode(message.encode()).decode()}]})
        receive("new"); receive("old"); receive("latest")
        products = self.service.product_dashboard(self.merchant)["products"]
        self.assertEqual(len(products), 2)
        ribs = next(product for product in products if product["sku"] == "RIB-1")
        self.assertEqual(ribs["unit_price_cents"], 120)
        self.assertEqual(ribs["version_count"], 3)
        history = self.service.product_history(self.merchant, ribs["id"])["history"]
        self.assertEqual([row["invoice_number"] for row in history], ["LATEST", "NEW", "OLD"])
        self.assertEqual(sum(row["is_current"] for row in history), 1)

    def test_pos_automatically_profiles_then_permission_activates_identity(self):
        self.order("auto-1", "2026-07-01T18:00:00+00:00")
        self.order("auto-2", "2026-07-15T18:00:00+00:00")
        with self.service.db.connect() as c:
            guest = c.execute("SELECT id,profile_status,phone FROM guests WHERE merchant_id=? AND payment_fingerprint='tok_guest_1'", (self.merchant,)).fetchone()
        self.assertEqual(guest["profile_status"], "anonymous")
        self.assertIsNone(guest["phone"])
        behavior = self.service.behavior_dashboard(self.merchant)["customers"][0]
        self.assertEqual(behavior["visit_count"], 2)
        self.assertEqual(behavior["favorite_item"], "Smoked Ribs")
        with self.assertRaises(ValueError):
            self.service.capture_identity(self.merchant, {"payment_fingerprint": "tok_guest_1", "phone": "+1555"})
        self.service.capture_identity(self.merchant, {"payment_fingerprint": "tok_guest_1", "phone": "+1555", "terms": {"accepted": True, "version": "terms-v1", "source": "checkout"}, "consent": {"sms": {"status": "granted", "disclosure_version": "sms-v1"}}})
        self.service.run_behavior_engine(self.merchant)
        prediction = self.service.prediction_dashboard(self.merchant)["predictions"][0]
        self.assertEqual(prediction["recommended_channel"], "sms")

    def test_configurable_pos_connection_normalizes_provider_payload(self):
        connection = self.service.create_pos_connection(self.merchant, {"provider": "custom-pos", "external_location_id": "location-1", "mapping": {"external_id": "sale.id", "occurred_at": "sale.time", "total_cents": "sale.total", "payment_fingerprint": "buyer.token", "items": {"path": "sale.lines", "name": "label", "quantity": "qty", "unit_price_cents": "price"}}})
        payload = json.dumps({"sale": {"id": "custom-1", "time": "2026-08-01T12:00:00+00:00", "total": 1800, "lines": [{"label": "Chicken Bowl", "qty": 1, "price": 1800}]}, "buyer": {"token": "custom-token"}}).encode()
        result = self.service.ingest_pos_webhook(connection["id"], payload, connection["webhook_secret"])
        self.assertFalse(result["duplicate"])
        customer = self.service.behavior_dashboard(self.merchant)["customers"][0]
        self.assertEqual(customer["favorite_item"], "Chicken Bowl")

    def test_recipe_mapping_openai_prediction_delivery_and_suppression(self):
        self.order("engine-1", "2026-06-01T18:00:00+00:00"); self.order("engine-2", "2026-06-15T18:00:00+00:00")
        profile = self.service.capture_identity(self.merchant, {"payment_fingerprint": "tok_guest_1", "phone": "+15550001", "terms": {"accepted": True, "version": "v1"}, "consent": {"sms": {"status": "granted", "disclosure_version": "v1"}}})
        self.service.ingest_invoice(self.merchant, {"external_id": "recipe-invoice", "vendor": "Foods", "invoice_date": "2026-07-01", "total_cents": 100, "items": [{"sku": "RIB", "ingredient": "Ribs", "quantity": 10, "unit": "case", "unit_cost_cents": 10}]})
        product = self.service.product_dashboard(self.merchant)["products"][0]
        menu = self.service.upsert_menu_item(self.merchant, {"external_id": "menu-ribs", "name": "Smoked Ribs", "price_cents": 3200})
        self.service.link_recipe(self.merchant, {"product_id": product["id"], "menu_item_id": menu["id"], "quantity_required": .5, "unit": "case"})
        self.assertEqual(len(self.service.recipe_dashboard(self.merchant)["recipe_links"]), 1)
        delivery = FakeDelivery(); self.service.predictor = FakePredictor(); self.service.delivery = delivery
        self.service.run_behavior_engine(self.merchant)
        dispatched = self.service.dispatch_campaigns(self.merchant)
        self.assertEqual(dispatched["messages"][0]["status"], "sent")
        self.assertEqual(delivery.sent[0][0], "sms")
        self.service.suppress(self.merchant, profile["id"], "sms")
        with self.service.db.connect() as c: self.assertEqual(c.execute("SELECT COUNT(*) count FROM suppressions").fetchone()["count"], 1)

    def test_square_signed_webhook_is_tenant_mapped_and_idempotent(self):
        with patch.dict(os.environ, {"SQUARE_WEBHOOK_SIGNATURE_KEY": "square-signing-key", "SQUARE_WEBHOOK_URL": "https://orbit.test/v1/webhooks/square"}):
            square = SquareIntegration(self.service.db, self.service, cipher=object())
        stamp = "2026-08-01T00:00:00+00:00"
        with self.service.db.connect() as c:
            c.execute("INSERT INTO square_installations VALUES(?,?,?,?,?,?,?,?,?)", ("install-1", self.merchant, "square-merchant-1", "token", None, None, "active", stamp, stamp))
            c.execute("INSERT INTO square_locations VALUES(?,?,?,?,?,?,?,?,?)", ("location-1", "install-1", self.merchant, "SQ-LOC-1", "Downtown", "UTC", "active", stamp, stamp))
        event = {"event_id": "square-event-1", "type": "order.created", "merchant_id": "square-merchant-1", "data": {"object": {"order": {"id": "SQ-ORDER-1", "location_id": "SQ-LOC-1", "customer_id": "SQ-CUSTOMER-1", "created_at": "2026-08-01T18:00:00+00:00", "total_money": {"amount": 2400, "currency": "USD"}, "line_items": [{"name": "Rib Plate", "quantity": "1", "total_money": {"amount": 2400, "currency": "USD"}}]}}}}
        raw = json.dumps(event, separators=(",", ":")).encode()
        signature = base64.b64encode(hmac.new(b"square-signing-key", b"https://orbit.test/v1/webhooks/square" + raw, hashlib.sha256).digest()).decode()
        self.assertTrue(square.verify_webhook(raw, signature))
        result = square.receive_webhook(raw)
        self.assertEqual(result["status"], "processed")
        self.assertTrue(square.receive_webhook(raw)["duplicate"])
        customer = self.service.behavior_dashboard(self.merchant)["customers"][0]
        self.assertEqual(customer["favorite_item"], "Rib Plate")
        self.assertEqual(customer["profile_status"], "anonymous")
        status = square.status(self.merchant)
        self.assertTrue(status["connected"])
        self.assertEqual(status["webhooks"]["total_events"], 1)
        with self.service.db.connect() as c: c.execute("UPDATE square_webhook_events SET status='dead' WHERE event_id='square-event-1'")
        self.assertEqual(square.retry_event(self.merchant, "square-event-1")["status"], "pending")
        square.process_pending()
        with self.service.db.connect() as c: self.assertEqual(c.execute("SELECT status FROM square_webhook_events WHERE event_id='square-event-1'").fetchone()["status"], "processed")

    def test_square_oauth_state_is_single_use_and_tokens_are_never_returned(self):
        class FakeCipher:
            def encrypt(self, value): return "encrypted:" + value
            def decrypt(self, value): return value.removeprefix("encrypted:")
        with patch.dict(os.environ, {"SQUARE_APPLICATION_ID": "app-id", "SQUARE_APPLICATION_SECRET": "app-secret", "PUBLIC_BASE_URL": "https://orbit.test", "SQUARE_ENVIRONMENT": "sandbox"}):
            square = SquareIntegration(self.service.db, self.service, cipher=FakeCipher())
            authorization = square.authorize(self.merchant)
            state = authorization["authorization_url"].split("state=")[1].split("&")[0]
            state = __import__("urllib.parse").parse.unquote(state)
            token = {"merchant_id": "sq-merchant", "access_token": "access", "refresh_token": "refresh", "expires_at": "2099-01-01T00:00:00+00:00"}
            with patch.object(SquareClient, "exchange_code", return_value=token), patch.object(SquareIntegration, "sync_locations", return_value={"locations": 1}):
                result = square.callback("code", state)
                self.assertEqual(result["status"], "connected")
                self.assertNotIn("access_token", result)
                with self.assertRaises(Exception): square.callback("code", state)
        with self.service.db.connect() as c:
            stored = c.execute("SELECT encrypted_access_token FROM square_installations WHERE merchant_id=?", (self.merchant,)).fetchone()
        self.assertEqual(stored["encrypted_access_token"], "encrypted:access")

if __name__ == "__main__": unittest.main()
