import hashlib
import hmac
import json
import re
import secrets
import uuid
import base64
import statistics
import math
from collections import Counter
from .pos import ConfigurablePOSAdapter
from .inventory import InventoryEngine
from .evaluation import EvaluationEngine
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .messaging import DeliveryError

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
        self.inventory = InventoryEngine(db)
        self.evaluations = EvaluationEngine(db)

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
            stamp = now()
            c.execute("""INSERT INTO recipe_links(id,merchant_id,product_id,menu_item_id,quantity_required,unit,confidence,status,waste_percent,yield_percent,packaging_cost_cents,substitution_group,confirmed_by,confirmed_at,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(product_id,menu_item_id) DO UPDATE SET
                quantity_required=excluded.quantity_required,unit=excluded.unit,confidence=excluded.confidence,status=excluded.status,
                waste_percent=excluded.waste_percent,yield_percent=excluded.yield_percent,packaging_cost_cents=excluded.packaging_cost_cents,
                substitution_group=excluded.substitution_group,confirmed_by=excluded.confirmed_by,confirmed_at=excluded.confirmed_at""",
                (link_id, merchant, product["id"], menu["id"], data["quantity_required"], normalize(data["unit"]), data.get("confidence", 1.0), data.get("status", "confirmed"), data.get("waste_percent", 0), data.get("yield_percent", 100), data.get("packaging_cost_cents", 0), data.get("substitution_group"), data.get("confirmed_by", "restaurant_manager"), stamp if data.get("status", "confirmed") == "confirmed" else None, stamp))
        return {"id": link_id}

    def recipe_dashboard(self, merchant):
        with self.db.connect() as c:
            rows = c.execute("""SELECT r.id,p.canonical_name ingredient,p.sku,m.name menu_item,m.external_id,r.quantity_required,r.unit,r.confidence,r.status,
                r.waste_percent,r.yield_percent,r.packaging_cost_cents,r.substitution_group,r.confirmed_by,r.confirmed_at
                FROM recipe_links r JOIN catalog_products p ON p.id=r.product_id JOIN menu_items m ON m.id=r.menu_item_id WHERE r.merchant_id=? ORDER BY m.name""", (merchant,)).fetchall()
        return {"recipe_links": [dict(row) for row in rows]}

    def set_unit_conversion(self, merchant, data): return self.inventory.set_conversion(merchant, data)
    def propose_recipes(self, merchant): return self.inventory.propose_recipes(merchant)
    def review_recipe_proposal(self, merchant, proposal_id, data): return self.inventory.review_proposal(merchant, proposal_id, data)
    def adjust_inventory(self, merchant, data): return self.inventory.adjustment(merchant, data)
    def inventory_dashboard(self, merchant, incentive_cents=0): return self.inventory.dashboard(merchant, incentive_cents)
    def set_campaign_policy(self, merchant, data): return self.evaluations.set_policy(merchant, data)
    def evaluation_dashboard(self, merchant): return self.evaluations.dashboard(merchant)
    def run_backtest(self, merchant, data=None): return self.evaluations.backtest(merchant, int((data or {}).get("holdout", 1)))
    def run_message_evaluation(self, merchant): return self.evaluations.evaluate_messages(merchant)

    def approve_campaign(self, merchant, campaign_id, approved_by="restaurant_manager"):
        with self.db.connect() as c:
            campaign = c.execute("SELECT id FROM campaigns WHERE id=? AND merchant_id=? AND status='approval_required'", (campaign_id, merchant)).fetchone()
            if not campaign: raise KeyError("campaign awaiting approval not found")
            c.execute("UPDATE campaigns SET status='queued' WHERE id=?", (campaign_id,))
            self._audit(c, merchant, "campaign.approved", "campaign", campaign_id, {"approved_by": approved_by})
        return {"id": campaign_id, "status": "queued"}

    def update_operational_state(self, merchant, data):
        with self.db.connect() as c:
            c.execute("""INSERT INTO merchant_operational_state(merchant_id,accepting_orders,capacity_remaining,preparation_minutes,promotions_json,updated_at)
                         VALUES(?,?,?,?,?,?) ON CONFLICT(merchant_id) DO UPDATE SET accepting_orders=excluded.accepting_orders,capacity_remaining=excluded.capacity_remaining,preparation_minutes=excluded.preparation_minutes,promotions_json=excluded.promotions_json,updated_at=excluded.updated_at""", (merchant, 1 if data.get("accepting_orders", True) else 0, data.get("capacity_remaining"), data.get("preparation_minutes"), json.dumps(data.get("promotions", [])), now()))
        return self.operational_dashboard(merchant)

    def operational_dashboard(self, merchant):
        with self.db.connect() as c: row = c.execute("SELECT * FROM merchant_operational_state WHERE merchant_id=?", (merchant,)).fetchone()
        if not row: return {"accepting_orders": True, "capacity_remaining": None, "preparation_minutes": None, "promotions": []}
        result = dict(row); result["accepting_orders"] = bool(result["accepting_orders"]); result["promotions"] = json.loads(result.pop("promotions_json") or "[]"); return result

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
            if not guest:
                c.execute("""INSERT INTO guests(id,merchant_id,payment_fingerprint,name,email,phone,profile_status,terms_version,terms_accepted_at,permission_source,created_at,updated_at)
                             VALUES(?,?,?,?,?,?, 'anonymous',NULL,NULL,NULL,?,?)""", (guest_id, merchant, fingerprint, None, None, None, stamp, stamp))
            c.execute("INSERT OR IGNORE INTO guest_identities VALUES(?,?,?,?,?,?,?,?,?)", (uid("gid"), merchant, guest_id, "payment_fingerprint", fingerprint, 0, "identity_capture", stamp, stamp))
            self._activate_identity(c, merchant, guest_id, data, stamp)
            c.execute("UPDATE orders SET guest_id=? WHERE merchant_id=? AND payment_fingerprint=? AND guest_id IS NULL", (guest_id, merchant, fingerprint))
        return self.guest_profile(merchant, guest_id)

    def _activate_identity(self, c, merchant, guest_id, data, stamp=None):
        terms, stamp = data.get("terms") or {}, stamp or now()
        if not data.get("phone"): raise ValueError("phone is required to activate an identifiable customer profile")
        if terms.get("accepted") is not True or not terms.get("version"): raise ValueError("explicit acceptance and a terms version are required")
        guest = c.execute("SELECT id FROM guests WHERE merchant_id=? AND id=?", (merchant, guest_id)).fetchone()
        if not guest: raise KeyError("guest not found")
        c.execute("UPDATE guests SET name=COALESCE(?,name),email=COALESCE(?,email),phone=?,profile_status='identified',terms_version=?,terms_accepted_at=?,permission_source=?,updated_at=? WHERE id=?",
                  (data.get("name"), data.get("email"), data["phone"], terms["version"], stamp, terms.get("source", "checkout"), stamp, guest_id))
        for channel in ("email", "sms"):
            consent = data.get("consent", {}).get(channel)
            if consent:
                if consent.get("status") not in ("granted", "denied") or not consent.get("disclosure_version"): raise ValueError(f"valid {channel} consent and disclosure_version are required")
                c.execute("INSERT INTO consents VALUES(?,?,?,?,?,?,?,?)", (uid("con"), merchant, guest_id, channel, consent["status"], consent["disclosure_version"], consent.get("source", terms.get("source", "checkout")), stamp))
        self._audit(c, merchant, "guest.permission_activated", "guest", guest_id, {"channels": list(data.get("consent", {})), "terms_version": terms["version"], "source": terms.get("source", "checkout")})

    def create_identity_claim(self, merchant, data):
        """Create a short-lived website link tied to the exact POS order/guest."""
        source, external = data.get("source", "square"), data.get("external_order_id")
        if not external: raise ValueError("external_order_id is required")
        token, stamp = secrets.token_urlsafe(32), now()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=min(60, max(5, int(data.get("expires_in_minutes", 15)))))).isoformat()
        with self.db.connect() as c:
            order = c.execute("SELECT id,guest_id FROM orders WHERE merchant_id=? AND source=? AND external_id=?", (merchant, source, external)).fetchone()
            if not order or not order["guest_id"]: raise KeyError("POS order does not have a linkable customer profile")
            claim_id = uid("clm")
            c.execute("INSERT INTO identity_claims VALUES(?,?,?,?,?,?,?,?)", (claim_id, merchant, order["guest_id"], order["id"], hashlib.sha256(token.encode()).hexdigest(), expires, None, stamp))
            self._audit(c, merchant, "identity.claim_created", "identity_claim", claim_id, {"order_id": order["id"], "source": source})
        return {"claim_token": token, "expires_at": expires, "claim_path": "/v1/identity/claim"}

    def accept_identity_claim(self, token, data):
        """Public opt-in: phone/terms are attached only to the guest behind this claim."""
        if not token: raise ValueError("claim_token is required")
        stamp = now()
        with self.db.connect() as c:
            claim = c.execute("SELECT * FROM identity_claims WHERE token_hash=? AND used_at IS NULL", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if not claim or claim["expires_at"] < stamp: raise ValueError("identity claim is invalid, expired, or already used")
            self._activate_identity(c, claim["merchant_id"], claim["guest_id"], data, stamp)
            c.execute("UPDATE identity_claims SET used_at=? WHERE id=? AND used_at IS NULL", (stamp, claim["id"]))
            self._audit(c, claim["merchant_id"], "identity.claim_accepted", "identity_claim", claim["id"], {"guest_id": claim["guest_id"]}, actor="guest")
        return self.guest_profile(claim["merchant_id"], claim["guest_id"])

    def _resolve_guest(self, c, merchant, data):
        """Resolve only provider-backed identifiers; never merge on names or behavior."""
        customer_id, fingerprint = data.get("provider_customer_id"), data.get("payment_fingerprint")
        customer = c.execute("SELECT guest_id FROM guest_identities WHERE merchant_id=? AND identity_type='provider_customer_id' AND identity_value=?", (merchant, customer_id)).fetchone() if customer_id else None
        card = c.execute("SELECT guest_id FROM guest_identities WHERE merchant_id=? AND identity_type='payment_fingerprint' AND identity_value=?", (merchant, fingerprint)).fetchone() if fingerprint else None
        if fingerprint and not card:
            legacy = c.execute("SELECT id FROM guests WHERE merchant_id=? AND payment_fingerprint=?", (merchant, fingerprint)).fetchone()
            if legacy:
                stamp = now(); c.execute("INSERT OR IGNORE INTO guest_identities VALUES(?,?,?,?,?,?,?,?,?)", (uid("gid"), merchant, legacy["id"], "payment_fingerprint", fingerprint, 0, "legacy_profile", stamp, stamp)); card = {"guest_id": legacy["id"]}
        guest_id = customer["guest_id"] if customer else card["guest_id"] if card else None
        if not guest_id and (customer_id or fingerprint):
            guest_id, stamp = uid("gst"), now()
            c.execute("INSERT INTO guests(id,merchant_id,payment_fingerprint,profile_status,created_at,updated_at) VALUES(?,?,?,'anonymous',?,?)", (guest_id, merchant, fingerprint, stamp, stamp))
            self._audit(c, merchant, "guest.anonymous_profile_created", "guest", guest_id, {"source": data["source"]})
        if not guest_id: return None
        stamp = now()
        if customer_id and not customer:
            c.execute("INSERT OR IGNORE INTO guest_identities VALUES(?,?,?,?,?,?,?,?,?)", (uid("gid"), merchant, guest_id, "provider_customer_id", customer_id, 1, data["source"], stamp, stamp))
        # A fingerprint becomes a verified customer relationship only when the same
        # provider transaction supplies both identifiers. Conflicts are retained as
        # separate profiles rather than silently merged.
        if fingerprint and not card:
            c.execute("INSERT OR IGNORE INTO guest_identities VALUES(?,?,?,?,?,?,?,?,?)", (uid("gid"), merchant, guest_id, "payment_fingerprint", fingerprint, 1 if customer_id else 0, data["source"], stamp, stamp))
            c.execute("UPDATE guests SET payment_fingerprint=COALESCE(payment_fingerprint,?),updated_at=? WHERE id=?", (fingerprint, stamp, guest_id))
        return guest_id

    def _insert_order_items(self, c, order_id, items):
        for item in items:
            modifiers = item.get("modifiers") or []
            c.execute("""INSERT INTO order_items(id,order_id,name,normalized_name,quantity,unit_price_cents,catalog_object_id,modifiers_json)
                         VALUES(?,?,?,?,?,?,?,?)""", (uid("itm"), order_id, item["name"], normalize(item["name"]), item.get("quantity", 1), item["unit_price_cents"], item.get("catalog_object_id"), json.dumps(modifiers)))

    def ingest_order(self, merchant, data):
        if any(k in data for k in ("card_number", "pan", "cvv")): raise ValueError("raw card data is forbidden")
        status = str(data.get("status", "completed")).lower()
        with self.db.connect() as c:
            existing = c.execute("SELECT id FROM orders WHERE merchant_id=? AND source=? AND external_id=?", (merchant, data["source"], data["external_id"])).fetchone()
            if existing:
                guest_id = self._resolve_guest(c, merchant, data)
                previous = c.execute("SELECT guest_id FROM orders WHERE id=?", (existing["id"],)).fetchone()["guest_id"]
                resolved_owner = None if previous and guest_id and previous != guest_id else previous or guest_id
                c.execute("""UPDATE orders SET guest_id=?,payment_fingerprint=COALESCE(?,payment_fingerprint),provider_customer_id=COALESCE(?,provider_customer_id),payment_id=COALESCE(?,payment_id),occurred_at=?,total_cents=?,currency=?,status=?,location_id=COALESCE(?,location_id),fulfillment_type=COALESCE(?,fulfillment_type),discount_cents=?,is_test=?,raw_json=? WHERE id=?""",
                          (resolved_owner, data.get("payment_fingerprint"), data.get("provider_customer_id"), data.get("payment_id"), data["occurred_at"], data["total_cents"], data.get("currency", "USD"), status, data.get("location_id"), data.get("fulfillment_type"), data.get("discount_cents", 0), 1 if data.get("is_test") else 0, json.dumps(data), existing["id"]))
                if data.get("items"):
                    c.execute("DELETE FROM order_items WHERE order_id=?", (existing["id"],))
                    self._insert_order_items(c, existing["id"], data["items"])
                owner = resolved_owner
                if owner: self._rebuild_behavior(c, merchant, owner)
                if previous and previous != owner: self._rebuild_behavior(c, merchant, previous)
                return {"id": existing["id"], "duplicate": True, "enriched": bool(guest_id)}
            guest_id = self._resolve_guest(c, merchant, data)
            order_id = uid("ord")
            c.execute("""INSERT INTO orders(id,merchant_id,external_id,guest_id,payment_fingerprint,occurred_at,total_cents,currency,source,raw_json,status,location_id,fulfillment_type,discount_cents,is_test,provider_customer_id,payment_id)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (order_id, merchant, data["external_id"], guest_id, data.get("payment_fingerprint"), data["occurred_at"], data["total_cents"], data.get("currency", "USD"), data["source"], json.dumps(data), status, data.get("location_id"), data.get("fulfillment_type"), data.get("discount_cents", 0), 1 if data.get("is_test") else 0, data.get("provider_customer_id"), data.get("payment_id")))
            self._insert_order_items(c, order_id, data.get("items", []))
            self._audit(c, merchant, "order.ingested", "order", order_id, {"source": data["source"]})
            if status == "completed" and not data.get("is_test"):
                self._attribute(c, merchant, order_id, guest_id, data["occurred_at"], data["total_cents"])
            if guest_id: self._rebuild_behavior(c, merchant, guest_id)
        return {"id": order_id, "duplicate": False}

    def _rebuild_behavior(self, c, merchant, guest_id):
        orders = c.execute("""SELECT o.id,o.occurred_at,
            MAX(0,o.total_cents-COALESCE((SELECT SUM(r.amount_cents) FROM refunds r WHERE r.order_id=o.id AND lower(r.status)='completed'),0)) total_cents,
            o.location_id,o.fulfillment_type,o.discount_cents
            FROM orders o WHERE o.merchant_id=? AND o.guest_id=? AND lower(o.status)='completed' AND o.is_test=0
            AND o.total_cents>COALESCE((SELECT SUM(r.amount_cents) FROM refunds r WHERE r.order_id=o.id AND lower(r.status)='completed'),0)
            ORDER BY o.occurred_at""", (merchant, guest_id)).fetchall()
        if not orders:
            c.execute("DELETE FROM behavior_profiles WHERE guest_id=?", (guest_id,)); c.execute("DELETE FROM guest_item_affinities WHERE guest_id=?", (guest_id,)); c.execute("DELETE FROM guest_item_pairs WHERE guest_id=?", (guest_id,)); c.execute("DELETE FROM guest_modifier_affinities WHERE guest_id=?", (guest_id,)); return
        parsed = []
        for order in orders:
            try: parsed.append(datetime.fromisoformat(order["occurred_at"].replace("Z", "+00:00")))
            except ValueError: continue
        intervals = [(right-left).total_seconds()/86400 for left,right in zip(parsed, parsed[1:]) if right > left]
        average_interval = statistics.median(intervals) if intervals else None
        interval_stddev = statistics.pstdev(intervals) if len(intervals) > 1 else 0 if intervals else None
        next_visit = (parsed[-1] + timedelta(days=average_interval)).isoformat() if parsed and average_interval else None
        weekdays, hours = Counter(value.weekday() for value in parsed), Counter(value.hour for value in parsed)
        weekday = weekdays.most_common(1)[0][0] if parsed else None
        hour = hours.most_common(1)[0][0] if parsed else None
        current = datetime.now(timezone.utc)
        last = parsed[-1] if parsed else None
        days_since = int(max(0, (current-last).total_seconds()/86400)) if last else None
        overdue_by = int(max(0, (current-datetime.fromisoformat(next_visit)).total_seconds()/86400)) if next_visit else 0
        status = "new" if len(orders) < 2 else "dormant" if overdue_by > max(60, 2*(average_interval or 30)) else "overdue" if overdue_by > max(1, (interval_stddev or 0)) else "due" if next_visit and datetime.fromisoformat(next_visit) <= current + timedelta(days=2) else "approaching" if next_visit and datetime.fromisoformat(next_visit) <= current + timedelta(days=7) else "active"
        regularity = 1 / (1 + ((interval_stddev or 0) / average_interval)) if average_interval else .25
        confidence = round(min(.99, (len(orders) / (len(orders) + 3)) * regularity), 4)
        spend = sum(order["total_cents"] for order in orders)
        remaining = max(1.0, (average_interval or 14) - min(days_since or 0, (average_interval or 14) * .9))
        probabilities = {str(horizon): round(max(0, min(.99, confidence * (1-math.exp(-horizon/remaining)))), 4) for horizon in (1,3,7,14)}
        locations = Counter(order["location_id"] for order in orders if order["location_id"])
        fulfillments = Counter(order["fulfillment_type"] for order in orders if order["fulfillment_type"])
        daypart = "morning" if hour is not None and hour < 11 else "lunch" if hour is not None and hour < 15 else "afternoon" if hour is not None and hour < 17 else "dinner" if hour is not None and hour < 22 else "late_night" if hour is not None else None
        discount_rate = sum(order["discount_cents"] > 0 for order in orders) / len(orders)
        median_ticket = round(statistics.median(order["total_cents"] for order in orders))
        c.execute("""INSERT INTO behavior_profiles(guest_id,merchant_id,visit_count,lifetime_spend_cents,average_ticket_cents,first_visit_at,last_visit_at,average_interval_days,favorite_weekday,favorite_hour,predicted_next_visit_at,behavior_status,confidence,interval_stddev_days,days_since_last_visit,overdue_by_days,weekday_distribution_json,hour_distribution_json,median_ticket_cents,return_probabilities_json,preferred_daypart,preferred_location_id,preferred_fulfillment_type,discount_visit_rate,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(guest_id) DO UPDATE SET visit_count=excluded.visit_count,lifetime_spend_cents=excluded.lifetime_spend_cents,average_ticket_cents=excluded.average_ticket_cents,first_visit_at=excluded.first_visit_at,last_visit_at=excluded.last_visit_at,average_interval_days=excluded.average_interval_days,favorite_weekday=excluded.favorite_weekday,favorite_hour=excluded.favorite_hour,predicted_next_visit_at=excluded.predicted_next_visit_at,behavior_status=excluded.behavior_status,confidence=excluded.confidence,interval_stddev_days=excluded.interval_stddev_days,days_since_last_visit=excluded.days_since_last_visit,overdue_by_days=excluded.overdue_by_days,weekday_distribution_json=excluded.weekday_distribution_json,hour_distribution_json=excluded.hour_distribution_json,median_ticket_cents=excluded.median_ticket_cents,return_probabilities_json=excluded.return_probabilities_json,preferred_daypart=excluded.preferred_daypart,preferred_location_id=excluded.preferred_location_id,preferred_fulfillment_type=excluded.preferred_fulfillment_type,discount_visit_rate=excluded.discount_visit_rate,updated_at=excluded.updated_at""",
                  (guest_id, merchant, len(orders), spend, round(spend/len(orders)), orders[0]["occurred_at"], orders[-1]["occurred_at"], average_interval, weekday, hour, next_visit, status, confidence, interval_stddev, days_since, overdue_by, json.dumps(weekdays), json.dumps(hours), median_ticket, json.dumps(probabilities), daypart, locations.most_common(1)[0][0] if locations else None, fulfillments.most_common(1)[0][0] if fulfillments else None, discount_rate, now()))
        item_rows = c.execute("""SELECT o.id order_id,o.occurred_at,i.normalized_name,i.name,i.quantity,i.unit_price_cents
                              FROM order_items i JOIN orders o ON o.id=i.order_id
                              WHERE o.merchant_id=? AND o.guest_id=? AND lower(o.status)='completed' AND o.is_test=0
                              AND o.total_cents>COALESCE((SELECT SUM(r.amount_cents) FROM refunds r WHERE r.order_id=o.id AND lower(r.status)='completed'),0)
                              ORDER BY o.occurred_at""", (merchant, guest_id)).fetchall()
        grouped, order_sets = {}, {}
        for item in item_rows:
            bucket = grouped.setdefault(item["normalized_name"], {"name": item["name"], "orders": set(), "quantity": 0, "spend": 0, "dates": []})
            bucket["orders"].add(item["order_id"]); bucket["quantity"] += item["quantity"]
            bucket["spend"] += round(item["quantity"] * item["unit_price_cents"])
            try: bucket["dates"].append(datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00")))
            except ValueError: pass
            order_sets.setdefault(item["order_id"], {"at": item["occurred_at"], "items": set()})["items"].add(item["normalized_name"])
        c.execute("DELETE FROM guest_item_affinities WHERE guest_id=?", (guest_id,))
        for item_key, item in grouped.items():
            dates = sorted(set(item["dates"])); gaps = [(b-a).total_seconds()/86400 for a,b in zip(dates, dates[1:]) if b > a]
            cadence = statistics.median(gaps) if gaps else None
            item_weekdays, item_hours = Counter(d.weekday() for d in dates), Counter(d.hour for d in dates)
            predicted = (dates[-1] + timedelta(days=cadence)).isoformat() if dates and cadence else None
            c.execute("INSERT INTO guest_item_affinities(guest_id,normalized_item,display_name,order_count,total_quantity,last_ordered_at,total_spend_cents,average_interval_days,preferred_weekday,preferred_hour,predicted_next_order_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (guest_id, item_key, item["name"], len(item["orders"]), item["quantity"], dates[-1].isoformat() if dates else orders[-1]["occurred_at"], item["spend"], cadence, item_weekdays.most_common(1)[0][0] if dates else None, item_hours.most_common(1)[0][0] if dates else None, predicted))
        pairs = {}
        for order_data in order_sets.values():
            values = sorted(order_data["items"])
            for index, first in enumerate(values):
                for second in values[index+1:]: pairs[(first, second)] = (pairs.get((first, second), (0, order_data["at"]))[0] + 1, order_data["at"])
        c.execute("DELETE FROM guest_item_pairs WHERE guest_id=?", (guest_id,))
        for (first, second), (count, last_ordered) in pairs.items():
            c.execute("INSERT INTO guest_item_pairs VALUES(?,?,?,?,?)", (guest_id, first, second, count, last_ordered))
        modifier_counts = {}
        for row in c.execute("""SELECT i.modifiers_json,o.occurred_at FROM order_items i JOIN orders o ON o.id=i.order_id
                              WHERE o.merchant_id=? AND o.guest_id=? AND lower(o.status)='completed' AND o.is_test=0""", (merchant, guest_id)):
            for modifier in json.loads(row["modifiers_json"] or "[]"):
                name = modifier.get("name") if isinstance(modifier, dict) else str(modifier)
                if name: modifier_counts[normalize(name)] = (name, modifier_counts.get(normalize(name), (name, 0, row["occurred_at"]))[1] + 1, row["occurred_at"])
        c.execute("DELETE FROM guest_modifier_affinities WHERE guest_id=?", (guest_id,))
        for key, (_, count, ordered_at) in modifier_counts.items(): c.execute("INSERT INTO guest_modifier_affinities VALUES(?,?,?,?)", (guest_id, key, count, ordered_at))

    def refresh_order_behavior(self, merchant, order_id):
        with self.db.connect() as c:
            order = c.execute("SELECT guest_id FROM orders WHERE merchant_id=? AND id=?", (merchant, order_id)).fetchone()
            if order and order["guest_id"]: self._rebuild_behavior(c, merchant, order["guest_id"])

    def run_behavior_engine(self, merchant):
        inventory_guardrails = self.inventory.dashboard(merchant)
        inventory_menu = {normalize(row["menu_item"]): row for row in inventory_guardrails["menu_items"]}
        policy = self.evaluations.policy(merchant)
        with self.db.connect() as c:
            guests = c.execute("SELECT id FROM guests WHERE merchant_id=?", (merchant,)).fetchall()
            for guest in guests: self._rebuild_behavior(c, merchant, guest["id"])
            profiles = c.execute("""SELECT b.*,g.profile_status,g.phone,g.email,g.name FROM behavior_profiles b JOIN guests g ON g.id=b.guest_id WHERE b.merchant_id=?""", (merchant,)).fetchall()
            inventory = [dict(row) for row in c.execute("""SELECT p.canonical_name,p.current_invoice_date,v.quantity,v.unit,v.unit_price_cents,GROUP_CONCAT(m.name) menu_items,
                MAX(0,v.quantity-COALESCE(SUM((SELECT COALESCE(SUM(oi.quantity),0) FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE o.merchant_id=p.merchant_id AND lower(o.status)='completed' AND o.is_test=0 AND o.occurred_at>=p.current_invoice_date AND (oi.catalog_object_id=m.external_id OR oi.normalized_name=m.normalized_name))*r.quantity_required),0)) estimated_remaining
                FROM catalog_products p JOIN product_versions v ON v.id=p.current_version_id LEFT JOIN recipe_links r ON r.product_id=p.id AND r.status='confirmed' LEFT JOIN menu_items m ON m.id=r.menu_item_id AND m.active=1 WHERE p.merchant_id=? GROUP BY p.id ORDER BY p.current_invoice_date DESC LIMIT 30""", (merchant,))]
            menu_economics = [dict(row) for row in c.execute("""SELECT m.name menu_item,m.price_cents,
                ROUND(SUM(v.unit_price_cents*r.quantity_required)) estimated_ingredient_cost_cents,
                m.price_cents-ROUND(SUM(v.unit_price_cents*r.quantity_required)) estimated_gross_margin_cents,
                GROUP_CONCAT(p.canonical_name) ingredients
                FROM menu_items m JOIN recipe_links r ON r.menu_item_id=m.id AND r.status='confirmed'
                JOIN catalog_products p ON p.id=r.product_id JOIN product_versions v ON v.id=p.current_version_id
                WHERE m.merchant_id=? AND m.active=1 GROUP BY m.id ORDER BY estimated_gross_margin_cents DESC""", (merchant,))]
            operational_row = c.execute("SELECT accepting_orders,capacity_remaining,preparation_minutes,promotions_json FROM merchant_operational_state WHERE merchant_id=?", (merchant,)).fetchone()
            operational = {"accepting_orders": bool(operational_row["accepting_orders"]), "capacity_remaining": operational_row["capacity_remaining"], "preparation_minutes": operational_row["preparation_minutes"], "promotions": json.loads(operational_row["promotions_json"] or "[]")} if operational_row else {"accepting_orders": True, "capacity_remaining": None, "preparation_minutes": None, "promotions": []}
            created = 0
            for profile in profiles:
                favorite = c.execute("SELECT normalized_item,display_name,order_count FROM guest_item_affinities WHERE guest_id=? ORDER BY order_count DESC,total_spend_cents DESC LIMIT 1", (profile["guest_id"],)).fetchone()
                channel = "sms" if profile["profile_status"] == "identified" and profile["phone"] and self._consented(c, merchant, profile["guest_id"], "sms") else None
                if not channel and profile["profile_status"] == "identified" and profile["email"] and self._consented(c, merchant, profile["guest_id"], "email"): channel = "email"
                affinities = [dict(row) for row in c.execute("SELECT * FROM guest_item_affinities WHERE guest_id=? ORDER BY order_count DESC,total_spend_cents DESC LIMIT 10", (profile["guest_id"],))]
                pairs = [dict(row) for row in c.execute("SELECT first_item,second_item,order_count,last_ordered_at FROM guest_item_pairs WHERE guest_id=? ORDER BY order_count DESC LIMIT 10", (profile["guest_id"],))]
                engagement = dict(c.execute("""SELECT COUNT(*) campaigns,
                    (SELECT COUNT(*) FROM message_events me JOIN outbound_messages om ON om.id=me.outbound_message_id WHERE om.merchant_id=? AND om.guest_id=? AND me.event_type='delivered') delivered,
                    (SELECT COUNT(*) FROM message_events me JOIN outbound_messages om ON om.id=me.outbound_message_id WHERE om.merchant_id=? AND om.guest_id=? AND me.event_type='opened') opened,
                    (SELECT COUNT(*) FROM message_events me JOIN outbound_messages om ON om.id=me.outbound_message_id WHERE om.merchant_id=? AND om.guest_id=? AND me.event_type='clicked') clicked,
                    (SELECT COALESCE(SUM(converted),0) FROM campaign_outcomes WHERE merchant_id=? AND guest_id=?) conversions,
                    (SELECT COALESCE(SUM(unsubscribed),0) FROM campaign_outcomes WHERE merchant_id=? AND guest_id=?) unsubscribes
                    FROM campaigns WHERE merchant_id=? AND guest_id=?""", (merchant, profile["guest_id"], merchant, profile["guest_id"], merchant, profile["guest_id"], merchant, profile["guest_id"], merchant, profile["guest_id"], merchant, profile["guest_id"])).fetchone())
                # Contact details and names never leave Orbit for prediction. The model
                # receives behavioral facts only; delivery resolves the recipient later.
                behavior = {key: profile[key] for key in ("visit_count", "lifetime_spend_cents", "average_ticket_cents", "median_ticket_cents", "first_visit_at", "last_visit_at", "average_interval_days", "interval_stddev_days", "days_since_last_visit", "overdue_by_days", "favorite_weekday", "favorite_hour", "predicted_next_visit_at", "behavior_status", "confidence", "weekday_distribution_json", "hour_distribution_json", "return_probabilities_json")}
                decision_facts = {"profile": behavior, "favorite_items": affinities, "frequent_item_pairs": pairs, "message_history": engagement, "recent_supplier_deliveries": inventory, "menu_economics": menu_economics, "operational_state": operational}
                context_key = hashlib.sha256(json.dumps(decision_facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
                if c.execute("SELECT 1 FROM predictions WHERE merchant_id=? AND guest_id=? AND trigger_ref LIKE ? LIMIT 1", (merchant, profile["guest_id"], f"{context_key}:%")).fetchone():
                    continue
                context = {"now": now(), **decision_facts}
                if self.predictor:
                    daily_limit = max(1, int(__import__("os").getenv("OPENAI_PREDICTION_MAX_DAILY_CALLS", "1000")))
                    today = datetime.now(timezone.utc).date().isoformat()
                    calls_today = c.execute("SELECT COUNT(*) n FROM prediction_runs WHERE merchant_id=? AND component='strategy_copy' AND created_at>=?", (merchant, today)).fetchone()["n"]
                    if calls_today >= daily_limit:
                        opportunities = []
                        metadata = {"model": self.predictor.__class__.__name__, "prompt_version": "budget-guard", "latency_ms": 0, "attempts": 0, "fallback": True}
                    else:
                        opportunities = self.predictor.predict(context)
                        metadata = getattr(self.predictor, "last_run_metadata", None) or {"model": self.predictor.__class__.__name__, "prompt_version": "unknown", "latency_ms": None, "attempts": 1, "fallback": False}
                    c.execute("INSERT INTO prediction_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (uid("prun"), merchant, profile["guest_id"], "strategy_copy", metadata.get("prompt_version", "unknown"), "fallback" if metadata.get("fallback") else "completed", metadata.get("latency_ms"), None, context_key, json.dumps(opportunities), None, metadata.get("attempts", 1), now()))
                elif profile["behavior_status"] == "overdue":
                    opportunities = [{"type": "habit_interruption", "item": favorite["normalized_item"] if favorite else "", "score": profile["confidence"], "reason": f"Expected return after {profile['visit_count']} visits", "send_at": now(), "subject": "Your usual is waiting", "message": f"Your usual {favorite['display_name'] if favorite else 'order'} is ready when you are."}]
                else: opportunities = []
                for opportunity in opportunities:
                    trigger = f"{context_key}:{opportunity['type']}"
                    try:
                        prediction_id = uid("pred")
                        item_key = favorite["normalized_item"] if favorite else normalize(opportunity.get("item", ""))
                        statistical_probabilities = json.loads(profile["return_probabilities_json"] or "{}")
                        statistical_score = max([float(profile["confidence"]), *[float(value) for value in statistical_probabilities.values()]])
                        economics = next((row for key,row in inventory_menu.items() if item_key and (item_key in key or key in item_key)), None)
                        delivery_row = next((row for row in inventory if item_key and (item_key in normalize(row["canonical_name"]) or item_key in normalize(row.get("menu_items") or ""))), None)
                        freshness_days = int(__import__("os").getenv("ORBIT_INVENTORY_FRESHNESS_DAYS", "90"))
                        fresh_after = (datetime.now(timezone.utc)-timedelta(days=freshness_days)).date().isoformat()
                        recent_delivery = bool(delivery_row and delivery_row.get("estimated_remaining", 0) > 0 and invoice_date_key(delivery_row.get("current_invoice_date")) >= fresh_after)
                        suppressed = bool(channel and c.execute("SELECT 1 FROM suppressions WHERE merchant_id=? AND guest_id=? AND channel=?", (merchant, profile["guest_id"], channel)).fetchone())
                        cooldown_days = int(__import__("os").getenv("ORBIT_CONTACT_COOLDOWN_DAYS", "7"))
                        cooldown_start = (datetime.now(timezone.utc)-timedelta(days=cooldown_days)).isoformat()
                        contacted_recently = bool(channel and c.execute("SELECT 1 FROM campaigns WHERE merchant_id=? AND guest_id=? AND channel=? AND sent_at>=?", (merchant, profile["guest_id"], channel, cooldown_start)).fetchone())
                        expected_value = int(profile["median_ticket_cents"] or profile["average_ticket_cents"])
                        margin = int(economics["estimated_contribution_margin_cents"]) if economics else 0
                        message_cost = int(__import__("os").getenv("ORBIT_SMS_COST_CENTS", "2")) if channel == "sms" else int(__import__("os").getenv("ORBIT_EMAIL_COST_CENTS", "1"))
                        expected_incremental_profit = round(max(0, margin) * statistical_score - message_cost)
                        capacity_available = operational["accepting_orders"] and (operational["capacity_remaining"] is None or operational["capacity_remaining"] > 0)
                        inventory_available = bool(economics and economics["estimated_portions"] > 0 and economics["status"] not in ("probably_unavailable", "inventory_uncertain"))
                        eligibility = {"authorized_channel": bool(channel), "not_suppressed": not suppressed, "cooldown_clear": not contacted_recently, "menu_and_recipe_confirmed": bool(economics), "estimated_inventory_available": inventory_available and recent_delivery, "inventory_confidence": economics["inventory_confidence"] if economics else 0, "capacity_available": capacity_available, "positive_expected_incremental_profit": expected_incremental_profit > 0 and margin >= policy["minimum_margin_cents"], "expected_incremental_profit_cents": expected_incremental_profit}
                        model_action = opportunity.get("action") or ("send_sms" if channel == "sms" else "send_email" if channel == "email" else "do_nothing")
                        do_not_contact = bool(opportunity.get("do_not_contact")) or statistical_score < .6 or not all((eligibility["authorized_channel"], eligibility["not_suppressed"], eligibility["cooldown_clear"], eligibility["menu_and_recipe_confirmed"], eligibility["estimated_inventory_available"], eligibility["capacity_available"], eligibility["positive_expected_incremental_profit"])) or model_action in ("wait", "do_nothing")
                        prediction_status = "permission_required" if not channel else "do_not_contact" if do_not_contact else "eligible"
                        probabilities = {f"within_{key}_days": value for key,value in statistical_probabilities.items()}
                        basket = [row["display_name"] for row in affinities[:3]] or [opportunity.get("item", "")]
                        statistical_center = profile["predicted_next_visit_at"]
                        if statistical_center:
                            center = datetime.fromisoformat(statistical_center.replace("Z", "+00:00"))
                            time_window_start, time_window_end = (center-timedelta(minutes=45)).isoformat(), (center+timedelta(minutes=45)).isoformat()
                        else:
                            time_window_start, time_window_end = opportunity.get("time_window_start"), opportunity.get("time_window_end")
                        c.execute("""INSERT INTO predictions(id,merchant_id,guest_id,prediction_type,normalized_item,score,reason,recommended_channel,recommended_send_at,status,trigger_ref,created_at,action,expected_order_value_cents,return_probabilities_json,time_window_start,time_window_end,predicted_basket_json,do_not_contact,eligibility_json)
                                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (prediction_id, merchant, profile["guest_id"], opportunity["type"], item_key, statistical_score, opportunity["reason"], channel, opportunity["send_at"], prediction_status, trigger, now(), "do_nothing" if do_not_contact else model_action, expected_value, json.dumps(probabilities), time_window_start, time_window_end, json.dumps(basket), 1 if do_not_contact else 0, json.dumps(eligibility)))
                        if not do_not_contact:
                            cohort_size = c.execute("SELECT COUNT(*) count FROM behavior_profiles WHERE merchant_id=?", (merchant,)).fetchone()["count"]
                            control_percent = max(0, min(50, int(__import__("os").getenv("ORBIT_CONTROL_PERCENT", "10"))))
                            control = cohort_size >= 20 and int(hashlib.sha256(f"{profile['guest_id']}:{trigger}".encode()).hexdigest()[:8], 16) % 100 < control_percent
                            campaign_id = uid("cam")
                            window_end = opportunity.get("time_window_end") or (datetime.fromisoformat(opportunity["send_at"].replace("Z", "+00:00")) + timedelta(days=7)).isoformat()
                            needs_approval = policy["mode"] == "pilot" or (policy["mode"] == "assisted" and (statistical_score < policy["automation_threshold"] or economics["inventory_confidence"] < policy["minimum_inventory_confidence"] or model_action == "incentive"))
                            campaign_status = "control" if control else "approval_required" if needs_approval else "queued"
                            c.execute("""INSERT INTO campaigns(id,merchant_id,guest_id,channel,trigger_type,trigger_ref,subject,body,status,scheduled_at,sent_at,created_at,action,control_group,prediction_window_end,eligibility_json)
                                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (campaign_id, merchant, profile["guest_id"], channel, opportunity["type"], prediction_id, opportunity["subject"], opportunity["message"], "control" if control else "queued", opportunity["send_at"], None, now(), model_action, 1 if control else 0, window_end, json.dumps(eligibility)))
                            c.execute("UPDATE campaigns SET status=? WHERE id=?", (campaign_status, campaign_id))
                            c.execute("INSERT INTO campaign_outcomes(campaign_id,merchant_id,guest_id,group_name) VALUES(?,?,?,?)", (campaign_id, merchant, profile["guest_id"], "control" if control else "messaged"))
                        created += 1
                    except __import__("sqlite3").IntegrityError: pass
            self._audit(c, merchant, "behavior.engine_ran", "merchant", merchant, {"profiles": len(profiles), "predictions_created": created})
        return {"profiles_analyzed": len(profiles), "predictions_created": created}

    def behavior_worker_loop(self, stop_event, interval=900):
        """Continuously refresh every restaurant without requiring a dashboard click."""
        while not stop_event.is_set():
            try:
                with self.db.connect() as c: merchants = [row["id"] for row in c.execute("SELECT id FROM merchants")]
                for merchant in merchants:
                    try: self.run_behavior_engine(merchant)
                    except Exception as error: print(f"Behavior engine failed for {merchant}: {error}", flush=True)
            except Exception as error: print(f"Behavior worker failed: {error}", flush=True)
            stop_event.wait(interval)

    def behavior_dashboard(self, merchant):
        with self.db.connect() as c:
            rows = c.execute("""SELECT b.*,g.profile_status,g.name,g.phone,g.email,
                              (SELECT display_name FROM guest_item_affinities a WHERE a.guest_id=b.guest_id ORDER BY order_count DESC,total_spend_cents DESC LIMIT 1) favorite_item
                              FROM behavior_profiles b JOIN guests g ON g.id=b.guest_id WHERE b.merchant_id=? ORDER BY b.last_visit_at DESC""", (merchant,)).fetchall()
            customers = []
            for row in rows:
                customer = dict(row)
                customer["weekday_distribution"] = json.loads(customer.pop("weekday_distribution_json") or "{}")
                customer["hour_distribution"] = json.loads(customer.pop("hour_distribution_json") or "{}")
                customer["return_probabilities"] = json.loads(customer.pop("return_probabilities_json") or "{}")
                customer["favorite_items"] = [dict(item) for item in c.execute("SELECT display_name,order_count,total_quantity,total_spend_cents,last_ordered_at,average_interval_days,preferred_weekday,preferred_hour,predicted_next_order_at FROM guest_item_affinities WHERE guest_id=? ORDER BY order_count DESC,total_spend_cents DESC LIMIT 10", (row["guest_id"],))]
                customer["frequent_combinations"] = [dict(pair) for pair in c.execute("SELECT first_item,second_item,order_count,last_ordered_at FROM guest_item_pairs WHERE guest_id=? ORDER BY order_count DESC LIMIT 10", (row["guest_id"],))]
                customer["favorite_modifiers"] = [dict(modifier) for modifier in c.execute("SELECT modifier_name,order_count,last_ordered_at FROM guest_modifier_affinities WHERE guest_id=? ORDER BY order_count DESC LIMIT 10", (row["guest_id"],))]
                customers.append(customer)
        return {"summary": {"profile_count": len(customers), "identified_count": sum(customer["profile_status"] == "identified" for customer in customers), "overdue_count": sum(customer["behavior_status"] == "overdue" for customer in customers)}, "customers": customers}

    def prediction_dashboard(self, merchant):
        with self.db.connect() as c: rows = c.execute("SELECT * FROM predictions WHERE merchant_id=? ORDER BY created_at DESC", (merchant,)).fetchall()
        predictions = []
        for row in rows:
            prediction = dict(row)
            for source, target, default in (("return_probabilities_json", "return_probabilities", {}), ("predicted_basket_json", "predicted_basket", []), ("eligibility_json", "eligibility", {})):
                prediction[target] = json.loads(prediction.pop(source) or json.dumps(default))
            predictions.append(prediction)
        return {"predictions": predictions}

    def suppress(self, merchant, guest_id, channel, reason="customer_opt_out"):
        with self.db.connect() as c:
            c.execute("INSERT OR REPLACE INTO suppressions VALUES(?,?,?,?,?)", (merchant, guest_id, channel, reason, now()))
            c.execute("INSERT INTO consents VALUES(?,?,?,?,?,?,?,?)", (uid("con"), merchant, guest_id, channel, "denied", "opt-out", reason, now()))
            self._audit(c, merchant, "guest.suppressed", "guest", guest_id, {"channel": channel, "reason": reason})
        return {"guest_id": guest_id, "channel": channel, "suppressed": True}

    def update_messaging_settings(self, merchant, data):
        timezone_name = data.get("timezone", "UTC")
        try: ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error: raise ValueError("invalid IANA timezone") from error
        for key in ("quiet_hours_start", "quiet_hours_end"):
            value = data.get(key, "21:00" if key.endswith("start") else "08:00")
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value): raise ValueError(f"invalid {key}")
        with self.db.connect() as c:
            c.execute("""INSERT INTO messaging_settings VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(merchant_id) DO UPDATE SET
                timezone=excluded.timezone,quiet_hours_start=excluded.quiet_hours_start,quiet_hours_end=excluded.quiet_hours_end,
                max_messages_per_guest_24h=excluded.max_messages_per_guest_24h,max_messages_per_merchant_day=excluded.max_messages_per_merchant_day,
                sms_help_text=excluded.sms_help_text,updated_at=excluded.updated_at""", (merchant, timezone_name, data.get("quiet_hours_start", "21:00"), data.get("quiet_hours_end", "08:00"), max(1, int(data.get("max_messages_per_guest_24h", 1))), max(1, int(data.get("max_messages_per_merchant_day", 100))), data.get("sms_help_text", "Reply STOP to opt out. Reply START to opt back in."), now()))
        return self.messaging_settings(merchant)

    def messaging_settings(self, merchant):
        with self.db.connect() as c: row = c.execute("SELECT * FROM messaging_settings WHERE merchant_id=?", (merchant,)).fetchone()
        return dict(row) if row else {"merchant_id": merchant, "timezone": "UTC", "quiet_hours_start": "21:00", "quiet_hours_end": "08:00", "max_messages_per_guest_24h": 1, "max_messages_per_merchant_day": 100, "sms_help_text": "Reply STOP to opt out. Reply START to opt back in."}

    def _messaging_allowed(self, c, merchant, guest_id, settings):
        local = datetime.now(timezone.utc).astimezone(ZoneInfo(settings["timezone"]))
        current, start, end = local.strftime("%H:%M"), settings["quiet_hours_start"], settings["quiet_hours_end"]
        quiet = start <= current < end if start < end else current >= start or current < end
        guest_count = c.execute("SELECT COUNT(*) n FROM outbound_messages WHERE merchant_id=? AND guest_id=? AND status IN ('sent','delivered') AND sent_at>=?", (merchant, guest_id, (datetime.now(timezone.utc)-timedelta(hours=24)).isoformat())).fetchone()["n"]
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        merchant_count = c.execute("SELECT COUNT(*) n FROM outbound_messages WHERE merchant_id=? AND status IN ('sent','delivered') AND sent_at>=?", (merchant, day_start)).fetchone()["n"]
        return {"quiet_hours_clear": not quiet, "guest_frequency_clear": guest_count < settings["max_messages_per_guest_24h"], "merchant_frequency_clear": merchant_count < settings["max_messages_per_merchant_day"]}

    def dispatch_campaigns(self, merchant, limit=100):
        if not self.delivery: raise RuntimeError("message delivery is not configured")
        results = []
        with self.db.connect() as c:
            campaigns = c.execute("""SELECT c.*,g.phone,g.email FROM campaigns c JOIN guests g ON g.id=c.guest_id LEFT JOIN suppressions s ON s.merchant_id=c.merchant_id AND s.guest_id=c.guest_id AND s.channel=c.channel WHERE c.merchant_id=? AND c.status='queued' AND c.scheduled_at<=? AND s.guest_id IS NULL ORDER BY c.scheduled_at LIMIT ?""", (merchant, now(), limit)).fetchall()
        for campaign in campaigns:
            recipient = campaign["phone"] if campaign["channel"] == "sms" else campaign["email"]
            try:
                with self.db.connect() as c:
                    already_ordered = c.execute("SELECT 1 FROM orders WHERE merchant_id=? AND guest_id=? AND lower(status)='completed' AND occurred_at>=? LIMIT 1", (merchant, campaign["guest_id"], campaign["created_at"])).fetchone()
                    if not self._consented(c, merchant, campaign["guest_id"], campaign["channel"]):
                        c.execute("UPDATE campaigns SET status='permission_revoked' WHERE id=?", (campaign["id"],)); results.append({"campaign_id": campaign["id"], "status": "permission_revoked"}); continue
                    if already_ordered:
                        c.execute("UPDATE campaigns SET status='stale_order_exists' WHERE id=?", (campaign["id"],)); results.append({"campaign_id": campaign["id"], "status": "stale_order_exists"}); continue
                    settings = self.messaging_settings(merchant)
                    checks = self._messaging_allowed(c, merchant, campaign["guest_id"], settings)
                    if not all(checks.values()): results.append({"campaign_id": campaign["id"], "status": "deferred", "checks": checks}); continue
                    existing_message = c.execute("SELECT * FROM outbound_messages WHERE campaign_id=? ORDER BY created_at DESC LIMIT 1", (campaign["id"],)).fetchone()
                    attempts = (existing_message["attempts"] if existing_message else 0) + 1
                    if existing_message and existing_message["next_attempt_at"] and existing_message["next_attempt_at"] > now():
                        results.append({"campaign_id": campaign["id"], "status": "retry_scheduled", "next_attempt_at": existing_message["next_attempt_at"]}); continue
                    message_id = existing_message["id"] if existing_message else uid("msg")
                    idempotency_key = existing_message["idempotency_key"] if existing_message else f"orbit-{campaign['id']}"
                    provider = "telnyx" if campaign["channel"] == "sms" else "resend"
                    if existing_message: c.execute("UPDATE outbound_messages SET status='sending',attempts=?,error=NULL WHERE id=?", (attempts, message_id))
                    else: c.execute("""INSERT INTO outbound_messages(id,merchant_id,campaign_id,guest_id,channel,recipient,provider_message_id,status,error,sent_at,created_at,provider,attempts,next_attempt_at,last_event_at,dead_lettered_at,idempotency_key)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (message_id, merchant, campaign["id"], campaign["guest_id"], campaign["channel"], recipient, None, "sending", None, None, now(), provider, attempts, None, None, None, idempotency_key))
                provider_id = self.delivery.send(campaign["channel"], recipient, campaign["subject"], campaign["body"], idempotency_key=idempotency_key)
                with self.db.connect() as c:
                    c.execute("UPDATE campaigns SET status='sent',sent_at=? WHERE id=?", (now(), campaign["id"]))
                    c.execute("UPDATE outbound_messages SET provider_message_id=?,status='sent',sent_at=?,next_attempt_at=NULL WHERE id=?", (provider_id, now(), message_id))
                results.append({"campaign_id": campaign["id"], "status": "sent", "provider_message_id": provider_id})
            except Exception as error:
                retryable = getattr(error, "retryable", True); max_attempts = max(1, int(__import__("os").getenv("ORBIT_MESSAGE_MAX_ATTEMPTS", "5")))
                dead = not retryable or attempts >= max_attempts
                retry_at = None if dead else (datetime.now(timezone.utc)+timedelta(seconds=min(3600, 30*(2**(attempts-1))))).isoformat()
                with self.db.connect() as c:
                    c.execute("UPDATE outbound_messages SET status=?,error=?,next_attempt_at=?,dead_lettered_at=? WHERE id=?", ("dead" if dead else "retrying", str(error), retry_at, now() if dead else None, message_id))
                    if dead: c.execute("UPDATE campaigns SET status='delivery_failed' WHERE id=?", (campaign["id"],))
                results.append({"campaign_id": campaign["id"], "status": "dead" if dead else "retrying", "next_attempt_at": retry_at, "error": str(error)})
        return {"messages": results}

    def message_worker_loop(self, stop_event, interval=30):
        """Dispatch approved campaigns and retry transient Telnyx/Resend failures."""
        while not stop_event.is_set():
            try:
                with self.db.connect() as c: merchants = [row["id"] for row in c.execute("SELECT id FROM merchants")]
                for merchant in merchants:
                    try: self.dispatch_campaigns(merchant)
                    except Exception as error: print(f"Message worker failed for {merchant}: {error}", flush=True)
            except Exception as error: print(f"Message worker failed: {error}", flush=True)
            stop_event.wait(interval)

    def dead_letters(self, merchant):
        with self.db.connect() as c:
            rows = c.execute("""SELECT om.id,om.campaign_id,om.channel,om.recipient,om.provider,om.attempts,om.error,om.dead_lettered_at,c.subject
                FROM outbound_messages om JOIN campaigns c ON c.id=om.campaign_id
                WHERE om.merchant_id=? AND om.status='dead' ORDER BY om.dead_lettered_at DESC""", (merchant,)).fetchall()
        return {"dead_letters": [dict(row) for row in rows]}

    def retry_dead_letter(self, merchant, message_id):
        with self.db.connect() as c:
            message = c.execute("SELECT campaign_id FROM outbound_messages WHERE id=? AND merchant_id=? AND status='dead'", (message_id, merchant)).fetchone()
            if not message: raise KeyError("dead-letter message not found")
            c.execute("UPDATE outbound_messages SET status='retrying',attempts=0,next_attempt_at=?,dead_lettered_at=NULL,error=NULL WHERE id=?", (now(), message_id))
            c.execute("UPDATE campaigns SET status='queued' WHERE id=?", (message["campaign_id"],))
            self._audit(c, merchant, "message.dead_letter_retried", "outbound_message", message_id)
        return {"id": message_id, "status": "retrying"}

    def record_message_event(self, merchant, data):
        event_type = data["event_type"].lower()
        if event_type not in ("queued", "sent", "delivered", "opened", "clicked", "bounced", "complained", "failed", "unsubscribed"): raise ValueError("unsupported message event")
        with self.db.connect() as c:
            message = c.execute("SELECT id,guest_id,channel,campaign_id FROM outbound_messages WHERE merchant_id=? AND provider_message_id=?", (merchant, data["provider_message_id"])).fetchone()
            if not message: raise KeyError("message not found")
            event_id, occurred = uid("mse"), data.get("occurred_at", now())
            c.execute("INSERT OR IGNORE INTO message_events VALUES(?,?,?,?,?,?)", (event_id, merchant, message["id"], event_type, occurred, json.dumps(data.get("metadata", {}))))
            if event_type in ("bounced", "complained", "failed"): c.execute("UPDATE outbound_messages SET status=?,error=?,last_event_at=? WHERE id=?", (event_type, data.get("error"), occurred, message["id"]))
            elif event_type in ("queued", "sent", "delivered"): c.execute("UPDATE outbound_messages SET status=?,last_event_at=? WHERE id=?", (event_type, occurred, message["id"]))
            if event_type in ("unsubscribed", "complained"):
                c.execute("INSERT OR REPLACE INTO suppressions VALUES(?,?,?,?,?)", (merchant, message["guest_id"], message["channel"], "provider_unsubscribe", occurred))
                c.execute("INSERT INTO consents VALUES(?,?,?,?,?,?,?,?)", (uid("con"), merchant, message["guest_id"], message["channel"], "denied", "provider-unsubscribe", "provider_unsubscribe", occurred))
                c.execute("UPDATE campaign_outcomes SET unsubscribed=1 WHERE campaign_id=?", (message["campaign_id"],))
        return {"event_id": event_id, "status": "recorded"}

    def record_provider_event(self, provider, event_id, event_type, provider_message_id, occurred_at=None, metadata=None):
        occurred = occurred_at or now(); normalized = event_type.lower()
        aliases = {"message.sent": "sent", "message.delivered": "delivered", "message.failed": "failed", "email.sent": "sent", "email.delivered": "delivered", "email.bounced": "bounced", "email.complained": "complained", "email.delivery_delayed": "queued"}
        normalized = aliases.get(normalized, normalized)
        with self.db.connect() as c:
            existing = c.execute("SELECT status FROM provider_webhook_events WHERE provider=? AND provider_event_id=?", (provider, event_id)).fetchone()
            if existing: return {"status": existing["status"], "duplicate": True}
            c.execute("INSERT INTO provider_webhook_events VALUES(?,?,?,?,?,?,?,?,?)", (uid("pwe"), provider, event_id, event_type, json.dumps(metadata or {}), "processing", None, now(), None))
            message = c.execute("SELECT merchant_id FROM outbound_messages WHERE provider=? AND provider_message_id=?", (provider, provider_message_id)).fetchone()
        if not message:
            with self.db.connect() as c: c.execute("UPDATE provider_webhook_events SET status='unmatched',processed_at=? WHERE provider=? AND provider_event_id=?", (now(), provider, event_id))
            return {"status": "unmatched", "duplicate": False}
        result = self.record_message_event(message["merchant_id"], {"provider_message_id": provider_message_id, "event_type": normalized, "occurred_at": occurred, "metadata": metadata or {}})
        with self.db.connect() as c: c.execute("UPDATE provider_webhook_events SET status='processed',processed_at=? WHERE provider=? AND provider_event_id=?", (now(), provider, event_id))
        return {**result, "duplicate": False}

    def handle_inbound_sms(self, data):
        text, sender = (data.get("text") or "").strip(), data.get("from")
        keyword = text.upper().split()[0] if text else ""
        if keyword not in {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "START", "UNSTOP", "HELP", "INFO"}:
            return {"status": "ignored"}
        with self.db.connect() as c:
            guests = c.execute("SELECT id,merchant_id,terms_accepted_at FROM guests WHERE phone=?", (sender,)).fetchall()
            changed = 0
            for guest in guests:
                if keyword in {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}:
                    c.execute("INSERT OR REPLACE INTO suppressions VALUES(?,?,?,?,?)", (guest["merchant_id"], guest["id"], "sms", "telnyx_stop", now()))
                    c.execute("INSERT INTO consents VALUES(?,?,?,?,?,?,?,?)", (uid("con"), guest["merchant_id"], guest["id"], "sms", "denied", "telnyx-keyword", "telnyx_stop", now())); changed += 1
                elif keyword in {"START", "UNSTOP"} and guest["terms_accepted_at"]:
                    c.execute("DELETE FROM suppressions WHERE merchant_id=? AND guest_id=? AND channel='sms'", (guest["merchant_id"], guest["id"]))
                    c.execute("INSERT INTO consents VALUES(?,?,?,?,?,?,?,?)", (uid("con"), guest["merchant_id"], guest["id"], "sms", "granted", "telnyx-keyword", "telnyx_start", now())); changed += 1
        if keyword in {"HELP", "INFO"} and self.delivery:
            help_text = __import__("os").getenv("ORBIT_SMS_HELP_TEXT", "OrbitGuest restaurant messages. Reply STOP to opt out. Reply START to opt back in.")
            self.delivery.send("sms", sender, "", help_text, idempotency_key=f"help-{hashlib.sha256((sender+text).encode()).hexdigest()[:24]}")
        return {"status": "processed", "keyword": keyword, "profiles_updated": changed}

    def _attribute(self, c, merchant, order_id, guest_id, occurred_at, revenue):
        if not guest_id: return
        campaign = c.execute("""SELECT id,status,control_group,COALESCE(sent_at,scheduled_at) exposure_at FROM campaigns
                              WHERE merchant_id=? AND guest_id=? AND status IN ('sent','control')
                              AND COALESCE(sent_at,scheduled_at)<=? AND (prediction_window_end IS NULL OR prediction_window_end>=?)
                              ORDER BY COALESCE(sent_at,scheduled_at) DESC LIMIT 1""", (merchant, guest_id, occurred_at, occurred_at)).fetchone()
        if campaign:
            seconds = max(0, int((datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))-datetime.fromisoformat(campaign["exposure_at"].replace("Z", "+00:00"))).total_seconds()))
            c.execute("UPDATE campaign_outcomes SET converted=1,order_id=?,revenue_cents=?,seconds_to_order=?,evaluated_at=? WHERE campaign_id=? AND converted=0", (order_id, revenue, seconds, now(), campaign["id"]))
            if not campaign["control_group"]: c.execute("INSERT OR IGNORE INTO attributions VALUES(?,?,?,?,?,?)", (uid("att"), merchant, campaign["id"], order_id, revenue, now()))

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
                # Supplier data updates operational facts. Customer predictions are
                # produced by the behavior engine only after recipe/menu checks.
            self._audit(c, merchant, "invoice.processed", "invoice", invoice_id, {"campaigns_created": created})
        return {"id": invoice_id, "duplicate": False, "campaigns_created": created}

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
            c.execute("UPDATE campaign_outcomes SET evaluated_at=COALESCE(evaluated_at,?) WHERE merchant_id=? AND campaign_id IN (SELECT id FROM campaigns WHERE prediction_window_end<?)", (now(), merchant, now()))
            row = c.execute("SELECT COUNT(DISTINCT campaign_id) converted_campaigns,COUNT(*) attributed_orders,COALESCE(SUM(revenue_cents),0) revenue_cents FROM attributions WHERE merchant_id=?", (merchant,)).fetchone()
            refunded = c.execute("""SELECT COALESCE(SUM(r.amount_cents),0) refunded_cents FROM refunds r JOIN attributions a ON a.order_id=r.order_id WHERE r.merchant_id=? AND r.status IN ('COMPLETED','completed')""", (merchant,)).fetchone()["refunded_cents"]
            groups = {entry["group_name"]: dict(entry) for entry in c.execute("SELECT group_name,COUNT(*) size,SUM(converted) conversions,COALESCE(SUM(revenue_cents),0) revenue FROM campaign_outcomes WHERE merchant_id=? GROUP BY group_name", (merchant,))}
            events = {entry["event_type"]: entry["count"] for entry in c.execute("SELECT event_type,COUNT(*) count FROM message_events WHERE merchant_id=? GROUP BY event_type", (merchant,))}
        result = dict(row); result["refunded_cents"] = refunded; result["net_attributed_revenue_cents"] = max(0, result["revenue_cents"] - refunded)
        messaged, control = groups.get("messaged", {"size": 0, "conversions": 0, "revenue": 0}), groups.get("control", {"size": 0, "conversions": 0, "revenue": 0})
        messaged_rate = (messaged["conversions"] or 0) / messaged["size"] if messaged["size"] else 0
        control_rate = (control["conversions"] or 0) / control["size"] if control["size"] else None
        average_value = (messaged["revenue"] or 0) / (messaged["conversions"] or 1)
        incremental = None if control_rate is None else round(max(0, messaged_rate-control_rate) * messaged["size"] * average_value)
        result["experiment"] = {"messaged_size": messaged["size"], "control_size": control["size"], "messaged_conversion_rate": round(messaged_rate, 4), "control_conversion_rate": round(control_rate, 4) if control_rate is not None else None, "estimated_incremental_revenue_cents": incremental}
        result["message_events"] = events
        return result
