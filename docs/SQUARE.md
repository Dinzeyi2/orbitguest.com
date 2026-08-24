# Square production integration

## Square Developer Dashboard

Create an application in the Square Developer Dashboard and configure:

- Production OAuth redirect URL: `https://<orbit-domain>/v1/integrations/square/callback`
- Production webhook URL: `https://<orbit-domain>/v1/webhooks/square`
- Webhook events: `order.created`, `order.updated`, `payment.created`, and
  `payment.updated`, plus the refund-created/updated events shown by Square for your
  application and `oauth.authorization.revoked`
- OAuth permissions: `MERCHANT_PROFILE_READ`, `ORDERS_READ`, `PAYMENTS_READ`,
  `CUSTOMERS_READ`, and `ITEMS_READ`

Use a separate Square sandbox application and Railway staging service before enabling
production sellers.

## Railway variables

```text
PUBLIC_BASE_URL=https://<orbit-domain>
SQUARE_ENVIRONMENT=production
SQUARE_APPLICATION_ID=<production application id>
SQUARE_APPLICATION_SECRET=<production application secret>
SQUARE_WEBHOOK_SIGNATURE_KEY=<production webhook signature key>
SQUARE_REDIRECT_URI=https://<orbit-domain>/v1/integrations/square/callback
SQUARE_WEBHOOK_URL=https://<orbit-domain>/v1/webhooks/square
SQUARE_API_VERSION=<the API version selected for your Square application>
TOKEN_ENCRYPTION_KEY=<Fernet key>
```

Generate the encryption key once, save it in a password manager, and never rotate it
without re-encrypting stored OAuth tokens:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Connect a restaurant

1. Authenticate as an Orbit merchant.
2. `POST /v1/integrations/square/authorize` with the merchant Bearer API key.
3. Redirect the restaurant owner to the returned `authorization_url`.
4. Square redirects to Orbit's callback. Orbit validates the single-use, ten-minute
   state, exchanges the code, encrypts access/refresh tokens, and syncs locations.
5. Run `POST /v1/integrations/square/catalog/sync` to import menu variations.
6. Run `POST /v1/integrations/square/sync` with `begin_at` and optional `end_at` ISO
   timestamps. Orbit paginates orders and payments, then links payment fingerprints.
7. Monitor `GET /v1/integrations/square/status`. A dead event can be deliberately
   replayed with `POST /v1/integrations/square/events/{event_id}/retry` after fixing
   the root cause.

## Security and reliability

- OAuth state is random, single-use, and expires after ten minutes.
- Tokens are encrypted at rest with Fernet; raw tokens are never returned by Orbit.
- Square webhook signatures cover the exact notification URL plus raw body.
- Webhook event IDs and Square order IDs are idempotent.
- Each Square merchant/location is explicitly mapped to one Orbit merchant.
- API calls retry rate limits and transient server/network failures.
- Access tokens refresh before expiration.
- Updated orders replace current line-item snapshots without duplicating history;
  refunds are stored separately and deducted from net attributed revenue.
- Signed webhooks are persisted before acknowledgment. A durable worker resumes
  interrupted events after restart, retries with exponential backoff, and moves an
  event to `dead` after ten failed attempts for operator review.

Before launch, confirm the current API version, permissions, webhook event names, and
OAuth requirements in the Square Developer Dashboard, then complete Square's required
application review and production seller test.
