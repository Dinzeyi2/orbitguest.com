# Railway deployment and supplier-email setup

## Deploy

1. Create a Railway service from this repository.
2. Add a persistent volume mounted at `/data`.
3. Set `OPENAI_API_KEY`, a long random `INBOUND_EMAIL_SECRET`,
   `ORBIT_STORAGE_DIR=/data/documents`, and `OPENAI_INVOICE_MODEL=gpt-4.1-mini`.
4. Railway uses the included `Procfile` and `/health` endpoint.

SQLite plus a volume is appropriate for an MVP with one application replica. Before
horizontal scaling, move relational data to Railway Postgres and documents to encrypted
object storage.

## Route `@invoices.orbitguest.com`

1. Add the inbound-mail provider's MX records for the `invoices.orbitguest.com`
   subdomain in your DNS provider. Keep normal `orbitguest.com` mail separate.
2. Configure a catch-all inbound route for `*@invoices.orbitguest.com`.
3. Configure the provider adapter to download attachments, base64-encode them into
   the documented payload, HMAC-sign the exact JSON body, and POST it to
   `https://<railway-domain>/v1/inbound/email`.
4. When a restaurant signs up, give it the unique `invoice_email` returned by Orbit.

The address contains the normalized restaurant name plus a random suffix. This makes
it readable for vendors while preventing collisions and easy address guessing.

## OpenAI extraction

The model is configurable through `OPENAI_INVOICE_MODEL`. The default is
`gpt-4.1-mini`, selected as a cost-conscious multimodal extraction model rather than
using the absolute smallest model at the expense of receipt accuracy. Validate model
quality and pricing on your own invoice set before production rollout.
