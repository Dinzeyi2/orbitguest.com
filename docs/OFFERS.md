# Restaurant offer enrollment

Every merchant receives a unique public enrollment page when it is created. By
default its URL is `${PUBLIC_BASE_URL}/join/{slug}`; set
`ORBIT_ENROLLMENT_BASE_URL` when the page should use another HTTPS domain.

## Dashboard setup

Create or replace the active restaurant-defined offer:

```http
POST /v1/offers
Authorization: Bearer <merchant-api-key>
Content-Type: application/json

{
  "name": "Welcome offer",
  "discount_type": "percent",
  "discount_value": 10,
  "promo_code": "WELCOME10",
  "offer_terms": "Valid once on one purchase.",
  "ends_at": "2026-12-31T23:59:59+00:00"
}
```

`GET /v1/offers` returns the copyable page URL, configured offers, enrollments,
SMS delivery state, POS-link state, and redemption state.

## Customer flow

The public `GET /join/{slug}` page collects an E.164 phone number plus explicit
terms and SMS consent. It submits to `POST /v1/public/enroll/{slug}/submit`.
Orbit sends exactly one welcome-offer SMS containing the restaurant name, the
restaurant-configured promo code and terms, and required STOP instructions.
Duplicate enrollment for the same phone and offer does not send another text.
Enrollment attempts are rate limited.

The JSON description used by custom frontends is available from
`GET /v1/public/enroll/{slug}`. It deliberately does not expose the promo code.

## Exact POS identity linking

A generic signup cannot safely identify an old anonymous payment profile from a
phone number alone. Orbit never guesses. It links deterministically in either
of two ways:

1. Append a valid order claim token to the page as `?claim_token=...`; enrollment
   activates the exact POS guest behind that claim.
2. When the code is redeemed, call authenticated `POST /v1/offers/redeem` with
   the phone, promo code, POS source, and external order ID. Orbit joins the
   enrollment to the exact guest attached to that completed POS order and moves
   the consented phone onto that merchant-scoped identity.

Codes are single-use per enrolled phone. Redemption is idempotently protected
by unique enrollment and order constraints, and expired, inactive, exhausted,
or previously redeemed offers are rejected.
