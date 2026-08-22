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
 name TEXT, email TEXT, phone TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(merchant_id, payment_fingerprint), FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS consents (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, guest_id TEXT NOT NULL, channel TEXT NOT NULL,
 status TEXT NOT NULL, disclosure_version TEXT NOT NULL, source TEXT NOT NULL, captured_at TEXT NOT NULL,
 FOREIGN KEY(guest_id) REFERENCES guests(id), FOREIGN KEY(merchant_id) REFERENCES merchants(id)
);
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, external_id TEXT NOT NULL, guest_id TEXT,
 payment_fingerprint TEXT, occurred_at TEXT NOT NULL, total_cents INTEGER NOT NULL,
 currency TEXT NOT NULL, source TEXT NOT NULL, raw_json TEXT NOT NULL,
 UNIQUE(merchant_id, source, external_id), FOREIGN KEY(guest_id) REFERENCES guests(id)
);
CREATE TABLE IF NOT EXISTS order_items (
 id TEXT PRIMARY KEY, order_id TEXT NOT NULL, name TEXT NOT NULL, normalized_name TEXT NOT NULL,
 quantity REAL NOT NULL, unit_price_cents INTEGER NOT NULL, FOREIGN KEY(order_id) REFERENCES orders(id)
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
CREATE TABLE IF NOT EXISTS inventory_events (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, invoice_id TEXT, ingredient TEXT NOT NULL,
 normalized_ingredient TEXT NOT NULL, quantity REAL NOT NULL, unit TEXT NOT NULL,
 unit_cost_cents INTEGER NOT NULL, occurred_at TEXT NOT NULL,
 FOREIGN KEY(invoice_id) REFERENCES invoices(id)
);
CREATE TABLE IF NOT EXISTS campaigns (
 id TEXT PRIMARY KEY, merchant_id TEXT NOT NULL, guest_id TEXT NOT NULL, channel TEXT NOT NULL,
 trigger_type TEXT NOT NULL, trigger_ref TEXT, subject TEXT, body TEXT NOT NULL,
 status TEXT NOT NULL, scheduled_at TEXT NOT NULL, sent_at TEXT, created_at TEXT NOT NULL,
 FOREIGN KEY(guest_id) REFERENCES guests(id)
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
CREATE INDEX IF NOT EXISTS idx_items_name ON order_items(normalized_name);
CREATE INDEX IF NOT EXISTS idx_consent_guest ON consents(merchant_id, guest_id, channel, captured_at);
CREATE INDEX IF NOT EXISTS idx_invoice_lines_invoice ON invoice_lines(invoice_id);
CREATE INDEX IF NOT EXISTS idx_documents_email ON invoice_documents(inbound_email_id);
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

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
