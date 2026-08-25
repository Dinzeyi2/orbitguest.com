"""OpenAI-backed extraction of invoice PDFs and receipt images."""

import base64
import json
import os
import urllib.error
import urllib.request

INVOICE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "vendor": {"type": "string"},
        "invoice_number": {"type": "string"},
        "invoice_date": {"type": "string", "description": "ISO-8601 date when visible, otherwise empty"},
        "currency": {"type": "string", "description": "ISO 4217 code, for example USD"},
        "subtotal_cents": {"type": "integer"},
        "tax_cents": {"type": "integer"},
        "total_cents": {"type": "integer"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "items": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "sku": {"type": "string"}, "description": {"type": "string"},
                    "quantity": {"type": "number"}, "unit": {"type": "string"},
                    "unit_price_cents": {"type": "integer"}, "line_total_cents": {"type": "integer"}
                },
                "required": ["sku", "description", "quantity", "unit", "unit_price_cents", "line_total_cents"]
            }
        }
    },
    "required": ["vendor", "invoice_number", "invoice_date", "currency", "subtotal_cents", "tax_cents", "total_cents", "confidence", "items"]
}

class ExtractionError(RuntimeError): pass

class OpenAIInvoiceExtractor:
    def __init__(self, api_key=None, model=None, endpoint="https://api.openai.com/v1/responses"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("OPENAI_INVOICE_MODEL", "gpt-4.1-mini")
        self.endpoint = endpoint

    def extract(self, content: bytes, content_type: str, filename: str) -> dict:
        if not self.api_key: raise ExtractionError("OPENAI_API_KEY is not configured")
        encoded = base64.b64encode(content).decode()
        if content_type == "application/pdf":
            document = {"type": "input_file", "filename": filename, "file_data": f"data:application/pdf;base64,{encoded}"}
        elif content_type.startswith("image/"):
            document = {"type": "input_image", "image_url": f"data:{content_type};base64,{encoded}", "detail": "high"}
        else:
            raise ExtractionError(f"unsupported attachment type: {content_type}")
        body = {
            "model": self.model,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "Extract this restaurant supplier invoice or receipt exactly. Never invent unreadable values. Use empty strings or zero where absent. Monetary fields must be integer minor units (cents). Return every line item."},
                document
            ]}],
            "text": {"format": {"type": "json_schema", "name": "restaurant_invoice", "strict": True, "schema": INVOICE_SCHEMA}}
        }
        request = urllib.request.Request(self.endpoint, json.dumps(body).encode(), {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode()[:1000]
            raise ExtractionError(f"OpenAI extraction failed ({error.code}): {detail}") from error
        except (OSError, ValueError) as error:
            raise ExtractionError(f"OpenAI extraction failed: {error}") from error
        text = result.get("output_text")
        if not text:
            for output in result.get("output", []):
                for part in output.get("content", []):
                    if part.get("type") == "output_text": text = part.get("text")
        if not text: raise ExtractionError("OpenAI returned no structured invoice output")
        try: return json.loads(text)
        except json.JSONDecodeError as error: raise ExtractionError("OpenAI returned invalid JSON") from error
