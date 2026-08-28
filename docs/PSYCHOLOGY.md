# Psychological Intelligence Layer

Orbit treats psychology as testable hypotheses, never facts about a person's
mind. The layer runs inside the behavior engine for anonymous and identified
merchant-scoped profiles; only separately consented profiles may be contacted.

## Continuously updated hypotheses

Each customer receives versioned hypotheses for habit strength, familiarity,
novelty, convenience, financial value, recognition, social belonging, friction
sensitivity, price sensitivity, timing susceptibility, and marketing fatigue.
Every record stores confidence, state, supporting and contradicting evidence,
observation count, experiment count, last observed/tested timestamps, and
whether the result is observational or experimentally supported.

Evidence uses valid completed POS orders and excludes test, canceled, and fully
refunded transactions. Calendar-position correlation is never represented as a
known salary payday. Context correlation uses only source-labelled observations
recorded through `POST /v1/behavior/context`.

## Strategy library

The versioned library contains habit cue, implementation intention, recognition,
controlled novelty, genuine scarcity, endowed progress, genuine loss aversion,
social belonging, sensory anticipation, reduced friction, and silence.
Strategies that mention scarcity, rewards, expiration, novelty, or preparation
facts declare the verified evidence they require. They must not be used from an
OpenAI invention or an unsupported psychological label.

## Experiments and learning

Each eligible campaign records its selected strategy and a durable experiment
assignment. Orbit retains the no-message control group, measures conversions,
order value, estimated incremental profit, time to order, and unsubscribes, and
feeds results back into the next hypothesis update. A deterministic 20% safe
exploration rate tests another low-risk strategy so early assumptions do not
become permanent labels. High marketing fatigue forces the `silence` strategy.

Friction observations can be sent to `POST /v1/behavior/interactions`, including
checkout started/abandoned, prepared-basket conversion, saved-payment use,
delivery selection, and pickup selection.

`GET /v1/dashboard/psychology` exposes the evidence ledger, strategy library,
experiment assignments, and aggregate strategy results. Psychology facts sent
to OpenAI exclude names, emails, phone numbers, and internal guest/merchant IDs.
