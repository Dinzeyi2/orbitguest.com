"""Offline prediction and message safety evaluations for Orbit."""

import json
import statistics
import uuid
from collections import Counter
from datetime import datetime


def _id(prefix): return f"{prefix}_{uuid.uuid4().hex}"
def _now(): return datetime.now().astimezone().isoformat()


class EvaluationEngine:
    def __init__(self, db): self.db = db

    def set_policy(self, merchant, data):
        mode = data.get("mode", "pilot")
        if mode not in ("pilot", "assisted", "autonomous"):
            raise ValueError("mode must be pilot, assisted, or autonomous")
        threshold = float(data.get("automation_threshold", .85))
        if not 0 <= threshold <= 1: raise ValueError("automation_threshold must be between 0 and 1")
        with self.db.connect() as c:
            c.execute("""INSERT INTO campaign_policies VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(merchant_id) DO UPDATE SET
                mode=excluded.mode,automation_threshold=excluded.automation_threshold,max_discount_cents=excluded.max_discount_cents,
                max_daily_messages=excluded.max_daily_messages,minimum_inventory_confidence=excluded.minimum_inventory_confidence,
                minimum_margin_cents=excluded.minimum_margin_cents,updated_at=excluded.updated_at""",
                (merchant, mode, threshold, int(data.get("max_discount_cents", 0)), int(data.get("max_daily_messages", 100)), float(data.get("minimum_inventory_confidence", .8)), int(data.get("minimum_margin_cents", 0)), _now()))
        return self.policy(merchant)

    def policy(self, merchant):
        with self.db.connect() as c: row = c.execute("SELECT * FROM campaign_policies WHERE merchant_id=?", (merchant,)).fetchone()
        return dict(row) if row else {"merchant_id": merchant, "mode": "pilot", "automation_threshold": .85, "max_discount_cents": 0, "max_daily_messages": 100, "minimum_inventory_confidence": .8, "minimum_margin_cents": 0}

    def backtest(self, merchant, holdout=1, model_version="behavior-stat-v1"):
        """Walk-forward holdout: train on earlier orders and predict the hidden visit."""
        cases = []
        with self.db.connect() as c:
            guests = c.execute("SELECT id FROM guests WHERE merchant_id=?", (merchant,)).fetchall()
            for guest in guests:
                orders = c.execute("""SELECT id,occurred_at,total_cents FROM orders WHERE merchant_id=? AND guest_id=?
                    AND lower(status)='completed' AND is_test=0 AND total_cents>COALESCE((SELECT SUM(amount_cents) FROM refunds WHERE order_id=orders.id AND lower(status)='completed'),0)
                    ORDER BY occurred_at""", (merchant, guest["id"])).fetchall()
                if len(orders) < holdout + 2: continue
                train, hidden = orders[:-holdout], orders[-holdout]
                dates = [datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00")) for row in train]
                intervals = [(b-a).total_seconds()/86400 for a,b in zip(dates, dates[1:])]
                predicted_at = dates[-1] + __import__("datetime").timedelta(days=statistics.median(intervals))
                actual_at = datetime.fromisoformat(hidden["occurred_at"].replace("Z", "+00:00"))
                favorite = c.execute("""SELECT oi.normalized_name,COUNT(*) n FROM order_items oi WHERE oi.order_id IN (%s)
                    GROUP BY oi.normalized_name ORDER BY n DESC,oi.normalized_name LIMIT 1""" % ",".join("?"*len(train)), tuple(row["id"] for row in train)).fetchone()
                hidden_items = {row["normalized_name"] for row in c.execute("SELECT normalized_name FROM order_items WHERE order_id=?", (hidden["id"],))}
                cases.append({"guest_id": guest["id"], "predicted_at": predicted_at.isoformat(), "actual_at": actual_at.isoformat(), "timing_error_hours": round(abs((actual_at-predicted_at).total_seconds())/3600, 2), "predicted_item": favorite["normalized_name"] if favorite else None, "item_hit": bool(favorite and favorite["normalized_name"] in hidden_items), "predicted_value_cents": round(statistics.median(row["total_cents"] for row in train)), "actual_value_cents": hidden["total_cents"]})
        timing = [case["timing_error_hours"] for case in cases]
        values = [abs(case["actual_value_cents"]-case["predicted_value_cents"]) for case in cases]
        metrics = {"case_count": len(cases), "timing_mae_hours": round(statistics.mean(timing), 2) if timing else None, "item_accuracy": round(sum(case["item_hit"] for case in cases)/len(cases), 4) if cases else None, "order_value_mae_cents": round(statistics.mean(values)) if values else None}
        return self._save(merchant, "historical_backtest", model_version, metrics, cases)

    def evaluate_messages(self, merchant, model_version="openai-strategy-v1"):
        banned = ("we track", "card fingerprint", "payment fingerprint", "we know you", "every sunday")
        cases = []
        with self.db.connect() as c:
            campaigns = c.execute("SELECT * FROM campaigns WHERE merchant_id=?", (merchant,)).fetchall()
            for campaign in campaigns:
                text = f"{campaign['subject'] or ''} {campaign['body']}".lower()
                prediction = c.execute("SELECT normalized_item,eligibility_json FROM predictions WHERE id=?", (campaign["trigger_ref"],)).fetchone()
                eligibility = json.loads(prediction["eligibility_json"] or "{}") if prediction else {}
                failures = []
                if any(term in text for term in banned): failures.append("creepy_or_sensitive_wording")
                if prediction and prediction["normalized_item"] and prediction["normalized_item"] not in text: failures.append("predicted_item_missing")
                if not eligibility.get("authorized_channel", False): failures.append("channel_not_authorized")
                if not eligibility.get("menu_and_recipe_confirmed", False): failures.append("unconfirmed_availability")
                cases.append({"campaign_id": campaign["id"], "passed": not failures, "failures": failures})
        metrics = {"case_count": len(cases), "pass_rate": round(sum(case["passed"] for case in cases)/len(cases), 4) if cases else None, "failure_counts": dict(Counter(failure for case in cases for failure in case["failures"]))}
        return self._save(merchant, "message_quality", model_version, metrics, cases)

    def _save(self, merchant, kind, version, metrics, cases):
        run_id = _id("eval")
        status = "completed" if cases else "insufficient_data"
        with self.db.connect() as c:
            c.execute("INSERT INTO evaluation_runs VALUES(?,?,?,?,?,?,?,?)", (run_id, merchant, kind, version, status, json.dumps(metrics), json.dumps(cases), _now()))
        return {"id": run_id, "evaluation_type": kind, "model_version": version, "status": status, "metrics": metrics, "cases": cases}

    def dashboard(self, merchant):
        with self.db.connect() as c: rows = c.execute("SELECT * FROM evaluation_runs WHERE merchant_id=? ORDER BY created_at DESC", (merchant,)).fetchall()
        return {"policy": self.policy(merchant), "evaluations": [{**dict(row), "metrics": json.loads(row["metrics_json"]), "cases": json.loads(row["cases_json"])} for row in rows]}
