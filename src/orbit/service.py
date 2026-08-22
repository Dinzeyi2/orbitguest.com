import hashlib
import hmac
import json
import re
import secrets
import uuid
import base64
from pathlib import Path
from datetime import datetime, timezone, timedelta

def now(): return datetime.now(timezone.utc).isoformat()
def uid(prefix): return f"{prefix}_{uuid.uuid4().hex}"
def normalize(value): return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

class OrbitService:
    ALLOWED_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic"}
    def __init__(self, db, extractor=None, storage_dir=None):
        self.db = db
        self.extractor = extractor
        self.storage_dir = Path(storage_dir or __import__("os").getenv("ORBIT_STORAGE_DIR", "/tmp/orbit-documents"))
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _audit(self, c, merchant, action, entity_type, entity_id, metadata=None, actor="api"):
        c.execute("INSERT INTO audit_log(id,merchant_id,action,actor,entity_type,entity_id,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (uid("audit"), merchant, action, actor, entity_type, entity_id, json.dumps(metadata or {}, sort_keys=True), now()))

    def create_merchant(self, name):
        merchant_id, key = uid("mer"), secrets.token_urlsafe(32)
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "restaurant"
        alias = f"{slug}-{secrets.token_hex(3)}@invoices.orbitguest.com"
        with self.db.connect() as c:
            c.execute("INSERT INTO merchants(id,name,api_key_hash,inbound_alias,created_at) VALUES(?,?,?,?,?)", (merchant_id, name, hashlib.sha256(key.encode()).hexdigest(), alias, now()))
            self._audit(c, merchant_id, "merchant.created", "merchant", merchant_id)
        return {"id": merchant_id, "name": name, "api_key": key, "invoice_email": alias}

    def verify_inbound_signature(self, raw_body, signature):
        secret = __import__("os").getenv("INBOUND_EMAIL_SECRET", "")
        return bool(secret and signature and hmac.compare_digest(hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest(), signature))

    def receive_email(self, data):
        """Receive a normalized, signed inbound-email webhook and process its attachments."""
        recipient = data["recipient"].lower()
        with self.db.connect() as c:
            merchant = c.execute("SELECT id FROM merchants WHERE lower(inbound_alias)=?", (recipient,)).fetchone()
            if not merchant: raise KeyError("unknown invoice email address")
            existing = c.execute("SELECT id,status FROM inbound_emails WHERE provider_message_id=?", (data["message_id"],)).fetchone()
            if existing: return {"id": existing["id"], "status": existing["status"], "duplicate": True}
            email_id = uid("eml")
            c.execute("INSERT INTO inbound_emails VALUES(?,?,?,?,?,?,?,?,?)", (email_id, merchant["id"], data["message_id"], data["sender"], recipient, data.get("subject"), data.get("received_at", now()), "processing", None))
            self._audit(c, merchant["id"], "email.received", "inbound_email", email_id, {"attachments": len(data.get("attachments", []))}, actor=data["sender"])
        processed, failed = [], []
        for attachment in data.get("attachments", []):
            try: processed.append(self._process_attachment(merchant["id"], email_id, data, attachment))
            except Exception as error: failed.append({"filename": attachment.get("filename", "unknown"), "error": str(error)})
        status = "processed" if processed and not failed else "needs_review" if processed else "failed"
        with self.db.connect() as c:
            c.execute("UPDATE inbound_emails SET status=?,error=? WHERE id=?", (status, json.dumps(failed) if failed else None, email_id))
        return {"id": email_id, "status": status, "duplicate": False, "documents": processed, "errors": failed}

    def _process_attachment(self, merchant, email_id, email_data, attachment):
        content_type, filename = attachment["content_type"].lower(), attachment["filename"]
        if content_type not in self.ALLOWED_TYPES: raise ValueError(f"unsupported attachment type: {content_type}")
        content = base64.b64decode(attachment["content_base64"], validate=True)
        if not content or len(content) > 20 * 1024 * 1024: raise ValueError("attachment must be between 1 byte and 20 MB")
        digest, document_id = hashlib.sha256(content).hexdigest(), uid("doc")
        directory = self.storage_dir / merchant / email_id; directory.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)
        path = directory / f"{document_id}-{safe_name}"; path.write_bytes(content)
        with self.db.connect() as c:
            c.execute("INSERT INTO invoice_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (document_id, email_id, None, filename, content_type, len(content), digest, str(path), "processing", None, None, None, now()))
        if not self.extractor: raise RuntimeError("invoice extractor is not configured")
        try:
            extracted = self.extractor.extract(content, content_type, filename)
            invoice = self._save_extraction(merchant, email_id, document_id, email_data, extracted)
            return {"document_id": document_id, "invoice_id": invoice, "filename": filename, "confidence": extracted["confidence"]}
        except Exception as error:
            with self.db.connect() as c: c.execute("UPDATE invoice_documents SET extraction_status='failed',error=? WHERE id=?", (str(error), document_id))
            raise

    def _save_extraction(self, merchant, email_id, document_id, email_data, extracted):
        invoice_id = uid("inv")
        external = extracted.get("invoice_number") or f"{email_data['message_id']}:{document_id}"
        with self.db.connect() as c:
            existing = c.execute("SELECT id FROM invoices WHERE merchant_id=? AND external_id=?", (merchant, external)).fetchone()
            if existing:
                c.execute("UPDATE invoice_documents SET invoice_id=?,extraction_status='duplicate',extraction_confidence=?,extraction_json=? WHERE id=?", (existing["id"], extracted["confidence"], json.dumps(extracted), document_id))
                return existing["id"]
            c.execute("""INSERT INTO invoices(id,merchant_id,external_id,vendor,invoice_date,currency,subtotal_cents,tax_cents,total_cents,source_message_id,raw_text,status,created_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (invoice_id, merchant, external, extracted["vendor"], extracted["invoice_date"] or email_data.get("received_at", now()), extracted["currency"], extracted["subtotal_cents"], extracted["tax_cents"], extracted["total_cents"], email_data["message_id"], None, "processed" if extracted["confidence"] >= .75 else "needs_review", now()))
            for item in extracted["items"]:
                c.execute("INSERT INTO invoice_lines VALUES(?,?,?,?,?,?,?,?,?)", (uid("line"), invoice_id, item.get("sku"), item["description"], normalize(item["description"]), item["quantity"], item["unit"], item["unit_price_cents"], item["line_total_cents"]))
                c.execute("INSERT INTO inventory_events VALUES(?,?,?,?,?,?,?,?,?)", (uid("ive"), merchant, invoice_id, item["description"], normalize(item["description"]), item["quantity"], item["unit"], item["unit_price_cents"], extracted["invoice_date"] or email_data.get("received_at", now())))
            c.execute("UPDATE invoice_documents SET invoice_id=?,extraction_status=?,extraction_confidence=?,extraction_json=? WHERE id=?", (invoice_id, "processed" if extracted["confidence"] >= .75 else "needs_review", extracted["confidence"], json.dumps(extracted), document_id))
            self._audit(c, merchant, "document.extracted", "invoice_document", document_id, {"invoice_id": invoice_id, "confidence": extracted["confidence"]})
        return invoice_id

    def invoice_dashboard(self, merchant):
        with self.db.connect() as c:
            invoices = [dict(row) for row in c.execute("SELECT id,external_id,vendor,invoice_date,currency,subtotal_cents,tax_cents,total_cents,status,created_at FROM invoices WHERE merchant_id=? ORDER BY invoice_date DESC", (merchant,))]
            for invoice in invoices:
                invoice["items"] = [dict(row) for row in c.execute("SELECT sku,description,quantity,unit,unit_price_cents,line_total_cents FROM invoice_lines WHERE invoice_id=? ORDER BY rowid", (invoice["id"],))]
            totals = dict(c.execute("SELECT COUNT(*) invoice_count,COALESCE(SUM(total_cents),0) total_spend_cents,COUNT(DISTINCT vendor) vendor_count FROM invoices WHERE merchant_id=?", (merchant,)).fetchone())
            pending = c.execute("SELECT COUNT(*) count FROM invoice_documents d JOIN inbound_emails e ON e.id=d.inbound_email_id WHERE e.merchant_id=? AND d.extraction_status IN ('processing','needs_review','failed')", (merchant,)).fetchone()["count"]
        return {"summary": {**totals, "documents_needing_attention": pending}, "invoices": invoices}

    def authenticate(self, key):
        hashed = hashlib.sha256(key.encode()).hexdigest()
        with self.db.connect() as c: row = c.execute("SELECT id FROM merchants WHERE api_key_hash=?", (hashed,)).fetchone()
        return row["id"] if row else None

    def capture_identity(self, merchant, data):
        fingerprint = data.get("payment_fingerprint")
        if not fingerprint or any(k in data for k in ("card_number", "pan", "cvv")):
            raise ValueError("A provider payment_fingerprint is required; raw card data is forbidden")
        stamp = now()
        with self.db.connect() as c:
            guest = c.execute("SELECT * FROM guests WHERE merchant_id=? AND payment_fingerprint=?", (merchant, fingerprint)).fetchone()
            guest_id = guest["id"] if guest else uid("gst")
            if guest:
                c.execute("UPDATE guests SET name=COALESCE(?,name),email=COALESCE(?,email),phone=COALESCE(?,phone),updated_at=? WHERE id=?",
                          (data.get("name"), data.get("email"), data.get("phone"), stamp, guest_id))
            else:
                c.execute("INSERT INTO guests VALUES(?,?,?,?,?,?,?,?)", (guest_id, merchant, fingerprint, data.get("name"), data.get("email"), data.get("phone"), stamp, stamp))
            for channel in ("email", "sms"):
                consent = data.get("consent", {}).get(channel)
                if consent:
                    c.execute("INSERT INTO consents VALUES(?,?,?,?,?,?,?,?)", (uid("con"), merchant, guest_id, channel, consent["status"], consent["disclosure_version"], consent.get("source", "checkout"), stamp))
            c.execute("UPDATE orders SET guest_id=? WHERE merchant_id=? AND payment_fingerprint=? AND guest_id IS NULL", (guest_id, merchant, fingerprint))
            self._audit(c, merchant, "guest.identity_captured", "guest", guest_id, {"channels": list(data.get("consent", {}))})
        return self.guest_profile(merchant, guest_id)

    def ingest_order(self, merchant, data):
        if any(k in data for k in ("card_number", "pan", "cvv")): raise ValueError("raw card data is forbidden")
        with self.db.connect() as c:
            existing = c.execute("SELECT id FROM orders WHERE merchant_id=? AND source=? AND external_id=?", (merchant, data["source"], data["external_id"])).fetchone()
            if existing: return {"id": existing["id"], "duplicate": True}
            guest = c.execute("SELECT id FROM guests WHERE merchant_id=? AND payment_fingerprint=?", (merchant, data.get("payment_fingerprint"))).fetchone()
            order_id = uid("ord")
            c.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)", (order_id, merchant, data["external_id"], guest["id"] if guest else None, data.get("payment_fingerprint"), data["occurred_at"], data["total_cents"], data.get("currency", "USD"), data["source"], json.dumps(data)))
            for item in data.get("items", []):
                c.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)", (uid("itm"), order_id, item["name"], normalize(item["name"]), item.get("quantity", 1), item["unit_price_cents"]))
            self._audit(c, merchant, "order.ingested", "order", order_id, {"source": data["source"]})
            self._attribute(c, merchant, order_id, guest["id"] if guest else None, data["occurred_at"], data["total_cents"])
        return {"id": order_id, "duplicate": False}

    def _attribute(self, c, merchant, order_id, guest_id, occurred_at, revenue):
        if not guest_id: return
        sent_after = (datetime.fromisoformat(occurred_at.replace("Z", "+00:00")) - timedelta(days=7)).isoformat()
        campaign = c.execute("SELECT id FROM campaigns WHERE merchant_id=? AND guest_id=? AND status='sent' AND sent_at BETWEEN ? AND ? ORDER BY sent_at DESC LIMIT 1", (merchant, guest_id, sent_after, occurred_at)).fetchone()
        if campaign:
            c.execute("INSERT OR IGNORE INTO attributions VALUES(?,?,?,?,?,?)", (uid("att"), merchant, campaign["id"], order_id, revenue, now()))

    def ingest_invoice(self, merchant, data):
        with self.db.connect() as c:
            existing = c.execute("SELECT id FROM invoices WHERE merchant_id=? AND external_id=?", (merchant, data["external_id"])).fetchone()
            if existing: return {"id": existing["id"], "duplicate": True, "campaigns_created": 0}
            invoice_id = uid("inv")
            c.execute("""INSERT INTO invoices(id,merchant_id,external_id,vendor,invoice_date,currency,subtotal_cents,tax_cents,total_cents,source_message_id,raw_text,status,created_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (invoice_id, merchant, data["external_id"], data["vendor"], data["invoice_date"], data.get("currency", "USD"), data.get("subtotal_cents", data["total_cents"]), data.get("tax_cents", 0), data["total_cents"], data.get("source_message_id"), data.get("raw_text"), "processed", now()))
            created = 0
            for item in data.get("items", []):
                ingredient = normalize(item["ingredient"])
                c.execute("INSERT INTO inventory_events VALUES(?,?,?,?,?,?,?,?,?)", (uid("ive"), merchant, invoice_id, item["ingredient"], ingredient, item["quantity"], item["unit"], item["unit_cost_cents"], data["invoice_date"]))
                created += self._create_inventory_campaigns(c, merchant, invoice_id, ingredient)
            self._audit(c, merchant, "invoice.processed", "invoice", invoice_id, {"campaigns_created": created})
        return {"id": invoice_id, "duplicate": False, "campaigns_created": created}

    def _create_inventory_campaigns(self, c, merchant, invoice_id, ingredient):
        rows = c.execute("""SELECT g.id,g.name,g.email,g.phone,COUNT(*) purchases FROM guests g JOIN orders o ON o.guest_id=g.id JOIN order_items i ON i.order_id=o.id WHERE g.merchant_id=? AND i.normalized_name LIKE ? GROUP BY g.id HAVING purchases>=2""", (merchant, f"%{ingredient}%")).fetchall()
        count = 0
        for guest in rows:
            channel = "sms" if guest["phone"] and self._consented(c, merchant, guest["id"], "sms") else "email" if guest["email"] and self._consented(c, merchant, guest["id"], "email") else None
            if not channel: continue
            campaign_id = uid("cam")
            first = guest["name"].split()[0] if guest["name"] else "there"
            c.execute("INSERT INTO campaigns VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (campaign_id, merchant, guest["id"], channel, "inventory_arrival", invoice_id, f"Fresh {ingredient} is here", f"Hi {first}, fresh {ingredient} just arrived. Come enjoy your favorite again this week.", "queued", now(), None, now()))
            count += 1
        return count

    def _consented(self, c, merchant, guest, channel):
        row = c.execute("SELECT status FROM consents WHERE merchant_id=? AND guest_id=? AND channel=? ORDER BY captured_at DESC,id DESC LIMIT 1", (merchant, guest, channel)).fetchone()
        return bool(row and row["status"] == "granted")

    def guest_profile(self, merchant, guest_id):
        with self.db.connect() as c:
            guest = c.execute("SELECT id,name,email,phone,created_at,updated_at FROM guests WHERE merchant_id=? AND id=?", (merchant, guest_id)).fetchone()
            if not guest: raise KeyError("guest not found")
            stats = c.execute("SELECT COUNT(*) visits,COALESCE(SUM(total_cents),0) spend,MAX(occurred_at) last_visit FROM orders WHERE merchant_id=? AND guest_id=?", (merchant, guest_id)).fetchone()
            favorites = c.execute("SELECT i.name,COUNT(*) orders FROM order_items i JOIN orders o ON o.id=i.order_id WHERE o.merchant_id=? AND o.guest_id=? GROUP BY i.normalized_name ORDER BY orders DESC LIMIT 5", (merchant, guest_id)).fetchall()
        return {**dict(guest), "visits": stats["visits"], "lifetime_spend_cents": stats["spend"], "last_visit": stats["last_visit"], "favorites": [dict(x) for x in favorites]}

    def list_campaigns(self, merchant):
        with self.db.connect() as c: rows = c.execute("SELECT * FROM campaigns WHERE merchant_id=? ORDER BY created_at DESC", (merchant,)).fetchall()
        return [dict(row) for row in rows]

    def mark_sent(self, merchant, campaign_id):
        with self.db.connect() as c:
            c.execute("UPDATE campaigns SET status='sent',sent_at=? WHERE merchant_id=? AND id=? AND status='queued'", (now(), merchant, campaign_id))
            if not c.execute("SELECT changes()").fetchone()[0]: raise KeyError("queued campaign not found")
            self._audit(c, merchant, "campaign.sent", "campaign", campaign_id)
        return {"id": campaign_id, "status": "sent"}

    def metrics(self, merchant):
        with self.db.connect() as c:
            row = c.execute("SELECT COUNT(DISTINCT campaign_id) converted_campaigns,COUNT(*) attributed_orders,COALESCE(SUM(revenue_cents),0) revenue_cents FROM attributions WHERE merchant_id=?", (merchant,)).fetchone()
        return dict(row)
