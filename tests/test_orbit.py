import tempfile
import unittest
import base64
from datetime import datetime, timezone
from orbit.db import Database
from orbit.service import OrbitService

class FakeExtractor:
    def extract(self, content, content_type, filename):
        return {"vendor": "Fresh Foods", "invoice_number": "INV-42", "invoice_date": "2026-08-22", "currency": "USD", "subtotal_cents": 9000, "tax_cents": 1000, "total_cents": 10000, "confidence": .97, "items": [{"sku": "RIB-1", "description": "Baby Back Ribs", "quantity": 40, "unit": "case", "unit_price_cents": 225, "line_total_cents": 9000}]}

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
        profile = self.service.capture_identity(self.merchant, {"payment_fingerprint": "tok_guest_1", "name": "Jamie", "phone": "+15550001111", "consent": {"sms": {"status": "granted", "disclosure_version": "v1"}}})
        self.assertEqual(profile["visits"], 2)
        invoice = self.service.ingest_invoice(self.merchant, {"external_id": "invoice-1", "vendor": "Foods Co", "invoice_date": "2026-08-20T10:00:00+00:00", "total_cents": 10000, "items": [{"ingredient": "Ribs", "quantity": 40, "unit": "case", "unit_cost_cents": 250}]})
        self.assertEqual(invoice["campaigns_created"], 1)
        campaign = self.service.list_campaigns(self.merchant)[0]
        self.assertEqual(campaign["channel"], "sms")
        self.service.mark_sent(self.merchant, campaign["id"])
        self.order("order-3", datetime.now(timezone.utc).isoformat())
        self.assertEqual(self.service.metrics(self.merchant)["revenue_cents"], 3200)

    def test_opted_out_guest_is_not_messaged(self):
        self.order("one"); self.order("two")
        self.service.capture_identity(self.merchant, {"payment_fingerprint": "tok_guest_1", "phone": "+1555", "consent": {"sms": {"status": "denied", "disclosure_version": "v1"}}})
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

if __name__ == "__main__": unittest.main()
