"""OpenAI-powered next-visit and next-best-action predictions."""

import json
import os
import urllib.error
import urllib.request

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

    def predict(self, context):
        if not self.api_key: raise RuntimeError("OPENAI_API_KEY is not configured")
        prompt = """You are Orbit's restaurant retention decision engine. Use only the supplied facts. Estimate return probability over 1, 3, 7, and 14 days, likely time window, basket, order value, and safest action. Find at most 3 high-confidence, non-spam opportunities. Set do_not_contact=true and action=do_nothing when evidence or operational availability is insufficient. Never claim an item is available unless confirmed recipe/menu and recent delivery facts support it. Prefer normal habits over discounts. Return concise customer-safe copy; do not mention surveillance, card data, or private profiling. ISO-8601 timestamps are required. Context:\n""" + json.dumps(context, separators=(",", ":"))
        body = {"model": self.model, "input": prompt, "text": {"format": {"type": "json_schema", "name": "retention_predictions", "strict": True, "schema": PREDICTION_SCHEMA}}}
        request = urllib.request.Request(self.endpoint, json.dumps(body).encode(), {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", "User-Agent": "OrbitGuest/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response: result = json.load(response)
        except urllib.error.HTTPError as error: raise RuntimeError(f"OpenAI prediction failed ({error.code}): {error.read().decode()[:500]}") from error
        text = result.get("output_text")
        if not text:
            for output in result.get("output", []):
                for part in output.get("content", []):
                    if part.get("type") == "output_text": text = part.get("text")
        if not text: raise RuntimeError("OpenAI returned no prediction output")
        return json.loads(text)["opportunities"]
