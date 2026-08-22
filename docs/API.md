# API workflow

All tenant routes require `Authorization: Bearer <merchant_api_key>`.

1. `POST /v1/merchants` with `{"name":"Smokehouse"}`. The response contains the
   restaurant's unique `invoice_email`.
2. POS calls `POST /v1/webhooks/pos/orders` with `external_id`, `source`,
   `occurred_at`, `total_cents`, optional provider `payment_fingerprint`, and `items`.
3. Checkout calls `POST /v1/guests/identify` with the fingerprint, contact details,
   and explicit consent records such as `{"sms":{"status":"granted",
   "disclosure_version":"2026-08-01","source":"checkout"}}`.
4. Your inbound-mail provider posts a signed payload to `POST /v1/inbound/email`:

```json
{
  "message_id": "provider-message-123",
  "sender": "billing@vendor.com",
  "recipient": "smokehouse-a1b2c3@invoices.orbitguest.com",
  "subject": "Invoice 1042",
  "received_at": "2026-08-22T10:00:00Z",
  "attachments": [{
    "filename": "invoice-1042.pdf",
    "content_type": "application/pdf",
    "content_base64": "JVBERi0x..."
  }]
}
```

The `X-Orbit-Signature` header is the lowercase hex HMAC-SHA256 of the exact raw
request body using `INBOUND_EMAIL_SECRET`. Each provider message and attachment
is idempotent. Attachments are limited to 20 MB and supported types are PDF,
JPEG, PNG, WebP, and HEIC.
5. `GET /v1/dashboard/invoices` returns the restaurant's summary, invoices, and
   fully extracted line items for a dashboard tracking table.
6. `GET /v1/campaigns` returns consent-filtered inventory campaigns.
7. After the delivery provider confirms dispatch, call
   `POST /v1/campaigns/{id}/sent`.
8. A later POS order from that guest is attributed within a seven-day window;
   `GET /v1/metrics` reports converted campaigns, orders, and revenue.

The current service preserves files on `ORBIT_STORAGE_DIR`, records extraction
confidence, and routes confidence below 0.75 to `needs_review`. Add malware scanning
and object-storage encryption before accepting untrusted production attachments.
