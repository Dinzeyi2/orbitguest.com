"""Evidence-based psychological features for Orbit's behavior engine.

The engine estimates decision context from observable POS behavior.  It never
claims to read hunger, emotion, salary, or private internal state.
"""
import json
import hashlib
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone


def _clamp(value):
    return round(max(0.0, min(1.0, value)), 4)


class PsychologyEngine:
    MODEL_VERSION = "psychology-v2"
    STRATEGIES = {
        "habit_cue": ("Habit cue", "Cue a demonstrated routine without claiming private knowledge", {}, "low"),
        "implementation_intention": ("Implementation intention", "Offer a specific, evidence-based time and action", {}, "low"),
        "recognition": ("Recognition", "Recognize a demonstrated returning-guest relationship", {}, "low"),
        "controlled_novelty": ("Controlled novelty", "Pair a familiar choice with a verified variation", {"novelty_evidence": True}, "medium"),
        "genuine_scarcity": ("Genuine scarcity", "Describe limited availability only from verified inventory", {"verified_inventory_limit": True}, "high"),
        "endowed_progress": ("Endowed progress", "Show real progress in a configured loyalty program", {"verified_reward_progress": True}, "medium"),
        "genuine_loss_aversion": ("Genuine loss aversion", "Mention a real disclosed credit expiration", {"verified_expiring_credit": True}, "high"),
        "social_belonging": ("Social belonging", "Use an evidence-derived, non-exclusive regular identity", {}, "low"),
        "sensory_anticipation": ("Sensory anticipation", "Use accurate menu and preparation facts", {"verified_menu_fact": True}, "medium"),
        "reduced_friction": ("Reduced friction", "Reduce demonstrated ordering friction", {}, "low"),
        "silence": ("Silence", "Do not contact when fatigue or evidence warrants it", {}, "low"),
    }

    def analyze(self, connection, merchant_id, profile):
        self._sync_strategies(connection)
        guest_id = profile["guest_id"]
        orders = connection.execute(
            """SELECT o.id,o.occurred_at,o.total_cents,o.discount_cents,o.fulfillment_type,o.location_id,
                      COUNT(i.id) item_lines,COALESCE(SUM(i.quantity),0) item_quantity
               FROM orders o LEFT JOIN order_items i ON i.order_id=o.id
               WHERE o.merchant_id=? AND o.guest_id=? AND lower(o.status)='completed' AND o.is_test=0
                 AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id=o.id
                   AND lower(r.status)='completed' GROUP BY r.order_id HAVING SUM(r.amount_cents)>=o.total_cents)
               GROUP BY o.id ORDER BY o.occurred_at""",
            (merchant_id, guest_id),
        ).fetchall()
        visits = len(orders)
        if not visits:
            return None

        distinct_items = connection.execute(
            """SELECT COUNT(DISTINCT i.normalized_name) n,COUNT(*) purchases
               FROM order_items i JOIN orders o ON o.id=i.order_id
               WHERE o.merchant_id=? AND o.guest_id=? AND lower(o.status)='completed' AND o.is_test=0""",
            (merchant_id, guest_id),
        ).fetchone()
        item_variety = (distinct_items["n"] or 0) / max(1, distinct_items["purchases"] or 0)
        average_quantity = sum(row["item_quantity"] for row in orders) / visits
        multi_item_rate = sum(row["item_quantity"] >= 2 for row in orders) / visits
        social = _clamp(.65 * multi_item_rate + .35 * min(1, average_quantity / 4))

        interval = float(profile["average_interval_days"] or 0)
        deviation = float(profile["interval_stddev_days"] or 0)
        regularity = 0 if not interval else _clamp(1 - deviation / max(interval, 1))
        pickup_rate = sum((row["fulfillment_type"] or "").lower() in ("pickup", "takeout") for row in orders) / visits
        convenience = _clamp(.7 * regularity + .3 * pickup_rate)
        novelty = _clamp(item_variety * 1.5)
        value = _clamp(sum((row["discount_cents"] or 0) > 0 for row in orders) / visits)
        recognition = _clamp(.5 * regularity + .5 * min(1, visits / 10))

        # Calendar-position affinity is observable; it is not asserted to be
        # the guest's salary payday without an explicit, consented source.
        pay_window = sum(datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00")).day in {1, 2, 3, 4, 5, 14, 15, 16, 17, 18, 28, 29, 30, 31} for row in orders)
        pay_cycle = _clamp(pay_window / visits)

        context_counts = Counter()
        for row in connection.execute(
            """SELECT s.signal_type,COUNT(DISTINCT o.id) matched
               FROM behavior_context_signals s JOIN orders o
                 ON o.merchant_id=s.merchant_id AND o.occurred_at BETWEEN s.starts_at AND s.ends_at
                AND (s.location_id IS NULL OR s.location_id=o.location_id)
               WHERE s.merchant_id=? AND o.guest_id=? AND lower(o.status)='completed'
               GROUP BY s.signal_type""", (merchant_id, guest_id)):
            context_counts[row["signal_type"]] = _clamp(row["matched"] / visits)

        predicted = profile["predicted_next_visit_at"]
        window_start = window_end = None
        routine_state = "insufficient_history"
        if predicted:
            center = datetime.fromisoformat(predicted.replace("Z", "+00:00"))
            width = max(30, min(120, round((deviation or 1) * 24 * 60 / 4)))
            window_start = (center - timedelta(minutes=width)).isoformat()
            window_end = (center + timedelta(minutes=width)).isoformat()
            current = datetime.now(timezone.utc)
            if current < center - timedelta(hours=6): routine_state = "approaching"
            elif current <= center + timedelta(hours=2): routine_state = "decision_window"
            elif profile["behavior_status"] in ("overdue", "dormant"): routine_state = "disrupted"
            else: routine_state = "passed"

        favorite_weekday = profile["favorite_weekday"]
        favorite_hour = profile["favorite_hour"]
        if favorite_weekday == 6 and visits >= 3: label = "Sunday Regular"
        elif profile["preferred_daypart"] == "morning" and visits >= 3: label = "First Cup Club"
        elif social >= .6 and visits >= 3: label = "Rib Table" if self._favorite_contains(connection, guest_id, "rib") else "Table Regular"
        elif favorite_hour is not None and favorite_hour >= 16 and visits >= 3: label = "After-Work Regular"
        elif visits >= 10: label = "Founding Guest"
        else: label = None

        mechanisms = {"convenience": convenience, "novelty": novelty, "value": value,
                      "recognition": recognition, "social": social}
        outcomes = connection.execute(
            """SELECT c.psychology_mechanism,COUNT(*) exposures,SUM(o.converted) conversions,SUM(o.unsubscribed) unsubscribes
               FROM campaigns c JOIN campaign_outcomes o ON o.campaign_id=c.id
               WHERE c.merchant_id=? AND c.guest_id=? AND c.psychology_mechanism IS NOT NULL
               GROUP BY c.psychology_mechanism""", (merchant_id, guest_id)).fetchall()
        for outcome in outcomes:
            learned = ((outcome["conversions"] or 0) + 1) / ((outcome["exposures"] or 0) + 2 + 2 * (outcome["unsubscribes"] or 0))
            if outcome["psychology_mechanism"] in mechanisms:
                mechanisms[outcome["psychology_mechanism"]] = _clamp(.7 * mechanisms[outcome["psychology_mechanism"]] + .3 * learned)
        mechanism = max(mechanisms, key=mechanisms.get)
        fatigue = self._fatigue(connection, merchant_id, guest_id)
        friction = self._friction(connection, merchant_id, guest_id)
        hypotheses = self._hypotheses(
            connection, merchant_id, guest_id, profile, visits, regularity, item_variety,
            convenience, novelty, value, recognition, social, pay_cycle, context_counts,
            fatigue, friction, outcomes,
        )
        strategy = self.choose_strategy(connection, merchant_id, guest_id, hypotheses, mechanism, fatigue)
        evidence = {
            "completed_visits": visits, "average_basket_quantity": round(average_quantity, 2),
            "multi_item_visit_rate": round(multi_item_rate, 4), "item_variety_rate": round(item_variety, 4),
            "visit_regularity": regularity, "pay_cycle_is_calendar_affinity_not_known_payday": True,
            "observed_context_matches": dict(context_counts),
            "mechanism_scores": mechanisms,
            "marketing_fatigue": fatigue,
            "friction": friction,
            "recommended_strategy": strategy,
        }
        result = {
            "guest_id": guest_id, "merchant_id": merchant_id, "routine_state": routine_state,
            "decision_window_start": window_start, "decision_window_end": window_end,
            "social_probability": social, "convenience_affinity": convenience,
            "novelty_affinity": novelty, "value_affinity": value,
            "recognition_affinity": recognition, "pay_cycle_affinity": pay_cycle,
            "context_affinities": dict(context_counts), "belonging_label": label,
            "recommended_mechanism": mechanism, "controlled_novelty": novelty >= .35,
            "recommended_strategy": strategy, "marketing_fatigue": fatigue,
            "friction_sensitivity": friction, "hypotheses": hypotheses, "evidence": evidence,
        }
        connection.execute(
            """INSERT INTO behavior_psychology_profiles(guest_id,merchant_id,routine_state,decision_window_start,decision_window_end,
               social_probability,convenience_affinity,novelty_affinity,value_affinity,recognition_affinity,pay_cycle_affinity,
               context_affinities_json,belonging_label,recommended_mechanism,controlled_novelty,recommended_strategy,
               marketing_fatigue_json,friction_sensitivity_json,evidence_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(guest_id) DO UPDATE SET
               routine_state=excluded.routine_state,decision_window_start=excluded.decision_window_start,
               decision_window_end=excluded.decision_window_end,social_probability=excluded.social_probability,
               convenience_affinity=excluded.convenience_affinity,novelty_affinity=excluded.novelty_affinity,
               value_affinity=excluded.value_affinity,recognition_affinity=excluded.recognition_affinity,
               pay_cycle_affinity=excluded.pay_cycle_affinity,context_affinities_json=excluded.context_affinities_json,
               belonging_label=excluded.belonging_label,recommended_mechanism=excluded.recommended_mechanism,
               controlled_novelty=excluded.controlled_novelty,recommended_strategy=excluded.recommended_strategy,
               marketing_fatigue_json=excluded.marketing_fatigue_json,friction_sensitivity_json=excluded.friction_sensitivity_json,
               evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
            (guest_id, merchant_id, routine_state, window_start, window_end, social, convenience, novelty,
             value, recognition, pay_cycle, json.dumps(dict(context_counts)), label, mechanism,
             1 if novelty >= .35 else 0, strategy, json.dumps(fatigue), json.dumps(friction), json.dumps(evidence), datetime.now(timezone.utc).isoformat()),
        )
        return result

    def _sync_strategies(self, c):
        stamp = datetime.now(timezone.utc).isoformat()
        for code, (name, description, requires, risk) in self.STRATEGIES.items():
            c.execute("""INSERT INTO psychology_strategies VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(code) DO UPDATE SET name=excluded.name,description=excluded.description,
                requires_json=excluded.requires_json,risk_level=excluded.risk_level,version=excluded.version,updated_at=excluded.updated_at""",
                (code, name, description, json.dumps(requires), risk, 1, self.MODEL_VERSION, stamp, stamp))

    def _hypotheses(self, c, merchant, guest, profile, visits, regularity, variety,
                    convenience, novelty, value, recognition, social, pay_cycle,
                    contexts, fatigue, friction, outcomes):
        weekday_counts = json.loads(profile["weekday_distribution_json"] or "{}")
        hour_counts = json.loads(profile["hour_distribution_json"] or "{}")
        weekday_consistency = max(weekday_counts.values(), default=0) / visits
        hour_consistency = max(hour_counts.values(), default=0) / visits
        non_discount = 1 - float(profile["discount_visit_rate"] or 0)
        habit = _clamp(.35 * regularity + .2 * weekday_consistency + .15 * hour_consistency + .15 * (1-variety) + .15 * non_discount)
        tested = {row["psychology_mechanism"]: int(row["exposures"] or 0) for row in outcomes}
        specs = {
            "habit_strength": (habit, [f"visit regularity {regularity:.0%}", f"weekday consistency {weekday_consistency:.0%}", f"non-discount visits {non_discount:.0%}"], []),
            "reward_familiarity": (_clamp(1-novelty), [f"item repetition {(1-variety):.0%}"], []),
            "reward_novelty": (novelty, [f"item variety {variety:.0%}"], []),
            "reward_convenience": (convenience, [f"convenience evidence {convenience:.0%}"], []),
            "reward_financial_value": (value, [f"discount visit rate {value:.0%}"], ["purchase during a discount does not prove incremental lift"]),
            "reward_recognition": (recognition, [f"repeat-visit recognition evidence {recognition:.0%}"], []),
            "reward_social_belonging": (social, [f"multi-item/group basket evidence {social:.0%}"], []),
            "friction_sensitivity": (friction["confidence"], friction["supporting_evidence"], friction["contradicting_evidence"]),
            "price_sensitivity": (value, [f"discount association {value:.0%}"], ["causality requires a randomized incentive test"]),
            "timing_susceptibility": (_clamp(max([regularity, pay_cycle, *contexts.values()], default=0)), [f"routine regularity {regularity:.0%}", f"calendar-position affinity {pay_cycle:.0%}", *[f"{k} overlap {v:.0%}" for k,v in contexts.items()]], []),
            "marketing_fatigue": (fatigue["confidence"], fatigue["supporting_evidence"], fatigue["contradicting_evidence"]),
        }
        result = []
        stamp = datetime.now(timezone.utc).isoformat()
        for kind, (confidence, supporting, contradicting) in specs.items():
            experiment_count = tested.get(kind.removeprefix("reward_"), 0)
            state = "insufficient_evidence" if visits < 3 else "likely" if confidence >= .65 else "possible" if confidence >= .35 else "unlikely"
            evidence_status = "experimentally_supported" if experiment_count else "observed_hypothesis"
            c.execute("""INSERT INTO psychological_hypotheses VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(guest_id,hypothesis_type) DO UPDATE SET state=excluded.state,confidence=excluded.confidence,
                supporting_evidence_json=excluded.supporting_evidence_json,contradicting_evidence_json=excluded.contradicting_evidence_json,
                observation_count=excluded.observation_count,experiment_count=excluded.experiment_count,last_observed_at=excluded.last_observed_at,
                last_tested_at=excluded.last_tested_at,model_version=excluded.model_version,evidence_status=excluded.evidence_status,updated_at=excluded.updated_at""",
                (guest, merchant, kind, state, confidence, json.dumps(supporting), json.dumps(contradicting), visits,
                 experiment_count, stamp, stamp if experiment_count else None, self.MODEL_VERSION, evidence_status, stamp))
            result.append({"type": kind, "state": state, "confidence": confidence, "supporting_evidence": supporting,
                           "contradicting_evidence": contradicting, "experiment_count": experiment_count,
                           "evidence_status": evidence_status})
        return result

    def _fatigue(self, c, merchant, guest):
        since = (datetime.now(timezone.utc)-timedelta(days=30)).isoformat()
        sent = c.execute("SELECT COUNT(*) n FROM outbound_messages WHERE merchant_id=? AND guest_id=? AND sent_at>=?", (merchant, guest, since)).fetchone()["n"]
        engaged = c.execute("""SELECT COUNT(DISTINCT om.id) n FROM outbound_messages om JOIN message_events e ON e.outbound_message_id=om.id
            WHERE om.merchant_id=? AND om.guest_id=? AND om.sent_at>=? AND e.event_type IN ('opened','clicked')""", (merchant, guest, since)).fetchone()["n"]
        unsubscribe = c.execute("SELECT COUNT(*) n FROM suppressions WHERE merchant_id=? AND guest_id=?", (merchant, guest)).fetchone()["n"]
        ignored = max(0, sent-engaged)
        confidence = _clamp(.12*sent + .14*ignored + .5*unsubscribe)
        return {"level": "high" if confidence >= .65 else "medium" if confidence >= .35 else "low", "confidence": confidence,
                "messages_30d": sent, "ignored_30d": ignored,
                "supporting_evidence": [f"{sent} messages in 30 days", f"{ignored} without open/click"],
                "contradicting_evidence": [f"{engaged} opened or clicked"] if engaged else []}

    def _friction(self, c, merchant, guest):
        counts = {row["event_type"]: row["n"] for row in c.execute("SELECT event_type,COUNT(*) n FROM behavior_interactions WHERE merchant_id=? AND guest_id=? GROUP BY event_type", (merchant, guest))}
        abandoned = counts.get("checkout_abandoned", 0)
        prepared = counts.get("prepared_basket_converted", 0)
        clicks = counts.get("checkout_started", 0)
        confidence = _clamp((abandoned + prepared) / max(2, clicks + prepared))
        return {"state": "likely" if confidence >= .65 else "possible" if confidence >= .35 else "insufficient_evidence",
                "confidence": confidence, "supporting_evidence": [f"{abandoned} checkout abandonments", f"{prepared} prepared-basket conversions"],
                "contradicting_evidence": []}

    def choose_strategy(self, c, merchant, guest, hypotheses, mechanism, fatigue):
        if fatigue["level"] == "high": return "silence"
        mapping = {"convenience": "implementation_intention", "novelty": "controlled_novelty",
                   "value": "habit_cue", "recognition": "recognition", "social": "social_belonging"}
        primary = mapping.get(mechanism, "habit_cue")
        confidence = {row["type"]: row["confidence"] for row in hypotheses}
        if primary == "controlled_novelty" and confidence.get("reward_novelty", 0) < .35: primary = "habit_cue"
        exposures = c.execute("SELECT COUNT(*) n FROM psychology_experiments WHERE merchant_id=? AND guest_id=?", (merchant, guest)).fetchone()["n"]
        # Deterministic 20% safe exploration prevents permanent early labels.
        explore = int(hashlib.sha256(f"{guest}:{exposures}".encode()).hexdigest()[:8], 16) % 5 == 0
        alternatives = ["habit_cue", "implementation_intention", "recognition", "social_belonging"]
        if explore:
            primary = next(code for code in alternatives if code != primary)
        return primary

    def assign_experiment(self, c, merchant, guest, campaign_id, strategy, control_group):
        experiment_id = f"pexp_{uuid.uuid4().hex}"
        c.execute("INSERT INTO psychology_experiments(id,merchant_id,guest_id,campaign_id,strategy_code,variant,control_group,assigned_at) VALUES(?,?,?,?,?,?,?,?)",
                  (experiment_id, merchant, guest, campaign_id, strategy, "control" if control_group else strategy, 1 if control_group else 0, datetime.now(timezone.utc).isoformat()))
        return experiment_id

    def record_interaction(self, c, merchant, guest, event_type, metadata, campaign_id=None, occurred_at=None):
        allowed = {"message_opened", "message_clicked", "checkout_started", "checkout_abandoned", "prepared_basket_converted", "saved_payment_used", "delivery_selected", "pickup_selected"}
        if event_type not in allowed: raise ValueError("unsupported behavior interaction")
        stamp = occurred_at or datetime.now(timezone.utc).isoformat()
        interaction_id = f"bint_{uuid.uuid4().hex}"
        c.execute("INSERT INTO behavior_interactions VALUES(?,?,?,?,?,?,?,?)", (interaction_id, merchant, guest, campaign_id, event_type, json.dumps(metadata or {}), stamp, datetime.now(timezone.utc).isoformat()))
        return interaction_id

    @staticmethod
    def _favorite_contains(connection, guest_id, needle):
        row = connection.execute("SELECT normalized_item FROM guest_item_affinities WHERE guest_id=? ORDER BY order_count DESC LIMIT 1", (guest_id,)).fetchone()
        return bool(row and needle in row["normalized_item"])
