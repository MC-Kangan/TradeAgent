# worth-buy-stocks — Trend-Scoring Skill

**Name:** `worth-buy-stocks`
**Required capability:** `PRICES`
**Implementation:** `src/trade_research/skills/worth_buy_stocks.py`

## Purpose

A 4-layer trend-scoring pipeline for US equities that produces a trading
discipline verdict ("是" / "观察" / "否" / "持仓需减风险" / "无法评分") from
daily OHLCV price data.  The algorithm is purely computational — it reuses
the existing `PRICES` provider capability and makes no network calls.

Later layers can only **downgrade** — never upgrade — the score or verdict
produced by earlier layers.  This constraint avoids "talking oneself into a
trade" by layering confirmatory signals.

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `benchmark_symbols` | `("SPY", "QQQ")` | Benchmark symbols for relative strength |

Internal weights (overridable at construction for testing):

| Parameter | Default | Description |
|---|---|---|
| `_momentum_weight` | 0.55 | Momentum weight in ALPHA composite |
| `_relative_strength_weight` | 0.35 | Relative strength weight |
| `_efficiency_weight` | 0.10 | Kaufman efficiency weight |
| `_ma_short` | 60 | Short moving average period |
| `_ma_long` | 200 | Long moving average period |
| `_min_bars` | 30 | Minimum bars for any analysis |
| `_min_bars_full` | 200 | Bars recommended for full pipeline |

## Algorithm

### Layer 1 — ALPHA Weighted Composite

```
Composite = 0.55 × NormalizedMomentum + 0.35 × RelativeStrength + 0.10 × EfficiencyRatio
```

- **Momentum (55%)**: 12-month return skipping the most recent month
  (`momentum_12_1`), normalised to 0–100.
- **Relative Strength (35%)**: Average return difference vs SPY and QQQ
  over 1m / 3m / 6m windows, normalised to 0–100.
- **Kaufman Efficiency (10%)**: 30-day price efficiency ratio × 100.

If benchmark data is unavailable, weights are renormalised to
momentum 85% / efficiency 15%.

### Layer 2 — Risk Veto

Computes a risk score (0–100, higher = riskier):

| Condition | Risk penalty |
|---|---|
| Close < MA60 < MA200 (bearish alignment) | +30 |
| Close < MA60 | +15 |
| Max drawdown > 20% over 252 bars | +25 |
| Max drawdown > 10% | +10 |
| Weekly MA bearish alignment (MA5 < MA10 < MA20 < MA30 with ≥1% spread) | +20 |
| Annualised volatility > 50% | +10 |

### Layer 3 — Technical Confirmation

Checks that can **block** a buy signal but never upgrade:

| Check | Block condition |
|---|---|
| MACD | MACD < signal line, or histogram declining over 3 bars |
| RSI(14) | > 70 (overbought) |
| KDJ(9,3,3) | %J > 100 (overbought), or %K < %D (bearish cross) |
| ADX(14) | < 20 (ranging market), or −DI > +DI (bearish DI cross) |
| Volume | Up/down volume ratio < 0.7 (distribution), OBV declining over 30 days |

### Layer 4 — Entry Timing

Classifies the current price position:

| Class | Code | Description |
|---|---|---|
| `trend_broken` | 0 | Below MA60 < MA200, no recovery |
| `overextended` | 1 | > MA20 + 3×ATR or RSI > 80 |
| `pullback_no_trigger` | 2 | Pullback from 30d high, reversal unconfirmed |
| `trend_continuation` | 3 | Above MA20, MACD bullish, ADX > 25 |
| `pullback_reversal` | 4 | Pullback with ≥ 2 reversal signals |
| `recovery_reversal` | 5 | Deep pullback (>12%) from broken trend with strong bounce |

Price levels computed: entry (current close), stop (below MA60/swing low with ATR
buffer), target (52-week high).

### Verdict Mapping

| Condition | Verdict |
|---|---|
| Composite ≥ 70, risk < 30, ≤ 1 block, no critical blocks | 是 (buy) |
| Composite ≥ 50, risk < 50, ≤ 2 blocks, no critical blocks | 观察 (watch) |
| Risk ≥ 50 or ≥ 3 blocks | 否 (no) |
| Composite ≥ 40, risk < 40 | 观察 (watch, borderline) |
| Risk ≥ 40 (held position concern) | 持仓需减风险 (reduce risk) |
| Otherwise | 否 (no) |

Critical blocks: `rsi_overbought`, `kdj_overbought`.

## Metrics Computed

| Metric | Type | Layer |
|---|---|---|
| `worth_buy_composite` | Float 0–100 | ALPHA |
| `worth_buy_momentum_score` | Float 0–100 | ALPHA |
| `worth_buy_relative_strength` | Float 0–100 | ALPHA |
| `worth_buy_efficiency_score` | Float 0–100 | ALPHA |
| `worth_buy_risk_veto` | Float 0–100 | Risk |
| `worth_buy_verdict` | Float (5/3/2/1/0) | Final |
| `worth_buy_entry_classification` | Float 0–5 | Entry |
| `worth_buy_entry_price` | Float | Entry |
| `worth_buy_stop_price` | Float | Entry |
| `worth_buy_target_price` | Float | Entry |
| `relative_strength_index_14` | Float 0–100 | Technical |
| `kdj_k` / `kdj_d` / `kdj_j` | Float | Technical |
| `adx_14` | Float | Technical |
| `efficiency_ratio` | Float 0–1 | Technical |
| `up_down_volume_ratio` | Float | Technical |
| `max_drawdown` | Float (negative %) | Risk |
| `weekly_bearish_alignment` | Float 0/1 | Risk |
| `simple_moving_average` | Float | Risk |

## Input Requirements

- Minimum 30 daily OHLCV bars from a `PRICES`-capable provider (Yahoo, CCXT,
  local CSV/Parquet/SQL)
- 200+ bars recommended for full MA200 computation and momentum_12_1
- Benchmarks (SPY, QQQ) optional — pipeline degrades gracefully

## Output Format

Returns an `AnalystResult` with:
- `summary`: "Verdict: X | Composite: N/100 | Risk: N/100 | Reason: ..."
- `status`: `COMPLETE` (no data issues) or `PARTIAL` (missing benchmarks,
  insufficient history, discarded OHLCV rows)
- `observations`: 15–20 derived `Observation` objects with `source="derived"`
- `methods`: Four `AnalysisMethod` entries — one per layer
- `signal`: `BULLISH` / `BEARISH` / `NEUTRAL` / `NOT_ASSESSED`

## Failure Modes

| Condition | Status | Notes |
|---|---|---|
| < 30 price bars | `PARTIAL` | Returns immediately, no observations |
| No benchmark data | `PARTIAL` | Runs without relative strength |
| Missing OHLCV fields | `PARTIAL` | Some indicators unavailable |
| Duplicate or invalid rows | `PARTIAL` | Discarded rows noted |
