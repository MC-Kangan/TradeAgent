# Signal evaluation methodology

`signal-evaluation` measures what happened after deterministic, timestamped trading
signals. It deliberately separates two concerns:

1. A **signal producer** decides when to be long or short using price, RSI, moving
   averages, implied volatility, fundamentals, or any other causal inputs.
2. The **evaluator** applies one fixed outcome definition to those instructions and
   reports win rate, payoff, expected value, uncertainty, and out-of-sample evidence.

This separation lets the same evaluator compare an MA crossover on equities, a
breakout on crypto, or an IV mean-reversion signal without embedding any of those
strategies in the scoring code.

The evaluator is an outcome-research tool. It is not a position-sizing engine, broker
simulator, or live recommendation system.

## Core inputs

### Outcome series

The target is one ordered scalar time series:

- `price`: normally evaluated with relative changes.
- `implied_volatility`: normally evaluated with absolute changes in decimal IV.
- `generic`: another consistently measured scalar quantity.

Each point contains a timezone-aware timestamp and observed value. Optional high and
low values allow intrabarrier testing. A series must use one consistent barrier basis:

- `observed_value`: only the sampled value can touch a barrier.
- `high_low`: every point must contain complete high and low values.

Signals must align exactly to timestamps in the outcome series. Multi-source signal
construction should therefore perform its as-of joins before calling the evaluator.

### Signal events

Each entry event has:

```python
SignalEvent(
    observed_at=timestamp,
    action="add_long",      # or "add_short"
    initial_risk=0.02,       # optional, known before the outcome
)
```

Events must be timezone-aware, ordered, unique, and use an entry action. `initial_risk` uses the
same units as the outcome change:

- Relative price study: `0.02` means 2% of entry value.
- Absolute IV study: `0.01` means one volatility point when IV is represented as a
  decimal such as `0.20`.

When signal-specific risk is omitted, the study-level `stop_loss` is one R. When it
is supplied, it also replaces that event's triple-barrier stop distance. The common
profit target does not change.

### Outcome specification

`OutcomeSpecification` fixes the rules applied to every signal:

- Relative or absolute changes.
- Entry lag in bars.
- Fixed evaluation horizon.
- Profit target and stop loss.
- Maximum triple-barrier holding horizon.
- Observed-value or high/low barrier basis.
- Bootstrap and random-baseline settings.
- Optional development, validation, and holdout periods.

Do not choose these settings after examining holdout results. Version a new experiment
when targets, stops, horizons, filters, or signal parameters change.

## Event timing

For a signal observed at bar `t`:

1. Entry occurs at `t + entry_lag_bars`; the default is the next observation.
2. The fixed-horizon view exits exactly `fixed_horizon_bars` after entry.
3. The triple-barrier view exits at the first target, stop, or time limit.

The next-bar entry prevents the signal bar from using its own future outcome. The
framework cannot prove that an external signal generator was causal, so the caller
must ensure every feature used at `t` was genuinely available at `t`.

For daily or snapshot OHLC data, target and stop can both appear inside the same bar
without revealing which traded first. Such an event is conservatively scored as a
stop loss and marked `same_bar_ambiguous`.

A known target or stop hit near the end of the dataset is retained. An event that has
not hit either barrier and lacks its complete time horizon is right-censored and
counted as skipped.

## Directional change and R

For entry value `E` and exit value `X`:

```text
Relative long change  = X / E - 1
Relative short change = -(X / E - 1)

Absolute long change  = X - E
Absolute short change = -(X - E)
```

For event `i`, with ex-ante risk `risk_i`:

```text
R_i = directional_change_i / risk_i
```

Positive changes are wins, negative changes are losses, and exact zero is a
breakeven. Maximum favorable and adverse excursions are reported in both native
units and R.

## Metrics

Let:

```text
N       = completed event count
p_win   = wins / N
p_loss  = losses / N
b       = breakevens / N
W       = average positive change
L       = absolute average negative change
W_R     = average positive R
L_R     = absolute average negative R
```

### Win rate

```text
win_rate = wins / N
```

Breakevens stay in the denominator. The evaluator also reports win rate on the
non-overlapping sample and its Wilson 95% lower confidence bound. The Wilson lower
bound is useful when comparing small samples because it penalizes an apparently high
win rate supported by few independent observations.

