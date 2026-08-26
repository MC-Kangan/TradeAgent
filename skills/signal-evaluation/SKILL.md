# Signal evaluation analyst contract

## Inputs

- One ordered, unique list of timezone-aware signal instructions. Each instruction
  contains only an observation timestamp and `long` or `short` direction.
- One ordered normalized outcome series aligned to the signal timestamps. Its typed
  identity records price, implied-volatility, or generic kind; units; source; and,
  for IV, tenor plus floating call-delta or fixed-strike coordinates.
- A relative-change convention for prices or an absolute-change convention for
  quantities such as implied volatility.
- A next-observation entry lag, fixed horizon, profit target, stop loss, and maximum
  holding horizon, all expressed explicitly in bars or outcome-series units.

Signal construction is outside this analyst. A caller may combine price, RSI,
moving averages, implied volatility, fundamentals, or any other causal data source
before supplying the resulting timestamped instructions.

## Method

Enter on the configured observation after each signal so the signal bar cannot use
its own future outcome. Produce two views over the same events:

1. Fixed horizon: measure the direction-adjusted change after a fixed number of bars.
2. Triple barrier: stop at the first profit target, stop loss, or time limit.

The series declares whether barriers use every observed scalar value or complete
high/low ranges. Mixed coverage is rejected. When high and low touch both barriers
in the same bar, record the stop loss.
This deterministic conservative rule avoids assuming an intrabar path that is not
present in the source data. Overlapping events remain in descriptive statistics,
while an outcome-independent maximum-horizon embargo supplies a non-overlapping
sample count and Wilson 95% lower bound for the win rate. A barrier hit observed
near the end of a dataset remains completed; only events with no hit and an
unfinished time horizon are right-censored and skipped.

## Output

- Event, non-overlapping-event, skipped, win, loss, and breakeven counts.
- Descriptive and non-overlapping win rates plus a Wilson 95% lower confidence bound.
- Average win, average loss, reward/risk ratio, the explicit `win rate × reward/risk`
  opportunity score, expected change, expectancy in R, and profit factor when each
  statistic is defined.
- At most 200 event details per view, with safe SHA-256 references to the signal
  instructions and normalized outcome series, plus a configuration hash covering
  every outcome rule.
- `not_assessed` directional signal: historical evaluation does not become a live
  recommendation.

## Provider boundary

The registered analyst consumes the bounded `OUTCOMES` provider protocol. Existing
`PRICES` providers are automatically adapted to the normalized `close` price series,
and authenticated callers may supply an immediate-only normalized series directly.
A historical SPY options dataset can therefore be normalized into absolute IV
points for research. Future Yahoo, Bloomberg, or internal `get_vol(ticker, delta,
tenor)` implementations provide the same typed series and perform only retrieval,
field mapping, timestamping, and provenance; they do not change evaluation semantics.

Raw licensed option payloads stay in the caller's authorized system and must never
be copied into fixtures, logs, reports, or commits.

## Limitations

The analyst does not discover signals, optimize thresholds, model execution costs,
infer missing timestamps, or correct selection and multiple-testing bias. A low
non-overlapping event count must be treated as weak evidence even when descriptive win
rate or reward/risk is high.
