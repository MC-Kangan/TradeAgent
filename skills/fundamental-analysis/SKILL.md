# Fundamental Analysis Skill

**Name:** `fundamental`
**Required capability:** `FUNDAMENTALS`
**Implementation:** `src/trade_research/skills/core.py:FundamentalSkill`

## Purpose

Computes 11 financial ratios and growth metrics from statement and valuation data. Designed
for equity screening — produces point-in-time factors like P/E, FCF yield, and ROE that
can be compared across companies.

## Metrics computed

| Metric | Formula | Type | Inputs |
|---|---|---|---|
| `revenue_growth` | (revenue_current − revenue_prior) / revenue_prior | Growth | revenue (current + prior) |
| `earnings_growth` | (earnings_current − earnings_prior) / earnings_prior | Growth | net_income or earnings (current + prior) |
| `operating_margin` | operating_income / revenue | Profitability | operating_income, revenue |
| `net_margin` | net_income / revenue | Profitability | net_income or earnings, revenue |
| `return_on_equity` | net_income / shareholders_equity | Efficiency | net_income or earnings, equity |
| `free_cash_flow` | direct value | Cash flow | free_cash_flow |
| `free_cash_flow_margin` | FCF / revenue | Cash flow | free_cash_flow, revenue |
| `leverage` | total_debt / equity | Risk | total_debt, equity |
| `price_to_earnings` | market_cap / net_income | Valuation | market_cap, net_income or earnings |
| `enterprise_value_to_ebitda` | EV / EBITDA | Valuation | enterprise_value, ebitda |
| `free_cash_flow_yield` | FCF / market_cap | Valuation | free_cash_flow, market_cap |

## Algorithm

1. **Select inputs:** `_select_metric()` finds the most recent compatible observation for
   each required metric. Accepts aliases (e.g., `net_income` or `earnings`). Falls back to
   observations without a `period_role` when no explicit "current" is available.

2. **Compatibility checks:**
   - **Growth factors** require current and prior observations from the same snapshot,
     period type, and currency, with `current.prior_period_ref == prior.period_ref`.
   - **Statement ratios** require both inputs to share snapshot, period end, period type,
     period ID, and currency.
   - **Valuation ratios** require the statement period end to fall on or before the
     valuation as-of date, with matching snapshot and currency.

3. **Compute:** Each metric is rounded to 10 decimal places and traced with an
   `Observation` carrying full input provenance.

## Input data requirements

- Statement metrics (`revenue`, `net_income`, `operating_income`, etc.) must carry:
  `period_role`, `period_end`, `period_type`, `period_ref`, `currency`, `snapshot_ref`
- Valuation metrics (`market_cap`, `enterprise_value`) must carry:
  `valuation_as_of`, `currency`, `snapshot_ref`
- All observations must have timezone-aware timestamps.

## Output

Returns `AnalystResult` with:
- `status`: `complete` if all 11 metrics computed, `partial` otherwise
- `summary`: human-readable list of missing inputs or "complete data"
- `observations`: tuple of derived `Observation` objects with `source="derived"`
- `missing_metrics`: enum values for metrics that could not be computed
- `limitations`: `MISSING_INPUTS`, `INCOMPATIBLE_INPUTS` as applicable

## Failure modes

| Condition | Behavior |
|---|---|
| No fundamentals provider configured | `ProviderConfigurationError` before analysis |
| Missing required input metrics | Skips dependent factors, returns `partial` |
| Incompatible period/currency/snapshot | Skips factor, reports incompatibility |
| Zero denominator | Skips factor (avoids division by zero) |