For `x` wins in `n` non-overlapping events, with `z = 1.9599639845`, the reported
lower bound is:

```text
p_hat = x / n
lower = [p_hat + z²/(2n)
         - z * sqrt(p_hat*(1-p_hat)/n + z²/(4n²))]
        / [1 + z²/n]
```

### Reward/risk ratio

```text
reward_risk_ratio = W_R / L_R
```

This is based on R rather than raw price or IV units, allowing events with different
declared risks to be compared consistently.

### Expected value

Native-unit expectancy is:

```text
expected_value = mean(directional_change_i)
               = p_win * W - p_loss * L
```

Risk-normalized expectancy is:

```text
expected_r = mean(R_i)
```

`expected_r` is the most direct version of the intuitive formula:

```text
EV_R = win rate * average win R - loss rate * average loss R
```

### Break-even win rate and edge

Because breakevens remain in the total event count, the unconditional break-even win
rate is:

```text
break_even_win_rate = (1 - b) * L_R / (W_R + L_R)
edge_over_break_even = p_win - break_even_win_rate
```

This ensures that a `+1R, 0R, -1R` sample has zero expectancy and zero edge.

### Win-rate × payoff diagnostic

```text
win_payoff_product = p_win * reward_risk_ratio
```

This is retained because it is intuitive, but it is **not expectancy**: it omits the
loss probability and breakeven structure. Use `expected_r` for the economic score.

### Profit factor and winner concentration

```text
profit_factor = sum(positive R) / abs(sum(negative R))

top_five_win_contribution = sum(five largest positive R) / sum(all positive R)
```

A high top-five contribution indicates that a small number of winners dominate the
result. Long and short event counts, win rates, and expected R are also reported
separately.

## Overlap and effective evidence

Signals can overlap, especially when their evaluation horizon is long. Counting all
overlapping trades is useful descriptively but exaggerates the amount of independent
evidence.

The evaluator therefore constructs a chronological non-overlapping sample. After
selecting one event, it applies an outcome-independent embargo:

- Fixed view: `fixed_horizon_bars`.
- Triple-barrier view: `max_holding_bars`.

It does not shorten the embargo when an event happens to exit early; doing so would
make sample selection depend on realized outcomes.

The primary conservative point estimate is:

```text
non_overlapping_expected_r = mean(R_i in the embargoed sample)
```

Always read `non_overlapping_event_count` beside this estimate.

## Bootstrap uncertainty

The evaluator uses a deterministic circular moving-block bootstrap over the ordered
non-overlapping R sequence:

1. Choose contiguous blocks with wraparound.
2. Concatenate blocks until the resample has the original sample length.
3. Calculate the resampled mean R.
4. Repeat `bootstrap_samples` times.

The default block length is `round(sqrt(N))`, bounded to the sample size. It can be
overridden with `bootstrap_block_length`.

Outputs include:

- `non_overlapping_expected_r_lower_95`
- `non_overlapping_expected_r_median`
- `non_overlapping_expected_r_upper_95`
- `bootstrap_positive_fraction`

The positive fraction is the share of resampled means above zero. It is a stability
diagnostic, not a Bayesian probability that the true EV is positive.

## Matched random-timestamp baseline

The baseline asks whether the signal timing adds value relative to random eligible
timestamps under the same outcome rules.

For every trial, the evaluator:

1. Samples the same number of timestamps as completed strategy events.
2. Preserves and shuffles the completed events' direction and risk profiles.
3. Applies the same entry lag, targets, stops, horizons, and non-overlap rule.
4. Records the random sample's non-overlapping expected R.

It reports the mean baseline EV, its 95% trial range, and:

```text
excess_expected_r = strategy non-overlapping expected R
                    - mean baseline expected R
```

For a credible comparison, supply `baseline_eligible_times`: every timestamp at which
the signal generator could causally have emitted an instruction, including timestamps
where it emitted nothing. This universe should reflect:

- Indicator warm-up.
- Trading-session restrictions.
- Required input availability.
- Asset or regime eligibility rules fixed before evaluation.
- Enough forward data for the selected outcome horizon.

