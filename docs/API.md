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
   `GET /v1/dashboard/products` returns one current row per vendor product, and
   `GET /v1/products/{product_id}/history` returns every historical version with
   its original invoice. Old invoices never replace a newer current value; newer
   invoices update the current snapshot while preserving every prior version.
   Products are matched within a restaurant and vendor by SKU when present, otherwise
   by normalized product description.
6. `GET /v1/campaigns` returns consent-filtered inventory campaigns.
7. After the delivery provider confirms dispatch, call
   `POST /v1/campaigns/{id}/sent`.
8. A later POS order from that guest is attributed within a seven-day window;
   `GET /v1/metrics` reports converted campaigns, orders, and revenue.

## Automatic POS behavior profiles and permission

Every order containing a provider `payment_fingerprint` automatically creates or
updates an anonymous, merchant-scoped behavior profile. `POST /v1/guests/identify`
activates that existing profile for contact and requires `phone`,
`terms: {"accepted":true,"version":"...","source":"checkout"}`, plus explicit
SMS/email consent. Profiling and permission are separate: Orbit may compute the
restaurant's operational profile before permission, but it never makes the profile
contactable or eligible for messaging until permission is recorded.

`POST /v1/engine/run` recomputes customer rhythms and predictions.
`GET /v1/dashboard/behaviors` returns visit/spend/frequency/day/time/favorite-item data.
`GET /v1/dashboard/predictions` returns eligible and permission-required opportunities.

## Complete seven-stage engine core

1. **POS connection:** `POST /v1/pos/connections` registers a restaurant/location and
   returns a one-time `webhook_secret`. Its mapping converts any provider JSON into
   Orbit's canonical order. Providers post to `/v1/webhooks/pos/{connection_id}` with
   `X-Orbit-POS-Secret`.
2. **Identity and permission:** POS fingerprints build anonymous histories;
   `/v1/guests/identify` activates contact only after phone, terms, and consent.
3. **Behavior:** every order refreshes visit rhythm, day/hour, spend, favorite items,
   expected return, interruption status, and confidence.
4. **Recipe intelligence:** `POST /v1/menu/items` stores POS menu items and
   `POST /v1/recipes/links` connects invoice products to menu items. View mappings at
   `GET /v1/dashboard/recipes`.
5. **OpenAI prediction:** `POST /v1/engine/run` sends profile, affinities, current
   supplier products, and confirmed menu mappings to OpenAI and queues high-confidence
   opportunities.
6. **Delivery:** `POST /v1/campaigns/dispatch` sends due consented campaigns through
   Twilio SMS or Resend email. `POST /v1/guests/{id}/suppress` blocks a channel and
   records denied consent.
7. **Attribution:** subsequent POS purchases from the same merchant-scoped profile are
   linked to the most recent sent campaign and exposed by `/v1/metrics`.

## Native Square integration

Orbit includes Square OAuth with expiring single-use state, encrypted token storage,
automatic refresh, location/catalog synchronization, historical order/payment
pagination, signed webhook verification, event idempotency, explicit tenant/location
mapping, card-fingerprint enrichment, and retry handling. Setup is documented in
[`docs/SQUARE.md`](SQUARE.md).

The current service preserves files on `ORBIT_STORAGE_DIR`, records extraction
confidence, and routes confidence below 0.75 to `needs_review`. Add malware scanning
and object-storage encryption before accepting untrusted production attachments.
