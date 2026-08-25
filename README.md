# Orbit backend

Orbit is a consent-first restaurant intelligence backend. This milestone focuses
on the supplier inbox: every restaurant receives a unique address such as
`smokehouse-a1b2c3@invoices.orbitguest.com`. Vendors email PDF or image receipts,
the inbound provider forwards them to Orbit, OpenAI extracts structured invoice
data, and the dashboard API returns a live tracking table. Orbit also joins POS orders,
guest identities, supplier invoices, inventory events, and retention campaigns
without storing raw payment-card data.

## What the MVP does

- Accepts idempotent POS order webhooks and builds merchant-scoped guest profiles.
- Links a provider-issued payment fingerprint to a guest only after identity capture.
- Records versioned email/SMS consent and suppresses opted-out guests.
- Creates a unique `@invoices.orbitguest.com` address when a restaurant signs up.
- Accepts signed inbound-email webhooks with PDF, JPEG, PNG, WebP, or HEIC receipts.
- Extracts invoices through OpenAI into vendor, date, invoice ID, totals, and line items.
- Stores the original document, extraction confidence, review state, inventory, and price history.
- Returns a dashboard-ready invoice table and spend summary.
- Maintains an append-only product price history: newly received products are added,
  newer invoices update the current snapshot, and late-arriving old invoices are
  inserted into history without overwriting current values.
- Automatically creates merchant-scoped anonymous behavior profiles from POS payment
  fingerprints. A customer activates identity and marketing only by providing a phone
  number, accepting a versioned legal disclosure, and granting channel consent.
- Provides short-lived, single-use digital-receipt claim tokens that connect a
  voluntary phone/terms submission to the exact anonymous POS order profile.
- Learns robust visit cadence, expected next visit, preferred day/hour distributions,
  spend, item-level cadence, favorite combinations, and habit interruptions, then
  produces permission-gated predictions without sending contact details to OpenAI.
- Joins Square Orders with Payments, prioritizes provider customer IDs over verified
  merchant-scoped fingerprints, records modifiers/location/fulfillment/discounts, and
  excludes canceled, test, and fully refunded transactions from visit behavior.
- Predicts 1/3/7/14-day return probabilities, likely basket/window/value, validates
  inventory/menu/margin/capacity/cooldown reality, supports an explicit do-nothing
  decision, and measures incremental lift with randomized control outcomes.
- Provides configurable POS-location adapters, confirmed ingredient-to-menu recipe
  links, OpenAI next-best-action predictions, real Telnyx/Resend delivery, suppression,
  and closed-loop POS revenue attribution.
- Finds consented guests whose preferences match newly delivered ingredients.
- Creates queued campaigns and attributes subsequent orders to those campaigns.
- Writes an immutable-style audit trail for every sensitive operation.

Orbit deliberately accepts **provider tokens/fingerprints only**. Never send PAN,
CVV, track data, or other raw cardholder data to this service.

## Run

```bash
OPENAI_API_KEY=... INBOUND_EMAIL_SECRET=... PYTHONPATH=src python -m orbit.api
```

Create a merchant, then use the returned `api_key` as `Authorization: Bearer ...`.
See [`docs/API.md`](docs/API.md) for the complete workflow.
See [`docs/RAILWAY.md`](docs/RAILWAY.md) for Railway and email-domain setup.
See [`docs/SQUARE.md`](docs/SQUARE.md) for the first native production POS integration.
See [`docs/DEMO.md`](docs/DEMO.md) for the strictly Sandbox-only behavior demo seeder.
See [`docs/INTELLIGENCE.md`](docs/INTELLIGENCE.md) for recipe/inventory guardrails,
prediction responsibilities, approvals, and evaluations.
See [`docs/MESSAGING.md`](docs/MESSAGING.md) for Telnyx SMS and Resend email setup.

For live email, configure Resend to send `email.received` events to
`/v1/webhooks/resend`, then set `RESEND_API_KEY` and `RESEND_WEBHOOK_SECRET`.

By default Orbit uses `/tmp/orbit/orbit.db`, so a new Railway deployment starts even
before a volume is configured. For persistent production data, mount a volume at
`/data` and set `ORBIT_DB_PATH=/data/orbit.db` and
`ORBIT_STORAGE_DIR=/data/documents`.

## Test

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

The evidence-based psychology layer inside the behavior engine is documented in [docs/PSYCHOLOGY.md](docs/PSYCHOLOGY.md).
