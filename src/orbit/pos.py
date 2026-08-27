"""Provider-neutral POS connection and payload normalization helpers."""

def path_get(value, path, default=None):
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current: return default
        current = current[part]
    return current

class ConfigurablePOSAdapter:
    REQUIRED = ("external_id", "occurred_at", "total_cents", "items")
    def __init__(self, provider, mapping): self.provider, self.mapping = provider, mapping
    def normalize(self, payload):
        result = {field: path_get(payload, path) for field, path in self.mapping.items() if field != "items"}
        result["source"] = self.provider
        item_mapping = self.mapping.get("items", {})
        raw_items = path_get(payload, item_mapping.get("path", "items"), [])
        result["items"] = [{field: path_get(item, path, 1 if field == "quantity" else None) for field, path in item_mapping.items() if field != "path"} for item in raw_items]
        missing = [field for field in self.REQUIRED if result.get(field) is None]
        if missing: raise ValueError(f"POS payload missing mapped fields: {', '.join(missing)}")
        return result
