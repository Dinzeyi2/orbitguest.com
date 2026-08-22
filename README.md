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
