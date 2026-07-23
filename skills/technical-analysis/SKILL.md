# Technical Analysis Skill

**Name:** `technical`
**Required capability:** `PRICES`
**Implementation:** `src/trade_research/skills/core.py:TechnicalSkill`

## Purpose

Computes 13 deterministic technical indicators from sorted, validated OHLCV price history.
All algorithms are closed-form — no ML, no parameter optimization, no lookahead bias.

## Configuration

The skill is a frozen dataclass with one configurable parameter:

```python
TechnicalSkill(window=20)  # default window for SMA, EMA, Bollinger, volatility
```

Internal windows are fixed as class-level constants (overridable for testing):
- RSI: 14 bars
- MACD: 12/26/9 (fast/slow/signal)
- ATR: 14 bars
- Momentum: 10 bars
- Volatility: 20 bars, annualized × √252
- Volume trend: 20 bars (last 20 vs prior 20)
- Bollinger: ±2.0 standard deviations

## Metrics computed

| Metric | Algorithm | Minimum bars |
|---|---|---|
| `price_return` | close_last / close_first − 1 | 2 |
| `simple_moving_average` | arithmetic mean of closes over window | window |
| `simple_moving_average_20` | SMA(20) — only when window=20 | 20 |
| `exponential_moving_average_20` | EMA(20) — only when window=20 | 20 |
| `relative_strength_index_14` | Wilder RSI(14) | 15 (14 + 1) |
| `macd_12_26` | EMA(12) − EMA(26) of closes | 26 + 9 |
| `macd_signal_9` | EMA(9) of MACD line | 26 + 9 |
| `macd_histogram` | MACD − signal line | 26 + 9 |
| `bollinger_middle_20` | SMA(20) — only when window=20 | 20 |
| `bollinger_upper_20_2` | SMA + 2σ — only when window=20 | 20 |
| `bollinger_lower_20_2` | SMA − 2σ — only when window=20 | 20 |
| `average_true_range_14` | Wilder ATR(14) | 14 (complete OHLCV) |
| `momentum_10` | close_t / close_{t-10} − 1 | 11 (10 + 1) |
| `annualized_volatility_20` | σ(daily returns) × √252 | 21 (20 + 1) |
| `volume_trend_20` | (avg volume last 20) / (avg volume prior 20) − 1 | 40 (20 + 20) |

## Algorithm

1. **Validate prices:** `_validated_prices()` deduplicates by timestamp, discards invalid
   rows (non-finite closes, negative prices, high < low, etc.), and sorts chronologically.

2. **Check thresholds:** Each metric has a minimum bar count. If insufficient data, the
   metric is listed as missing rather than computed from inadequate data.

3. **Compute indicators:** Each uses a closed-form algorithm:
   - **EMA:** `(value − ema) × multiplier + ema` where `multiplier = 2/(period+1)`
   - **RSI:** Wilder smoothing with arithmetic-mean seed; returns 100 if zero loss,
     50 if zero gain
   - **MACD:** EMA(12) − EMA(26) of close; signal is EMA(9) of MACD
   - **ATR:** Wilder smoothing of true range (max of high−low, |high−prev_close|,
     |low−prev_close|)
   - **Volatility:** Population stddev of daily log-relative returns, annualized
   - **Volume trend:** Ratio of average volume in last half vs first half of volume window

4. **Provenance:** Each derived `Observation` records the exact input price series
   (up to `MAX_PROVENANCE_ITEMS` points), algorithm identifier, window label, start/end
   timestamps, and a SHA-256 series reference.

## Input data requirements

- Price points must have timezone-aware `observed_at`
- Close must be finite and positive
- For ATR and Bollinger bands: complete OHLCV (open, high, low, close, volume all present)
- Maximum 4096 price points per request

## Output

Returns `AnalystResult` with:
- `status`: `complete` if all indicators computed, `partial` otherwise
- `summary`: lists missing indicators or "complete data"
- `observations`: derived metrics with `source="derived"` and full input provenance

## Failure modes

| Condition | Behavior |
|---|---|
| No prices provider configured | `ProviderConfigurationError` |
| Non-finite close or negative volume | `ProviderConfigurationError` (before analysis) |
| Duplicate timestamps | Discards duplicates, continues |
| Insufficient bars for indicator | Skips that indicator, marks `partial` |
| Incomplete OHLCV | Skips ATR (requires O/H/L/C/V), notes limitation |
| Prior volume window = 0 | Skips volume_trend |
