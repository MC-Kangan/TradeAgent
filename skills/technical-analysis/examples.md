# Technical Analysis Examples

## Complete data scenario (40 bars, default window=20)

Input: 40 chronologically ordered OHLCV points with sequential closes from 100 to 139,
open = close − 0.5, high = close + 1, low = close − 1, volume = 100 (first 20), 200 (last 20).

Expected output:

| Metric | Value | Notes |
|---|---|---|
| price_return | 0.39 | (139 − 100) / 100 |
| simple_moving_average_20 | 129.5 | Avg of last 20 closes |
| exponential_moving_average_20 | 129.5 | Convergent on linear series |
| relative_strength_index_14 | 100.0 | Monotonic upward = all gains |
| macd_12_26 | 7.0 | Fast EMA − slow EMA |
| macd_signal_9 | 7.0 | EMA of MACD |
| macd_histogram | 0.0 | MACD − signal |
| bollinger_middle_20 | 129.5 | SMA(20) |
| bollinger_upper_20_2 | ~141.03 | SMA + 2σ |
| bollinger_lower_20_2 | ~117.97 | SMA − 2σ |
| average_true_range_14 | 2.0 | Constant range |
| momentum_10 | ~0.07752 | (139 − 129) / 129 |
| annualized_volatility_20 | ~0.00571 | Near-zero on linear trend |
| volume_trend_20 | 1.0 | 200/100 − 1 |

## Close-only data (no OHLCV)

When only close prices are provided (e.g., from a legacy system that tracks PX_LAST):

```python
PricePoint(
    observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    close=100.0,
    source="internal",
    provenance={"provider_kind": "internal", "vendor_field": "PX_LAST", ...},
)
```

Expected behavior:
- SMA, EMA, RSI, MACD, momentum, and volatility all compute from close prices
- ATR is skipped (requires O/H/L/C/V)
- Bollinger bands require only close prices (computed from `pstdev` of closes) — they still compute
- ATR is skipped (requires O/H/L/C/V)
- Status: `partial`, summary: `"partial data: missing complete OHLCV fields, average_true_range"`

## Insufficient data scenario (5 bars)

When only 5 price points are provided:

Expected behavior:
- `price_return` computes (≥ 2 bars)
- All other indicators are skipped (need ≥ 10-40 bars)
- Status: `partial`, summary lists all missing indicators

## CLI invocation

```sh
trade-research run-skill technical AAPL
trade-research run-skill technical BTC/USDT --market CRYPTO  # requires CCXT
```
