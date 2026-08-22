# Railway deployment and supplier-email setup

## Deploy

1. Create a Railway service from this repository.
2. Deploy once with no variables. Orbit now starts safely using
   `/tmp/orbit/orbit.db`, and Railway should pass `/health`.
3. Add a persistent volume mounted at `/data` so invoices survive redeployments.
4. Add these Railway variables:
   - `ORBIT_DB_PATH=/data/orbit.db`
   - `ORBIT_STORAGE_DIR=/data/documents`
   - `OPENAI_API_KEY=<your OpenAI API key>`
   - `INBOUND_EMAIL_SECRET=<a long random secret>`
   - `OPENAI_INVOICE_MODEL=gpt-4.1-mini`
5. Redeploy. Railway uses the included start command, its injected `PORT`, and the
   `/health` endpoint.

The previous start command always used `/data/orbit.db`. Railway failed when `/data`
did not exist because no volume was mounted. The application now creates database
directories automatically and starts from a writable `/tmp` default when the volume
has not been configured.

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
