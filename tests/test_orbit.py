import tempfile
import unittest
import base64
from pathlib import Path
from datetime import datetime, timezone
from orbit.db import Database
from orbit.service import OrbitService
from orbit.resend import ResendInboundClient
from orbit.square import SquareIntegration, SquareClient
from orbit.demo import BehaviorDemoSeeder, DemoSeedError
from orbit.prediction import OpenAIBehaviorPredictor
from orbit.messaging import MessageDelivery, DeliveryError
import hashlib
import hmac
import json
import time
import os
import sqlite3
import urllib.error
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
        return [{"type": "next_visit", "item": favorite, "score": .91, "reason": "Recurring two-week visit rhythm", "send_at": datetime.now(timezone.utc).isoformat(), "subject": "Your usual", "message": f"Your {favorite} is available."}]

class CapturingPredictor(FakePredictor):
    def __init__(self): self.contexts = []
    def predict(self, context):
        self.contexts.append(context)
        return super().predict(context)

class FakeDelivery:
    def __init__(self): self.sent = []
    def send(self, channel, to, subject, body, idempotency_key=None):
        self.sent.append((channel, to, subject, body)); return "provider-message-1"

class FailingDelivery:
    def __init__(self, retryable=True): self.retryable, self.calls = retryable, 0
    def send(self, *args, **kwargs):
        self.calls += 1; raise DeliveryError("provider unavailable", self.retryable)

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

    def queued_campaign(self, guest_id, campaign_id="campaign-test"):
        stamp = datetime.now(timezone.utc).isoformat()
        with self.service.db.connect() as connection:
            connection.execute("""INSERT INTO campaigns(id,merchant_id,guest_id,channel,trigger_type,trigger_ref,subject,body,status,scheduled_at,sent_at,created_at,action,control_group,prediction_window_end,eligibility_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (campaign_id, self.merchant, guest_id, "sms", "next_visit", "prediction-test", "Your favorite", "Your favorite is available. Reply STOP to opt out.", "queued", stamp, None, stamp, "send_sms", 0, None, "{}"))
        return campaign_id

    def test_closed_loop_from_pos_to_invoice_campaign_and_revenue(self):
        for index, stamp in enumerate(("2026-05-18", "2026-06-01", "2026-06-15", "2026-06-29", "2026-07-13", "2026-07-27", "2026-08-10")):
            self.order(f"order-{index}", f"{stamp}T18:00:00+00:00")
        profile = self.service.capture_identity(self.merchant, {"payment_fingerprint": "tok_guest_1", "name": "Jamie", "phone": "+15550001111", "terms": {"accepted": True, "version": "terms-v1"}, "consent": {"sms": {"status": "granted", "disclosure_version": "v1"}}})
        self.assertEqual(profile["visits"], 7)
        invoice = self.service.ingest_invoice(self.merchant, {"external_id": "invoice-1", "vendor": "Foods Co", "invoice_date": "2026-08-20T10:00:00+00:00", "total_cents": 10000, "items": [{"ingredient": "Ribs", "quantity": 40, "unit": "case", "unit_cost_cents": 250}]})
        self.assertEqual(invoice["campaigns_created"], 0)
        product = self.service.product_dashboard(self.merchant)["products"][0]
        menu = self.service.upsert_menu_item(self.merchant, {"external_id": "ribs", "name": "Smoked Ribs", "price_cents": 3200})
        self.service.link_recipe(self.merchant, {"product_id": product["id"], "menu_item_id": menu["id"], "quantity_required": .5, "unit": "case"})
        self.service.predictor = FakePredictor(); self.service.run_behavior_engine(self.merchant)
        campaign = self.service.list_campaigns(self.merchant)[0]
        self.assertEqual(campaign["channel"], "sms")
        self.assertIsNotNone(campaign["psychology_strategy"])
        psychology_experiments = self.service.psychology_dashboard(self.merchant)["experiments"]
        self.assertEqual(psychology_experiments[0]["campaign_id"], campaign["id"])
        inventory_prediction = self.service.prediction_dashboard(self.merchant)["predictions"][0]
        self.assertEqual(inventory_prediction["prediction_type"], "next_visit")
        self.assertEqual(inventory_prediction["status"], "eligible")
        self.service.approve_campaign(self.merchant, campaign["id"])
        self.service.mark_sent(self.merchant, campaign["id"])
        self.order("order-return", datetime.now(timezone.utc).isoformat())
        self.assertEqual(self.service.metrics(self.merchant)["revenue_cents"], 3200)
        self.assertEqual(self.service.psychology_dashboard(self.merchant)["experiments"][0]["converted"], 1)

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

    def test_legacy_railway_database_adds_messaging_columns_before_provider_index(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.execute("""CREATE TABLE outbound_messages (
                id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
                guest_id TEXT NOT NULL, channel TEXT NOT NULL, recipient TEXT NOT NULL,
                provider_message_id TEXT, status TEXT NOT NULL, error TEXT,
                sent_at TEXT, created_at TEXT NOT NULL)""")
            connection.commit(); connection.close()
            database = Database(str(path))
            with database.connect() as migrated:
                columns = {row["name"] for row in migrated.execute("PRAGMA table_info(outbound_messages)")}
                indexes = {row["name"] for row in migrated.execute("PRAGMA index_list(outbound_messages)")}
            self.assertTrue({"provider", "attempts", "next_attempt_at", "last_event_at", "dead_lettered_at", "idempotency_key"}.issubset(columns))
            self.assertIn("idx_messages_provider", indexes)

    def test_existing_merchants_receive_collision_safe_offer_pages_on_startup(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "legacy-merchants.db")
            database = Database(path)
            with database.connect() as connection:
                connection.execute("INSERT INTO merchants VALUES(?,?,?,?,?)", ("legacy-one", "Legacy Cafe", "key-one", "legacy-one@invoices.test", "2026-01-01T00:00:00+00:00"))
                connection.execute("INSERT INTO merchants VALUES(?,?,?,?,?)", ("legacy-two", "Legacy Cafe", "key-two", "legacy-two@invoices.test", "2026-01-02T00:00:00+00:00"))
            Database(path)
            Database(path)  # The repair must remain idempotent across redeploys.
            with database.connect() as connection:
                pages = connection.execute("SELECT merchant_id,slug,terms_version FROM merchant_enrollment_pages ORDER BY merchant_id").fetchall()
                migration = connection.execute("SELECT 1 FROM orbit_migrations WHERE name='merchant_enrollment_page_backfill_v1'").fetchone()
            self.assertEqual(len(pages), 2)
            self.assertEqual(pages[0]["slug"], "legacy-cafe")
            self.assertTrue(pages[1]["slug"].startswith("legacy-cafe-"))
            self.assertNotEqual(pages[0]["slug"], pages[1]["slug"])
            self.assertTrue(all(page["terms_version"] == "orbit-offers-v1" for page in pages))
            self.assertIsNotNone(migration)
            dashboard = OrbitService(Database(path), storage_dir=self.documents.name).offer_dashboard("legacy-one")
            self.assertEqual(dashboard["page"]["slug"], "legacy-cafe")

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

    def test_order_claim_links_phone_to_exact_pos_profile_with_permission(self):
        self.order("claim-order", "2026-08-03T18:30:00+00:00")
        self.service.ingest_order(self.merchant, {"external_id": "different-customer", "source": "square", "payment_fingerprint": "other-token", "occurred_at": "2026-08-03T18:35:00+00:00", "total_cents": 1200, "items": [{"name": "Tea", "quantity": 1, "unit_price_cents": 1200}]})
        claim = self.service.create_identity_claim(self.merchant, {"source": "square", "external_order_id": "claim-order"})
        with self.assertRaises(ValueError): self.service.accept_identity_claim(claim["claim_token"], {"phone": "+15551234567", "terms": {"accepted": False, "version": "terms-2026-08"}})
        profile = self.service.accept_identity_claim(claim["claim_token"], {"phone": "+15551234567", "terms": {"accepted": True, "version": "terms-2026-08", "source": "digital_receipt"}, "consent": {"sms": {"status": "granted", "disclosure_version": "sms-2026-08", "source": "digital_receipt"}}})
        self.assertEqual(profile["phone"], "+15551234567")
        self.assertEqual(profile["permission_source"], "digital_receipt")
        with self.service.db.connect() as c:
            linked = c.execute("SELECT payment_fingerprint FROM guests WHERE id=?", (profile["id"],)).fetchone()
            other = c.execute("SELECT phone FROM guests WHERE merchant_id=? AND payment_fingerprint='other-token'", (self.merchant,)).fetchone()
            consent = c.execute("SELECT status FROM consents WHERE guest_id=? AND channel='sms' ORDER BY captured_at DESC", (profile["id"],)).fetchone()
        self.assertEqual(linked["payment_fingerprint"], "tok_guest_1")
        self.assertIsNone(other["phone"])
        self.assertEqual(consent["status"], "granted")
        with self.assertRaises(ValueError): self.service.accept_identity_claim(claim["claim_token"], {"phone": "+15550000000", "terms": {"accepted": True, "version": "v2"}})

    def test_restaurant_offer_link_sends_real_first_offer_and_claim_links_exact_pos_guest(self):
        self.order("offer-claim-order")
        claim = self.service.create_identity_claim(self.merchant, {"external_order_id": "offer-claim-order"})
        self.service.delivery = FakeDelivery()
        offer = self.service.configure_offer(self.merchant, {"name": "Welcome offer", "discount_type": "percent", "discount_value": 10, "promo_code": "WELCOME10", "offer_terms": "Valid on one purchase."})
        slug = offer["enrollment_url"].rsplit("/", 1)[-1]
        page = self.service.public_enrollment_page(slug)
        self.assertEqual(page["offer"]["discount_value"], 10)
        self.assertNotIn("promo_code", page["offer"])
        result = self.service.enroll_in_offer(slug, {"phone": "+15550101010", "accept_terms": True, "sms_consent": True, "terms_version": page["terms_version"], "claim_token": claim["claim_token"]})
        self.assertTrue(result["linked_to_pos"]); self.assertEqual(result["status"], "offer_sent")
        self.assertEqual(len(self.service.delivery.sent), 1)
        body = self.service.delivery.sent[0][3]
        self.assertIn("WELCOME10", body); self.assertIn("10% off", body); self.assertIn("STOP", body)
        delivered = self.service.record_provider_event("telnyx", "offer-delivered-event", "message.delivered", "provider-message-1", datetime.now(timezone.utc).isoformat(), {})
        self.assertEqual(delivered["status"], "delivered")
        duplicate = self.service.enroll_in_offer(slug, {"phone": "+15550101010", "accept_terms": True, "sms_consent": True, "terms_version": page["terms_version"]})
        self.assertTrue(duplicate["duplicate"]); self.assertEqual(len(self.service.delivery.sent), 1)
        with self.service.db.connect() as connection:
            order_guest = connection.execute("SELECT guest_id FROM orders WHERE external_id='offer-claim-order'").fetchone()["guest_id"]
            enrolled = connection.execute("SELECT contact_guest_id,linked_guest_id FROM offer_enrollments WHERE id=?", (result["enrollment_id"],)).fetchone()
        self.assertEqual(enrolled["contact_guest_id"], order_guest); self.assertEqual(enrolled["linked_guest_id"], order_guest)
        from orbit.api import Handler
        markup = Handler._enrollment_html(None, slug, page)
        self.assertIn('id="submit" type="submit">Text me the promo code</button>', markup); self.assertIn("/v1/public/enroll/", markup)
        self.assertNotIn("WELCOME10", markup)
        self.assertIn("By entering your phone number and selecting “Text me the promo code,” you expressly agree", markup)
        self.assertIn("recurring marketing and promotional text messages from Smokehouse", markup)
        self.assertIn("Message frequency varies", markup); self.assertIn("Reply HELP for help or STOP to opt out", markup)
        self.assertIn("Consent is not a condition of purchase", markup)
        self.assertIn("Your mobile information will not be sold or shared with third parties for promotional or marketing purposes", markup)
        self.assertIn('href="/privacy"', markup); self.assertIn('href="/terms"', markup)
        self.assertNotIn('type="checkbox"', markup)
        self.assertIn("accept_terms:true,sms_consent:true", markup)
        with self.assertRaises(ValueError):
            self.service.enroll_in_offer(slug, {"phone": "+15550999999", "accept_terms": False, "sms_consent": False, "terms_version": page["terms_version"]})
        with self.assertRaises(ValueError):
            self.service.enroll_in_offer(slug, {"phone": "+15550999999", "accept_terms": True, "sms_consent": False, "terms_version": page["terms_version"]})

    def test_offer_redemption_connects_generic_signup_to_exact_pos_identity(self):
        self.service.delivery = FakeDelivery()
        offer = self.service.configure_offer(self.merchant, {"discount_value": 10, "promo_code": "COFFEE10", "offer_terms": "One use."})
        slug = offer["enrollment_url"].rsplit("/", 1)[-1]; page = self.service.public_enrollment_page(slug)
        enrolled = self.service.enroll_in_offer(slug, {"phone": "+15550202020", "accept_terms": True, "sms_consent": True, "terms_version": page["terms_version"]})
        self.assertFalse(enrolled["linked_to_pos"])
        self.service.ingest_order(self.merchant, {"external_id": "coffee-redemption", "source": "square", "payment_fingerprint": "coffee-card", "occurred_at": datetime.now(timezone.utc).isoformat(), "total_cents": 900, "discount_cents": 100, "items": [{"name": "Latte", "quantity": 1, "unit_price_cents": 1000}]})
        redemption = self.service.redeem_offer(self.merchant, {"phone": "+15550202020", "promo_code": "COFFEE10", "external_order_id": "coffee-redemption", "discount_cents": 100})
        self.assertTrue(redemption["linked_to_pos"])
        with self.service.db.connect() as connection:
            target = connection.execute("SELECT phone,profile_status FROM guests WHERE id=?", (redemption["guest_id"],)).fetchone()
            consent = connection.execute("SELECT status FROM consents WHERE guest_id=? AND channel='sms' ORDER BY captured_at DESC", (redemption["guest_id"],)).fetchone()
        self.assertEqual(target["phone"], "+15550202020"); self.assertEqual(target["profile_status"], "identified"); self.assertEqual(consent["status"], "granted")
        with self.assertRaises(ValueError): self.service.redeem_offer(self.merchant, {"phone": "+15550202020", "promo_code": "COFFEE10", "external_order_id": "coffee-redemption"})

    def test_behavior_engine_learns_cadence_time_items_and_combinations_without_sharing_pii(self):
        for external, occurred in (("habit-1", "2026-06-07T18:30:00+00:00"), ("habit-2", "2026-06-21T18:35:00+00:00"), ("habit-3", "2026-07-05T18:25:00+00:00")):
            self.service.ingest_order(self.merchant, {"external_id": external, "source": "square", "payment_fingerprint": "habit-token", "occurred_at": occurred, "total_cents": 3800, "items": [{"name": "Smoked Ribs", "quantity": 1, "unit_price_cents": 3200}, {"name": "Garlic Fries", "quantity": 1, "unit_price_cents": 600}]})
        self.service.capture_identity(self.merchant, {"payment_fingerprint": "habit-token", "name": "Marcus", "phone": "+15559990000", "terms": {"accepted": True, "version": "v1"}, "consent": {"sms": {"status": "granted", "disclosure_version": "v1"}}})
        predictor = CapturingPredictor(); self.service.predictor = predictor
        result = self.service.run_behavior_engine(self.merchant)
        self.assertEqual(result["profiles_analyzed"], 1)
        customer = self.service.behavior_dashboard(self.merchant)["customers"][0]
        self.assertAlmostEqual(customer["average_interval_days"], 14, places=1)
        self.assertEqual(customer["favorite_weekday"], 6)
        self.assertEqual(customer["favorite_hour"], 18)
        self.assertEqual(customer["favorite_items"][0]["display_name"], "Smoked Ribs")
        self.assertEqual(customer["frequent_combinations"][0]["order_count"], 3)
        self.assertGreater(customer["return_probabilities"]["7"], customer["return_probabilities"]["1"])
        self.assertEqual(customer["preferred_daypart"], "dinner")
        context = predictor.contexts[0]
        self.assertNotIn("phone", context["profile"])
        self.assertNotIn("email", context["profile"])
        self.assertNotIn("name", context["profile"])
        self.assertEqual(context["frequent_item_pairs"][0]["order_count"], 3)
        self.assertEqual(self.service.run_behavior_engine(self.merchant)["predictions_created"], 0)
        self.assertEqual(len(predictor.contexts), 1)
        self.assertEqual(self.service.prediction_dashboard(self.merchant)["predictions"][0]["status"], "do_not_contact")

    def test_behavior_psychology_learns_context_social_pattern_and_decision_mechanism(self):
        dates = ("2026-06-07", "2026-06-21", "2026-07-05", "2026-07-19", "2026-08-02", "2026-08-16")
        for index, stamp in enumerate(dates):
            self.service.ingest_order(self.merchant, {"external_id": f"psych-{index}", "source": "square", "payment_fingerprint": "psych-card", "occurred_at": f"{stamp}T18:00:00+00:00", "total_cents": 4200, "discount_cents": 500 if index % 2 == 0 else 0, "fulfillment_type": "pickup", "items": [{"name": "Rib Plate", "quantity": 2, "unit_price_cents": 1800}, {"name": "Mac and Cheese", "quantity": 1, "unit_price_cents": 600}]})
        self.service.record_behavior_context(self.merchant, {"signal_type": "sporting_event", "starts_at": "2026-07-19T16:00:00+00:00", "ends_at": "2026-07-19T22:00:00+00:00", "source": "league_schedule", "confidence": 1, "value": {"event": "home game"}})
        with self.service.db.connect() as connection: guest_id = connection.execute("SELECT id FROM guests WHERE payment_fingerprint='psych-card'").fetchone()["id"]
        self.service.record_behavior_interaction(self.merchant, {"guest_id": guest_id, "event_type": "checkout_started"})
        self.service.record_behavior_interaction(self.merchant, {"guest_id": guest_id, "event_type": "checkout_abandoned"})
        self.service.run_behavior_engine(self.merchant)
        psychology = self.service.behavior_dashboard(self.merchant)["customers"][0]["psychology"]
        self.assertGreater(psychology["social_probability"], .5)
        self.assertGreater(psychology["convenience_affinity"], .5)
        self.assertEqual(psychology["belonging_label"], "Sunday Regular")
        self.assertIn(psychology["routine_state"], {"approaching", "decision_window", "disrupted", "passed"})
        self.assertGreater(psychology["context_affinities"]["sporting_event"], 0)
        self.assertTrue(psychology["evidence"]["pay_cycle_is_calendar_affinity_not_known_payday"])
        self.assertGreater(psychology["friction_sensitivity"]["confidence"], 0)
        intelligence = self.service.psychology_dashboard(self.merchant)
        self.assertGreaterEqual(len(intelligence["hypotheses"]), 11)
        habit = next(row for row in intelligence["hypotheses"] if row["hypothesis_type"] == "habit_strength")
        self.assertTrue(habit["supporting_evidence"]); self.assertIsNotNone(habit["last_observed_at"])
        scarcity = next(row for row in intelligence["strategies"] if row["code"] == "genuine_scarcity")
        self.assertTrue(scarcity["requires"]["verified_inventory_limit"])

    def test_psychology_selects_silence_when_marketing_fatigue_is_high(self):
        for index, stamp in enumerate(("2026-06-07", "2026-06-21", "2026-07-05")):
            self.service.ingest_order(self.merchant, {"external_id": f"fatigue-order-{index}", "source": "square", "payment_fingerprint": "fatigue-card", "occurred_at": f"{stamp}T18:00:00+00:00", "total_cents": 2000, "items": [{"name": "Sushi", "quantity": 1, "unit_price_cents": 2000}]})
        with self.service.db.connect() as connection: guest_id = connection.execute("SELECT id FROM guests WHERE payment_fingerprint='fatigue-card'").fetchone()["id"]
        campaign_id = self.queued_campaign(guest_id, "fatigue-campaign")
        stamp = datetime.now(timezone.utc).isoformat()
        with self.service.db.connect() as connection:
            for index in range(6):
                connection.execute("""INSERT INTO outbound_messages(id,merchant_id,campaign_id,guest_id,channel,recipient,status,sent_at,created_at,provider,attempts,idempotency_key)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (f"fatigue-message-{index}", self.merchant, campaign_id, guest_id, "sms", "+15550000000", "delivered", stamp, stamp, "telnyx", 1, f"fatigue-{index}"))
        predictor = CapturingPredictor(); self.service.predictor = predictor
        self.service.run_behavior_engine(self.merchant)
        self.assertEqual(predictor.contexts[0]["psychology"]["recommended_strategy"], "silence")
        self.assertEqual(predictor.contexts[0]["psychology"]["marketing_fatigue"]["level"], "high")

    def test_square_payment_enriches_order_with_customer_card_items_and_modifiers(self):
        square = SquareIntegration(self.service.db, self.service, cipher=object())
        order = {"id": "joined-order", "state": "OPEN", "location_id": "L1", "created_at": "2026-08-01T18:00:00+00:00", "total_money": {"amount": 3400, "currency": "USD"}, "line_items": [{"catalog_object_id": "RIB-VAR", "name": "Ribs", "quantity": "1", "total_money": {"amount": 3400}, "modifiers": [{"catalog_object_id": "SAUCE", "name": "Extra sauce", "quantity": "1"}]}]}
        square._ingest_square_order(self.merchant, order)
        self.assertEqual(self.service.behavior_dashboard(self.merchant)["summary"]["profile_count"], 0)
        payment = {"id": "PAY-1", "order_id": "joined-order", "status": "COMPLETED", "customer_id": "SQ-CUSTOMER", "location_id": "L1", "created_at": "2026-08-01T18:02:00+00:00", "amount_money": {"amount": 3400, "currency": "USD"}, "card_details": {"card": {"fingerprint": "CARD-FP"}}}
        square._ingest_square_order(self.merchant, {**order, "state": "COMPLETED"}, payment)
        with self.service.db.connect() as c:
            stored = c.execute("SELECT status,provider_customer_id,payment_fingerprint,payment_id FROM orders WHERE external_id='joined-order'").fetchone()
            identities = c.execute("SELECT identity_type,verified FROM guest_identities ORDER BY identity_type").fetchall()
            item = c.execute("SELECT catalog_object_id,modifiers_json FROM order_items").fetchone()
        self.assertEqual(dict(stored), {"status": "completed", "provider_customer_id": "SQ-CUSTOMER", "payment_fingerprint": "CARD-FP", "payment_id": "PAY-1"})
        self.assertEqual({row["identity_type"] for row in identities}, {"provider_customer_id", "payment_fingerprint"})
        self.assertTrue(all(row["verified"] for row in identities))
        self.assertEqual(item["catalog_object_id"], "RIB-VAR")
        self.assertEqual(json.loads(item["modifiers_json"])[0]["name"], "Extra sauce")

    def test_customer_identity_priority_does_not_merge_conflicting_cards(self):
        def sale(external, customer, fingerprint):
            return self.service.ingest_order(self.merchant, {"external_id": external, "source": "square", "provider_customer_id": customer, "payment_fingerprint": fingerprint, "status": "completed", "occurred_at": "2026-08-01T18:00:00+00:00", "total_cents": 1000, "items": [{"name": "Coffee", "quantity": 1, "unit_price_cents": 1000}]})
        sale("a", "CUSTOMER-A", "CARD-A"); sale("b", "CUSTOMER-B", "CARD-B"); sale("conflict", "CUSTOMER-A", "CARD-B")
        sale("split", "CUSTOMER-A", "CARD-A"); sale("split", "CUSTOMER-B", "CARD-B")
        with self.service.db.connect() as c:
            guests = c.execute("SELECT COUNT(*) count FROM guests WHERE merchant_id=?", (self.merchant,)).fetchone()["count"]
            a_guest = c.execute("SELECT guest_id FROM guest_identities WHERE identity_type='provider_customer_id' AND identity_value='CUSTOMER-A'").fetchone()["guest_id"]
            conflict_guest = c.execute("SELECT guest_id FROM orders WHERE external_id='conflict'").fetchone()["guest_id"]
            split_guest = c.execute("SELECT guest_id FROM orders WHERE external_id='split'").fetchone()["guest_id"]
        self.assertEqual(guests, 2)
        self.assertEqual(conflict_guest, a_guest)
        self.assertIsNone(split_guest)

    def test_canceled_test_and_fully_refunded_orders_are_not_visits(self):
        for external, status, is_test in (("valid-1", "completed", False), ("valid-2", "completed", False), ("cancel", "canceled", False), ("test", "completed", True)):
            self.service.ingest_order(self.merchant, {"external_id": external, "source": "square", "payment_fingerprint": "filter-card", "status": status, "is_test": is_test, "occurred_at": "2026-08-01T18:00:00+00:00", "total_cents": 1000, "items": [{"name": "Coffee", "quantity": 1, "unit_price_cents": 1000}]})
        customer = self.service.behavior_dashboard(self.merchant)["customers"][0]
        self.assertEqual(customer["visit_count"], 2)
        with self.service.db.connect() as c:
            order = c.execute("SELECT id FROM orders WHERE external_id='valid-2'").fetchone()["id"]
            c.execute("INSERT INTO refunds VALUES(?,?,?,?,?,?,?,?,?)", ("refund-full", self.merchant, "refund-full", order, 1000, "USD", "COMPLETED", "2026-08-02T00:00:00+00:00", "{}"))
        self.service.refresh_order_behavior(self.merchant, order)
        self.assertEqual(self.service.behavior_dashboard(self.merchant)["customers"][0]["visit_count"], 1)

    def test_control_group_conversion_is_measured_without_last_touch_attribution(self):
        exposure = "2026-08-10T10:00:00+00:00"; window = "2026-08-17T10:00:00+00:00"
        self.service.ingest_order(self.merchant, {"external_id": "seed-message", "source": "square", "payment_fingerprint": "message-card", "status": "completed", "occurred_at": "2026-08-01T10:00:00+00:00", "total_cents": 1000, "items": [{"name": "Coffee", "quantity": 1, "unit_price_cents": 1000}]})
        self.service.ingest_order(self.merchant, {"external_id": "seed-control", "source": "square", "payment_fingerprint": "control-card", "status": "completed", "occurred_at": "2026-08-01T10:00:00+00:00", "total_cents": 1000, "items": [{"name": "Coffee", "quantity": 1, "unit_price_cents": 1000}]})
        with self.service.db.connect() as c:
            message_guest = c.execute("SELECT id FROM guests WHERE payment_fingerprint='message-card'").fetchone()["id"]
            control_guest = c.execute("SELECT id FROM guests WHERE payment_fingerprint='control-card'").fetchone()["id"]
            c.execute("INSERT INTO campaigns(id,merchant_id,guest_id,channel,trigger_type,trigger_ref,subject,body,status,scheduled_at,sent_at,created_at,action,control_group,prediction_window_end,eligibility_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("campaign-message", self.merchant, message_guest, "sms", "next_visit", "p1", "Hi", "Return", "sent", exposure, exposure, exposure, "send_sms", 0, window, "{}"))
            c.execute("INSERT INTO campaigns(id,merchant_id,guest_id,channel,trigger_type,trigger_ref,subject,body,status,scheduled_at,sent_at,created_at,action,control_group,prediction_window_end,eligibility_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("campaign-control", self.merchant, control_guest, "sms", "next_visit", "p2", "Hi", "Return", "control", exposure, None, exposure, "do_nothing", 1, window, "{}"))
            c.execute("INSERT INTO campaign_outcomes(campaign_id,merchant_id,guest_id,group_name) VALUES(?,?,?,?)", ("campaign-message", self.merchant, message_guest, "messaged"))
            c.execute("INSERT INTO campaign_outcomes(campaign_id,merchant_id,guest_id,group_name) VALUES(?,?,?,?)", ("campaign-control", self.merchant, control_guest, "control"))
        self.service.ingest_order(self.merchant, {"external_id": "return-message", "source": "square", "payment_fingerprint": "message-card", "status": "completed", "occurred_at": "2026-08-12T10:00:00+00:00", "total_cents": 2000, "items": [{"name": "Coffee", "quantity": 2, "unit_price_cents": 1000}]})
        self.service.ingest_order(self.merchant, {"external_id": "return-control", "source": "square", "payment_fingerprint": "control-card", "status": "completed", "occurred_at": "2026-08-12T10:00:00+00:00", "total_cents": 2000, "items": [{"name": "Coffee", "quantity": 2, "unit_price_cents": 1000}]})
        with self.service.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) count FROM attributions").fetchone()["count"], 1)
            self.assertEqual(c.execute("SELECT SUM(converted) converted FROM campaign_outcomes").fetchone()["converted"], 2)
        metrics = self.service.metrics(self.merchant)
        self.assertEqual(metrics["experiment"]["control_size"], 1)
        self.assertEqual(metrics["experiment"]["messaged_size"], 1)

    def test_configurable_pos_connection_normalizes_provider_payload(self):
        connection = self.service.create_pos_connection(self.merchant, {"provider": "custom-pos", "external_location_id": "location-1", "mapping": {"external_id": "sale.id", "occurred_at": "sale.time", "total_cents": "sale.total", "payment_fingerprint": "buyer.token", "items": {"path": "sale.lines", "name": "label", "quantity": "qty", "unit_price_cents": "price"}}})
        payload = json.dumps({"sale": {"id": "custom-1", "time": "2026-08-01T12:00:00+00:00", "total": 1800, "lines": [{"label": "Chicken Bowl", "qty": 1, "price": 1800}]}, "buyer": {"token": "custom-token"}}).encode()
        result = self.service.ingest_pos_webhook(connection["id"], payload, connection["webhook_secret"])
        self.assertFalse(result["duplicate"])
        customer = self.service.behavior_dashboard(self.merchant)["customers"][0]
        self.assertEqual(customer["favorite_item"], "Chicken Bowl")

    def test_recipe_mapping_openai_prediction_delivery_and_suppression(self):
        for index, stamp in enumerate(("2026-05-18", "2026-06-01", "2026-06-15", "2026-06-29", "2026-07-13", "2026-07-27", "2026-08-10")):
            self.order(f"engine-{index}", f"{stamp}T18:00:00+00:00")
        profile = self.service.capture_identity(self.merchant, {"payment_fingerprint": "tok_guest_1", "phone": "+15550001", "terms": {"accepted": True, "version": "v1"}, "consent": {"sms": {"status": "granted", "disclosure_version": "v1"}}})
        self.service.ingest_invoice(self.merchant, {"external_id": "recipe-invoice", "vendor": "Foods", "invoice_date": "2026-07-01", "total_cents": 100, "items": [{"sku": "RIB", "ingredient": "Ribs", "quantity": 10, "unit": "case", "unit_cost_cents": 10}]})
        product = self.service.product_dashboard(self.merchant)["products"][0]
        menu = self.service.upsert_menu_item(self.merchant, {"external_id": "menu-ribs", "name": "Smoked Ribs", "price_cents": 3200})
        self.service.link_recipe(self.merchant, {"product_id": product["id"], "menu_item_id": menu["id"], "quantity_required": .5, "unit": "case"})
        self.assertEqual(len(self.service.recipe_dashboard(self.merchant)["recipe_links"]), 1)
        delivery = FakeDelivery(); self.service.predictor = FakePredictor(); self.service.delivery = delivery
        self.service.run_behavior_engine(self.merchant)
        campaign = self.service.list_campaigns(self.merchant)[0]
        self.service.approve_campaign(self.merchant, campaign["id"])
        hour = datetime.now(timezone.utc).hour
        self.service.update_messaging_settings(self.merchant, {"timezone": "UTC", "quiet_hours_start": f"{(hour+1)%24:02d}:00", "quiet_hours_end": f"{(hour+2)%24:02d}:00"})
        dispatched = self.service.dispatch_campaigns(self.merchant)
        self.assertEqual(dispatched["messages"][0]["status"], "sent")
        self.assertEqual(delivery.sent[0][0], "sms")
        self.service.record_message_event(self.merchant, {"provider_message_id": "provider-message-1", "event_type": "delivered"})
        self.assertEqual(self.service.metrics(self.merchant)["message_events"]["delivered"], 1)
        self.service.suppress(self.merchant, profile["id"], "sms")
        with self.service.db.connect() as c: self.assertEqual(c.execute("SELECT COUNT(*) count FROM suppressions").fetchone()["count"], 1)

    def test_square_signed_webhook_is_tenant_mapped_and_idempotent(self):
        with patch.dict(os.environ, {"SQUARE_WEBHOOK_SIGNATURE_KEY": "square-signing-key", "SQUARE_WEBHOOK_URL": "https://orbit.test/v1/webhooks/square"}):
            square = SquareIntegration(self.service.db, self.service, cipher=object())
        stamp = "2026-08-01T00:00:00+00:00"
        with self.service.db.connect() as c:
            c.execute("INSERT INTO square_installations VALUES(?,?,?,?,?,?,?,?,?,?)", ("install-1", self.merchant, "square-merchant-1", "production", "token", None, None, "active", stamp, stamp))
            c.execute("INSERT INTO square_locations(id,installation_id,merchant_id,environment,square_merchant_id,verified_at,square_location_id,name,timezone,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("location-1", "install-1", self.merchant, "production", "square-merchant-1", stamp, "SQ-LOC-1", "Downtown", "UTC", "active", stamp, stamp))
        event = {"event_id": "square-event-1", "type": "order.updated", "merchant_id": "square-merchant-1", "data": {"object": {"order": {"id": "SQ-ORDER-1", "location_id": "SQ-LOC-1", "customer_id": "SQ-CUSTOMER-1", "state": "COMPLETED", "created_at": "2026-08-01T18:00:00+00:00", "total_money": {"amount": 2400, "currency": "USD"}, "line_items": [{"name": "Rib Plate", "quantity": "1", "total_money": {"amount": 2400, "currency": "USD"}}]}}}}
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
            self.assertIn("session=true", authorization["authorization_url"])
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

    def test_square_production_oauth_disables_session(self):
        with patch.dict(os.environ, {"SQUARE_APPLICATION_ID": "app-id", "PUBLIC_BASE_URL": "https://orbit.test", "SQUARE_ENVIRONMENT": "production"}):
            square = SquareIntegration(self.service.db, self.service, cipher=object())
            authorization = square.authorize(self.merchant)
        self.assertIn("session=false", authorization["authorization_url"])

    def test_square_sandbox_to_production_reconnect_is_environment_isolated(self):
        class FakeCipher:
            def encrypt(self, value): return "encrypted:" + value
            def decrypt(self, value): return value.removeprefix("encrypted:")

        stamp = "2026-08-01T00:00:00+00:00"
        with self.service.db.connect() as c:
            c.execute("INSERT INTO square_installations VALUES(?,?,?,?,?,?,?,?,?,?)", ("sandbox-install", self.merchant, "sandbox-merchant", "sandbox", "encrypted:sandbox-token", None, None, "active", stamp, stamp))
            c.execute("INSERT INTO square_locations(id,installation_id,merchant_id,environment,square_merchant_id,verified_at,square_location_id,name,timezone,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("sandbox-location", "sandbox-install", self.merchant, "sandbox", "sandbox-merchant", stamp, "SANDBOX-LOC", "Sandbox Cafe", "UTC", "active", stamp, stamp))
            c.execute("INSERT INTO square_sync_state VALUES(?,?,?,?,?,?)", ("sandbox-install", "sandbox", None, stamp, "complete", None))
            c.execute("INSERT INTO square_webhook_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("sandbox-event", "order.created", "sandbox-merchant", "SANDBOX-LOC", "sandbox", "{}", "pending", None, stamp, None, 0, stamp))

        environment = {"SQUARE_APPLICATION_ID": "production-app", "SQUARE_APPLICATION_SECRET": "production-secret", "PUBLIC_BASE_URL": "https://api.orbitguest.com", "SQUARE_ENVIRONMENT": "production"}
        with patch.dict(os.environ, environment):
            square = SquareIntegration(self.service.db, self.service, cipher=FakeCipher())
            authorization = square.authorize(self.merchant)
            state = authorization["authorization_url"].split("state=")[1].split("&")[0]
            state = __import__("urllib.parse").parse.unquote(state)
            production_token = {"merchant_id": "production-merchant", "access_token": "production-token", "refresh_token": "production-refresh", "expires_at": "2099-01-01T00:00:00+00:00"}
            with patch.object(SquareClient, "exchange_code", return_value=production_token), patch.object(SquareClient, "request", return_value={"locations": [{"id": "PRODUCTION-LOC", "name": "Real Restaurant", "timezone": "UTC", "status": "ACTIVE"}]}):
                square.callback("production-code", state)

            status = square.status(self.merchant)
            self.assertEqual(status["installation"]["environment"], "production")
            self.assertEqual([location["square_location_id"] for location in status["locations"]], ["PRODUCTION-LOC"])
            self.assertEqual(status["webhooks"]["total_events"], 0)
            self.assertEqual(square.process_pending(), {"processed": 0, "examined": 0})
            searched = []
            def production_api(method, path, body=None, *args, **kwargs):
                if path == "/v2/locations": return {"locations": [{"id": "PRODUCTION-LOC", "name": "Real Restaurant", "timezone": "UTC", "status": "ACTIVE"}]}
                if path == "/v2/orders/search": searched.append(body["location_ids"]); return {"orders": []}
                if path.startswith("/v2/payments?"): return {"payments": []}
                raise AssertionError(path)
            with patch.object(SquareClient, "request", side_effect=production_api):
                square.historical_sync(self.merchant, "2026-01-01T00:00:00Z")
            self.assertEqual(searched, [["PRODUCTION-LOC"]])

        with self.service.db.connect() as c:
            installation = c.execute("SELECT environment,encrypted_access_token FROM square_installations WHERE merchant_id=?", (self.merchant,)).fetchone()
            self.assertEqual(dict(installation), {"environment": "production", "encrypted_access_token": "encrypted:production-token"})
            self.assertEqual(c.execute("SELECT COUNT(*) count FROM square_locations WHERE environment='sandbox'").fetchone()["count"], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) count FROM square_sync_state WHERE environment='sandbox'").fetchone()["count"], 0)
            self.assertEqual(c.execute("SELECT status FROM square_webhook_events WHERE event_id='sandbox-event'").fetchone()["status"], "pending")

    def test_live_production_status_repairs_same_environment_sandbox_contamination(self):
        class FakeCipher:
            def decrypt(self, value): return value
        stamp = "2026-08-20T00:00:00+00:00"
        with self.service.db.connect() as c:
            c.execute("INSERT INTO square_installations VALUES(?,?,?,?,?,?,?,?,?,?)", ("prod-install", self.merchant, "PROD-MERCHANT", "production", "production-token", None, None, "active", stamp, stamp))
            c.execute("INSERT INTO square_locations(id,installation_id,merchant_id,environment,square_merchant_id,verified_at,square_location_id,name,timezone,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("prod-location", "prod-install", self.merchant, "production", "PROD-MERCHANT", stamp, "LQB7QYHECNV53", "Orbit", "UTC", "active", stamp, stamp))
            c.execute("INSERT INTO square_locations(id,installation_id,merchant_id,environment,square_merchant_id,verified_at,square_location_id,name,timezone,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("contaminated-location", "prod-install", self.merchant, "production", "SANDBOX-MERCHANT", stamp, "LGH68WD15NM92", "Orbit Test Restaurant", "UTC", "active", stamp, stamp))
        with patch.dict(os.environ, {"SQUARE_ENVIRONMENT": "production"}), patch.object(SquareClient, "request", return_value={"locations": [{"id": "LQB7QYHECNV53", "name": "Orbit", "timezone": "UTC", "status": "ACTIVE"}]}):
            status = SquareIntegration(self.service.db, self.service, cipher=FakeCipher()).status(self.merchant)
        self.assertEqual([row["square_location_id"] for row in status["locations"]], ["LQB7QYHECNV53"])
        self.assertIsNone(status["location_sync_error"])
        with self.service.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) count FROM square_locations WHERE square_location_id='LGH68WD15NM92'").fetchone()["count"], 0)
            token = c.execute("SELECT encrypted_access_token FROM square_installations WHERE merchant_id=?", (self.merchant,)).fetchone()["encrypted_access_token"]
        self.assertEqual(token, "production-token")

    def test_pre_environment_database_preserves_authorization_and_discards_unknown_mappings(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as legacy_file:
            connection = sqlite3.connect(legacy_file.name)
            connection.executescript("""
                CREATE TABLE square_installations (id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL UNIQUE, square_merchant_id TEXT NOT NULL, encrypted_access_token TEXT NOT NULL, encrypted_refresh_token TEXT, token_expires_at TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE square_locations (id TEXT PRIMARY KEY, installation_id TEXT NOT NULL, merchant_id TEXT NOT NULL, square_location_id TEXT NOT NULL UNIQUE, name TEXT, timezone TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE square_webhook_events (event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, square_merchant_id TEXT, square_location_id TEXT, payload_json TEXT NOT NULL, status TEXT NOT NULL, error TEXT, received_at TEXT NOT NULL, processed_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT);
                CREATE TABLE square_sync_state (installation_id TEXT PRIMARY KEY, cursor TEXT, last_synced_at TEXT, status TEXT NOT NULL, error TEXT);
                INSERT INTO square_installations VALUES ('legacy-install','legacy-merchant','production-square-merchant','encrypted-production-token',NULL,NULL,'active','2026-01-01','2026-01-01');
                INSERT INTO square_locations VALUES ('unknown-location','legacy-install','legacy-merchant','UNKNOWN-ENV-LOC','Old mapping','UTC','active','2026-01-01','2026-01-01');
                INSERT INTO square_sync_state VALUES ('legacy-install',NULL,'2026-01-01','complete',NULL);
            """)
            connection.commit(); connection.close()
            database = Database(legacy_file.name)
            class LegacyCipher:
                def decrypt(self, value): return value
            with patch.dict(os.environ, {"SQUARE_ENVIRONMENT": "production"}):
                with patch.object(SquareClient, "request", return_value={"locations": [{"id": "PROD-LOC", "name": "Production", "status": "ACTIVE"}]}):
                    status = SquareIntegration(database, object(), cipher=LegacyCipher()).status("legacy-merchant")
            self.assertTrue(status["connected"])
            self.assertEqual(status["installation"]["environment"], "production")
            self.assertEqual([row["square_location_id"] for row in status["locations"]], ["PROD-LOC"])
            with database.connect() as c:
                row = c.execute("SELECT encrypted_access_token FROM square_installations WHERE merchant_id='legacy-merchant'").fetchone()
                self.assertEqual(row["encrypted_access_token"], "encrypted-production-token")
                self.assertEqual(c.execute("SELECT COUNT(*) count FROM square_sync_state").fetchone()["count"], 0)

    def test_railway_declares_cryptography_runtime_dependency(self):
        requirements = (Path(__file__).parents[1] / "requirements.txt").read_text()
        self.assertIn("cryptography>=43,<47", requirements)
        start = (Path(__file__).parents[1] / "Procfile").read_text()
        self.assertIn("python scripts/check_runtime.py", start)

    def test_live_messaging_runtime_check_requires_telnyx_and_resend_configuration(self):
        from scripts.check_runtime import main as check_runtime
        with patch.dict(os.environ, {"ORBIT_ENABLE_LIVE_MESSAGING": "true", "TELNYX_API_KEY": ""}, clear=False):
            with self.assertRaises(SystemExit) as raised: check_runtime()
        self.assertIn("TELNYX_API_KEY", str(raised.exception))

    def test_behavior_demo_seeder_is_sandbox_only_and_builds_realistic_profiles(self):
        seeder = BehaviorDemoSeeder(self.service)
        with patch.dict(os.environ, {"SQUARE_ENVIRONMENT": "production", "ORBIT_DEMO_MODE": "true"}):
            with self.assertRaises(DemoSeedError): seeder.seed(self.merchant)
        with patch.dict(os.environ, {"SQUARE_ENVIRONMENT": "sandbox", "ORBIT_DEMO_MODE": "false"}):
            with self.assertRaises(DemoSeedError): seeder.seed(self.merchant)
        protected = self.service.create_merchant("Production Protected")["id"]
        stamp = "2026-08-01T00:00:00+00:00"
        with self.service.db.connect() as connection:
            connection.execute("INSERT INTO square_installations VALUES(?,?,?,?,?,?,?,?,?,?)", ("protected-install", protected, "protected-square", "production", "token", None, None, "active", stamp, stamp))
        with patch.dict(os.environ, {"SQUARE_ENVIRONMENT": "sandbox", "ORBIT_DEMO_MODE": "true"}):
            with self.assertRaises(DemoSeedError): seeder.seed(protected)
        self.service.predictor = FakePredictor()
        with patch.dict(os.environ, {"SQUARE_ENVIRONMENT": "sandbox", "ORBIT_DEMO_MODE": "true"}):
            result = seeder.seed(self.merchant)
        self.assertGreaterEqual(result["profiles"]["profile_count"], 14)
        self.assertEqual(len(result["created_profiles"]), 14)
        self.assertGreaterEqual(result["profiles"]["profile_count"] - result["profiles"]["identified_count"], 10)
        self.assertGreater(result["orders_created"], 50)
        self.assertEqual(result["canceled_orders"], 1)
        self.assertEqual(result["fully_refunded_orders"], 1)
        self.assertEqual(result["ribs_regular"]["visit_count"], 7)
        self.assertAlmostEqual(result["ribs_regular"]["average_interval_days"], 14, places=1)
        self.assertEqual(result["ribs_regular"]["favorite_item"], "Smoked Ribs")
        self.assertTrue(result["expected_predictions"])
        with self.service.db.connect() as connection:
            canceled = connection.execute("SELECT status FROM orders WHERE external_id='demo-canceled'").fetchone()
            modifiers = connection.execute("SELECT COUNT(*) count FROM order_items WHERE modifiers_json<>'[]'").fetchone()["count"]
            locations = connection.execute("SELECT COUNT(DISTINCT location_id) count FROM orders WHERE external_id LIKE 'demo-%'").fetchone()["count"]
            fulfillments = connection.execute("SELECT COUNT(DISTINCT fulfillment_type) count FROM orders WHERE external_id LIKE 'demo-%'").fetchone()["count"]
            discounts = connection.execute("SELECT COUNT(*) count FROM orders WHERE external_id LIKE 'demo-%' AND discount_cents>0").fetchone()["count"]
            jordan = next(profile for profile in result["created_profiles"] if profile["label"] == "jordan")
            jordan_prediction = connection.execute("SELECT * FROM predictions WHERE guest_id=? AND normalized_item='classic burger' ORDER BY created_at DESC LIMIT 1", (jordan["guest_id"],)).fetchone()
            jordan_campaign = connection.execute("SELECT * FROM campaigns WHERE guest_id=? ORDER BY created_at DESC LIMIT 1", (jordan["guest_id"],)).fetchone()
        self.assertEqual(canceled["status"], "canceled")
        self.assertGreater(modifiers, 0)
        self.assertGreaterEqual(locations, 2)
        self.assertGreaterEqual(fulfillments, 3)
        self.assertGreater(discounts, 0)
        self.assertIsNotNone(jordan_prediction)
        eligibility = json.loads(jordan_prediction["eligibility_json"])
        for check in ("authorized_channel", "not_suppressed", "cooldown_clear", "menu_and_recipe_confirmed", "estimated_inventory_available", "capacity_available", "positive_expected_incremental_profit"):
            self.assertTrue(eligibility[check], check)
        self.assertIsNotNone(jordan_campaign)
        self.assertEqual(jordan_campaign["status"], "approval_required")
        self.assertEqual(jordan_campaign["action"], "send_sms")
        self.assertIn("Classic Burger", jordan_campaign["body"])
        self.assertIn("STOP", jordan_campaign["body"])
        self.assertNotIn("discount", jordan_campaign["body"].lower())
        self.assertNotIn("% off", jordan_campaign["body"].lower())
        self.assertGreaterEqual(result["profiles"]["profile_count"] - result["profiles"]["identified_count"], 10)

    def test_recipe_inventory_reconciles_sales_refunds_waste_and_margin(self):
        invoice = self.service.ingest_invoice(self.merchant, {"external_id": "stock-1", "vendor": "Butcher", "invoice_date": "2026-07-01", "total_cents": 90000, "items": [{"sku": "RIB", "ingredient": "Ribs", "quantity": 100, "unit": "lb", "unit_cost_cents": 900, "line_total_cents": 90000}]})
        self.assertFalse(invoice["duplicate"])
        product = self.service.product_dashboard(self.merchant)["products"][0]
        menu = self.service.upsert_menu_item(self.merchant, {"external_id": "rib-plate", "name": "Rib Plate", "price_cents": 3200})
        self.service.link_recipe(self.merchant, {"product_id": product["id"], "menu_item_id": menu["id"], "quantity_required": .75, "unit": "lb", "waste_percent": 10, "yield_percent": 90, "packaging_cost_cents": 100})
        for index in range(20):
            self.service.ingest_order(self.merchant, {"external_id": f"stock-sale-{index}", "source": "square", "payment_fingerprint": f"stock-{index}", "occurred_at": f"2026-07-{index+2:02d}T18:00:00+00:00", "total_cents": 3200, "status": "completed", "items": [{"name": "Rib Plate", "catalog_object_id": "rib-plate", "quantity": 1, "unit_price_cents": 3200}]})
        canceled = self.service.ingest_order(self.merchant, {"external_id": "stock-canceled", "source": "square", "payment_fingerprint": "stock-x", "occurred_at": "2026-07-25T18:00:00+00:00", "total_cents": 3200, "status": "canceled", "items": [{"name": "Rib Plate", "catalog_object_id": "rib-plate", "quantity": 1, "unit_price_cents": 3200}]})
        refunded = self.service.ingest_order(self.merchant, {"external_id": "stock-refunded", "source": "square", "payment_fingerprint": "stock-y", "occurred_at": "2026-07-26T18:00:00+00:00", "total_cents": 3200, "status": "completed", "items": [{"name": "Rib Plate", "catalog_object_id": "rib-plate", "quantity": 1, "unit_price_cents": 3200}]})
        with self.service.db.connect() as connection:
            connection.execute("INSERT INTO refunds VALUES(?,?,?,?,?,?,?,?,?)", ("refund-stock", self.merchant, "refund-stock", refunded["id"], 3200, "USD", "COMPLETED", "2026-07-27T00:00:00+00:00", "{}"))
        dashboard = self.service.inventory_dashboard(self.merchant)
        ribs = dashboard["ingredients"][0]; plate = dashboard["menu_items"][0]
        self.assertAlmostEqual(ribs["estimated_quantity"], 81.667, places=3)
        self.assertEqual(plate["estimated_portions"], 89)
        self.assertEqual(plate["status"], "safe_to_promote")
        self.assertEqual(plate["estimated_contribution_margin_cents"], 2275)
        self.service.adjust_inventory(self.merchant, {"product_id": product["id"], "quantity": 5, "unit": "lb", "reason": "spoilage"})
        self.assertAlmostEqual(self.service.inventory_dashboard(self.merchant)["ingredients"][0]["estimated_quantity"], 76.667, places=3)
        self.service.adjust_inventory(self.merchant, {"product_id": product["id"], "quantity": 50, "unit": "lb", "reason": "count"})
        self.assertEqual(self.service.inventory_dashboard(self.merchant)["ingredients"][0]["estimated_quantity"], 50)
        with self.service.db.connect() as connection:
            consumed_orders = connection.execute("SELECT COUNT(DISTINCT order_id) n FROM inventory_consumptions WHERE merchant_id=?", (self.merchant,)).fetchone()["n"]
        self.assertEqual(consumed_orders, 20)

    def test_recipe_proposals_require_manager_confirmation_and_explicit_portions(self):
        self.service.ingest_invoice(self.merchant, {"external_id": "proposal-stock", "vendor": "Vendor", "invoice_date": "2026-07-01", "total_cents": 1000, "items": [{"ingredient": "Salmon", "quantity": 10, "unit": "lb", "unit_cost_cents": 100}]})
        self.service.upsert_menu_item(self.merchant, {"external_id": "salmon", "name": "Grilled Salmon", "price_cents": 2400})
        proposal = self.service.propose_recipes(self.merchant)["proposals"][0]
        self.assertEqual(proposal["status"], "pending")
        with self.assertRaises(ValueError): self.service.review_recipe_proposal(self.merchant, proposal["id"], {"decision": "confirmed", "components": proposal["components"]})
        component = {**proposal["components"][0], "quantity_required": .5, "unit": "lb", "yield_percent": 90}
        result = self.service.review_recipe_proposal(self.merchant, proposal["id"], {"decision": "confirmed", "reviewed_by": "Chef", "components": [component]})
        self.assertEqual(result["status"], "confirmed")
        recipe = self.service.recipe_dashboard(self.merchant)["recipe_links"][0]
        self.assertEqual(recipe["status"], "confirmed")

    def test_prediction_backtesting_policy_and_message_safety_evaluation(self):
        for index, day in enumerate((1, 15, 29, 43)):
            self.service.ingest_order(self.merchant, {"external_id": f"eval-{index}", "source": "square", "payment_fingerprint": "eval-guest", "occurred_at": f"2026-06-{day if day <= 30 else day-30:02d}T18:00:00+00:00" if day <= 30 else f"2026-07-{day-30:02d}T18:00:00+00:00", "total_cents": 3000, "items": [{"name": "Ribs", "quantity": 1, "unit_price_cents": 3000}]})
        backtest = self.service.run_backtest(self.merchant)
        self.assertEqual(backtest["status"], "completed")
        self.assertEqual(backtest["metrics"]["case_count"], 1)
        self.assertEqual(backtest["metrics"]["item_accuracy"], 1)
        policy = self.service.set_campaign_policy(self.merchant, {"mode": "assisted", "automation_threshold": .9, "minimum_margin_cents": 500})
        self.assertEqual(policy["mode"], "assisted")
        evaluations = self.service.evaluation_dashboard(self.merchant)
        self.assertEqual(evaluations["evaluations"][0]["evaluation_type"], "historical_backtest")

    def test_openai_strategy_failure_retries_then_safely_sends_nothing(self):
        with patch.dict(os.environ, {"OPENAI_PREDICTION_MAX_ATTEMPTS": "2", "OPENAI_PREDICTION_TIMEOUT_SECONDS": "1"}):
            predictor = OpenAIBehaviorPredictor(api_key="test")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")) as request:
            self.assertEqual(predictor.predict({"profile": {"return_probabilities_json": "{}"}}), [])
        self.assertEqual(request.call_count, 2)
        self.assertTrue(predictor.last_run_metadata["fallback"])
        self.assertEqual(predictor.last_run_metadata["prompt_version"], "strategy-v2")

    def test_telnyx_replaces_twilio_and_uses_bearer_json_api(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return b'{"data":{"id":"telnyx-message-1"}}'
        with patch.dict(os.environ, {"TELNYX_API_KEY": "KEY", "TELNYX_FROM_NUMBER": "+15550000000", "TELNYX_MESSAGING_PROFILE_ID": "profile-1", "TELNYX_ALLOWED_COUNTRY_PREFIXES": "+1"}, clear=False):
            delivery = MessageDelivery()
        with patch("urllib.request.urlopen", return_value=Response()) as urlopen:
            message_id = delivery.send("sms", "+15550001111", "", "Hello. Reply STOP to opt out.", idempotency_key="orbit-campaign-1")
        self.assertEqual(message_id, "telnyx-message-1")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.telnyx.com/v2/messages")
        self.assertEqual(request.get_header("Authorization"), "Bearer KEY")
        self.assertEqual(request.get_header("Idempotency-key"), "orbit-campaign-1")
        payload = json.loads(request.data)
        self.assertEqual(payload["messaging_profile_id"], "profile-1")
        self.assertNotIn("TWILIO", str(request.header_items()).upper())

    def test_telnyx_webhook_signature_stop_start_help_and_delivery_sync(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        private = Ed25519PrivateKey.generate(); public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        with patch.dict(os.environ, {"TELNYX_PUBLIC_KEY": base64.b64encode(public).decode()}): delivery = MessageDelivery()
        raw, timestamp = b'{"data":{"event_type":"message.delivered"}}', str(int(time.time()))
        signature = base64.b64encode(private.sign(timestamp.encode() + b"|" + raw)).decode()
        self.assertTrue(delivery.verify_telnyx(raw, {"telnyx-signature-ed25519": signature, "telnyx-timestamp": timestamp}))
        self.assertFalse(delivery.verify_telnyx(raw + b" ", {"telnyx-signature-ed25519": signature, "telnyx-timestamp": timestamp}))

        self.order("sms-identity")
        guest = self.service.capture_identity(self.merchant, {"payment_fingerprint": "tok_guest_1", "phone": "+15550001111", "terms": {"accepted": True, "version": "v1"}, "consent": {"sms": {"status": "granted", "disclosure_version": "v1"}}})
        self.assertEqual(self.service.handle_inbound_sms({"from": "+15550001111", "text": "STOP"})["profiles_updated"], 1)
        with self.service.db.connect() as connection: self.assertEqual(connection.execute("SELECT COUNT(*) n FROM suppressions WHERE guest_id=? AND channel='sms'", (guest["id"],)).fetchone()["n"], 1)
        self.assertEqual(self.service.handle_inbound_sms({"from": "+15550001111", "text": "START"})["profiles_updated"], 1)
        with self.service.db.connect() as connection: self.assertEqual(connection.execute("SELECT COUNT(*) n FROM suppressions WHERE guest_id=? AND channel='sms'", (guest["id"],)).fetchone()["n"], 0)
        self.service.delivery = FakeDelivery(); self.service.handle_inbound_sms({"from": "+15550001111", "text": "HELP"})
        self.assertIn("STOP", self.service.delivery.sent[0][3])

        campaign = self.queued_campaign(guest["id"], "campaign-event")
        stamp = datetime.now(timezone.utc).isoformat()
        with self.service.db.connect() as connection:
            connection.execute("""INSERT INTO outbound_messages(id,merchant_id,campaign_id,guest_id,channel,recipient,provider_message_id,status,error,sent_at,created_at,provider,attempts,next_attempt_at,last_event_at,dead_lettered_at,idempotency_key)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", ("message-event", self.merchant, campaign, guest["id"], "sms", "+15550001111", "telnyx-event-1", "sent", None, stamp, stamp, "telnyx", 1, None, None, None, "orbit-event"))
        result = self.service.record_provider_event("telnyx", "event-1", "message.delivered", "telnyx-event-1", stamp, {})
        self.assertFalse(result["duplicate"])
        self.assertTrue(self.service.record_provider_event("telnyx", "event-1", "message.delivered", "telnyx-event-1", stamp, {})["duplicate"])
        with self.service.db.connect() as connection: self.assertEqual(connection.execute("SELECT status FROM outbound_messages WHERE id='message-event'").fetchone()["status"], "delivered")
        with self.service.db.connect() as connection: connection.execute("UPDATE outbound_messages SET provider='resend',provider_message_id='resend-event-1',channel='email' WHERE id='message-event'")
        self.service.record_provider_event("resend", "email-complaint-1", "email.complained", "resend-event-1", stamp, {})
        with self.service.db.connect() as connection: self.assertEqual(connection.execute("SELECT COUNT(*) n FROM suppressions WHERE guest_id=? AND channel='email'", (guest["id"],)).fetchone()["n"], 1)

    def test_quiet_hours_frequency_caps_and_delivery_dead_letter(self):
        self.order("retry-identity")
        guest = self.service.capture_identity(self.merchant, {"payment_fingerprint": "tok_guest_1", "phone": "+15550002222", "terms": {"accepted": True, "version": "v1"}, "consent": {"sms": {"status": "granted", "disclosure_version": "v1"}}})
        self.queued_campaign(guest["id"], "campaign-retry")
        current = datetime.now(timezone.utc)
        self.service.update_messaging_settings(self.merchant, {"timezone": "UTC", "quiet_hours_start": current.strftime("%H:%M"), "quiet_hours_end": (current+__import__("datetime").timedelta(hours=1)).strftime("%H:%M")})
        self.service.delivery = FakeDelivery()
        self.assertEqual(self.service.dispatch_campaigns(self.merchant)["messages"][0]["status"], "deferred")
        self.service.update_messaging_settings(self.merchant, {"timezone": "UTC", "quiet_hours_start": f"{(current.hour+1)%24:02d}:00", "quiet_hours_end": f"{(current.hour+2)%24:02d}:00"})
        stamp = current.isoformat()
        with self.service.db.connect() as connection:
            connection.execute("""INSERT INTO outbound_messages(id,merchant_id,campaign_id,guest_id,channel,recipient,provider_message_id,status,error,sent_at,created_at,provider,attempts,next_attempt_at,last_event_at,dead_lettered_at,idempotency_key)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", ("frequency-message", self.merchant, "campaign-retry", guest["id"], "sms", "+15550002222", "frequency-provider", "sent", None, stamp, stamp, "telnyx", 1, None, None, None, "frequency-key"))
            checks = self.service._messaging_allowed(connection, self.merchant, guest["id"], self.service.messaging_settings(self.merchant))
            connection.execute("DELETE FROM outbound_messages WHERE id='frequency-message'")
        self.assertFalse(checks["guest_frequency_clear"])
        self.service.delivery = FailingDelivery(retryable=True)
        with patch.dict(os.environ, {"ORBIT_MESSAGE_MAX_ATTEMPTS": "2"}):
            first = self.service.dispatch_campaigns(self.merchant)["messages"][0]
            self.assertEqual(first["status"], "retrying")
            with self.service.db.connect() as connection: connection.execute("UPDATE outbound_messages SET next_attempt_at=? WHERE campaign_id=?", ((current-__import__("datetime").timedelta(minutes=1)).isoformat(), "campaign-retry"))
            second = self.service.dispatch_campaigns(self.merchant)["messages"][0]
        self.assertEqual(second["status"], "dead")
        with self.service.db.connect() as connection:
            message = connection.execute("SELECT status,attempts,dead_lettered_at FROM outbound_messages WHERE campaign_id='campaign-retry'").fetchone()
        self.assertEqual(message["status"], "dead"); self.assertEqual(message["attempts"], 2); self.assertIsNotNone(message["dead_lettered_at"])
        dead = self.service.dead_letters(self.merchant)["dead_letters"]
        self.assertEqual(len(dead), 1)
        self.assertEqual(self.service.retry_dead_letter(self.merchant, dead[0]["id"])["status"], "retrying")

if __name__ == "__main__": unittest.main()
