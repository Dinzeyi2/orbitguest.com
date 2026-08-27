"""Strictly sandbox-only realistic behavior-engine demonstration data."""

import os
from datetime import datetime, timedelta, timezone


class DemoSeedError(RuntimeError):
    pass


class DemoBehaviorPredictor:
    """Guarantee one reviewable staging decision while preserving the real predictor."""

    def __init__(self, delegate):
        self.delegate = delegate

    def predict(self, context):
        favorites = context.get("favorite_items") or []
        favorite = favorites[0].get("display_name", "") if favorites else ""
        if favorite.lower() == "classic burger":
            send_at = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
            return [{
                "type": "next_visit", "item": "Classic Burger", "score": .9,
                "reason": "Strong recurring burger pattern with confirmed availability",
                "send_at": send_at, "subject": "Your burger favorite is available",
                "message": "Your Classic Burger favorite is available. Want us to have it ready for your next visit? Reply STOP to opt out.",
                "action": "send_sms", "do_not_contact": False,
            }]
        return self.delegate.predict(context) if self.delegate else []


class BehaviorDemoSeeder:
    ITEMS = {
        "ribs": ("Smoked Ribs", 2800, [{"name": "Spicy BBQ"}]),
        "mac": ("Mac and Cheese", 700, [{"name": "Extra cheese"}]),
        "burger": ("Classic Burger", 1800, [{"name": "No onions"}]),
        "fries": ("Garlic Fries", 600, []),
        "salmon": ("Grilled Salmon", 2600, [{"name": "Lemon butter"}]),
        "salad": ("Garden Salad", 1200, [{"name": "Dressing on side"}]),
        "latte": ("Vanilla Latte", 650, [{"name": "Oat milk"}]),
        "coffee": ("Drip Coffee", 400, []),
    }

    def __init__(self, service):
        self.service = service

    def _assert_safe(self, merchant):
        if os.getenv("SQUARE_ENVIRONMENT", "").lower() != "sandbox":
            raise DemoSeedError("Demo seeding is allowed only when SQUARE_ENVIRONMENT=sandbox")
        if os.getenv("ORBIT_DEMO_MODE", "").lower() != "true":
            raise DemoSeedError("Demo seeding requires ORBIT_DEMO_MODE=true")
        with self.service.db.connect() as connection:
            production = connection.execute("SELECT 1 FROM square_installations WHERE merchant_id=? AND environment='production' AND status='active'", (merchant,)).fetchone()
        if production:
            raise DemoSeedError("Demo seeding is forbidden for a merchant with an active Production Square installation")

    def _item(self, key, quantity=1):
        name, price, modifiers = self.ITEMS[key]
        return {"name": name, "catalog_object_id": f"demo-{key}", "quantity": quantity, "unit_price_cents": price, "modifiers": modifiers}

    def seed(self, merchant):
        self._assert_safe(merchant)
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        # Inclusive endpoints produce a full 90-day demonstration window.
        start = now - timedelta(days=89)
        labels = {}
        created_orders = canceled_orders = 0

        # Marcus: a deliberately clear approximately-14-day ribs habit.
        ribs_start = (now - timedelta(days=85)).replace(hour=18)
        for visit in range(7):
            occurred = ribs_start + timedelta(days=14 * visit)
            items = [self._item("ribs"), self._item("mac")]
            total = sum(item["quantity"] * item["unit_price_cents"] for item in items)
            self.service.ingest_order(merchant, {"external_id": f"demo-marcus-{visit}", "source": "square", "provider_customer_id": "demo-customer-marcus", "payment_fingerprint": "demo-fp-marcus", "payment_id": f"demo-pay-marcus-{visit}", "status": "completed", "occurred_at": occurred.isoformat(), "total_cents": total, "currency": "USD", "location_id": "demo-downtown", "fulfillment_type": "PICKUP", "discount_cents": 0, "items": items})
            created_orders += 1

        patterns = [
            ("alex", 7, 8, "latte", "coffee", "demo-uptown", "PICKUP"),
            ("sarah", 10, 12, "salmon", "salad", "demo-downtown", "DINE_IN"),
            ("jordan", 6, 19, "burger", "fries", "demo-uptown", "DELIVERY"),
            ("taylor", 9, 13, "salad", "coffee", "demo-downtown", "DINE_IN"),
            ("casey", 12, 18, "ribs", "fries", "demo-downtown", "PICKUP"),
            ("morgan", 5, 9, "latte", "mac", "demo-uptown", "PICKUP"),
            ("riley", 15, 20, "salmon", "fries", "demo-downtown", "DELIVERY"),
            ("jamie", 8, 11, "burger", "salad", "demo-uptown", "DINE_IN"),
            ("avery", 11, 17, "ribs", "mac", "demo-downtown", "DINE_IN"),
            ("quinn", 6, 7, "coffee", "latte", "demo-uptown", "PICKUP"),
            ("drew", 13, 14, "salmon", "mac", "demo-downtown", "DELIVERY"),
            ("skyler", 9, 21, "burger", "fries", "demo-uptown", "DINE_IN"),
            ("parker", 16, 16, "salad", "latte", "demo-downtown", "PICKUP"),
        ]
        for index, (label, cadence, hour, favorite, companion, location, fulfillment) in enumerate(patterns):
            fingerprint = f"demo-fp-{label}"
            visit = 0
            occurred = (start + timedelta(days=index % 6)).replace(hour=hour)
            while occurred <= now:
                items = [self._item(favorite), self._item(companion)] if visit % 3 != 2 else [self._item(favorite)]
                discount = 300 if (index + visit) % 4 == 0 else 0
                total = max(0, sum(item["quantity"] * item["unit_price_cents"] for item in items) - discount)
                self.service.ingest_order(merchant, {"external_id": f"demo-{label}-{visit}", "source": "square", "provider_customer_id": f"demo-customer-{label}", "payment_fingerprint": fingerprint, "payment_id": f"demo-pay-{label}-{visit}", "status": "completed", "occurred_at": occurred.isoformat(), "total_cents": total, "currency": "USD", "location_id": location, "fulfillment_type": fulfillment, "discount_cents": discount, "items": items})
                created_orders += 1; visit += 1; occurred += timedelta(days=cadence)

        # Invalid visits exist in the source history but must not affect behavior.
        self.service.ingest_order(merchant, {"external_id": "demo-canceled", "source": "square", "payment_fingerprint": "demo-fp-marcus", "status": "canceled", "occurred_at": (now - timedelta(days=2)).isoformat(), "total_cents": 3500, "currency": "USD", "location_id": "demo-downtown", "fulfillment_type": "PICKUP", "items": [self._item("ribs"), self._item("mac")]})
        canceled_orders += 1
        refunded = self.service.ingest_order(merchant, {"external_id": "demo-refunded", "source": "square", "payment_fingerprint": "demo-fp-sarah", "status": "completed", "occurred_at": (now - timedelta(days=3)).isoformat(), "total_cents": 3300, "currency": "USD", "location_id": "demo-downtown", "fulfillment_type": "DINE_IN", "items": [self._item("salmon"), self._item("salad")]})
        with self.service.db.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO refunds VALUES(?,?,?,?,?,?,?,?,?)", ("demo-refund", merchant, "demo-refund", refunded["id"], 3300, "USD", "COMPLETED", (now - timedelta(days=2)).isoformat(), "{}"))
        self.service.refresh_order_behavior(merchant, refunded["id"])

        # Four opt in; Marcus plus ten others remain anonymous and non-contactable.
        for position, label in enumerate(("alex", "sarah", "jordan", "taylor")):
            self.service.capture_identity(merchant, {"payment_fingerprint": f"demo-fp-{label}", "name": label.title(), "phone": f"+15550001{position:02d}", "terms": {"accepted": True, "version": "demo-v1", "source": "demo_receipt"}, "consent": {"sms": {"status": "granted" if position < 3 else "denied", "disclosure_version": "demo-sms-v1", "source": "demo_receipt"}}})

        invoice = self.service.ingest_invoice(merchant, {"external_id": "demo-ribs-delivery", "vendor": "Demo Foods", "invoice_date": now.date().isoformat(), "currency": "USD", "total_cents": 144000, "items": [{"sku": "DEMO-RIB", "ingredient": "Ribs", "quantity": 100, "unit": "portion", "unit_cost_cents": 900, "line_total_cents": 90000}, {"sku": "DEMO-BURGER", "ingredient": "Burger Patties", "quantity": 120, "unit": "portion", "unit_cost_cents": 450, "line_total_cents": 54000}]})
        products = self.service.product_dashboard(merchant)["products"]
        product = next(product for product in products if product["sku"] == "DEMO-RIB")
        ribs_menu = self.service.upsert_menu_item(merchant, {"external_id": "demo-ribs", "name": "Smoked Ribs", "price_cents": 2800})
        self.service.link_recipe(merchant, {"product_id": product["id"], "menu_item_id": ribs_menu["id"], "quantity_required": 1, "unit": "portion", "confidence": 1})
        burger_product = next(product for product in products if product["sku"] == "DEMO-BURGER")
        burger_menu = self.service.upsert_menu_item(merchant, {"external_id": "demo-burger", "name": "Classic Burger", "price_cents": 1800})
        self.service.link_recipe(merchant, {"product_id": burger_product["id"], "menu_item_id": burger_menu["id"], "quantity_required": 1, "unit": "portion", "confidence": 1, "packaging_cost_cents": 50})
        self.service.update_operational_state(merchant, {"accepting_orders": True, "capacity_remaining": 40, "preparation_minutes": 25, "promotions": []})
        self.service.set_campaign_policy(merchant, {"mode": "pilot", "automation_threshold": .85, "max_discount_cents": 0, "minimum_inventory_confidence": .8, "minimum_margin_cents": 500})
        predictor = self.service.predictor
        self.service.predictor = DemoBehaviorPredictor(predictor)
        try:
            engine = self.service.run_behavior_engine(merchant)
        finally:
            self.service.predictor = predictor
        dashboard = self.service.behavior_dashboard(merchant)
        predictions = self.service.prediction_dashboard(merchant)["predictions"]
        with self.service.db.connect() as connection:
            for row in connection.execute("SELECT id,payment_fingerprint FROM guests WHERE merchant_id=? AND payment_fingerprint LIKE 'demo-fp-%'", (merchant,)):
                labels[row["payment_fingerprint"].removeprefix("demo-fp-")] = row["id"]
        ribs_profile = next(profile for profile in dashboard["customers"] if profile["guest_id"] == labels["marcus"])
        label_by_guest = {guest_id: label for label, guest_id in labels.items()}
        created_profiles = [{"label": label_by_guest[profile["guest_id"]], "guest_id": profile["guest_id"], "profile_status": profile["profile_status"], "visit_count": profile["visit_count"], "favorite_item": profile["favorite_item"], "average_interval_days": profile["average_interval_days"], "preferred_daypart": profile["preferred_daypart"], "behavior_status": profile["behavior_status"], "return_probabilities": profile["return_probabilities"]} for profile in dashboard["customers"] if profile["guest_id"] in label_by_guest]
        return {"merchant_id": merchant, "orders_created": created_orders, "canceled_orders": canceled_orders, "fully_refunded_orders": 1, "profiles": dashboard["summary"], "created_profiles": created_profiles, "engine": engine, "ribs_regular": {"guest_id": labels["marcus"], "visit_count": ribs_profile["visit_count"], "average_interval_days": ribs_profile["average_interval_days"], "favorite_item": ribs_profile["favorite_item"], "return_probabilities": ribs_profile["return_probabilities"]}, "expected_predictions": [prediction for prediction in predictions if prediction["guest_id"] in labels.values()], "invoice_id": invoice["id"]}
