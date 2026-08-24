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

For a simple website/digital-receipt opt-in without exposing a payment fingerprint:

1. The authenticated restaurant backend calls `POST /v1/identity/claims` with
   `{"source":"square","external_order_id":"ORDER_ID"}`.
2. Orbit returns a short-lived, single-use `claim_token` tied to the exact POS order
   and its existing anonymous guest profile.
3. The customer-facing website displays the terms and optional channel choices.
4. If the customer chooses to participate, the website calls the public
   `POST /v1/identity/claim` with the token, phone number, accepted terms version,
   and explicit SMS/email consent. Orbit activates only that exact POS profile.

The token expires after 15 minutes by default, is stored only as a SHA-256 hash, and
cannot be reused. The customer can decline by doing nothing; Orbit never invents or
looks up their contact information.

`POST /v1/engine/run` recomputes customer rhythms and predictions.
`GET /v1/dashboard/behaviors` returns visit/spend/frequency, day/hour distributions,
favorite-item cadence, expected next item order, and frequent item combinations.
`GET /v1/dashboard/predictions` returns eligible and permission-required opportunities.

## Complete behavior decision loop

1. **POS connection:** `POST /v1/pos/connections` registers a restaurant/location and
   returns a one-time `webhook_secret`. Its mapping converts any provider JSON into
   Orbit's canonical order. Providers post to `/v1/webhooks/pos/{connection_id}` with
   `X-Orbit-POS-Secret`.
2. **Identity and permission:** POS fingerprints build anonymous histories;
   `/v1/guests/identify` activates contact only after phone, terms, and consent.
3. **Behavior:** completed, non-test, non-fully-refunded orders refresh robust visit
   cadence, median/average spend, regularity, daypart/day/hour distributions, location,
   fulfillment, discount response, item/modifier cadence, combinations, expected return,
   1/3/7/14-day probabilities, interruption status, and confidence. Canceled, duplicate,
   test, and fully refunded orders are excluded.
4. **Recipe intelligence:** `POST /v1/menu/items` stores POS menu items and
   `POST /v1/recipes/links` connects invoice products to menu items. View mappings at
   `GET /v1/dashboard/recipes`.
5. **OpenAI prediction:** `POST /v1/engine/run` sends behavioral statistics (never
   name, phone, or email), affinities, combinations, prior message response, supplier
   products, estimated inventory, margins, capacity, preparation time, promotions, and
   confirmed menu mappings to OpenAI. It predicts return windows, basket, order value,
   and an action including `wait` or `do_nothing`.
6. **Eligibility/action:** before queuing anything, Orbit rechecks consent, suppression,
   contact cooldown, menu/recipe mapping, estimated inventory, capacity, and expected
   incremental profit. `POST /v1/operations/state` supplies capacity, preparation time,
   and existing promotions. A new order before dispatch makes a campaign stale.
7. **Delivery:** `POST /v1/campaigns/dispatch` sends due consented campaigns through
   Twilio SMS or Resend email. `POST /v1/guests/{id}/suppress` blocks a channel and
   records denied consent. `POST /v1/messages/events` records delivered/opened/clicked,
   failed, bounced, and unsubscribe outcomes.
8. **Incrementality:** eligible cohorts of at least 20 receive a deterministic randomized
   holdout controlled by `ORBIT_CONTROL_PERCENT` (default 10%). Purchases inside the
   prediction window update both messaged and control outcomes; only messaged purchases
   receive last-touch attribution. `/v1/metrics` reports conversion rates and estimated
   incremental revenue rather than treating every post-message purchase as caused.

## Native Square integration

Orbit includes Square OAuth with expiring single-use state, encrypted token storage,
automatic refresh, location/catalog synchronization, historical order/payment
pagination, signed webhook verification, event idempotency, explicit tenant/location
mapping, card-fingerprint enrichment, and retry handling. Setup is documented in
[`docs/SQUARE.md`](SQUARE.md).

## Sandbox demo data

Authenticated `POST /v1/demo/behavior/seed` creates a realistic 90-day POS dataset,
runs the behavior engine, and returns profiles plus expected predictions. It refuses
unless `SQUARE_ENVIRONMENT=sandbox` and `ORBIT_DEMO_MODE=true`, and it rejects any
merchant with an active Production Square installation. See [`docs/DEMO.md`](DEMO.md).

The current service preserves files on `ORBIT_STORAGE_DIR`, records extraction
confidence, and routes confidence below 0.75 to `needs_review`. Add malware scanning
and object-storage encryption before accepting untrusted production attachments.
