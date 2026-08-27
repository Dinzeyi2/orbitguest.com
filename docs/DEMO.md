# Sandbox behavior-engine demo

The demo seeder is deliberately impossible to run unless both conditions are true:

```text
SQUARE_ENVIRONMENT=sandbox
ORBIT_DEMO_MODE=true
```

It also refuses a merchant that has an active Production Square installation in the
same database. Do not set `ORBIT_DEMO_MODE=true` on the Production Railway service.

## Run against staging

Add `ORBIT_DEMO_MODE=true` to the **staging** Railway service, confirm its Square
environment is `sandbox`, deploy, and run:

```bash
python scripts/seed_behavior_demo.py \
  --url https://staging-api.orbitguest.com \
  --api-key YOUR_SANDBOX_RESTAURANT_API_KEY
```

The script calls authenticated `POST /v1/demo/behavior/seed`. The response includes:

- Counts for created orders, profiles, canceled and refunded examples.
- The ribs regular's visit count, approximately 14-day cadence, favorite item, and
  return probabilities.
- The behavior engine's run summary.
- Predictions returned by the configured OpenAI behavior predictor.

The dataset spans roughly 90 days and creates 14 POS profiles (at least 10 remain
anonymous), varied locations, weekdays/hours, fulfillment types, modifiers, baskets,
discounts, canceled and fully refunded orders, and mixed SMS consent states.

After testing, set `ORBIT_DEMO_MODE=false` or remove it and redeploy staging.
