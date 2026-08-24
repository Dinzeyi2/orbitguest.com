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
   - `RESEND_API_KEY=<your Resend API key>`
   - `RESEND_WEBHOOK_SECRET=<the whsec_ value from the Resend webhook>`
   - `OPENAI_PREDICTION_MODEL=gpt-4.1-mini`
   - `ORBIT_EMAIL_FROM=Orbit <messages@your-verified-domain>`
   - `TWILIO_ACCOUNT_SID=<your Twilio account SID>`
   - `TWILIO_AUTH_TOKEN=<your Twilio auth token>`
   - `TWILIO_FROM_NUMBER=<your Twilio sending number>`
   - Square variables listed in [`docs/SQUARE.md`](SQUARE.md)
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

1. In Resend, add `invoices.orbitguest.com` as a receiving domain and copy the MX
   record Resend gives you into the DNS provider for `orbitguest.com`. Keep normal
   `orbitguest.com` mail separate.
2. In Resend Webhooks, add
   `https://<railway-domain>/v1/webhooks/resend` and enable `email.received`.
3. Copy the webhook signing secret beginning with `whsec_` into Railway as
   `RESEND_WEBHOOK_SECRET`; add your Resend API key as `RESEND_API_KEY`.
4. Redeploy Orbit after saving the variables.
5. When a restaurant signs up, give it the unique `invoice_email` returned by Orbit.

The address contains the normalized restaurant name plus a random suffix. This makes
it readable for vendors while preventing collisions and easy address guessing. Resend
receives the message, signs the webhook, and Orbit downloads each attachment before
passing it to OpenAI.

If Railway logs previously showed Cloudflare `Error 1010: browser_signature_banned`,
deploy commit `HEAD` or newer. Orbit now identifies its server-side Resend API requests
with an explicit application user agent instead of Python's blocked default signature.

## OpenAI extraction

The model is configurable through `OPENAI_INVOICE_MODEL`. The default is
`gpt-4.1-mini`, selected as a cost-conscious multimodal extraction model rather than
using the absolute smallest model at the expense of receipt accuracy. Validate model
quality and pricing on your own invoice set before production rollout.

## Test one real invoice before connecting email

1. Create a restaurant:
   `curl -X POST "$ORBIT_URL/v1/merchants" -H 'Content-Type: application/json' -d '{"name":"My Restaurant"}'`
2. Copy the returned `invoice_email`.
3. From this repository, run:

```bash
ORBIT_URL=https://your-service.up.railway.app \
ORBIT_INVOICE_EMAIL=my-restaurant-xxxxxx@invoices.orbitguest.com \
INBOUND_EMAIL_SECRET='the-same-value-set-in-railway' \
python scripts/send_test_invoice.py /path/to/real-invoice.pdf
```

A successful response has `"status": "processed"` and an `invoice_id`. This tests
Railway storage, webhook security, OpenAI extraction, and database persistence. It
simulates the inbound provider; connecting Resend or another provider is the next step.
