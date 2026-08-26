# Price Action Structure

`price-action-structure` provides deterministic daily-chart context for equities and
crypto. It describes observed market structure; it does not issue trade instructions.

## Input

- Daily open, high, low, and close bars from the `PRICES` provider
- Volume is optional
- Up to 252 recent bars are used

## Method

1. Confirm swing highs and lows with three bars on both sides.
2. Label consecutive swing highs as `HH` or `LH`, and swing lows as `HL` or `LL`.
3. Classify structure as `uptrend`, `downtrend`, `mixed`, or `unavailable`.
4. Cluster confirmed pivot prices within 0.5 ATR(14). Clusters with at least two
   touches become support, resistance, or flip zones.
5. Return at most eight zones and 180 chart bars.

The skill never returns provisional pivots. Results with fewer than 30 valid bars are
partial, and fewer than 15 bars cannot produce the structured presentation.

## Output

The `price-action-structure-v1` presentation contains daily bars, confirmed pivots,
the ATR(14) value, structure classification, and bounded zones. The signal is always
`not_assessed`; consumers should present this as research context rather than advice.
