"""OpenAI-powered next-visit and next-best-action predictions."""

import json
import os
import urllib.error
import urllib.request
import hashlib
import time

PREDICTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"opportunities": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "type": {"type": "string", "enum": ["habit_interruption", "inventory_match", "next_visit"]},
            "item": {"type": "string"}, "score": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"}, "send_at": {"type": "string"},
            "subject": {"type": "string"}, "message": {"type": "string"},
            "action": {"type": "string", "enum": ["send_sms", "send_email", "prebuilt_cart", "variation", "incentive", "wait", "do_nothing"]},
            "return_probabilities": {"type": "object", "additionalProperties": False, "properties": {"within_1_day": {"type": "number"}, "within_3_days": {"type": "number"}, "within_7_days": {"type": "number"}, "within_14_days": {"type": "number"}}, "required": ["within_1_day", "within_3_days", "within_7_days", "within_14_days"]},
            "time_window_start": {"type": "string"}, "time_window_end": {"type": "string"},
            "predicted_basket": {"type": "array", "items": {"type": "string"}},
            "expected_order_value_cents": {"type": "integer", "minimum": 0},
            "do_not_contact": {"type": "boolean"}
        }, "required": ["type", "item", "score", "reason", "send_at", "subject", "message", "action", "return_probabilities", "time_window_start", "time_window_end", "predicted_basket", "expected_order_value_cents", "do_not_contact"]
    }}}, "required": ["opportunities"]
}

class OpenAIBehaviorPredictor:
    def __init__(self, api_key=None, model=None, endpoint="https://api.openai.com/v1/responses"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("OPENAI_PREDICTION_MODEL", "gpt-4.1-mini")
        self.endpoint = endpoint
        self.prompt_version = os.getenv("OPENAI_PREDICTION_PROMPT_VERSION", "strategy-v2")
        self.max_attempts = max(1, int(os.getenv("OPENAI_PREDICTION_MAX_ATTEMPTS", "3")))
        self.timeout = max(1, int(os.getenv("OPENAI_PREDICTION_TIMEOUT_SECONDS", "30")))
        self.max_input_chars = max(1000, int(os.getenv("OPENAI_PREDICTION_MAX_INPUT_CHARS", "30000")))
        self.last_run_metadata = None

    def predict(self, context):
        if not self.api_key: raise RuntimeError("OPENAI_API_KEY is not configured")
        facts = json.dumps(context, separators=(",", ":"))
        if len(facts) > self.max_input_chars: raise RuntimeError("prediction context exceeds configured limit")
        prompt = """You are Orbit's constrained retention strategy and copy layer, not its mathematical predictor. Use only supplied facts. Copy the statistical return probabilities, timing, basket and value supplied by Orbit; never recalculate or invent them. Select only among permitted actions. Set do_not_contact=true and action=do_nothing whenever consent, inventory, profitability, capacity, cooldown, or evidence is insufficient. Never invent availability, prices, discounts, customer history, or consent. Produce at most 3 opportunities. Use friendly customer-safe language without mentioning surveillance, frequency tracking, card data, or private profiling. ISO-8601 timestamps are required. Context:\n""" + facts
        body = {"model": self.model, "input": prompt, "text": {"format": {"type": "json_schema", "name": "retention_predictions", "strict": True, "schema": PREDICTION_SCHEMA}}}
        request = urllib.request.Request(self.endpoint, json.dumps(body).encode(), {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "User-Agent": "OrbitGuest/0.1"})
        started, result, last_error = time.monotonic(), None, None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response: result = json.load(response)
                break
            except urllib.error.HTTPError as error:
                last_error = RuntimeError(f"OpenAI prediction failed ({error.code}): {error.read().decode()[:500]}")
                if error.code < 500 and error.code != 429: break
            except (urllib.error.URLError, TimeoutError) as error: last_error = error
            if attempt < self.max_attempts: time.sleep(min(2 ** (attempt-1), 4))
        self.last_run_metadata = {"model": self.model, "prompt_version": self.prompt_version, "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(), "latency_ms": round((time.monotonic()-started)*1000), "attempts": attempt, "fallback": result is None}
        if result is None:
            # Safe deterministic fallback: silence. The statistical profile remains
            # available on the dashboard for human review.
            return []
        text = result.get("output_text")
        if not text:
            for output in result.get("output", []):
                for part in output.get("content", []):
                    if part.get("type") == "output_text": text = part.get("text")
        if not text: raise RuntimeError("OpenAI returned no prediction output")
        opportunities = json.loads(text)["opportunities"]
        allowed = {"send_sms", "send_email", "prebuilt_cart", "variation", "incentive", "wait", "do_nothing"}
        for opportunity in opportunities:
            if opportunity["action"] not in allowed: raise RuntimeError("OpenAI returned an unauthorized action")
            if opportunity["action"] in ("wait", "do_nothing"): opportunity["do_not_contact"] = True
        return opportunities
