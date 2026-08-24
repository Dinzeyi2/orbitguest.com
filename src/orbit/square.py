"""Production Square OAuth, API, webhook verification, and order synchronization."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

def utcnow(): return datetime.now(timezone.utc).isoformat()

class SquareError(RuntimeError): pass

class TokenCipher:
    def __init__(self, key=None):
        key = key or os.getenv("TOKEN_ENCRYPTION_KEY")
        if not key: raise SquareError("TOKEN_ENCRYPTION_KEY is required for Square OAuth")
        try:
            from cryptography.fernet import Fernet
            self.fernet = Fernet(key.encode())
        except ImportError as error: raise SquareError("cryptography package is required") from error
        except Exception as error: raise SquareError("TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from error
    def encrypt(self, value): return self.fernet.encrypt(value.encode()).decode()
    def decrypt(self, value): return self.fernet.decrypt(value.encode()).decode()

class SquareClient:
    def __init__(self, access_token=None, environment=None):
        self.access_token = access_token
        self.environment = environment or os.getenv("SQUARE_ENVIRONMENT", "production")
        self.base = "https://connect.squareupsandbox.com" if self.environment == "sandbox" else "https://connect.squareup.com"
        self.version = os.getenv("SQUARE_API_VERSION")

    def request(self, method, path, body=None, token=None, attempts=3):
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "OrbitGuest/1.0"}
        if self.version: headers["Square-Version"] = self.version
        auth = token or self.access_token
        if auth: headers["Authorization"] = f"Bearer {auth}"
        request = urllib.request.Request(self.base + path, payload, headers, method=method)
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=30) as response: return json.load(response)
            except urllib.error.HTTPError as error:
                detail = error.read().decode()[:1000]
                if error.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                    time.sleep(2 ** attempt); continue
                raise SquareError(f"Square API failed ({error.code}): {detail}") from error
            except OSError as error:
                if attempt + 1 < attempts: time.sleep(2 ** attempt); continue
                raise SquareError(f"Square API network failure: {error}") from error

    def exchange_code(self, code, redirect_uri):
        return self.request("POST", "/oauth2/token", {"client_id": os.environ["SQUARE_APPLICATION_ID"], "client_secret": os.environ["SQUARE_APPLICATION_SECRET"], "code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri, "short_lived": True})
    def refresh(self, refresh_token):
        return self.request("POST", "/oauth2/token", {"client_id": os.environ["SQUARE_APPLICATION_ID"], "client_secret": os.environ["SQUARE_APPLICATION_SECRET"], "refresh_token": refresh_token, "grant_type": "refresh_token"})

class SquareIntegration:
    SCOPES = "MERCHANT_PROFILE_READ ORDERS_READ PAYMENTS_READ CUSTOMERS_READ ITEMS_READ"
    def __init__(self, db, orbit, cipher=None):
        self.db, self.orbit = db, orbit
        self.app_id = os.getenv("SQUARE_APPLICATION_ID")
        self.signature_key = os.getenv("SQUARE_WEBHOOK_SIGNATURE_KEY")
        self.public_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        self.redirect_uri = os.getenv("SQUARE_REDIRECT_URI") or f"{self.public_url}/v1/integrations/square/callback"
        self.webhook_url = os.getenv("SQUARE_WEBHOOK_URL") or f"{self.public_url}/v1/webhooks/square"
        self.cipher = cipher

    def _cipher(self):
        if not self.cipher: self.cipher = TokenCipher()
        return self.cipher

    def authorize(self, merchant):
        if not self.app_id or not self.public_url: raise SquareError("SQUARE_APPLICATION_ID and PUBLIC_BASE_URL are required")
        state, stamp = secrets.token_urlsafe(32), utcnow()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        with self.db.connect() as c: c.execute("INSERT INTO square_oauth_states VALUES(?,?,?,?,?)", (state, merchant, expires, None, stamp))
        host = "https://connect.squareupsandbox.com" if os.getenv("SQUARE_ENVIRONMENT") == "sandbox" else "https://connect.squareup.com"
        query = urllib.parse.urlencode({"client_id": self.app_id, "scope": self.SCOPES, "session": "false", "state": state, "redirect_uri": self.redirect_uri})
        return {"authorization_url": f"{host}/oauth2/authorize?{query}", "expires_at": expires}

    def callback(self, code, state):
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM square_oauth_states WHERE state=? AND used_at IS NULL", (state,)).fetchone()
            if not row or row["expires_at"] < utcnow(): raise SquareError("invalid or expired OAuth state")
            c.execute("UPDATE square_oauth_states SET used_at=? WHERE state=?", (utcnow(), state))
        token = SquareClient().exchange_code(code, self.redirect_uri)
        installation_id, stamp = f"sqi_{secrets.token_hex(16)}", utcnow()
        cipher = self._cipher()
        with self.db.connect() as c:
            c.execute("""INSERT INTO square_installations VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(merchant_id) DO UPDATE SET square_merchant_id=excluded.square_merchant_id,encrypted_access_token=excluded.encrypted_access_token,encrypted_refresh_token=excluded.encrypted_refresh_token,token_expires_at=excluded.token_expires_at,status='active',updated_at=excluded.updated_at""", (installation_id, row["merchant_id"], token["merchant_id"], cipher.encrypt(token["access_token"]), cipher.encrypt(token["refresh_token"]) if token.get("refresh_token") else None, token.get("expires_at"), "active", stamp, stamp))
        self.sync_locations(row["merchant_id"])
        return {"merchant_id": row["merchant_id"], "square_merchant_id": token["merchant_id"], "status": "connected"}

    def _installation(self, merchant):
        with self.db.connect() as c: row = c.execute("SELECT * FROM square_installations WHERE merchant_id=? AND status='active'", (merchant,)).fetchone()
        if not row: raise SquareError("Square is not connected")
        if row["token_expires_at"] and row["token_expires_at"] <= (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat():
            refreshed = SquareClient().refresh(self._cipher().decrypt(row["encrypted_refresh_token"]))
            with self.db.connect() as c: c.execute("UPDATE square_installations SET encrypted_access_token=?,encrypted_refresh_token=?,token_expires_at=?,updated_at=? WHERE id=?", (self._cipher().encrypt(refreshed["access_token"]), self._cipher().encrypt(refreshed.get("refresh_token") or self._cipher().decrypt(row["encrypted_refresh_token"])), refreshed.get("expires_at"), utcnow(), row["id"]))
            return self._installation(merchant)
        return row, SquareClient(self._cipher().decrypt(row["encrypted_access_token"]))

    def sync_locations(self, merchant):
        installation, client = self._installation(merchant)
        locations = client.request("GET", "/v2/locations").get("locations", [])
        with self.db.connect() as c:
            for location in locations:
                stamp = utcnow(); location_id = f"sql_{hashlib.sha256(location['id'].encode()).hexdigest()[:24]}"
                c.execute("""INSERT INTO square_locations VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(square_location_id) DO UPDATE SET name=excluded.name,timezone=excluded.timezone,status=excluded.status,updated_at=excluded.updated_at""", (location_id, installation["id"], merchant, location["id"], location.get("name"), location.get("timezone"), location.get("status", "ACTIVE").lower(), stamp, stamp))
        return {"locations": len(locations)}

    def verify_webhook(self, raw, signature):
        if not self.signature_key or not self.webhook_url or not signature: return False
        expected = base64.b64encode(hmac.new(self.signature_key.encode(), self.webhook_url.encode() + raw, hashlib.sha256).digest()).decode()
        return hmac.compare_digest(expected, signature)

    def enqueue_webhook(self, raw):
        event = json.loads(raw); event_id, event_type = event["event_id"], event["type"]
        data = event.get("data", {}); obj = data.get("object", {})
        location_id = (obj.get("order") or obj.get("payment") or {}).get("location_id")
        with self.db.connect() as c:
            existing = c.execute("SELECT status FROM square_webhook_events WHERE event_id=?", (event_id,)).fetchone()
            if existing: return {"event_id": event_id, "duplicate": True}
            c.execute("""INSERT INTO square_webhook_events(event_id,event_type,square_merchant_id,square_location_id,payload_json,status,error,received_at,processed_at,attempts,next_attempt_at)
                         VALUES(?,?,?,?,?,'pending',?,?,NULL,0,?)""", (event_id, event_type, event.get("merchant_id"), location_id, json.dumps(event), None, utcnow(), utcnow()))
        return {"event_id": event_id, "duplicate": False, "status": "pending"}

    def receive_webhook(self, raw):
        queued = self.enqueue_webhook(raw)
        if queued["duplicate"]: return queued
        self.process_pending(limit=1, event_id=queued["event_id"])
        with self.db.connect() as c: row = c.execute("SELECT status,error FROM square_webhook_events WHERE event_id=?", (queued["event_id"],)).fetchone()
        if row["status"] != "processed": raise SquareError(row["error"] or "Square event processing failed")
        return {**queued, "status": "processed"}

    def process_pending(self, limit=25, event_id=None):
        current = utcnow()
        with self.db.connect() as c:
            if event_id:
                rows = c.execute("SELECT * FROM square_webhook_events WHERE event_id=? AND status='pending'", (event_id,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM square_webhook_events WHERE status='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY received_at LIMIT ?", (current, limit)).fetchall()
        processed = 0
        for row in rows:
            with self.db.connect() as c:
                c.execute("UPDATE square_webhook_events SET status='processing',attempts=attempts+1 WHERE event_id=? AND status='pending'", (row["event_id"],))
                if not c.execute("SELECT changes()").fetchone()[0]: continue
            try:
                self._process_event(json.loads(row["payload_json"]))
                with self.db.connect() as c: c.execute("UPDATE square_webhook_events SET status='processed',error=NULL,processed_at=? WHERE event_id=?", (utcnow(), row["event_id"]))
                processed += 1
            except Exception as error:
                attempts = row["attempts"] + 1
                retry_at = (datetime.now(timezone.utc) + timedelta(seconds=min(3600, 2 ** min(attempts, 10)))).isoformat()
                status = "dead" if attempts >= 10 else "pending"
                with self.db.connect() as c: c.execute("UPDATE square_webhook_events SET status=?,error=?,next_attempt_at=?,processed_at=? WHERE event_id=?", (status, str(error)[:2000], retry_at, utcnow(), row["event_id"]))
        return {"processed": processed, "examined": len(rows)}

    def _process_event(self, event):
        event_type = event["type"]
        data = event.get("data", {}); obj = data.get("object", {})
        location_id = (obj.get("order") or obj.get("payment") or {}).get("location_id")
        merchant = self._merchant_for(event.get("merchant_id"), location_id)
        if event_type.startswith("order."): self._ingest_square_order(merchant, obj["order"])
        elif event_type.startswith("payment.") and obj.get("payment", {}).get("order_id"):
            installation, client = self._installation(merchant)
            order = client.request("GET", f"/v2/orders/{obj['payment']['order_id']}")["order"]
            self._ingest_square_order(merchant, order, obj["payment"])
        elif event_type.startswith("refund."):
            refund = obj.get("refund", {})
            installation, client = self._installation(merchant)
            payment = client.request("GET", f"/v2/payments/{refund['payment_id']}").get("payment", {})
            with self.db.connect() as c:
                order = c.execute("SELECT id FROM orders WHERE merchant_id=? AND source='square' AND external_id=?", (merchant, payment.get("order_id"))).fetchone()
                c.execute("""INSERT INTO refunds VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(merchant_id,external_id) DO UPDATE SET amount_cents=excluded.amount_cents,status=excluded.status,occurred_at=excluded.occurred_at,raw_json=excluded.raw_json""", (f"ref_{hashlib.sha256(refund['id'].encode()).hexdigest()[:24]}", merchant, refund["id"], order["id"] if order else None, refund.get("amount_money", {}).get("amount", 0), refund.get("amount_money", {}).get("currency", "USD"), refund.get("status", "UNKNOWN"), refund.get("updated_at") or refund.get("created_at") or utcnow(), json.dumps(refund)))
        elif event_type == "oauth.authorization.revoked":
            with self.db.connect() as c: c.execute("UPDATE square_installations SET status='revoked',updated_at=? WHERE square_merchant_id=?", (utcnow(), event.get("merchant_id")))

    def recover_interrupted_events(self):
        with self.db.connect() as c:
            c.execute("UPDATE square_webhook_events SET status='pending',next_attempt_at=? WHERE status='processing'", (utcnow(),))

    def worker_loop(self, stop_event, interval=1.0):
        self.recover_interrupted_events()
        while not stop_event.is_set():
            try: self.process_pending()
            except Exception as error: print(f"Square worker failed: {error}", flush=True)
            stop_event.wait(interval)

    def _merchant_for(self, square_merchant_id, location_id):
        with self.db.connect() as c:
            row = c.execute("""SELECT i.merchant_id FROM square_installations i LEFT JOIN square_locations l ON l.installation_id=i.id WHERE i.square_merchant_id=? AND (? IS NULL OR l.square_location_id=?) LIMIT 1""", (square_merchant_id, location_id, location_id)).fetchone()
        if not row: raise SquareError("Square event is not mapped to an Orbit restaurant")
        return row["merchant_id"]

    def _ingest_square_order(self, merchant, order, payment=None):
        fingerprint = None
        if payment: fingerprint = payment.get("card_details", {}).get("card", {}).get("fingerprint")
        fingerprint = fingerprint or (f"square_customer:{order['customer_id']}" if order.get("customer_id") else None)
        items = []
        for line in order.get("line_items", []):
            quantity = float(line.get("quantity", 1)); total = line.get("total_money", {}).get("amount", 0)
            items.append({"name": line.get("name") or line.get("catalog_object_id") or "Unknown item", "quantity": quantity, "unit_price_cents": round(total / quantity) if quantity else total})
        return self.orbit.ingest_order(merchant, {"external_id": order["id"], "source": "square", "payment_fingerprint": fingerprint, "occurred_at": order.get("closed_at") or order.get("created_at"), "total_cents": order.get("total_money", {}).get("amount", 0), "currency": order.get("total_money", {}).get("currency", "USD"), "items": items, "square_location_id": order.get("location_id")})

    def historical_sync(self, merchant, begin_at, end_at=None):
        installation, client = self._installation(merchant)
        with self.db.connect() as c: locations = [row["square_location_id"] for row in c.execute("SELECT square_location_id FROM square_locations WHERE installation_id=? AND status='active'", (installation["id"],))]
        cursor = None; imported = 0
        while True:
            query = {"location_ids": locations, "query": {"filter": {"date_time_filter": {"created_at": {"start_at": begin_at, **({"end_at": end_at} if end_at else {})}}}, "sort": {"sort_field": "CREATED_AT", "sort_order": "ASC"}}, "limit": 500}
            if cursor: query["cursor"] = cursor
            page = client.request("POST", "/v2/orders/search", query)
            for order in page.get("orders", []): self._ingest_square_order(merchant, order); imported += 1
            cursor = page.get("cursor")
            if not cursor: break
        payments = self._sync_payments(merchant, client, locations, begin_at, end_at)
        with self.db.connect() as c: c.execute("INSERT OR REPLACE INTO square_sync_state VALUES(?,?,?,?,?)", (installation["id"], None, utcnow(), "complete", None))
        return {"orders_imported": imported, "payments_processed": payments}

    def _sync_payments(self, merchant, client, locations, begin_at, end_at):
        processed = 0
        for location in locations:
            cursor = None
            while True:
                params = {"location_id": location, "begin_time": begin_at, "limit": 100, "sort_order": "ASC"}
                if end_at: params["end_time"] = end_at
                if cursor: params["cursor"] = cursor
                page = client.request("GET", "/v2/payments?" + urllib.parse.urlencode(params))
                for payment in page.get("payments", []):
                    if not payment.get("order_id"): continue
                    fingerprint = payment.get("card_details", {}).get("card", {}).get("fingerprint")
                    fingerprint = fingerprint or (f"square_customer:{payment['customer_id']}" if payment.get("customer_id") else None)
                    self.orbit.ingest_order(merchant, {"external_id": payment["order_id"], "source": "square", "payment_fingerprint": fingerprint, "occurred_at": payment.get("created_at"), "total_cents": payment.get("amount_money", {}).get("amount", 0), "currency": payment.get("amount_money", {}).get("currency", "USD"), "items": []})
                    processed += 1
                cursor = page.get("cursor")
                if not cursor: break
        return processed

    def sync_catalog(self, merchant):
        installation, client = self._installation(merchant)
        cursor = None; count = 0
        while True:
            request = {"object_types": ["ITEM"], "include_deleted_objects": False, "include_related_objects": True, "limit": 100}
            if cursor: request["cursor"] = cursor
            page = client.request("POST", "/v2/catalog/search", request)
            for item in page.get("objects", []):
                item_data = item.get("item_data", {})
                for variation in item_data.get("variations", []):
                    variation_data = variation.get("item_variation_data", {})
                    name = " - ".join(part for part in (item_data.get("name"), variation_data.get("name")) if part)
                    self.orbit.upsert_menu_item(merchant, {"external_id": variation["id"], "name": name or variation["id"], "price_cents": variation_data.get("price_money", {}).get("amount", 0), "active": not variation.get("is_deleted", False) and variation_data.get("sellable", True)})
                    count += 1
            cursor = page.get("cursor")
            if not cursor: break
        return {"menu_variations_synced": count}

    def status(self, merchant):
        with self.db.connect() as c:
            installation = c.execute("SELECT id,square_merchant_id,token_expires_at,status,created_at,updated_at FROM square_installations WHERE merchant_id=?", (merchant,)).fetchone()
            if not installation: return {"connected": False}
            locations = [dict(row) for row in c.execute("SELECT square_location_id,name,timezone,status,updated_at FROM square_locations WHERE merchant_id=? ORDER BY name", (merchant,))]
            events = dict(c.execute("""SELECT COUNT(*) total_events,SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) pending_events,SUM(CASE WHEN status='dead' THEN 1 ELSE 0 END) dead_events,MAX(processed_at) last_event_at FROM square_webhook_events WHERE square_merchant_id=?""", (installation["square_merchant_id"],)).fetchone())
            sync = c.execute("SELECT last_synced_at,status,error FROM square_sync_state WHERE installation_id=?", (installation["id"],)).fetchone()
        return {"connected": installation["status"] == "active", "installation": dict(installation), "locations": locations, "webhooks": events, "historical_sync": dict(sync) if sync else None}

    def retry_event(self, merchant, event_id):
        with self.db.connect() as c:
            event = c.execute("""SELECT e.event_id FROM square_webhook_events e JOIN square_installations i ON i.square_merchant_id=e.square_merchant_id WHERE e.event_id=? AND i.merchant_id=?""", (event_id, merchant)).fetchone()
            if not event: raise SquareError("Square event not found")
            c.execute("UPDATE square_webhook_events SET status='pending',error=NULL,next_attempt_at=? WHERE event_id=?", (utcnow(), event_id))
        return {"event_id": event_id, "status": "pending"}
