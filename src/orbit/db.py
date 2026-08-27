import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS merchants (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, api_key_hash TEXT NOT NULL UNIQUE,
 inbound_alias TEXT UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS guests (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, payment_fingerprint TEXT,
 name TEXT, email TEXT, phone TEXT, profile_status TEXT NOT NULL DEFAULT 'anonymous',
 terms_version TEXT, terms_accepted_at TEXT, permission_source TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(merchant_id, payment_fingerprint), FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS consents (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, guest_id TEXT NOT NULL, channel TEXT NOT NULL,
 status TEXT NOT NULL, disclosure_version TEXT NOT NULL, source TEXT NOT NULL, captured_at TEXT NOT NULL,
 FOREIGN KEY(guest_id) REFERENCES guests(id), FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS identity_claims (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, guest_id TEXT NOT NULL,
 order_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL,
 used_at TEXT, created_at TEXT NOT NULL,
 FOREIGN KEY(merchant_id) REFERENCES merchants(id), FOREIGN KEY(guest_id) REFERENCES guests(id),
 FOREIGN KEY(order_id) REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS merchant_enrollment_pages (
 merchant_id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1,
 terms_version TEXT NOT NULL, headline TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS merchant_offers (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, name TEXT NOT NULL,
 discount_type TEXT NOT NULL, discount_value INTEGER NOT NULL, promo_code TEXT NOT NULL,
 offer_terms TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT,
 max_redemptions INTEGER, active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(merchant_id,promo_code), FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS offer_enrollments (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, offer_id TEXT NOT NULL,
 contact_guest_id TEXT NOT NULL, linked_guest_id TEXT, phone TEXT NOT NULL,
 consent_version TEXT NOT NULL, terms_version TEXT NOT NULL, consented_at TEXT NOT NULL,
 status TEXT NOT NULL, provider_message_id TEXT, send_error TEXT,
 claim_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(offer_id,phone), FOREIGN KEY(offer_id) REFERENCES merchant_offers(id),
 FOREIGN KEY(contact_guest_id) REFERENCES guests(id), FOREIGN KEY(linked_guest_id) REFERENCES guests(id)
);
CREATE TABLE IF NOT EXISTS offer_redemptions (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, enrollment_id TEXT NOT NULL UNIQUE,
 offer_id TEXT NOT NULL, order_id TEXT NOT NULL UNIQUE, redeemed_at TEXT NOT NULL,
 discount_cents INTEGER, created_at TEXT NOT NULL,
 FOREIGN KEY(enrollment_id) REFERENCES offer_enrollments(id),
 FOREIGN KEY(offer_id) REFERENCES merchant_offers(id), FOREIGN KEY(order_id) REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS offer_enrollment_attempts (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, phone_hash TEXT NOT NULL,
 ip_hash TEXT NOT NULL, attempted_at TEXT NOT NULL, outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, external_id TEXT NOT NULL, guest_id TEXT,
 payment_fingerprint TEXT, occurred_at TEXT NOT NULL, total_cents INTEGER NOT NULL,
 currency TEXT NOT NULL, source TEXT NOT NULL, raw_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'completed', location_id TEXT, fulfillment_type TEXT,
 discount_cents INTEGER NOT NULL DEFAULT 0, is_test INTEGER NOT NULL DEFAULT 0,
 provider_customer_id TEXT, payment_id TEXT,
 UNIQUE(merchant_id, source, external_id), FOREIGN KEY(guest_id) REFERENCES guests(id)
);
CREATE TABLE IF NOT EXISTS pos_connections (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, provider TEXT NOT NULL,
 external_location_id TEXT NOT NULL, display_name TEXT NOT NULL,
 webhook_secret_hash TEXT NOT NULL, mapping_json TEXT NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(merchant_id,provider,external_location_id), FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS square_oauth_states (
 state TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, expires_at TEXT NOT NULL,
 used_at TEXT, created_at TEXT NOT NULL, FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS square_installations (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL UNIQUE, square_merchant_id TEXT NOT NULL,
 environment TEXT NOT NULL,
 encrypted_access_token TEXT NOT NULL, encrypted_refresh_token TEXT,
 token_expires_at TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS square_locations (
 id TEXT PRIMARY KEY, installation_id TEXT NOT NULL, merchant_id TEXT NOT NULL,
 environment TEXT NOT NULL, square_merchant_id TEXT, verified_at TEXT,
 square_location_id TEXT NOT NULL UNIQUE, name TEXT, timezone TEXT, status TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(installation_id) REFERENCES square_installations(id)
);
CREATE TABLE IF NOT EXISTS square_webhook_events (
 event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, square_merchant_id TEXT,
 square_location_id TEXT, environment TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
 error TEXT, received_at TEXT NOT NULL, processed_at TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT
);
CREATE TABLE IF NOT EXISTS square_sync_state (
 installation_id TEXT PRIMARY KEY, environment TEXT NOT NULL, cursor TEXT, last_synced_at TEXT,
 status TEXT NOT NULL, error TEXT, FOREIGN KEY(installation_id) REFERENCES square_installations(id)
);
CREATE TABLE IF NOT EXISTS orbit_migrations (
 name TEXT PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refunds (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, external_id TEXT NOT NULL,
 order_id TEXT, amount_cents INTEGER NOT NULL, currency TEXT NOT NULL,
 status TEXT NOT NULL, occurred_at TEXT NOT NULL, raw_json TEXT NOT NULL,
 UNIQUE(merchant_id,external_id), FOREIGN KEY(order_id) REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS order_items (
 id TEXT PRIMARY KEY, order_id TEXT NOT NULL, name TEXT NOT NULL, normalized_name TEXT NOT NULL,
 quantity REAL NOT NULL, unit_price_cents INTEGER NOT NULL, catalog_object_id TEXT,
 modifiers_json TEXT NOT NULL DEFAULT '[]', FOREIGN KEY(order_id) REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS guest_identities (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, guest_id TEXT NOT NULL,
 identity_type TEXT NOT NULL, identity_value TEXT NOT NULL, verified INTEGER NOT NULL,
 source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(merchant_id,identity_type,identity_value), FOREIGN KEY(guest_id) REFERENCES guests(id)
);
CREATE TABLE IF NOT EXISTS behavior_profiles (
 guest_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, visit_count INTEGER NOT NULL,
 lifetime_spend_cents INTEGER NOT NULL, average_ticket_cents INTEGER NOT NULL,
 first_visit_at TEXT, last_visit_at TEXT, average_interval_days REAL,
 favorite_weekday INTEGER, favorite_hour INTEGER, predicted_next_visit_at TEXT,
 behavior_status TEXT NOT NULL, confidence REAL NOT NULL,
 interval_stddev_days REAL, days_since_last_visit REAL, overdue_by_days REAL,
 weekday_distribution_json TEXT NOT NULL DEFAULT '{}', hour_distribution_json TEXT NOT NULL DEFAULT '{}',
 median_ticket_cents INTEGER, return_probabilities_json TEXT NOT NULL DEFAULT '{}',
 preferred_daypart TEXT, preferred_location_id TEXT, preferred_fulfillment_type TEXT,
 discount_visit_rate REAL NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL,
 FOREIGN KEY(guest_id) REFERENCES guests(id)
);
CREATE TABLE IF NOT EXISTS behavior_context_signals (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, location_id TEXT,
 signal_type TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
 value_json TEXT NOT NULL, source TEXT NOT NULL, confidence REAL NOT NULL,
 created_at TEXT NOT NULL, FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS behavior_psychology_profiles (
 guest_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL,
 routine_state TEXT NOT NULL, decision_window_start TEXT, decision_window_end TEXT,
 social_probability REAL NOT NULL, convenience_affinity REAL NOT NULL,
 novelty_affinity REAL NOT NULL, value_affinity REAL NOT NULL,
 recognition_affinity REAL NOT NULL, pay_cycle_affinity REAL NOT NULL,
 context_affinities_json TEXT NOT NULL, belonging_label TEXT,
 recommended_mechanism TEXT NOT NULL, controlled_novelty INTEGER NOT NULL DEFAULT 0,
 recommended_strategy TEXT NOT NULL DEFAULT 'habit_cue',
 marketing_fatigue_json TEXT NOT NULL DEFAULT '{}', friction_sensitivity_json TEXT NOT NULL DEFAULT '{}',
 evidence_json TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(guest_id) REFERENCES guests(id)
);
CREATE TABLE IF NOT EXISTS psychological_hypotheses (
 guest_id TEXT NOT NULL, merchant_id TEXT NOT NULL, hypothesis_type TEXT NOT NULL,
 state TEXT NOT NULL, confidence REAL NOT NULL, supporting_evidence_json TEXT NOT NULL,
 contradicting_evidence_json TEXT NOT NULL, observation_count INTEGER NOT NULL,
 experiment_count INTEGER NOT NULL DEFAULT 0, last_observed_at TEXT,
 last_tested_at TEXT, model_version TEXT NOT NULL, evidence_status TEXT NOT NULL,
 updated_at TEXT NOT NULL, PRIMARY KEY(guest_id,hypothesis_type),
 FOREIGN KEY(guest_id) REFERENCES guests(id)
);
CREATE TABLE IF NOT EXISTS psychology_strategies (
 code TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
 requires_json TEXT NOT NULL, risk_level TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 version TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS psychology_experiments (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, guest_id TEXT NOT NULL,
 campaign_id TEXT NOT NULL UNIQUE, strategy_code TEXT NOT NULL, variant TEXT NOT NULL,
 control_group INTEGER NOT NULL DEFAULT 0, assigned_at TEXT NOT NULL,
 converted INTEGER NOT NULL DEFAULT 0, incremental_profit_cents INTEGER,
 order_value_cents INTEGER, seconds_to_order INTEGER, unsubscribed INTEGER NOT NULL DEFAULT 0,
 evaluated_at TEXT, FOREIGN KEY(campaign_id) REFERENCES campaigns(id),
 FOREIGN KEY(strategy_code) REFERENCES psychology_strategies(code)
);
CREATE TABLE IF NOT EXISTS behavior_interactions (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, guest_id TEXT NOT NULL,
 campaign_id TEXT, event_type TEXT NOT NULL, metadata_json TEXT NOT NULL,
 occurred_at TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(guest_id) REFERENCES guests(id)
);
CREATE TABLE IF NOT EXISTS guest_item_affinities (
 guest_id TEXT NOT NULL, normalized_item TEXT NOT NULL, display_name TEXT NOT NULL,
 order_count INTEGER NOT NULL, total_quantity REAL NOT NULL, last_ordered_at TEXT NOT NULL,
 total_spend_cents INTEGER NOT NULL DEFAULT 0, average_interval_days REAL,
 preferred_weekday INTEGER, preferred_hour INTEGER, predicted_next_order_at TEXT,
 PRIMARY KEY(guest_id, normalized_item), FOREIGN KEY(guest_id) REFERENCES guests(id)
);
CREATE TABLE IF NOT EXISTS guest_item_pairs (
 guest_id TEXT NOT NULL, first_item TEXT NOT NULL, second_item TEXT NOT NULL,
 order_count INTEGER NOT NULL, last_ordered_at TEXT NOT NULL,
 PRIMARY KEY(guest_id,first_item,second_item), FOREIGN KEY(guest_id) REFERENCES guests(id)
);
CREATE TABLE IF NOT EXISTS guest_modifier_affinities (
 guest_id TEXT NOT NULL, modifier_name TEXT NOT NULL, order_count INTEGER NOT NULL,
 last_ordered_at TEXT NOT NULL, PRIMARY KEY(guest_id,modifier_name),
 FOREIGN KEY(guest_id) REFERENCES guests(id)
);
CREATE TABLE IF NOT EXISTS predictions (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, guest_id TEXT NOT NULL,
 prediction_type TEXT NOT NULL, normalized_item TEXT, score REAL NOT NULL,
 reason TEXT NOT NULL, recommended_channel TEXT, recommended_send_at TEXT,
 status TEXT NOT NULL, trigger_ref TEXT, created_at TEXT NOT NULL,
 action TEXT NOT NULL DEFAULT 'wait', expected_order_value_cents INTEGER,
 return_probabilities_json TEXT NOT NULL DEFAULT '{}', time_window_start TEXT,
 time_window_end TEXT, predicted_basket_json TEXT NOT NULL DEFAULT '[]',
 do_not_contact INTEGER NOT NULL DEFAULT 0, eligibility_json TEXT NOT NULL DEFAULT '{}',
 UNIQUE(merchant_id,guest_id,prediction_type,normalized_item,trigger_ref),
 FOREIGN KEY(guest_id) REFERENCES guests(id)
);
CREATE TABLE IF NOT EXISTS menu_items (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, external_id TEXT NOT NULL,
 name TEXT NOT NULL, normalized_name TEXT NOT NULL, price_cents INTEGER NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(merchant_id,external_id), FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS recipe_links (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, product_id TEXT NOT NULL,
 menu_item_id TEXT NOT NULL, quantity_required REAL NOT NULL, unit TEXT NOT NULL,
 confidence REAL NOT NULL, status TEXT NOT NULL, waste_percent REAL NOT NULL DEFAULT 0,
 yield_percent REAL NOT NULL DEFAULT 100, packaging_cost_cents INTEGER NOT NULL DEFAULT 0,
 substitution_group TEXT, confirmed_by TEXT, confirmed_at TEXT, created_at TEXT NOT NULL,
 UNIQUE(product_id,menu_item_id), FOREIGN KEY(product_id) REFERENCES catalog_products(id),
 FOREIGN KEY(menu_item_id) REFERENCES menu_items(id)
);
CREATE TABLE IF NOT EXISTS merchant_operational_state (
 merchant_id TEXT PRIMARY KEY, accepting_orders INTEGER NOT NULL DEFAULT 1,
 capacity_remaining INTEGER, preparation_minutes INTEGER,
 promotions_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL,
 FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS invoices (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, external_id TEXT NOT NULL, vendor TEXT NOT NULL,
 invoice_date TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'USD', subtotal_cents INTEGER NOT NULL DEFAULT 0,
 tax_cents INTEGER NOT NULL DEFAULT 0, total_cents INTEGER NOT NULL, source_message_id TEXT,
 raw_text TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(merchant_id, external_id), FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS inbound_emails (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, provider_message_id TEXT NOT NULL,
 sender TEXT NOT NULL, recipient TEXT NOT NULL, subject TEXT, received_at TEXT NOT NULL,
 status TEXT NOT NULL, error TEXT, UNIQUE(provider_message_id),
 FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS invoice_documents (
 id TEXT PRIMARY KEY, inbound_email_id TEXT NOT NULL, invoice_id TEXT,
 filename TEXT NOT NULL, content_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 sha256 TEXT NOT NULL, storage_path TEXT NOT NULL, extraction_status TEXT NOT NULL,
 extraction_confidence REAL, extraction_json TEXT, error TEXT, created_at TEXT NOT NULL,
 UNIQUE(inbound_email_id, sha256), FOREIGN KEY(inbound_email_id) REFERENCES inbound_emails(id)
);
CREATE TABLE IF NOT EXISTS invoice_lines (
 id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, sku TEXT, description TEXT NOT NULL,
 normalized_description TEXT NOT NULL, quantity REAL NOT NULL, unit TEXT NOT NULL,
 unit_price_cents INTEGER NOT NULL, line_total_cents INTEGER NOT NULL,
 FOREIGN KEY(invoice_id) REFERENCES invoices(id)
);
CREATE TABLE IF NOT EXISTS catalog_products (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, vendor TEXT NOT NULL,
 product_key TEXT NOT NULL, sku TEXT, canonical_name TEXT NOT NULL,
 normalized_name TEXT NOT NULL, current_version_id TEXT,
 current_invoice_date TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(merchant_id, vendor, product_key),
 FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS product_versions (
 id TEXT PRIMARY KEY, product_id TEXT NOT NULL, invoice_id TEXT NOT NULL,
 invoice_line_id TEXT NOT NULL, effective_date TEXT NOT NULL,
 quantity REAL NOT NULL, unit TEXT NOT NULL, unit_price_cents INTEGER NOT NULL,
 line_total_cents INTEGER NOT NULL, is_current INTEGER NOT NULL DEFAULT 0,
 recorded_at TEXT NOT NULL,
 FOREIGN KEY(product_id) REFERENCES catalog_products(id),
 FOREIGN KEY(invoice_id) REFERENCES invoices(id),
 FOREIGN KEY(invoice_line_id) REFERENCES invoice_lines(id)
);
CREATE TABLE IF NOT EXISTS inventory_events (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, invoice_id TEXT, ingredient TEXT NOT NULL,
 normalized_ingredient TEXT NOT NULL, quantity REAL NOT NULL, unit TEXT NOT NULL,
 unit_cost_cents INTEGER NOT NULL, occurred_at TEXT NOT NULL,
 FOREIGN KEY(invoice_id) REFERENCES invoices(id)
);
CREATE TABLE IF NOT EXISTS unit_conversions (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, from_unit TEXT NOT NULL, to_unit TEXT NOT NULL,
 multiplier REAL NOT NULL CHECK(multiplier>0), ingredient_key TEXT NOT NULL DEFAULT '*',
 created_at TEXT NOT NULL, UNIQUE(merchant_id,ingredient_key,from_unit,to_unit)
);
CREATE TABLE IF NOT EXISTS inventory_adjustments (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, product_id TEXT NOT NULL, location_id TEXT,
 quantity REAL NOT NULL, unit TEXT NOT NULL, reason TEXT NOT NULL,
 occurred_at TEXT NOT NULL, notes TEXT, created_at TEXT NOT NULL,
 FOREIGN KEY(product_id) REFERENCES catalog_products(id)
);
CREATE TABLE IF NOT EXISTS inventory_consumptions (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, order_id TEXT NOT NULL, order_item_id TEXT NOT NULL,
 recipe_link_id TEXT NOT NULL, product_id TEXT NOT NULL, location_id TEXT,
 quantity REAL NOT NULL, unit TEXT NOT NULL, occurred_at TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(order_item_id,recipe_link_id), FOREIGN KEY(order_id) REFERENCES orders(id),
 FOREIGN KEY(recipe_link_id) REFERENCES recipe_links(id), FOREIGN KEY(product_id) REFERENCES catalog_products(id)
);
CREATE TABLE IF NOT EXISTS recipe_proposals (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, menu_item_id TEXT NOT NULL,
 components_json TEXT NOT NULL, rationale TEXT NOT NULL, confidence REAL NOT NULL,
 status TEXT NOT NULL, reviewed_by TEXT, reviewed_at TEXT, created_at TEXT NOT NULL,
 FOREIGN KEY(menu_item_id) REFERENCES menu_items(id)
);
CREATE TABLE IF NOT EXISTS prediction_model_versions (
 id TEXT PRIMARY KEY, merchant_id TEXT, component TEXT NOT NULL, version TEXT NOT NULL,
 model TEXT, prompt_hash TEXT, configuration_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, UNIQUE(merchant_id,component,version)
);
CREATE TABLE IF NOT EXISTS prediction_runs (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, guest_id TEXT, component TEXT NOT NULL,
 version TEXT NOT NULL, status TEXT NOT NULL, latency_ms INTEGER, estimated_cost_micros INTEGER,
 input_hash TEXT NOT NULL, output_json TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluation_runs (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, evaluation_type TEXT NOT NULL,
 model_version TEXT NOT NULL, status TEXT NOT NULL, metrics_json TEXT NOT NULL,
 cases_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_policies (
 merchant_id TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'pilot', automation_threshold REAL NOT NULL DEFAULT .85,
 max_discount_cents INTEGER NOT NULL DEFAULT 0, max_daily_messages INTEGER NOT NULL DEFAULT 100,
 minimum_inventory_confidence REAL NOT NULL DEFAULT .8, minimum_margin_cents INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL, FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS campaigns (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, guest_id TEXT NOT NULL, channel TEXT NOT NULL,
 trigger_type TEXT NOT NULL, trigger_ref TEXT, subject TEXT, body TEXT NOT NULL,
 status TEXT NOT NULL, scheduled_at TEXT NOT NULL, sent_at TEXT, created_at TEXT NOT NULL,
 action TEXT NOT NULL DEFAULT 'send_message', control_group INTEGER NOT NULL DEFAULT 0,
 prediction_window_end TEXT, eligibility_json TEXT NOT NULL DEFAULT '{}',
 psychology_mechanism TEXT,
 psychology_strategy TEXT,
 FOREIGN KEY(guest_id) REFERENCES guests(id)
);
CREATE TABLE IF NOT EXISTS outbound_messages (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
 guest_id TEXT NOT NULL, channel TEXT NOT NULL, recipient TEXT NOT NULL,
 provider_message_id TEXT, status TEXT NOT NULL, error TEXT,
 sent_at TEXT, created_at TEXT NOT NULL, provider TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, last_event_at TEXT,
 dead_lettered_at TEXT, idempotency_key TEXT,
 FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
);
CREATE TABLE IF NOT EXISTS messaging_settings (
 merchant_id TEXT PRIMARY KEY, timezone TEXT NOT NULL DEFAULT 'UTC',
 quiet_hours_start TEXT NOT NULL DEFAULT '21:00', quiet_hours_end TEXT NOT NULL DEFAULT '08:00',
 max_messages_per_guest_24h INTEGER NOT NULL DEFAULT 1,
 max_messages_per_merchant_day INTEGER NOT NULL DEFAULT 100,
 sms_help_text TEXT NOT NULL DEFAULT 'Reply STOP to opt out. Reply START to opt back in.',
 updated_at TEXT NOT NULL, FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS provider_webhook_events (
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, provider_event_id TEXT NOT NULL,
 event_type TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
 error TEXT, received_at TEXT NOT NULL, processed_at TEXT,
 UNIQUE(provider,provider_event_id)
);
CREATE TABLE IF NOT EXISTS message_events (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, outbound_message_id TEXT NOT NULL,
 event_type TEXT NOT NULL, occurred_at TEXT NOT NULL, metadata_json TEXT NOT NULL,
 UNIQUE(outbound_message_id,event_type,occurred_at), FOREIGN KEY(outbound_message_id) REFERENCES outbound_messages(id)
);
CREATE TABLE IF NOT EXISTS campaign_outcomes (
 campaign_id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, guest_id TEXT NOT NULL,
 group_name TEXT NOT NULL, converted INTEGER NOT NULL DEFAULT 0, order_id TEXT,
 revenue_cents INTEGER NOT NULL DEFAULT 0, seconds_to_order INTEGER,
 unsubscribed INTEGER NOT NULL DEFAULT 0, evaluated_at TEXT,
 FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
);
CREATE TABLE IF NOT EXISTS suppressions (
 merchant_id TEXT NOT NULL, guest_id TEXT NOT NULL, channel TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(merchant_id,guest_id,channel)
);
CREATE TABLE IF NOT EXISTS attributions (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, campaign_id TEXT NOT NULL, order_id TEXT NOT NULL,
 revenue_cents INTEGER NOT NULL, attributed_at TEXT NOT NULL, UNIQUE(campaign_id, order_id)
);
CREATE TABLE IF NOT EXISTS audit_log (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, merchant_id TEXT,
 action TEXT NOT NULL, actor TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT,
 metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_guest_time ON orders(merchant_id, guest_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_guest_identities ON guest_identities(merchant_id,identity_type,identity_value);
CREATE INDEX IF NOT EXISTS idx_items_name ON order_items(normalized_name);
CREATE INDEX IF NOT EXISTS idx_consent_guest ON consents(merchant_id, guest_id, channel, captured_at);
CREATE INDEX IF NOT EXISTS idx_identity_claim_order ON identity_claims(merchant_id,order_id,expires_at);
CREATE INDEX IF NOT EXISTS idx_offer_active ON merchant_offers(merchant_id,active,starts_at,ends_at);
CREATE INDEX IF NOT EXISTS idx_offer_enrollment_phone ON offer_enrollments(merchant_id,phone,status);
CREATE INDEX IF NOT EXISTS idx_offer_attempts ON offer_enrollment_attempts(phone_hash,ip_hash,attempted_at);
CREATE INDEX IF NOT EXISTS idx_invoice_lines_invoice ON invoice_lines(invoice_id);
CREATE INDEX IF NOT EXISTS idx_documents_email ON invoice_documents(inbound_email_id);
CREATE INDEX IF NOT EXISTS idx_products_merchant ON catalog_products(merchant_id, vendor, normalized_name);
CREATE INDEX IF NOT EXISTS idx_product_versions_history ON product_versions(product_id, effective_date DESC, recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_behavior_merchant ON behavior_profiles(merchant_id, behavior_status);
CREATE INDEX IF NOT EXISTS idx_behavior_context ON behavior_context_signals(merchant_id,starts_at,ends_at);
CREATE INDEX IF NOT EXISTS idx_psychology_merchant ON behavior_psychology_profiles(merchant_id,routine_state,recommended_mechanism);
CREATE INDEX IF NOT EXISTS idx_hypotheses_merchant ON psychological_hypotheses(merchant_id,hypothesis_type,confidence);
CREATE INDEX IF NOT EXISTS idx_psych_experiments ON psychology_experiments(merchant_id,strategy_code,assigned_at);
CREATE INDEX IF NOT EXISTS idx_behavior_interactions ON behavior_interactions(merchant_id,guest_id,event_type,occurred_at);
CREATE INDEX IF NOT EXISTS idx_predictions_merchant ON predictions(merchant_id, status, recommended_send_at);
CREATE INDEX IF NOT EXISTS idx_pos_connections ON pos_connections(merchant_id,provider,status);
CREATE INDEX IF NOT EXISTS idx_square_events_status ON square_webhook_events(status,received_at);
CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(merchant_id,order_id,status);
CREATE INDEX IF NOT EXISTS idx_recipe_menu ON recipe_links(merchant_id,menu_item_id,status);
CREATE INDEX IF NOT EXISTS idx_inventory_consumption_order ON inventory_consumptions(merchant_id,order_id);
CREATE INDEX IF NOT EXISTS idx_inventory_adjustment_product ON inventory_adjustments(merchant_id,product_id,occurred_at);
CREATE INDEX IF NOT EXISTS idx_prediction_runs_merchant ON prediction_runs(merchant_id,component,created_at);
CREATE INDEX IF NOT EXISTS idx_messages_campaign ON outbound_messages(campaign_id,status);
"""

class Database:
    def __init__(self, path: str = "orbit.db"):
        self.path = path
        if path != ":memory:":
            database_dir = Path(path).expanduser().resolve().parent
            try:
                database_dir.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise RuntimeError(
                    f"Orbit cannot create the database directory '{database_dir}'. "
                    "Set ORBIT_DB_PATH to a writable path or mount a Railway volume."
                ) from error
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(merchants)")}
            if "inbound_alias" not in columns:
                connection.execute("ALTER TABLE merchants ADD COLUMN inbound_alias TEXT")
                connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_merchants_alias ON merchants(inbound_alias)")
            invoice_columns = {row["name"] for row in connection.execute("PRAGMA table_info(invoices)")}
            for name, definition in (("currency", "TEXT NOT NULL DEFAULT 'USD'"), ("subtotal_cents", "INTEGER NOT NULL DEFAULT 0"), ("tax_cents", "INTEGER NOT NULL DEFAULT 0")):
                if name not in invoice_columns:
                    connection.execute(f"ALTER TABLE invoices ADD COLUMN {name} {definition}")
            guest_columns = {row["name"] for row in connection.execute("PRAGMA table_info(guests)")}
            for name, definition in (("profile_status", "TEXT NOT NULL DEFAULT 'anonymous'"), ("terms_version", "TEXT"), ("terms_accepted_at", "TEXT"), ("permission_source", "TEXT")):
                if name not in guest_columns:
                    connection.execute(f"ALTER TABLE guests ADD COLUMN {name} {definition}")
            behavior_columns = {row["name"] for row in connection.execute("PRAGMA table_info(behavior_profiles)")}
            for name, definition in (("interval_stddev_days", "REAL"), ("days_since_last_visit", "REAL"), ("overdue_by_days", "REAL"), ("weekday_distribution_json", "TEXT NOT NULL DEFAULT '{}'"), ("hour_distribution_json", "TEXT NOT NULL DEFAULT '{}'")):
                if name not in behavior_columns:
                    connection.execute(f"ALTER TABLE behavior_profiles ADD COLUMN {name} {definition}")
            psychology_columns = {row["name"] for row in connection.execute("PRAGMA table_info(behavior_psychology_profiles)")}
            for name, definition in (("recommended_strategy", "TEXT NOT NULL DEFAULT 'habit_cue'"), ("marketing_fatigue_json", "TEXT NOT NULL DEFAULT '{}'"), ("friction_sensitivity_json", "TEXT NOT NULL DEFAULT '{}'")):
                if name not in psychology_columns: connection.execute(f"ALTER TABLE behavior_psychology_profiles ADD COLUMN {name} {definition}")
            affinity_columns = {row["name"] for row in connection.execute("PRAGMA table_info(guest_item_affinities)")}
            for name, definition in (("total_spend_cents", "INTEGER NOT NULL DEFAULT 0"), ("average_interval_days", "REAL"), ("preferred_weekday", "INTEGER"), ("preferred_hour", "INTEGER"), ("predicted_next_order_at", "TEXT")):
                if name not in affinity_columns:
                    connection.execute(f"ALTER TABLE guest_item_affinities ADD COLUMN {name} {definition}")
            order_columns = {row["name"] for row in connection.execute("PRAGMA table_info(orders)")}
            for name, definition in (("status", "TEXT NOT NULL DEFAULT 'completed'"), ("location_id", "TEXT"), ("fulfillment_type", "TEXT"), ("discount_cents", "INTEGER NOT NULL DEFAULT 0"), ("is_test", "INTEGER NOT NULL DEFAULT 0"), ("provider_customer_id", "TEXT"), ("payment_id", "TEXT")):
                if name not in order_columns: connection.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")
            item_columns = {row["name"] for row in connection.execute("PRAGMA table_info(order_items)")}
            for name, definition in (("catalog_object_id", "TEXT"), ("modifiers_json", "TEXT NOT NULL DEFAULT '[]'")):
                if name not in item_columns: connection.execute(f"ALTER TABLE order_items ADD COLUMN {name} {definition}")
            for name, definition in (("median_ticket_cents", "INTEGER"), ("return_probabilities_json", "TEXT NOT NULL DEFAULT '{}'"), ("preferred_daypart", "TEXT"), ("preferred_location_id", "TEXT"), ("preferred_fulfillment_type", "TEXT"), ("discount_visit_rate", "REAL NOT NULL DEFAULT 0")):
                if name not in behavior_columns: connection.execute(f"ALTER TABLE behavior_profiles ADD COLUMN {name} {definition}")
            campaign_columns = {row["name"] for row in connection.execute("PRAGMA table_info(campaigns)")}
            for name, definition in (("action", "TEXT NOT NULL DEFAULT 'send_message'"), ("control_group", "INTEGER NOT NULL DEFAULT 0"), ("prediction_window_end", "TEXT"), ("eligibility_json", "TEXT NOT NULL DEFAULT '{}'")):
                if name not in campaign_columns: connection.execute(f"ALTER TABLE campaigns ADD COLUMN {name} {definition}")
            if "psychology_mechanism" not in campaign_columns:
                connection.execute("ALTER TABLE campaigns ADD COLUMN psychology_mechanism TEXT")
            if "psychology_strategy" not in campaign_columns:
                connection.execute("ALTER TABLE campaigns ADD COLUMN psychology_strategy TEXT")
            prediction_columns = {row["name"] for row in connection.execute("PRAGMA table_info(predictions)")}
            for name, definition in (("action", "TEXT NOT NULL DEFAULT 'wait'"), ("expected_order_value_cents", "INTEGER"), ("return_probabilities_json", "TEXT NOT NULL DEFAULT '{}'"), ("time_window_start", "TEXT"), ("time_window_end", "TEXT"), ("predicted_basket_json", "TEXT NOT NULL DEFAULT '[]'"), ("do_not_contact", "INTEGER NOT NULL DEFAULT 0"), ("eligibility_json", "TEXT NOT NULL DEFAULT '{}'")):
                if name not in prediction_columns: connection.execute(f"ALTER TABLE predictions ADD COLUMN {name} {definition}")
            message_columns = {row["name"] for row in connection.execute("PRAGMA table_info(outbound_messages)")}
            for name, definition in (("provider", "TEXT"), ("attempts", "INTEGER NOT NULL DEFAULT 0"), ("next_attempt_at", "TEXT"), ("last_event_at", "TEXT"), ("dead_lettered_at", "TEXT"), ("idempotency_key", "TEXT")):
                if name not in message_columns: connection.execute(f"ALTER TABLE outbound_messages ADD COLUMN {name} {definition}")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_idempotency ON outbound_messages(idempotency_key) WHERE idempotency_key IS NOT NULL")
            # These indexes must be created after legacy Railway databases receive
            # the new messaging columns above. Creating them in SCHEMA would make
            # executescript fail before migrations can run.
            connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_provider ON outbound_messages(provider,provider_message_id)")
            recipe_columns = {row["name"] for row in connection.execute("PRAGMA table_info(recipe_links)")}
            for name, definition in (("waste_percent", "REAL NOT NULL DEFAULT 0"), ("yield_percent", "REAL NOT NULL DEFAULT 100"), ("packaging_cost_cents", "INTEGER NOT NULL DEFAULT 0"), ("substitution_group", "TEXT"), ("confirmed_by", "TEXT"), ("confirmed_at", "TEXT")):
                if name not in recipe_columns: connection.execute(f"ALTER TABLE recipe_links ADD COLUMN {name} {definition}")
            square_event_columns = {row["name"] for row in connection.execute("PRAGMA table_info(square_webhook_events)")}
            for name, definition in (("attempts", "INTEGER NOT NULL DEFAULT 0"), ("next_attempt_at", "TEXT")):
                if name not in square_event_columns:
                    connection.execute(f"ALTER TABLE square_webhook_events ADD COLUMN {name} {definition}")
            for table in ("square_installations", "square_locations", "square_webhook_events", "square_sync_state"):
                columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                if "environment" not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN environment TEXT NOT NULL DEFAULT 'legacy'")
            location_columns = {row["name"] for row in connection.execute("PRAGMA table_info(square_locations)")}
            square_location_repair = "square_merchant_id" not in location_columns
            if "square_merchant_id" not in location_columns: connection.execute("ALTER TABLE square_locations ADD COLUMN square_merchant_id TEXT")
            if "verified_at" not in location_columns: connection.execute("ALTER TABLE square_locations ADD COLUMN verified_at TEXT")
            if square_location_repair and not connection.execute("SELECT 1 FROM orbit_migrations WHERE name='square_location_provenance_v1'").fetchone():
                # Existing location rows cannot prove whether they came from Sandbox
                # or Production. Quarantine them without touching the encrypted,
                # currently valid installation tokens. The next status/sync refresh
                # re-verifies the real Production location directly with Square.
                connection.execute("UPDATE square_locations SET environment='legacy',square_merchant_id=NULL,verified_at=NULL")
                connection.execute("UPDATE square_sync_state SET environment='legacy'")
                connection.execute("UPDATE square_webhook_events SET environment='legacy'")
                connection.execute("INSERT INTO orbit_migrations(name,applied_at) VALUES('square_location_provenance_v1',datetime('now'))")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_square_install_environment ON square_installations(merchant_id,environment,status)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_square_location_environment ON square_locations(merchant_id,environment,status)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_square_event_environment ON square_webhook_events(environment,status,received_at)")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
