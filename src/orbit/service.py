import hashlib
import hmac
import json
import re
import secrets
import uuid
import base64
from collections import Counter
from .pos import ConfigurablePOSAdapter
from pathlib import Path
from datetime import datetime, timezone, timedelta

def now(): return datetime.now(timezone.utc).isoformat()
def uid(prefix): return f"{prefix}_{uuid.uuid4().hex}"
def normalize(value): return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
def invoice_date_key(value):
    """Return a sortable ISO date, keeping malformed/unknown dates safely oldest."""
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value or "")
    return match.group(0) if match else "0001-01-01"

class OrbitService:
    ALLOWED_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic"}
    def __init__(self, db, extractor=None, storage_dir=None, predictor=None, delivery=None):
        self.db = db
        self.extractor = extractor
        self.storage_dir = Path(storage_dir or __import__("os").getenv("ORBIT_STORAGE_DIR", "/tmp/orbit-documents"))
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.predictor = predictor
        self.delivery = delivery

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
                line_id = uid("line")
                c.execute("INSERT INTO invoice_lines VALUES(?,?,?,?,?,?,?,?,?)", (line_id, invoice_id, item.get("sku"), item["description"], normalize(item["description"]), item["quantity"], item["unit"], item["unit_price_cents"], item["line_total_cents"]))
                c.execute("INSERT INTO inventory_events VALUES(?,?,?,?,?,?,?,?,?)", (uid("ive"), merchant, invoice_id, item["description"], normalize(item["description"]), item["quantity"], item["unit"], item["unit_price_cents"], extracted["invoice_date"] or email_data.get("received_at", now())))
                self._record_product_version(c, merchant, extracted["vendor"], invoice_id, line_id, extracted["invoice_date"] or email_data.get("received_at", now()), item)
            c.execute("UPDATE invoice_documents SET invoice_id=?,extraction_status=?,extraction_confidence=?,extraction_json=? WHERE id=?", (invoice_id, "processed" if extracted["confidence"] >= .75 else "needs_review", extracted["confidence"], json.dumps(extracted), document_id))
            self._audit(c, merchant, "document.extracted", "invoice_document", document_id, {"invoice_id": invoice_id, "confidence": extracted["confidence"]})
        return invoice_id

    def _record_product_version(self, c, merchant, vendor, invoice_id, line_id, effective_date, item):
        """Append a product version and update the current snapshot only when newer."""
        vendor_key = normalize(vendor) or "unknown vendor"
        sku = (item.get("sku") or "").strip()
        name = item["description"].strip()
        product_key = f"sku:{normalize(sku)}" if sku else f"name:{normalize(name)}"
        product = c.execute("SELECT * FROM catalog_products WHERE merchant_id=? AND vendor=? AND product_key=?", (merchant, vendor_key, product_key)).fetchone()
        stamp = now()
        if product:
            product_id = product["id"]
            incoming_is_current = invoice_date_key(effective_date) >= invoice_date_key(product["current_invoice_date"])
        else:
            product_id, incoming_is_current = uid("prd"), True
            c.execute("""INSERT INTO catalog_products(id,merchant_id,vendor,product_key,sku,canonical_name,normalized_name,current_version_id,current_invoice_date,created_at,updated_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (product_id, merchant, vendor_key, product_key, sku or None, name, normalize(name), None, None, stamp, stamp))
        version_id = uid("pvr")
        if incoming_is_current:
            c.execute("UPDATE product_versions SET is_current=0 WHERE product_id=? AND is_current=1", (product_id,))
        c.execute("""INSERT INTO product_versions(id,product_id,invoice_id,invoice_line_id,effective_date,quantity,unit,unit_price_cents,line_total_cents,is_current,recorded_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (version_id, product_id, invoice_id, line_id, invoice_date_key(effective_date), item["quantity"], item["unit"], item["unit_price_cents"], item["line_total_cents"], 1 if incoming_is_current else 0, stamp))
        if incoming_is_current:
            c.execute("UPDATE catalog_products SET sku=COALESCE(?,sku),canonical_name=?,normalized_name=?,current_version_id=?,current_invoice_date=?,updated_at=? WHERE id=?", (sku or None, name, normalize(name), version_id, invoice_date_key(effective_date), stamp, product_id))
        self._audit(c, merchant, "product.version_recorded", "catalog_product", product_id, {"version_id": version_id, "invoice_id": invoice_id, "became_current": incoming_is_current, "effective_date": invoice_date_key(effective_date)})

    def product_dashboard(self, merchant):
        with self.db.connect() as c:
            rows = c.execute("""SELECT p.id,p.vendor,p.sku,p.canonical_name,p.current_invoice_date,p.created_at,p.updated_at,
                              v.quantity,v.unit,v.unit_price_cents,v.line_total_cents,
                              (SELECT COUNT(*) FROM product_versions h WHERE h.product_id=p.id) version_count
                              FROM catalog_products p LEFT JOIN product_versions v ON v.id=p.current_version_id
                              WHERE p.merchant_id=? ORDER BY p.canonical_name""", (merchant,)).fetchall()
        return {"products": [dict(row) for row in rows]}

    def product_history(self, merchant, product_id):
        with self.db.connect() as c:
            product = c.execute("SELECT id,vendor,sku,canonical_name,current_invoice_date,created_at,updated_at FROM catalog_products WHERE merchant_id=? AND id=?", (merchant, product_id)).fetchone()
            if not product: raise KeyError("product not found")
            versions = c.execute("""SELECT v.id,v.effective_date,v.quantity,v.unit,v.unit_price_cents,v.line_total_cents,v.is_current,v.recorded_at,
                                  i.external_id invoice_number,i.invoice_date,i.status invoice_status
                                  FROM product_versions v JOIN invoices i ON i.id=v.invoice_id
                                  WHERE v.product_id=? ORDER BY v.effective_date DESC,v.recorded_at DESC""", (product_id,)).fetchall()
        return {"product": dict(product), "history": [dict(row) for row in versions]}

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

    def create_pos_connection(self, merchant, data):
        connection_id, secret = uid("pos"), secrets.token_urlsafe(32)
        mapping = data.get("mapping") or {"external_id": "id", "occurred_at": "occurred_at", "total_cents": "total_cents", "payment_fingerprint": "payment_fingerprint", "currency": "currency", "items": {"path": "items", "name": "name", "quantity": "quantity", "unit_price_cents": "unit_price_cents"}}
        with self.db.connect() as c:
            c.execute("INSERT INTO pos_connections VALUES(?,?,?,?,?,?,?,?,?)", (connection_id, merchant, data["provider"].lower(), data["external_location_id"], data.get("display_name", data["external_location_id"]), hashlib.sha256(secret.encode()).hexdigest(), json.dumps(mapping), "active", now()))
            self._audit(c, merchant, "pos.connected", "pos_connection", connection_id, {"provider": data["provider"], "location": data["external_location_id"]})
        return {"id": connection_id, "webhook_secret": secret, "webhook_url": f"/v1/webhooks/pos/{connection_id}"}

    def ingest_pos_webhook(self, connection_id, raw_body, signature):
        with self.db.connect() as c: connection = c.execute("SELECT * FROM pos_connections WHERE id=? AND status='active'", (connection_id,)).fetchone()
        if not connection: raise KeyError("POS connection not found")
        # The stored hash lets Orbit authenticate a supplied per-connection secret; provider adapters can replace this with native signature verification.
        if not signature or not hmac.compare_digest(connection["webhook_secret_hash"], hashlib.sha256(signature.encode()).hexdigest()): raise ValueError("invalid POS webhook secret")
        payload = json.loads(raw_body)
        order = ConfigurablePOSAdapter(connection["provider"], json.loads(connection["mapping_json"])).normalize(payload)
        return self.ingest_order(connection["merchant_id"], order)

    def upsert_menu_item(self, merchant, data):
        stamp = now()
        with self.db.connect() as c:
            existing = c.execute("SELECT id FROM menu_items WHERE merchant_id=? AND external_id=?", (merchant, data["external_id"])).fetchone()
            item_id = existing["id"] if existing else uid("menu")
            c.execute("""INSERT INTO menu_items VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(merchant_id,external_id) DO UPDATE SET name=excluded.name,normalized_name=excluded.normalized_name,price_cents=excluded.price_cents,active=excluded.active,updated_at=excluded.updated_at""", (item_id, merchant, data["external_id"], data["name"], normalize(data["name"]), data["price_cents"], 1 if data.get("active", True) else 0, stamp, stamp))
        return {"id": item_id}

    def link_recipe(self, merchant, data):
        with self.db.connect() as c:
            product = c.execute("SELECT id FROM catalog_products WHERE merchant_id=? AND id=?", (merchant, data["product_id"])).fetchone()
            menu = c.execute("SELECT id FROM menu_items WHERE merchant_id=? AND id=?", (merchant, data["menu_item_id"])).fetchone()
            if not product or not menu: raise KeyError("product or menu item not found")
            link_id = uid("rcp")
            c.execute("""INSERT INTO recipe_links VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(product_id,menu_item_id) DO UPDATE SET quantity_required=excluded.quantity_required,unit=excluded.unit,confidence=excluded.confidence,status=excluded.status""", (link_id, merchant, product["id"], menu["id"], data["quantity_required"], data["unit"], data.get("confidence", 1.0), data.get("status", "confirmed"), now()))
        return {"id": link_id}

    def recipe_dashboard(self, merchant):
        with self.db.connect() as c:
            rows = c.execute("""SELECT r.id,p.canonical_name ingredient,p.sku,m.name menu_item,m.external_id,r.quantity_required,r.unit,r.confidence,r.status FROM recipe_links r JOIN catalog_products p ON p.id=r.product_id JOIN menu_items m ON m.id=r.menu_item_id WHERE r.merchant_id=? ORDER BY m.name""", (merchant,)).fetchall()
        return {"recipe_links": [dict(row) for row in rows]}

    def capture_identity(self, merchant, data):
        fingerprint = data.get("payment_fingerprint")
        if not fingerprint or any(k in data for k in ("card_number", "pan", "cvv")):
            raise ValueError("A provider payment_fingerprint is required; raw card data is forbidden")
        terms = data.get("terms") or {}
        if not data.get("phone"):
            raise ValueError("phone is required to activate an identifiable customer profile")
        if terms.get("accepted") is not True or not terms.get("version"):
            raise ValueError("explicit acceptance and a terms version are required")
        stamp = now()
        with self.db.connect() as c:
            guest = c.execute("SELECT * FROM guests WHERE merchant_id=? AND payment_fingerprint=?", (merchant, fingerprint)).fetchone()
            guest_id = guest["id"] if guest else uid("gst")
            if guest:
                c.execute("UPDATE guests SET name=COALESCE(?,name),email=COALESCE(?,email),phone=?,profile_status='identified',terms_version=?,terms_accepted_at=?,permission_source=?,updated_at=? WHERE id=?",
                          (data.get("name"), data.get("email"), data["phone"], terms["version"], stamp, terms.get("source", "checkout"), stamp, guest_id))
            else:
                c.execute("""INSERT INTO guests(id,merchant_id,payment_fingerprint,name,email,phone,profile_status,terms_version,terms_accepted_at,permission_source,created_at,updated_at)
                             VALUES(?,?,?,?,?,?,'identified',?,?,?,?,?)""", (guest_id, merchant, fingerprint, data.get("name"), data.get("email"), data["phone"], terms["version"], stamp, terms.get("source", "checkout"), stamp, stamp))
            for channel in ("email", "sms"):
                consent = data.get("consent", {}).get(channel)
                if consent:
                    c.execute("INSERT INTO consents VALUES(?,?,?,?,?,?,?,?)", (uid("con"), merchant, guest_id, channel, consent["status"], consent["disclosure_version"], consent.get("source", "checkout"), stamp))
            c.execute("UPDATE orders SET guest_id=? WHERE merchant_id=? AND payment_fingerprint=? AND guest_id IS NULL", (guest_id, merchant, fingerprint))
            self._audit(c, merchant, "guest.permission_activated", "guest", guest_id, {"channels": list(data.get("consent", {})), "terms_version": terms["version"], "source": terms.get("source", "checkout")})
        return self.guest_profile(merchant, guest_id)

    def ingest_order(self, merchant, data):
        if any(k in data for k in ("card_number", "pan", "cvv")): raise ValueError("raw card data is forbidden")
        with self.db.connect() as c:
            existing = c.execute("SELECT id FROM orders WHERE merchant_id=? AND source=? AND external_id=?", (merchant, data["source"], data["external_id"])).fetchone()
            if existing:
                fingerprint = data.get("payment_fingerprint")
                if fingerprint:
                    guest = c.execute("SELECT id FROM guests WHERE merchant_id=? AND payment_fingerprint=?", (merchant, fingerprint)).fetchone()
                    if not guest:
                        anonymous_id, stamp = uid("gst"), now()
                        c.execute("""INSERT INTO guests(id,merchant_id,payment_fingerprint,profile_status,created_at,updated_at) VALUES(?,?,?,'anonymous',?,?)""", (anonymous_id, merchant, fingerprint, stamp, stamp))
                        guest = {"id": anonymous_id}
                    c.execute("UPDATE orders SET payment_fingerprint=?,guest_id=COALESCE(guest_id,?) WHERE id=?", (fingerprint, guest["id"], existing["id"]))
                    self._rebuild_behavior(c, merchant, guest["id"])
                if data.get("items"):
                    c.execute("UPDATE orders SET occurred_at=?,total_cents=?,currency=?,raw_json=? WHERE id=?", (data["occurred_at"], data["total_cents"], data.get("currency", "USD"), json.dumps(data), existing["id"]))
                    c.execute("DELETE FROM order_items WHERE order_id=?", (existing["id"],))
                    for item in data["items"]:
                        c.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)", (uid("itm"), existing["id"], item["name"], normalize(item["name"]), item.get("quantity", 1), item["unit_price_cents"]))
                    owner = c.execute("SELECT guest_id FROM orders WHERE id=?", (existing["id"],)).fetchone()
                    if owner["guest_id"]: self._rebuild_behavior(c, merchant, owner["guest_id"])
                return {"id": existing["id"], "duplicate": True, "enriched": bool(fingerprint)}
            fingerprint = data.get("payment_fingerprint")
            guest = c.execute("SELECT id FROM guests WHERE merchant_id=? AND payment_fingerprint=?", (merchant, fingerprint)).fetchone()
            if not guest and fingerprint:
                anonymous_id, stamp = uid("gst"), now()
                c.execute("""INSERT INTO guests(id,merchant_id,payment_fingerprint,profile_status,created_at,updated_at)
                             VALUES(?,?,?,'anonymous',?,?)""", (anonymous_id, merchant, fingerprint, stamp, stamp))
                guest = {"id": anonymous_id}
                self._audit(c, merchant, "guest.anonymous_profile_created", "guest", anonymous_id, {"source": data["source"]})
            order_id = uid("ord")
            c.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?)", (order_id, merchant, data["external_id"], guest["id"] if guest else None, data.get("payment_fingerprint"), data["occurred_at"], data["total_cents"], data.get("currency", "USD"), data["source"], json.dumps(data)))
            for item in data.get("items", []):
                c.execute("INSERT INTO order_items VALUES(?,?,?,?,?,?)", (uid("itm"), order_id, item["name"], normalize(item["name"]), item.get("quantity", 1), item["unit_price_cents"]))
            self._audit(c, merchant, "order.ingested", "order", order_id, {"source": data["source"]})
            self._attribute(c, merchant, order_id, guest["id"] if guest else None, data["occurred_at"], data["total_cents"])
            if guest: self._rebuild_behavior(c, merchant, guest["id"])
        return {"id": order_id, "duplicate": False}

    def _rebuild_behavior(self, c, merchant, guest_id):
        orders = c.execute("SELECT id,occurred_at,total_cents FROM orders WHERE merchant_id=? AND guest_id=? ORDER BY occurred_at", (merchant, guest_id)).fetchall()
        if not orders: return
        parsed = []
        for order in orders:
            try: parsed.append(datetime.fromisoformat(order["occurred_at"].replace("Z", "+00:00")))
            except ValueError: continue
        intervals = [(right-left).total_seconds()/86400 for left,right in zip(parsed, parsed[1:]) if right > left]
        average_interval = sum(intervals)/len(intervals) if intervals else None
        next_visit = (parsed[-1] + timedelta(days=average_interval)).isoformat() if parsed and average_interval else None
        weekday = Counter(value.weekday() for value in parsed).most_common(1)[0][0] if parsed else None
        hour = Counter(value.hour for value in parsed).most_common(1)[0][0] if parsed else None
        current = datetime.now(timezone.utc)
        status = "new" if len(orders) < 2 else "overdue" if next_visit and datetime.fromisoformat(next_visit) < current else "active"
        confidence = min(1.0, len(orders) / 6)
        spend = sum(order["total_cents"] for order in orders)
        c.execute("""INSERT INTO behavior_profiles(guest_id,merchant_id,visit_count,lifetime_spend_cents,average_ticket_cents,first_visit_at,last_visit_at,average_interval_days,favorite_weekday,favorite_hour,predicted_next_visit_at,behavior_status,confidence,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(guest_id) DO UPDATE SET visit_count=excluded.visit_count,lifetime_spend_cents=excluded.lifetime_spend_cents,average_ticket_cents=excluded.average_ticket_cents,first_visit_at=excluded.first_visit_at,last_visit_at=excluded.last_visit_at,average_interval_days=excluded.average_interval_days,favorite_weekday=excluded.favorite_weekday,favorite_hour=excluded.favorite_hour,predicted_next_visit_at=excluded.predicted_next_visit_at,behavior_status=excluded.behavior_status,confidence=excluded.confidence,updated_at=excluded.updated_at""",
                  (guest_id, merchant, len(orders), spend, round(spend/len(orders)), orders[0]["occurred_at"], orders[-1]["occurred_at"], average_interval, weekday, hour, next_visit, status, confidence, now()))
        items = c.execute("""SELECT i.normalized_name,i.name,SUM(i.quantity) quantity,COUNT(DISTINCT o.id) orders,MAX(o.occurred_at) last_ordered
                          FROM order_items i JOIN orders o ON o.id=i.order_id WHERE o.merchant_id=? AND o.guest_id=?
                          GROUP BY i.normalized_name""", (merchant, guest_id)).fetchall()
        for item in items:
            c.execute("""INSERT INTO guest_item_affinities VALUES(?,?,?,?,?,?) ON CONFLICT(guest_id,normalized_item) DO UPDATE SET display_name=excluded.display_name,order_count=excluded.order_count,total_quantity=excluded.total_quantity,last_ordered_at=excluded.last_ordered_at""",
                      (guest_id, item["normalized_name"], item["name"], item["orders"], item["quantity"], item["last_ordered"]))

    def run_behavior_engine(self, merchant):
        with self.db.connect() as c:
            guests = c.execute("SELECT id FROM guests WHERE merchant_id=?", (merchant,)).fetchall()
            for guest in guests: self._rebuild_behavior(c, merchant, guest["id"])
            profiles = c.execute("""SELECT b.*,g.profile_status,g.phone,g.email,g.name FROM behavior_profiles b JOIN guests g ON g.id=b.guest_id WHERE b.merchant_id=?""", (merchant,)).fetchall()
            created = 0
            for profile in profiles:
                favorite = c.execute("SELECT normalized_item,display_name,order_count FROM guest_item_affinities WHERE guest_id=? ORDER BY order_count DESC LIMIT 1", (profile["guest_id"],)).fetchone()
                channel = "sms" if profile["profile_status"] == "identified" and profile["phone"] and self._consented(c, merchant, profile["guest_id"], "sms") else None
                if not channel and profile["profile_status"] == "identified" and profile["email"] and self._consented(c, merchant, profile["guest_id"], "email"): channel = "email"
                affinities = [dict(row) for row in c.execute("SELECT * FROM guest_item_affinities WHERE guest_id=? ORDER BY order_count DESC LIMIT 10", (profile["guest_id"],))]
                inventory = [dict(row) for row in c.execute("""SELECT p.canonical_name,p.current_invoice_date,v.quantity,v.unit,v.unit_price_cents,GROUP_CONCAT(m.name) menu_items FROM catalog_products p JOIN product_versions v ON v.id=p.current_version_id LEFT JOIN recipe_links r ON r.product_id=p.id AND r.status='confirmed' LEFT JOIN menu_items m ON m.id=r.menu_item_id AND m.active=1 WHERE p.merchant_id=? GROUP BY p.id ORDER BY p.current_invoice_date DESC LIMIT 30""", (merchant,))]
                context = {"now": now(), "profile": dict(profile), "favorite_items": affinities, "inventory_and_menu": inventory}
                if self.predictor:
                    opportunities = self.predictor.predict(context)
                elif profile["behavior_status"] == "overdue":
                    opportunities = [{"type": "habit_interruption", "item": favorite["normalized_item"] if favorite else "", "score": profile["confidence"], "reason": f"Expected return after {profile['visit_count']} visits", "send_at": now(), "subject": "Your usual is waiting", "message": f"Your usual {favorite['display_name'] if favorite else 'order'} is ready when you are."}]
                else: opportunities = []
                for opportunity in opportunities:
                    trigger = f"{profile['predicted_next_visit_at']}:{opportunity['type']}"
                    try:
                        prediction_id = uid("pred")
                        c.execute("INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (prediction_id, merchant, profile["guest_id"], opportunity["type"], normalize(opportunity["item"]), opportunity["score"], opportunity["reason"], channel, opportunity["send_at"], "eligible" if channel else "permission_required", trigger, now()))
                        if channel and opportunity["score"] >= .6:
                            c.execute("INSERT INTO campaigns VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (uid("cam"), merchant, profile["guest_id"], channel, opportunity["type"], prediction_id, opportunity["subject"], opportunity["message"], "queued", opportunity["send_at"], None, now()))
                        created += 1
                    except __import__("sqlite3").IntegrityError: pass
            self._audit(c, merchant, "behavior.engine_ran", "merchant", merchant, {"profiles": len(profiles), "predictions_created": created})
        return {"profiles_analyzed": len(profiles), "predictions_created": created}

    def behavior_dashboard(self, merchant):
        with self.db.connect() as c:
            rows = c.execute("""SELECT b.*,g.profile_status,g.name,g.phone,
                              (SELECT display_name FROM guest_item_affinities a WHERE a.guest_id=b.guest_id ORDER BY order_count DESC LIMIT 1) favorite_item
                              FROM behavior_profiles b JOIN guests g ON g.id=b.guest_id WHERE b.merchant_id=? ORDER BY b.last_visit_at DESC""", (merchant,)).fetchall()
        return {"customers": [dict(row) for row in rows]}

    def prediction_dashboard(self, merchant):
        with self.db.connect() as c: rows = c.execute("SELECT * FROM predictions WHERE merchant_id=? ORDER BY created_at DESC", (merchant,)).fetchall()
        return {"predictions": [dict(row) for row in rows]}

    def suppress(self, merchant, guest_id, channel, reason="customer_opt_out"):
        with self.db.connect() as c:
            c.execute("INSERT OR REPLACE INTO suppressions VALUES(?,?,?,?,?)", (merchant, guest_id, channel, reason, now()))
            c.execute("INSERT INTO consents VALUES(?,?,?,?,?,?,?,?)", (uid("con"), merchant, guest_id, channel, "denied", "opt-out", reason, now()))
            self._audit(c, merchant, "guest.suppressed", "guest", guest_id, {"channel": channel, "reason": reason})
        return {"guest_id": guest_id, "channel": channel, "suppressed": True}

    def dispatch_campaigns(self, merchant, limit=100):
        if not self.delivery: raise RuntimeError("message delivery is not configured")
        results = []
        with self.db.connect() as c:
            campaigns = c.execute("""SELECT c.*,g.phone,g.email FROM campaigns c JOIN guests g ON g.id=c.guest_id LEFT JOIN suppressions s ON s.merchant_id=c.merchant_id AND s.guest_id=c.guest_id AND s.channel=c.channel WHERE c.merchant_id=? AND c.status='queued' AND c.scheduled_at<=? AND s.guest_id IS NULL ORDER BY c.scheduled_at LIMIT ?""", (merchant, now(), limit)).fetchall()
        for campaign in campaigns:
            recipient = campaign["phone"] if campaign["channel"] == "sms" else campaign["email"]
            try:
                provider_id = self.delivery.send(campaign["channel"], recipient, campaign["subject"], campaign["body"])
                with self.db.connect() as c:
                    c.execute("UPDATE campaigns SET status='sent',sent_at=? WHERE id=?", (now(), campaign["id"]))
                    c.execute("INSERT INTO outbound_messages VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uid("msg"), merchant, campaign["id"], campaign["guest_id"], campaign["channel"], recipient, provider_id, "sent", None, now(), now()))
                results.append({"campaign_id": campaign["id"], "status": "sent", "provider_message_id": provider_id})
            except Exception as error:
                with self.db.connect() as c: c.execute("INSERT INTO outbound_messages VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uid("msg"), merchant, campaign["id"], campaign["guest_id"], campaign["channel"], recipient or "", None, "failed", str(error), None, now()))
                results.append({"campaign_id": campaign["id"], "status": "failed", "error": str(error)})
        return {"messages": results}

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
                line_id = uid("line")
                line_total = item.get("line_total_cents", round(item["quantity"] * item["unit_cost_cents"]))
                c.execute("INSERT INTO invoice_lines VALUES(?,?,?,?,?,?,?,?,?)", (line_id, invoice_id, item.get("sku"), item["ingredient"], ingredient, item["quantity"], item["unit"], item["unit_cost_cents"], line_total))
                c.execute("INSERT INTO inventory_events VALUES(?,?,?,?,?,?,?,?,?)", (uid("ive"), merchant, invoice_id, item["ingredient"], ingredient, item["quantity"], item["unit"], item["unit_cost_cents"], data["invoice_date"]))
                self._record_product_version(c, merchant, data["vendor"], invoice_id, line_id, data["invoice_date"], {"sku": item.get("sku"), "description": item["ingredient"], "quantity": item["quantity"], "unit": item["unit"], "unit_price_cents": item["unit_cost_cents"], "line_total_cents": line_total})
                created += self._create_inventory_campaigns(c, merchant, invoice_id, ingredient)
            self._audit(c, merchant, "invoice.processed", "invoice", invoice_id, {"campaigns_created": created})
        return {"id": invoice_id, "duplicate": False, "campaigns_created": created}

    def _create_inventory_campaigns(self, c, merchant, invoice_id, ingredient):
        rows = c.execute("""SELECT g.id,g.name,g.email,g.phone,g.profile_status,COUNT(*) purchases FROM guests g JOIN orders o ON o.guest_id=g.id JOIN order_items i ON i.order_id=o.id WHERE g.merchant_id=? AND i.normalized_name LIKE ? GROUP BY g.id HAVING purchases>=2""", (merchant, f"%{ingredient}%")).fetchall()
        count = 0
        for guest in rows:
            channel = "sms" if guest["profile_status"] == "identified" and guest["phone"] and self._consented(c, merchant, guest["id"], "sms") else "email" if guest["profile_status"] == "identified" and guest["email"] and self._consented(c, merchant, guest["id"], "email") else None
            score = min(1.0, guest["purchases"] / 5)
            c.execute("""INSERT OR IGNORE INTO predictions(id,merchant_id,guest_id,prediction_type,normalized_item,score,reason,recommended_channel,recommended_send_at,status,trigger_ref,created_at)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (uid("pred"), merchant, guest["id"], "inventory_match", ingredient, score, f"Fresh {ingredient} arrived and this guest ordered a matching item {guest['purchases']} times", channel, now(), "eligible" if channel else "permission_required", invoice_id, now()))
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
            guest = c.execute("SELECT id,name,email,phone,profile_status,terms_version,terms_accepted_at,permission_source,created_at,updated_at FROM guests WHERE merchant_id=? AND id=?", (merchant, guest_id)).fetchone()
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
            refunded = c.execute("""SELECT COALESCE(SUM(r.amount_cents),0) refunded_cents FROM refunds r JOIN attributions a ON a.order_id=r.order_id WHERE r.merchant_id=? AND r.status IN ('COMPLETED','completed')""", (merchant,)).fetchone()["refunded_cents"]
        result = dict(row); result["refunded_cents"] = refunded; result["net_attributed_revenue_cents"] = max(0, result["revenue_cents"] - refunded)
        return result
