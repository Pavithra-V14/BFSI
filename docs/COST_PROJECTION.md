# Cost projection

Spec 18: free tiers only during the pilot; real spend is expected to be **$0**. This page says how
the projection is made and how to read it. The planning basis is a placeholder: **100 queries a
day [T]**, no monthly ceiling given yet.

## Make it

```
uv run python scripts/cost_projection.py
uv run python scripts/cost_projection.py --queries-per-day 400
```

No call is made and no key is read. It prints, per provider group, the requests and tokens a day
and for 30 days, the **shadow cost** of every group that has a list price (what the same tokens
would cost: `app/gateway/pricing.json`, else LiteLLM's price map, for the default models), a list
of the groups that have **no** price (shown as `UNPRICED` with the reason, never counted as $0),
and the billed cost (0 on the free tiers). `--json` has the same split (`priced`, `unpriced`).

## How to read it

- **Billed cost** is what a provider invoices. On the free plans it is 0. When a provider's free
  daily limit is reached the gateway moves to the next provider; it does not buy more.
- **Shadow cost** is the price you would pay if you moved to paid plans. It is what the dashboard
  shows next to the billed cost, always labelled separately.
- The **per-call sizes are placeholders** [T] copied from the evaluation estimator; Phase 16
  replaces them with measured numbers from the baseline run and compares the projection with a
  dry run (spec 17.6, "a cost projection compared with a dry run").
- Cohere's rerank may be the largest per-query cost on a paid plan: verify its price and trial
  limits [T] (spec 18). It is billed per search, not per token, and has no price in
  `pricing.json`, so the projection shows it as **UNPRICED** and says the total is a lower bound.
  Add its price to `pricing.json` (or read it off the provider's page) to include it.

## Where the limits are

The free-tier daily limits the gateway plans against are the defaults in `.env.example`
(`GATEWAY_LIMITS_*`); several are unpublished [T]. The same numbers drive the evaluation and load
estimates, so a plan that does not fit a day there will not fit it here.

## Alerts

`ALERT_DAILY_TOKEN_PLAN` (default 340,000, about 100 queries at 3,400 tokens) and
`ALERT_BUDGET_VARIANCE` (1.2) raise a warning when today's tokens pass the plan by the variance.
Change the plan there if you change the planning basis.
