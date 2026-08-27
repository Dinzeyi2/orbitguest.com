# Recipe, inventory, prediction, and evaluation operations

Orbit uses inventory only as a retention guardrail. It is not a physical inventory
system and never presents invoice-derived quantities as exact counts.

## Recipe and inventory workflow

1. Supplier invoices append purchases and prices to the product history.
2. The Square catalog supplies active menu items and selling prices.
3. `POST /v1/recipes/proposals` creates conservative ingredient-name suggestions.
   Suggestions remain `pending`; Orbit does not invent quantities or confirm recipes.
4. A manager reviews `POST /v1/recipes/proposals/{id}/review` with `decision`,
   `reviewed_by`, and explicit components. Every component includes `product_id`,
   `quantity_required`, `unit`, `waste_percent`, `yield_percent`, optional
   `packaging_cost_cents`, and optional `substitution_group`.
5. If invoice and recipe units differ, configure a restaurant/ingredient-specific
   conversion with `POST /v1/inventory/conversions`. Missing conversions lower
   confidence; Orbit never guesses them.
6. Completed, non-test, non-fully-refunded POS items produce idempotent ingredient
   consumption. Canceled, test, and fully refunded orders are excluded.
7. Physical counts, waste, spoilage, remakes, staff meals, theft, transfers, and
   corrections use `POST /v1/inventory/adjustments` and remain auditable events.
8. `GET /v1/dashboard/inventory` returns ingredient ranges/confidence and menu-item
   portions, costs, packaging, margin, and one of `safe_to_promote`,
   `available_low_margin`, `probably_unavailable`, or `inventory_uncertain`.

The estimate is purchase quantity minus recipe-derived consumption plus adjustments.
Waste and yield increase the effective quantity consumed per sold portion. Contribution
margin is selling price minus estimated ingredient cost, packaging, and campaign
incentive. A current physical count should be posted as a `count`/`correction` before
high-volume promotions.

## Prediction responsibilities

Orbit's deterministic behavior profile calculates cadence, uncertainty, return
probabilities, preferred timing, item affinities, combinations, and expected value.
The rules layer enforces permission, suppression, cooldown, confirmed recipes,
inventory confidence, margin, restaurant capacity, and message cost.

OpenAI is only the constrained strategy/copy layer. It receives behavioral and
operational facts without contact details and may choose only an allowed action. It is
instructed not to invent availability, history, discounts, consent, prices, or
probabilities. Structured JSON is required. Calls have configurable input, timeout,
and retry limits; exhausted retries safely produce no opportunity.

Configuration:

```text
OPENAI_PREDICTION_MODEL=gpt-4.1-mini
OPENAI_PREDICTION_PROMPT_VERSION=strategy-v2
OPENAI_PREDICTION_MAX_ATTEMPTS=3
OPENAI_PREDICTION_TIMEOUT_SECONDS=30
OPENAI_PREDICTION_MAX_INPUT_CHARS=30000
OPENAI_PREDICTION_MAX_DAILY_CALLS=1000
```

Every model invocation records component/version, input hash, latency, attempts,
status, and structured output in `prediction_runs` for audit and cost governance.

## Approval policy

`POST /v1/campaign-policy` configures:

- `pilot`: every eligible campaign requires manager approval.
- `assisted`: high-confidence, no-discount, sufficiently certain campaigns can queue;
  lower-confidence or incentive actions require approval.
- `autonomous`: rules-eligible campaigns queue within restaurant policies.

Approval uses `POST /v1/campaigns/{id}/approve`. Dispatch still rechecks current
consent and whether the guest has already ordered. Large discounts and unusual
campaigns should remain approval-only through restaurant policy.

## Evaluations

- `POST /v1/evaluations/backtest` performs walk-forward historical holdout testing.
  It reports timing MAE, item accuracy, and order-value MAE, and stores every case.
- `POST /v1/evaluations/messages` checks generated campaigns for permission,
  operational grounding, predicted-item consistency, and prohibited creepy wording.
- `GET /v1/dashboard/evaluations` returns policy and versioned evaluation history.
- Control assignments and outcomes already feed `GET /v1/metrics`, which reports
  treatment/control conversion rates and estimated incremental revenue.

Do not enable autonomous mode merely because unit tests pass. Before doing so, run
backtests on representative restaurant history, define acceptance thresholds, review
message failures, run a controlled pilot, and verify positive incremental profit.
