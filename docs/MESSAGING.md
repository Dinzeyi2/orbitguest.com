# Production messaging: Telnyx SMS and Resend email

Orbit does not use Twilio. SMS delivery uses Telnyx; email delivery uses Resend.

## Telnyx setup

1. Create a Telnyx Messaging Profile and purchase or assign an approved sender number.
2. Complete every registration required for each destination country. Orbit cannot
   perform carrier registration for you.
3. Configure the profile webhook as
   `https://api.orbitguest.com/v1/webhooks/telnyx` and copy its Ed25519 public key.
4. Add these Railway variables:

```text
TELNYX_API_KEY=KEY...
TELNYX_FROM_NUMBER=+15551234567
TELNYX_MESSAGING_PROFILE_ID=...
TELNYX_PUBLIC_KEY=...
TELNYX_ALLOWED_COUNTRY_PREFIXES=+1
ORBIT_MESSAGE_MAX_ATTEMPTS=5
ORBIT_MESSAGE_INTERVAL_SECONDS=30
ORBIT_ENABLE_LIVE_MESSAGING=true
```

`TELNYX_ALLOWED_COUNTRY_PREFIXES` is a comma-separated deployment guard. Orbit rejects
destinations outside it. Set only prefixes covered by your approved sender and
registration. Telnyx webhooks are signature- and timestamp-verified before processing.

Orbit handles STOP/STOPALL/UNSUBSCRIBE/CANCEL/END/QUIT, START/UNSTOP, and HELP/INFO.
STOP immediately suppresses SMS across matching Orbit profiles. START restores SMS only
for a profile that already accepted terms. HELP sends the configured help response.

## Resend setup

Verify the sending domain in Resend and set:

```text
RESEND_API_KEY=re_...
ORBIT_EMAIL_FROM=Restaurant via Orbit <messages@orbitguest.com>
RESEND_WEBHOOK_SECRET=whsec_...
```

Configure `https://api.orbitguest.com/v1/webhooks/resend` for delivered, bounced,
complained, and delayed events in addition to inbound `email.received`. Complaints
suppress future email. Webhook events are idempotent and synchronize delivery status.

## Restaurant policy

Configure authenticated `POST /v1/messaging/settings`:

```json
{
  "timezone": "America/New_York",
  "quiet_hours_start": "21:00",
  "quiet_hours_end": "08:00",
  "max_messages_per_guest_24h": 1,
  "max_messages_per_merchant_day": 100,
  "sms_help_text": "OrbitGuest restaurant messages. Reply STOP to opt out."
}
```

Queued campaigns are deferred during local quiet hours or when frequency caps are
reached. Transient provider failures retry with exponential backoff. Permanent failures
and exhausted retries become `dead`, set the campaign to `delivery_failed`, and remain
available for operator investigation. Provider idempotency keys prevent duplicate sends.
Operators can inspect `GET /v1/messaging/dead-letters` and deliberately retry one with
`POST /v1/messaging/dead-letters/{message_id}/retry` after correcting the cause.

Before live sending, test one approved number, STOP/START/HELP, delivery/failure events,
email bounce/complaint handling, quiet-hour boundaries, retries, and country registration
in a controlled environment.