When the universe is omitted, the evaluator uses timestamps between the earliest and
latest completed signal as a bounded smoke-test fallback. That is weaker evidence and
should not be used for a promotion decision. If the universe contains too few eligible
timestamps, baseline metrics remain unavailable rather than silently changing the
sample size.

## Development, validation, and holdout periods

Periods are ordered, unique, and non-overlapping. An event belongs to a period only
when:

```text
signal_at >= period.start_at
and exit_at <= period.end_at
```

An otherwise completed event that crosses the end boundary is counted as purged.
An instruction without a completed full-history outcome is counted as skipped.

For serious research:

1. Develop signal logic and choose parameters in `development`.
2. Use `validation` for limited model comparison if needed.
3. Freeze the strategy before `holdout` starts.
4. Read holdout metrics without tuning the strategy again.

The managed skill records:

- `strategy_frozen_at`: when the rule and parameters were fixed.
- `evaluation_data_end`: the final observation permitted in the run.
- `variant_count`: how many variants were tried.
- `holdout_is_post_freeze`: whether the holdout begins after the freeze timestamp.

Observations after `evaluation_data_end` are trimmed. Full-history and period metrics
are exported through the same standard observation schema with
`evaluation_period = full_history | development | validation | holdout`.

## Evaluating a new Python strategy

The smallest workflow has four parts: load data, create instructions, define the
candidate universe and periods, then call `evaluate_signals`.

```python
from trade_research.domain import InstrumentId
from trade_research.providers import OutcomePoint, YahooPriceProvider
from trade_research.signals import moving_average_crossover
from trade_research.skills.signal_evaluation import (
    EvaluationPeriod,
    OutcomeSpecification,
    evaluate_signals,
)

instrument = InstrumentId(symbol="SPY", market="ETF")
prices = YahooPriceProvider(range_="5y", interval="1d").price_history(instrument)

# The signal function may use any causal inputs. Its only evaluator-facing output is
# an ordered tuple of canonical SignalEvent values.
slow_window = 50
instructions = moving_average_crossover(
    prices,
    fast_window=20,
    slow_window=slow_window,
)

points = tuple(
    OutcomePoint(
        observed_at=bar.observed_at,
        value=bar.close,
        high=bar.high,
        low=bar.low,
    )
    for bar in prices
)

entry_lag = 1
fixed_horizon = 10
max_holding = 20
required_forward = entry_lag + max(fixed_horizon, max_holding)

# The MA strategy cannot emit a signal before the slow window exists. Include every
# causally eligible timestamp after warm-up, not only timestamps with actual signals.
last_candidate_index = len(prices) - required_forward
eligible_times = tuple(
    bar.observed_at
    for bar in prices[slow_window:last_candidate_index]
)

split = int(len(prices) * 0.8)
specification = OutcomeSpecification(
    change_kind="relative",
    fixed_horizon_bars=fixed_horizon,
    profit_target=0.03,
    stop_loss=0.02,
    max_holding_bars=max_holding,
    entry_lag_bars=entry_lag,
    barrier_basis="high_low",
    bootstrap_samples=1_000,
    baseline_trials=200,
    baseline_eligible_times=eligible_times,
    periods=(
        EvaluationPeriod(
            name="development",
            start_at=prices[0].observed_at,
            end_at=prices[split - 1].observed_at,
        ),
        EvaluationPeriod(
            name="holdout",
            start_at=prices[split].observed_at,
            end_at=prices[-1].observed_at,
        ),
    ),
)

study = evaluate_signals(points, instructions, specification)

print("Full-history EV R:", study.triple_barrier.expected_r)
print("Independent EV R:", study.triple_barrier.non_overlapping_expected_r)
print(
    "Independent 95% interval:",
    study.triple_barrier.non_overlapping_expected_r_lower_95,
    study.triple_barrier.non_overlapping_expected_r_upper_95,
)
print("Excess vs random:", study.triple_barrier.excess_expected_r)

holdout = next(period for period in study.periods if period.name == "holdout")
print("Holdout independent N:", holdout.triple_barrier.non_overlapping_event_count)
print("Holdout independent EV R:", holdout.triple_barrier.non_overlapping_expected_r)
```

