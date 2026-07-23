# markov-method — Markov Regime Detection Skill

**Name:** `markov-method`
**Required capability:** `PRICES`
**Implementation:** `src/trade_research/skills/markov_method.py`
**Original algorithm:** https://github.com/jackson-video-resources/markov-hedge-fund-method

## Purpose

A regime-detection pipeline that labels each trading day Bull/Bear/Sideways via
rolling returns, builds a 3×3 Markov transition matrix, computes the stationary
(long-run) distribution, and emits a signed signal (bull_prob − bear_prob) with
conviction. The algorithm works on **any asset** — equities, crypto, FX — and
is purely computational, reusing the existing `PRICES` provider capability.

Framework by Roan (@RohOnChain); refactored into plugin form by Lewis Jackson.

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `window` | 20 | Lookback window (trading days) for rolling-return calculation |
| `threshold` | 0.05 | ±5% threshold for Bull/Bear regime boundaries |
| `min_train` | 252 | Minimum training bars (~1 year) for regime detection |
| `run_walkforward` | `False` | Enable walk-forward backtest (O(n²), validation only) |

## Algorithm

### Step 1 — Regime Labelling

Each day is classified using a rolling-return rule:

```
rolling_return[i] = (close[i] - close[i - window]) / close[i - window]

if rolling_return >= +threshold → Bull  (2)
if rolling_return <= −threshold → Bear  (0)
otherwise                      → Sideways (1)
```

Days before the first `window` bars default to Sideways.

### Step 2 — Transition Matrix

A 3×3 maximum-likelihood transition matrix is built from all regime-to-regime
transitions in the labelled history.  Rows are from-states, columns are
to-states (order: Bear=0, Sideways=1, Bull=2).  Each row sums to 1.0.  If a
regime never occurs, its row defaults to [1/3, 1/3, 1/3].

```
P[i][j] = count(transitions from i to j) / count(total transitions out of i)
```

### Step 3 — Stationary Distribution

The long-run (unconditional) regime mix is computed via power iteration:
start from uniform [1/3, 1/3, 1/3], repeatedly multiply by P until convergence
(tolerance 1e-10, max 1000 iterations).

```
π = π @ P   (solved iteratively)
```

### Step 4 — Signal

A signed signal is emitted from the current regime's next-step probabilities:

```
signal = P(Bull | current_regime) − P(Bear | current_regime)
```

- **Sign** → direction (positive = bullish bias, negative = bearish bias)
- **Magnitude** (∈ [−1, 1]) → conviction

### Step 5 — Walk-Forward Backtest (optional)

When `run_walkforward=True`, a no-lookahead backtest refits the full pipeline
at each step using only data available up to that point. The signal is applied
as a position size (−1 to 1) against the forward 1-day return.  Aggregate
metrics reported: annualised Sharpe ratio, maximum drawdown, and trade count.

## Metrics Computed

| Metric | Type | Description |
|---|---|---|
| `MARKOV_CURRENT_REGIME` | str | Bull / Bear / Sideways as of the last bar |
| `MARKOV_SIGNAL` | float | bull_prob − bear_prob ∈ [−1, 1] |
| `MARKOV_STATIONARY_BULL` | float | Long-run probability of Bull regime |
| `MARKOV_STATIONARY_BEAR` | float | Long-run probability of Bear regime |
| `MARKOV_STATIONARY_SIDEWAYS` | float | Long-run probability of Sideways regime |
| `MARKOV_PERSISTENCE_BULL` | float | P(Bull \| Bull) — diagonal persistence |
| `MARKOV_PERSISTENCE_BEAR` | float | P(Bear \| Bear) — diagonal persistence |
| `MARKOV_PERSISTENCE_SIDEWAYS` | float | P(Sideways \| Sideways) — diagonal persistence |
| `MARKOV_WALKFORWARD_SHARPE` | float | Annualised Sharpe (only when `run_walkforward=True`) |
| `MARKOV_WALKFORWARD_MAX_DRAWDOWN` | float | Max peak-to-trough drawdown (only when `run_walkforward=True`) |

## Signal Mapping

| Signal range | `SignalKind` | Interpretation |
|---|---|---|
| > +0.3 | `BULLISH` | Strong bullish regime bias |
| −0.3 to +0.3 | `NEUTRAL` | Sideways / mixed regime |
| < −0.3 | `BEARISH` | Strong bearish regime bias |

## Input Requirements

| Requirement | Value |
|---|---|
| Minimum price bars | 30 |
| Recommended bars | 252+ (full training) |
| Price fields used | `close` |
| Provider capability | `PRICES` |

## Output Format

Returns an `AnalystResult` with:

| Field | Value |
|---|---|
| `analyst` | `"markov-method"` |
| `summary` | Single-line: regime, signal, stationary distribution percentages |
| `status` | `COMPLETE` (≥252 bars) or `PARTIAL` (<252 bars) |
| `signal` | `BULLISH`, `BEARISH`, or `NEUTRAL` |
| `observations` | 8–10 derived observations with `source="derived"` |
| `methods` | 3–4 `AnalysisMethod` entries (regime detection, matrix, stationary, walkforward) |

## Failure Modes

| Condition | Status | Behaviour |
|---|---|---|
| < 30 bars | `PARTIAL` | Empty result, `NOT_ASSESSED` signal |
| 30–251 bars | `PARTIAL` | Full metrics computed, `PARTIAL` flag set |
| Flat / no-regime data | `COMPLETE` | All Sideways, signal ≈ 0, `NEUTRAL` |
| Walk-forward with < 252 bars | `PARTIAL` | Walk-forward skipped, other metrics unaffected |
