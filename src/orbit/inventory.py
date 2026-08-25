"""Estimated recipe/inventory intelligence used as a retention guardrail."""

import json
import math
import re
import uuid
from datetime import datetime, timezone


def _id(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _key(value):
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


class InventoryError(ValueError):
    pass


class InventoryEngine:
    """Reconciles purchases, recipes, sales, waste, and stock adjustments.

    Quantities are estimates. A conversion must be explicitly configured when the
    invoice unit differs from the confirmed recipe unit; Orbit never guesses one.
    """

    REASONS = {"count", "waste", "spoilage", "staff_meal", "complimentary", "theft", "transfer_in", "transfer_out", "remake", "correction"}

    def __init__(self, db):
        self.db = db

    def set_conversion(self, merchant, data):
        multiplier = float(data["multiplier"])
        if multiplier <= 0:
            raise InventoryError("conversion multiplier must be positive")
        source, target = _key(data["from_unit"]), _key(data["to_unit"])
        if not source or not target:
            raise InventoryError("from_unit and to_unit are required")
        ingredient = _key(data.get("ingredient_key") or "*") or "*"
        conversion_id = _id("ucv")
        with self.db.connect() as c:
            c.execute("""INSERT INTO unit_conversions VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(merchant_id,ingredient_key,from_unit,to_unit)
                DO UPDATE SET multiplier=excluded.multiplier,created_at=excluded.created_at""",
                (conversion_id, merchant, source, target, multiplier, ingredient, _now()))
        return {"id": conversion_id, "from_unit": source, "to_unit": target, "multiplier": multiplier, "ingredient_key": ingredient}

    def _convert(self, c, merchant, ingredient, quantity, source, target):
        source, target, ingredient = _key(source), _key(target), _key(ingredient)
        if source == target:
            return float(quantity)
        row = c.execute("""SELECT multiplier FROM unit_conversions WHERE merchant_id=?
            AND ingredient_key IN (?, '*') AND from_unit=? AND to_unit=?
            ORDER BY CASE WHEN ingredient_key=? THEN 0 ELSE 1 END LIMIT 1""",
            (merchant, ingredient, source, target, ingredient)).fetchone()
        if row:
            return float(quantity) * row["multiplier"]
        reverse = c.execute("""SELECT multiplier FROM unit_conversions WHERE merchant_id=?
            AND ingredient_key IN (?, '*') AND from_unit=? AND to_unit=?
            ORDER BY CASE WHEN ingredient_key=? THEN 0 ELSE 1 END LIMIT 1""",
            (merchant, ingredient, target, source, ingredient)).fetchone()
        if reverse:
            return float(quantity) / reverse["multiplier"]
        return None

    def propose_recipes(self, merchant):
        """Create reviewable, conservative name-overlap suggestions—never confirms them."""
        proposals = []
        with self.db.connect() as c:
            menu = c.execute("SELECT id,name,normalized_name FROM menu_items WHERE merchant_id=? AND active=1", (merchant,)).fetchall()
            products = c.execute("SELECT id,canonical_name,normalized_name FROM catalog_products WHERE merchant_id=?", (merchant,)).fetchall()
            for item in menu:
                menu_tokens = set(item["normalized_name"].split())
                ranked = []
                for product in products:
                    product_tokens = set(product["normalized_name"].split())
                    overlap = len(menu_tokens & product_tokens) / max(1, len(product_tokens))
                    if overlap:
                        ranked.append((overlap, product))
                components = [{"product_id": product["id"], "ingredient": product["canonical_name"], "quantity_required": None, "unit": None, "match_confidence": round(score, 3)} for score, product in sorted(ranked, reverse=True)[:5]]
                if not components:
                    continue
                proposal_id = _id("rpp")
                confidence = max(component["match_confidence"] for component in components)
                c.execute("INSERT INTO recipe_proposals VALUES(?,?,?,?,?,?,?,?,?,?)", (proposal_id, merchant, item["id"], json.dumps(components), "Ingredient/menu name overlap; manager must supply portions and units", confidence, "pending", None, None, _now()))
                proposals.append({"id": proposal_id, "menu_item": item["name"], "components": components, "confidence": confidence, "status": "pending"})
        return {"proposals": proposals}

    def review_proposal(self, merchant, proposal_id, data):
        decision = data.get("decision")
        if decision not in ("confirmed", "rejected"):
            raise InventoryError("decision must be confirmed or rejected")
        reviewer = data.get("reviewed_by") or "restaurant_manager"
        with self.db.connect() as c:
            proposal = c.execute("SELECT * FROM recipe_proposals WHERE id=? AND merchant_id=? AND status='pending'", (proposal_id, merchant)).fetchone()
            if not proposal:
                raise KeyError("pending recipe proposal not found")
            if decision == "confirmed":
                components = data.get("components") or []
                if not components:
                    raise InventoryError("confirmed recipes require components")
                for component in components:
                    if float(component.get("quantity_required") or 0) <= 0 or not component.get("unit"):
                        raise InventoryError("each recipe component requires a positive quantity and unit")
                    self._upsert_recipe(c, merchant, proposal["menu_item_id"], component, reviewer)
            c.execute("UPDATE recipe_proposals SET status=?,reviewed_by=?,reviewed_at=? WHERE id=?", (decision, reviewer, _now(), proposal_id))
        return {"id": proposal_id, "status": decision}

    def _upsert_recipe(self, c, merchant, menu_item_id, data, reviewer):
        product = c.execute("SELECT id FROM catalog_products WHERE id=? AND merchant_id=?", (data["product_id"], merchant)).fetchone()
        if not product:
            raise KeyError("ingredient product not found")
        waste, yield_percent = float(data.get("waste_percent", 0)), float(data.get("yield_percent", 100))
        if not 0 <= waste < 100 or not 0 < yield_percent <= 100:
            raise InventoryError("waste_percent must be 0..<100 and yield_percent must be >0..100")
        c.execute("""INSERT INTO recipe_links(id,merchant_id,product_id,menu_item_id,quantity_required,unit,confidence,status,waste_percent,yield_percent,packaging_cost_cents,substitution_group,confirmed_by,confirmed_at,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(product_id,menu_item_id) DO UPDATE SET
            quantity_required=excluded.quantity_required,unit=excluded.unit,confidence=excluded.confidence,status='confirmed',
            waste_percent=excluded.waste_percent,yield_percent=excluded.yield_percent,packaging_cost_cents=excluded.packaging_cost_cents,
            substitution_group=excluded.substitution_group,confirmed_by=excluded.confirmed_by,confirmed_at=excluded.confirmed_at""",
            (_id("rcp"), merchant, product["id"], menu_item_id, float(data["quantity_required"]), _key(data["unit"]), float(data.get("confidence", 1)), "confirmed", waste, yield_percent, int(data.get("packaging_cost_cents", 0)), data.get("substitution_group"), reviewer, _now(), _now()))

    def adjustment(self, merchant, data):
        if data["reason"] not in self.REASONS:
            raise InventoryError("unsupported inventory adjustment reason")
        quantity = float(data["quantity"])
        if data["reason"] in {"waste", "spoilage", "staff_meal", "complimentary", "theft", "transfer_out", "remake"} and quantity > 0:
            quantity = -quantity
        adjustment_id = _id("adj")
        with self.db.connect() as c:
            if not c.execute("SELECT 1 FROM catalog_products WHERE id=? AND merchant_id=?", (data["product_id"], merchant)).fetchone():
                raise KeyError("product not found")
            c.execute("INSERT INTO inventory_adjustments VALUES(?,?,?,?,?,?,?,?,?,?)", (adjustment_id, merchant, data["product_id"], data.get("location_id"), quantity, _key(data["unit"]), data["reason"], data.get("occurred_at", _now()), data.get("notes"), _now()))
        return {"id": adjustment_id, "quantity": quantity, "reason": data["reason"]}

    def reconcile_sales(self, merchant):
        created = 0
        with self.db.connect() as c:
            c.execute("""DELETE FROM inventory_consumptions WHERE merchant_id=? AND order_id IN (
                SELECT o.id FROM orders o WHERE o.merchant_id=? AND (lower(o.status)<>'completed' OR o.is_test=1
                OR o.total_cents<=COALESCE((SELECT SUM(r.amount_cents) FROM refunds r WHERE r.order_id=o.id AND lower(r.status)='completed'),0)))""", (merchant, merchant))
            rows = c.execute("""SELECT o.id order_id,o.location_id,o.occurred_at,oi.id order_item_id,oi.quantity,
                m.id menu_item_id FROM orders o JOIN order_items oi ON oi.order_id=o.id
                JOIN menu_items m ON m.merchant_id=o.merchant_id AND (m.external_id=oi.catalog_object_id OR m.normalized_name=oi.normalized_name)
                WHERE o.merchant_id=? AND lower(o.status)='completed' AND o.is_test=0
                AND o.total_cents>COALESCE((SELECT SUM(r.amount_cents) FROM refunds r WHERE r.order_id=o.id AND lower(r.status)='completed'),0)""", (merchant,)).fetchall()
            for row in rows:
                recipes = c.execute("SELECT * FROM recipe_links WHERE merchant_id=? AND menu_item_id=? AND status='confirmed'", (merchant, row["menu_item_id"])).fetchall()
                for recipe in recipes:
                    effective = recipe["quantity_required"] * row["quantity"] * (1 + recipe["waste_percent"] / 100) / (recipe["yield_percent"] / 100)
                    before = c.total_changes
                    c.execute("INSERT OR IGNORE INTO inventory_consumptions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (_id("con"), merchant, row["order_id"], row["order_item_id"], recipe["id"], recipe["product_id"], row["location_id"], effective, recipe["unit"], row["occurred_at"], _now()))
                    created += c.total_changes > before
        return {"consumptions_created": created}

    def dashboard(self, merchant, incentive_cents=0):
        self.reconcile_sales(merchant)
        ingredients, menu_items = [], []
        with self.db.connect() as c:
            products = c.execute("SELECT * FROM catalog_products WHERE merchant_id=? ORDER BY canonical_name", (merchant,)).fetchall()
            balances = {}
            for product in products:
                recipes = c.execute("SELECT * FROM recipe_links WHERE product_id=? AND status='confirmed'", (product["id"],)).fetchall()
                target = recipes[0]["unit"] if recipes else (c.execute("SELECT unit FROM inventory_events WHERE merchant_id=? AND normalized_ingredient=? ORDER BY occurred_at DESC LIMIT 1", (merchant, product["normalized_name"])).fetchone() or {"unit": "unit"})["unit"]
                purchased = 0.0; conversion_missing = False
                count = c.execute("SELECT quantity,unit,occurred_at FROM inventory_adjustments WHERE merchant_id=? AND product_id=? AND reason='count' ORDER BY occurred_at DESC,created_at DESC LIMIT 1", (merchant, product["id"])).fetchone()
                first_purchase = c.execute("SELECT MIN(occurred_at) at FROM inventory_events WHERE merchant_id=? AND normalized_ingredient=?", (merchant, product["normalized_name"])).fetchone()["at"]
                baseline = count["occurred_at"] if count else first_purchase or "0001-01-01"
                initial = 0.0
                if count:
                    converted_count = self._convert(c, merchant, product["normalized_name"], count["quantity"], count["unit"], target)
                    if converted_count is None: conversion_missing = True
                    else: initial = converted_count
                purchase_sql = "SELECT quantity,unit FROM inventory_events WHERE merchant_id=? AND normalized_ingredient=?" + (" AND occurred_at>?" if count else "")
                purchase_args = (merchant, product["normalized_name"], baseline) if count else (merchant, product["normalized_name"])
                for event in c.execute(purchase_sql, purchase_args):
                    value = self._convert(c, merchant, product["normalized_name"], event["quantity"], event["unit"], target)
                    if value is None: conversion_missing = True
                    else: purchased += value
                consumed = sum(row["quantity"] for row in c.execute("SELECT quantity FROM inventory_consumptions WHERE merchant_id=? AND product_id=? AND unit=? AND occurred_at>=?", (merchant, product["id"], target, baseline)))
                adjusted = 0.0
                for adjustment in c.execute("SELECT quantity,unit FROM inventory_adjustments WHERE merchant_id=? AND product_id=? AND reason<>'count' AND occurred_at>=?", (merchant, product["id"], baseline)):
                    value = self._convert(c, merchant, product["normalized_name"], adjustment["quantity"], adjustment["unit"], target)
                    if value is None: conversion_missing = True
                    else: adjusted += value
                estimate = initial + purchased - consumed + adjusted
                mapping_confidence = min([row["confidence"] for row in recipes], default=.35)
                confidence = max(.05, min(.99, mapping_confidence * (.55 if conversion_missing else 1)))
                uncertainty = max(abs(estimate) * (1-confidence), sum(abs(row["quantity_required"]) for row in recipes) if recipes else 1)
                balances[product["id"]] = (estimate, target, confidence)
                ingredients.append({"product_id": product["id"], "ingredient": product["canonical_name"], "estimated_quantity": round(estimate, 3), "unit": target, "range_low": round(max(0, estimate-uncertainty), 3), "range_high": round(max(0, estimate+uncertainty), 3), "confidence": round(confidence, 3), "conversion_missing": conversion_missing})
            for menu in c.execute("SELECT * FROM menu_items WHERE merchant_id=? AND active=1", (merchant,)):
                links = c.execute("""SELECT r.*,p.canonical_name,p.current_version_id,v.unit invoice_unit,v.unit_price_cents
                    FROM recipe_links r JOIN catalog_products p ON p.id=r.product_id LEFT JOIN product_versions v ON v.id=p.current_version_id
                    WHERE r.menu_item_id=? AND r.status='confirmed'""", (menu["id"],)).fetchall()
                costs, portions, confidences, missing = 0.0, [], [], False
                packaging = max([row["packaging_cost_cents"] for row in links], default=0)
                for link in links:
                    balance, unit, confidence = balances.get(link["product_id"], (0, link["unit"], .05))
                    required = link["quantity_required"] * (1 + link["waste_percent"]/100) / (link["yield_percent"]/100)
                    portions.append(max(0, balance / required)); confidences.append(confidence)
                    invoice_quantity = self._convert(c, merchant, link["canonical_name"], required, link["unit"], link["invoice_unit"] or link["unit"])
                    if invoice_quantity is None: missing = True
                    else: costs += invoice_quantity * (link["unit_price_cents"] or 0)
                confidence = min(confidences, default=0)
                portions_estimate = min(portions) if portions else 0
                margin = round(menu["price_cents"] - costs - packaging - int(incentive_cents))
                status = "probably_unavailable" if portions_estimate < 1 else "inventory_uncertain" if confidence < .8 or missing else "available_low_margin" if margin <= 0 else "safe_to_promote"
                menu_items.append({"menu_item_id": menu["id"], "menu_item": menu["name"], "price_cents": menu["price_cents"], "estimated_ingredient_cost_cents": round(costs), "packaging_cost_cents": packaging, "campaign_incentive_cents": int(incentive_cents), "estimated_contribution_margin_cents": margin, "estimated_portions": math.floor(portions_estimate), "inventory_confidence": round(confidence, 3), "status": status, "requires_manager_approval": status != "safe_to_promote"})
        return {"disclaimer": "Inventory is estimated from invoices, confirmed recipes, completed non-refunded sales, and adjustments; it is not a physical count.", "ingredients": ingredients, "menu_items": menu_items}