Run the existing end-to-end example with:

```bash
.venv/bin/python examples/ma_signal_evaluation.py
```

To test another strategy, replace `moving_average_crossover(...)` with a function that
returns ordered `SignalEvent` values. The evaluator does not need to know whether
those events came from Turtle channels, RSI, gaps, option IV, or a multi-source
model.

## Using the managed skill or another application

For HTTP/MCP/native application integration, select `signal-evaluation` and provide
parameters under `skill_parameters.signal-evaluation`. The normalized shape is:

```json
{
  "signal_name": "rsi-ma-iv",
  "target_series": {
    "name": "close",
    "kind": "price",
    "unit": "price",
    "strike_convention": "not_applicable"
  },
  "instructions": [
    {
      "observed_at": "2026-01-05T00:00:00Z",
      "direction": "long",
      "initial_risk": 0.03
    }
  ],
  "experiment": {
    "experiment_id": "rsi-ma-iv-001",
    "strategy_version": "1.0.0",
    "strategy_frozen_at": "2026-09-30T00:00:00Z",
    "evaluation_data_end": "2026-12-31T00:00:00Z",
    "variant_count": 12
  },
  "change_kind": "relative",
  "entry_lag_bars": 1,
  "fixed_horizon_bars": 21,
  "profit_target": 0.06,
  "stop_loss": 0.03,
  "max_holding_bars": 63,
  "bootstrap_samples": 1000,
  "baseline_trials": 200,
  "baseline_eligible_times": [
    "2026-01-05T00:00:00Z"
  ],
  "periods": [
    {
      "name": "development",
      "start_at": "2024-01-01T00:00:00Z",
      "end_at": "2026-09-30T00:00:00Z"
    },
    {
      "name": "holdout",
      "start_at": "2026-10-01T00:00:00Z",
      "end_at": "2026-12-31T00:00:00Z"
    }
  ]
}
```

In a real request, `baseline_eligible_times` should contain the complete candidate
universe, not just the example signal timestamp. Price bars can be supplied inline or
retrieved through a registered provider. Fixed-strike or floating-delta IV data uses
the same request with an `implied_volatility` target series.

## How to read a result

For promotion decisions, read metrics in this order:

1. **Holdout non-overlapping event count** — is there enough independent evidence?
2. **Holdout non-overlapping expected R** — is the conservative point estimate
   positive?
3. **Holdout 95% interval** — does uncertainty still include zero?
4. **Excess expected R** — does timing beat matched random timestamps?
5. **Winner concentration and direction splits** — is the result diversified across
   trades and long/short regimes?
6. **Development versus holdout stability** — did the edge survive without retuning?

A positive point estimate with an interval crossing zero is a research candidate, not
a validated edge. `win_payoff_product` should never override negative expected R or a
weak holdout.

## Prices, crypto, and implied volatility

The evaluator is asset-agnostic because it scores a normalized scalar series:

- Equity or crypto price: usually `change_kind="relative"`.
- Fixed-delta IV: usually `change_kind="absolute"`, with call delta and tenor.
- Fixed-strike IV: usually `change_kind="absolute"`, with strike and tenor.

For IV represented as decimal volatility, a move from `0.20` to `0.23` is `+0.03`, or
three volatility points. A one-point risk declaration is `initial_risk=0.01`.

IV movement expectancy is **not option P&L**. Executable option evaluation additionally
requires strike and expiry rolls, premium, delta/gamma/vega/theta, volatility-surface
movement, bid/ask spreads, commissions, and exercise/settlement rules.

## What this framework does not answer

The evaluator does not currently model:

- Commissions, spread, slippage, market impact, or borrow.
- Position sizing, pyramiding, capital constraints, or portfolio overlap.
- Strategy-native exits that differ from the common outcome definition.
- Formal correction for testing many strategy variants.
- Whether an externally generated signal actually avoided lookahead.
- Full option valuation or Greeks-based P&L.

Use this framework first to answer: **does the timestamped signal appear to contain
repeatable directional information under a fixed risk definition?** If the answer is
credible, move the strategy into a dedicated execution-aware backtest.

For the fixed analyst contract and security boundary, see [SKILL.md](SKILL.md).
