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
5. Detect completed rejection, inside-bar, and engulfing candle events. Rejection
   candles require a dominant wick of at least 60%, an opposite wick below 15%, a
   body below 30%, and a range of at least 0.4 trailing causal ATR(14). Engulfing candles
   require opposite colours, strict body growth and full prior-body coverage, with
   a range of at least 0.3 causal ATR(14). Inside bars use strict high/low containment.
6. Ignore zero-range candles and return at most eight zones, ten candle events in
   newest-first order, and 180 chart bars.

The skill never returns provisional pivots. Results with fewer than 30 valid bars are
partial, and fewer than 15 bars cannot produce the structured presentation.

## Output

The `price-action-structure-v2` presentation contains daily bars, confirmed pivots,
the ATR(14) value, structure classification, bounded zones, and completed candle
events. Event direction describes candle geometry only; it is not a forecast or an
investment action. The signal is always `not_assessed`.
