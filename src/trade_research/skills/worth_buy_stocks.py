"""Worth-buy-stocks trend-scoring skill — a 4-layer pipeline for US equities.

Integrates the worth-buy-stocks algorithm as a frozen-dataclass ResearchSkill.
Purely computational: reuses the existing PRICES capability for OHLCV data.
Benchmark symbols (SPY, QQQ) are configurable; the algorithm degrades
gracefully when benchmark data is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from trade_research.domain import (
    AnalysisMethod,
    AnalystResult,
    InstrumentId,
    MetricKind,
    Observation,
    ReportStatus,
    SignalKind,
)
from trade_research.domain.provenance import DerivedAlgorithm
from trade_research.providers import CapabilityName, PricePoint, ProviderRegistry
from trade_research.skills.indicators import (
    adx,
    annualized_volatility,
    atr,
    efficiency_ratio,
    ema_series,
    kdj,
    macd_series,
    max_drawdown,
    missing_metric_kinds,
    momentum_12_1,
    obv,
    rsi,
    sanitize_text,
    sma,
    to_weekly,
    up_down_volume_ratio,
    validated_prices,
    weekly_bearish_check,
)
from trade_research.skills.indicators import (
    limitations as _limitations_fn,
)
from trade_research.skills.indicators import (
    summary as _summary_fn,
)

# ---------------------------------------------------------------------------
# Verdict constants
# ---------------------------------------------------------------------------

_VERDICT_BUY = "是"
_VERDICT_WATCH = "观察"
_VERDICT_NO = "否"
_VERDICT_REDUCE_RISK = "持仓需减风险"
_VERDICT_CANNOT_SCORE = "无法评分"

_ENTRY_CLASSES = {
    0: "trend_broken",
    1: "overextended",
    2: "pullback_no_trigger",
    3: "trend_continuation",
    4: "pullback_reversal",
    5: "recovery_reversal",
}


# ---------------------------------------------------------------------------
# Skill class
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorthBuyStocksSkill:
    """Four-layer trend-scoring pipeline producing a trading discipline verdict.

    Layers operate sequentially and later layers can only downgrade — never
    upgrade — the score or verdict produced by earlier layers:

    1. **ALPHA Weighted** — composite of momentum (55%), relative strength
       vs SPY/QQQ (35%), and Kaufman trend efficiency (10%).
    2. **Risk Veto** — MA60/MA200 checks, weekly bearish alignment, max
       drawdown, and market risk-off regime detection.
    3. **Technical Confirmation** — MACD, RSI, KDJ, volume-price action,
       ADX.  Blocks buy signals but never upgrades.
    4. **Entry Timing** — Overheating, pullback depth, reversal signals.
       Classifies the entry setup and suggests price levels.
    """

    benchmark_symbols: tuple[str, str] = ("SPY", "QQQ")

    # -- internal (init=False so they are not part of the constructor) --
    _name: str = field(default="worth-buy-stocks", init=False, repr=False)
    _momentum_weight: float = field(default=0.55, init=False, repr=False)
    _relative_strength_weight: float = field(default=0.35, init=False, repr=False)
    _efficiency_weight: float = field(default=0.10, init=False, repr=False)
    _ma_short: int = field(default=60, init=False, repr=False)
    _ma_long: int = field(default=200, init=False, repr=False)
    _kdj_n: int = field(default=9, init=False, repr=False)
    _kdj_k: int = field(default=3, init=False, repr=False)
    _kdj_d: int = field(default=3, init=False, repr=False)
    _adx_n: int = field(default=14, init=False, repr=False)
    _atr_n: int = field(default=14, init=False, repr=False)
    _min_bars: int = field(default=30, init=False, repr=False)
    _min_bars_full: int = field(default=200, init=False, repr=False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        return (CapabilityName.PRICES,)

    def __getattribute__(self, attribute: str) -> object:
        if attribute == "name":
            return object.__getattribute__(self, "_name")
        return object.__getattribute__(self, attribute)

    # ------------------------------------------------------------------
    # analyze()
    # ------------------------------------------------------------------

    def analyze(
        self, instrument: InstrumentId, providers: ProviderRegistry
    ) -> AnalystResult:
        missing: list[str] = []
        all_factors: list[Observation] = []

        # -- 1. Fetch target prices -----------------------------------
        target_raw = providers.prices(instrument)
        target_prices, discarded, incomplete = validated_prices(target_raw)
        if discarded:
            missing.append("discarded invalid or duplicate OHLCV")
        if incomplete:
            missing.append("complete OHLCV fields for target")

        if len(target_prices) < self._min_bars:
            missing.append("insufficient price history for scoring")
            return _empty_result(instrument, self._name, missing)

        closes = [p.close for p in target_prices]
        highs = [cast(float, p.high) for p in target_prices]
        lows = [cast(float, p.low) for p in target_prices]
        vols = [cast(float, p.volume) for p in target_prices]

        # -- 2. Fetch benchmark prices ---------------------------------
        benchmark_data: dict[str, tuple[PricePoint, ...]] = {}
        for bm_sym in self.benchmark_symbols:
            try:
                bm_inst = InstrumentId(symbol=bm_sym, market="US")
                bm_raw = providers.prices(bm_inst)
                bm_clean, _, _ = validated_prices(bm_raw)
                if len(bm_clean) >= self._min_bars:
                    benchmark_data[bm_sym] = bm_clean
            except Exception:
                pass

        has_benchmarks = len(benchmark_data) > 0
        if not has_benchmarks:
            missing.append("benchmark data unavailable — relative strength skipped")

        # -- 3. Layer 1: ALPHA Weighted score -------------------------
        alpha_factors, alpha_composite = _layer1_alpha_weighted(
            instrument, closes, highs, lows, vols,
            target_prices, benchmark_data,
            self._momentum_weight, self._relative_strength_weight,
            self._efficiency_weight, has_benchmarks,
        )
        all_factors.extend(alpha_factors)

        # -- 4. Layer 2: Risk Veto ------------------------------------
        risk_factors, risk_score = _layer2_risk_veto(
            instrument, target_prices, closes, highs, lows, vols,
            self._ma_short, self._ma_long, self._atr_n,
        )
        all_factors.extend(risk_factors)

        # -- 5. Layer 3: Technical Confirmation -----------------------
        tech_factors, tech_blocks = _layer3_technical_confirmation(
            instrument, target_prices, closes, highs, lows, vols,
            self._kdj_n, self._kdj_k, self._kdj_d, self._adx_n, self._atr_n,
        )
        all_factors.extend(tech_factors)

        # -- 6. Layer 4: Entry Timing ----------------------------------
        entry_factors = _layer4_entry_timing(
            instrument, target_prices, closes, highs, lows, vols,
            self._atr_n, self._ma_short,
        )
        all_factors.extend(entry_factors)

        # -- 7. Determine verdict --------------------------------------
        verdict, verdict_reason = _determine_verdict(
            alpha_composite, risk_score, tech_blocks, len(target_prices),
            self._min_bars_full,
        )

        # -- 8. Build AnalystResult ------------------------------------
        observed_at = max(p.observed_at for p in target_prices)
        # Add verdict as an observation
        verdict_codes = {_VERDICT_BUY: 5, _VERDICT_WATCH: 3, _VERDICT_NO: 1,
                         _VERDICT_REDUCE_RISK: 2, _VERDICT_CANNOT_SCORE: 0}
        verdict_factor = Observation(
            instrument=instrument,
            metric=MetricKind.WORTH_BUY_VERDICT,
            value=float(verdict_codes.get(verdict, 0)),
            source="derived",
            observed_at=observed_at,
            provenance={
                "algorithm": DerivedAlgorithm.WORTH_BUY_ALPHA_WEIGHTED.value,
                "window": "252_observations",
            },
        )
        all_factors.append(verdict_factor)

        methods = (
            AnalysisMethod(
                algorithm=DerivedAlgorithm.WORTH_BUY_ALPHA_WEIGHTED,
                window="252_observations",
            ),
            AnalysisMethod(
                algorithm=DerivedAlgorithm.WORTH_BUY_RISK_VETO,
                window="252_observations",
            ),
            AnalysisMethod(
                algorithm=DerivedAlgorithm.WORTH_BUY_TECHNICAL_CONFIRMATION,
                window="252_observations",
            ),
            AnalysisMethod(
                algorithm=DerivedAlgorithm.WORTH_BUY_ENTRY_TIMING,
                window="60_observations",
            ),
        )

        signal_map = {
            _VERDICT_BUY: SignalKind.BULLISH,
            _VERDICT_WATCH: SignalKind.NEUTRAL,
            _VERDICT_NO: SignalKind.BEARISH,
            _VERDICT_REDUCE_RISK: SignalKind.BEARISH,
            _VERDICT_CANNOT_SCORE: SignalKind.NOT_ASSESSED,
        }
        signal = signal_map.get(verdict)

        # Build summary text
        summary_text = sanitize_text(
            f"Verdict: {verdict} | Composite: {alpha_composite:.0f}/100 "
            f"| Risk: {risk_score:.0f}/100 | Reason: {verdict_reason}"
        )

        return AnalystResult(
            analyst=self._name,
            instrument=instrument,
            summary=summary_text,
            status=ReportStatus.PARTIAL if missing else ReportStatus.COMPLETE,
            missing_metrics=missing_metric_kinds(missing) if missing else (),
            limitations=_limitations_fn(missing) if missing else (),
            methods=methods,
            signal=signal,
            observations=tuple(all_factors),
        )


# ---------------------------------------------------------------------------
# Layer 1: ALPHA Weighted composite score
# ---------------------------------------------------------------------------


def _layer1_alpha_weighted(
    instrument: InstrumentId,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    vols: list[float],
    prices: tuple[PricePoint, ...],
    benchmarks: dict[str, tuple[PricePoint, ...]],
    momentum_weight: float,
    rs_weight: float,
    efficiency_weight: float,
    has_benchmarks: bool,
) -> tuple[list[Observation], float]:
    """Compute the ALPHA-weighted composite score (0-100)."""
    factors: list[Observation] = []
    observed_at = max(p.observed_at for p in prices)
    score_parts: dict[str, float] = {}

    # --- Momentum (55%) ---
    mom = momentum_12_1(closes)
    if mom is not None:
        mom_score = _normalize_momentum(mom)
        score_parts["momentum"] = mom_score * momentum_weight
        factors.append(_build_scoring_obs(
            instrument, MetricKind.WORTH_BUY_MOMENTUM_SCORE, mom_score,
            observed_at, DerivedAlgorithm.WORTH_BUY_ALPHA_WEIGHTED, "momentum_12_1",
        ))
    else:
        score_parts["momentum"] = 0.0

    # --- Relative strength vs benchmarks (35%) ---
    if has_benchmarks:
        rs_scores: list[float] = []
        for bm_sym, bm_prices in benchmarks.items():
            bm_closes = [p.close for p in bm_prices]
            rs = _relative_strength_score(closes, bm_closes)
            if rs is not None:
                rs_scores.append(rs)
                factors.append(_build_scoring_obs(
                    instrument, MetricKind.WORTH_BUY_RELATIVE_STRENGTH, rs,
                    observed_at, DerivedAlgorithm.RELATIVE_STRENGTH,
                    f"vs_{bm_sym.lower()}",
                ))
        if rs_scores:
            score_parts["relative_strength"] = statistics_mean(rs_scores) * rs_weight
        else:
            score_parts["relative_strength"] = 0.0
    else:
        # Without benchmarks, redistribute weight to momentum + efficiency
        redistribution = momentum_weight + efficiency_weight
        if redistribution > 0:
            momentum_weight = momentum_weight / redistribution
            efficiency_weight = efficiency_weight / redistribution
        score_parts["relative_strength"] = 0.0

    # --- Kaufman efficiency (10%) ---
    er = efficiency_ratio(closes, 30)
    if er is not None:
        score_parts["efficiency"] = er * 100 * efficiency_weight
        factors.append(_build_scoring_obs(
            instrument, MetricKind.WORTH_BUY_EFFICIENCY_SCORE, er * 100,
            observed_at, DerivedAlgorithm.KAUFMAN_EFFICIENCY, "30_day",
        ))
    else:
        score_parts["efficiency"] = 0.0

    # Composite
    composite = round(sum(score_parts.values()), 2)
    factors.append(_build_scoring_obs(
        instrument, MetricKind.WORTH_BUY_COMPOSITE, composite,
        observed_at, DerivedAlgorithm.WORTH_BUY_ALPHA_WEIGHTED,
        "composite_0_100",
    ))

    return factors, composite


def _normalize_momentum(mom: float) -> float:
    """Normalize a momentum return to a 0-100 score.

    Uses a sigmoid-like scaling: returns above +50% map near 100,
    returns below -50% map near 0.
    """
    # Clamp to [-0.5, 0.5] then scale to [0, 100]
    clamped = max(-0.5, min(0.5, mom))
    return (clamped + 0.5) * 100


def _relative_strength_score(
    target_closes: list[float], bench_closes: list[float]
) -> float | None:
    """Score relative strength over 1m, 3m, 6m windows. Returns 0-100 or None."""
    windows = {"1m": 21, "3m": 63, "6m": 126}
    diffs: list[float] = []
    for _name, window in windows.items():
        if len(target_closes) < window + 1 or len(bench_closes) < window + 1:
            continue
        t_ret = target_closes[-1] / target_closes[-window - 1] - 1
        b_ret = bench_closes[-1] / bench_closes[-window - 1] - 1
        diffs.append(t_ret - b_ret)
    if not diffs:
        return None
    avg_diff = statistics_mean(diffs)
    # Normalize: difference of +0.20 → 100, -0.20 → 0
    return max(0.0, min(100.0, (avg_diff + 0.20) / 0.40 * 100))


# ---------------------------------------------------------------------------
# Layer 2: Risk Veto
# ---------------------------------------------------------------------------


def _layer2_risk_veto(
    instrument: InstrumentId,
    prices: tuple[PricePoint, ...],
    closes: list[float],
    highs: list[float],
    lows: list[float],
    vols: list[float],
    ma_short: int,
    ma_long: int,
    atr_period: int,
) -> tuple[list[Observation], float]:
    """Compute risk score (0-100, higher = riskier)."""
    factors: list[Observation] = []
    observed_at = max(p.observed_at for p in prices)
    risk_flags: list[str] = []
    risk_score = 0.0

    # MA alignment
    ma_short_val = sma(closes, ma_short)
    ma_long_val = sma(closes, ma_long)

    if ma_short_val is not None and ma_long_val is not None:
        factors.append(_build_scoring_obs(
            instrument, MetricKind.SIMPLE_MOVING_AVERAGE, ma_short_val,
            observed_at, DerivedAlgorithm.SIMPLE_MOVING_AVERAGE, f"{ma_short}_observations",
        ))
        if closes[-1] < ma_short_val < ma_long_val:
            risk_flags.append("bearish_ma_alignment")
            risk_score += 30
        elif closes[-1] < ma_short_val:
            risk_flags.append("below_ma_short")
            risk_score += 15
    else:
        risk_flags.append("ma_unavailable")

    # Max drawdown
    dd = max_drawdown(closes, 252)
    if dd is not None:
        factors.append(_build_scoring_obs(
            instrument, MetricKind.MAX_DRAWDOWN, dd * 100,
            observed_at, DerivedAlgorithm.EFFICIENCY_RATIO_CALC, "252_day",
        ))
        if dd < -0.20:
            risk_flags.append("severe_drawdown")
            risk_score += 25
        elif dd < -0.10:
            risk_flags.append("moderate_drawdown")
            risk_score += 10

    # Weekly structure
    weekly = to_weekly(prices)
    if weekly:
        w_closes = [cast(float, w["close"]) for w in weekly]
        bearish_check = weekly_bearish_check(w_closes)
        if bearish_check.get("bearish") is True:
            factors.append(_build_scoring_obs(
                instrument, MetricKind.WEEKLY_BEARISH_ALIGNMENT, 1.0,
                observed_at, DerivedAlgorithm.WORTH_BUY_RISK_VETO,
                "weekly_ma_bearish",
            ))
            risk_flags.append("weekly_bearish")
            risk_score += 20
        else:
            factors.append(_build_scoring_obs(
                instrument, MetricKind.WEEKLY_BEARISH_ALIGNMENT, 0.0,
                observed_at, DerivedAlgorithm.WORTH_BUY_RISK_VETO,
                "weekly_ma_ok",
            ))

    # Annualized volatility
    vol = annualized_volatility(closes, 63)
    if vol is not None and vol > 0.50:
        risk_flags.append("high_volatility")
        risk_score += 10

    factors.append(_build_scoring_obs(
        instrument, MetricKind.WORTH_BUY_RISK_VETO, min(100.0, risk_score),
        observed_at, DerivedAlgorithm.WORTH_BUY_RISK_VETO,
        f"flags={','.join(risk_flags) if risk_flags else 'none'}",
    ))

    return factors, min(100.0, risk_score)


# ---------------------------------------------------------------------------
# Layer 3: Technical Confirmation
# ---------------------------------------------------------------------------


def _layer3_technical_confirmation(
    instrument: InstrumentId,
    prices: tuple[PricePoint, ...],
    closes: list[float],
    highs: list[float],
    lows: list[float],
    vols: list[float],
    kdj_n: int,
    kdj_k: int,
    kdj_d: int,
    adx_n: int,
    atr_n: int,
) -> tuple[list[Observation], list[str]]:
    """Compute technical confirmation indicators. Returns factors + blocking reasons."""
    factors: list[Observation] = []
    observed_at = max(p.observed_at for p in prices)
    blocks: list[str] = []

    # MACD
    if len(closes) >= 26 + 9:
        macd_vals = macd_series(closes, 12, 26)
        if macd_vals:
            signal_vals = ema_series(macd_vals, 9)
            if signal_vals:
                macd_now = macd_vals[-1]
                signal_now = signal_vals[-1]
                if macd_now < signal_now:
                    blocks.append("macd_bearish")
                # MACD histogram declining
                if len(macd_vals) >= 3 and len(signal_vals) >= 3:
                    hist_now = macd_vals[-1] - signal_vals[-1]
                    hist_prev = macd_vals[-3] - signal_vals[-3]
                    if hist_now < hist_prev:
                        blocks.append("macd_histogram_declining")

    # RSI(14)
    if len(closes) >= 15:
        rsi14 = rsi(closes, 14)
        factors.append(_build_scoring_obs(
            instrument, MetricKind.RELATIVE_STRENGTH_INDEX_14, rsi14,
            observed_at, DerivedAlgorithm.WILDER_RSI, "14_observations",
        ))
        if rsi14 > 70:
            blocks.append("rsi_overbought")
        elif rsi14 < 30:
            blocks.append("rsi_oversold")

    # KDJ
    kdj_result = kdj(highs, lows, closes, kdj_n, kdj_k, kdj_d)
    if kdj_result["K"] is not None:
        k_val = kdj_result["K"]
        d_val = kdj_result["D"]
        j_val = kdj_result["J"]
        if k_val is not None:
            factors.append(_build_scoring_obs(
                instrument, MetricKind.KDJ_K, k_val,
                observed_at, DerivedAlgorithm.KDJ_CALCULATION, f"{kdj_n}_{kdj_k}_{kdj_d}",
            ))
        if d_val is not None:
            factors.append(_build_scoring_obs(
                instrument, MetricKind.KDJ_D, d_val,
                observed_at, DerivedAlgorithm.KDJ_CALCULATION, f"{kdj_n}_{kdj_k}_{kdj_d}",
            ))
        if j_val is not None:
            factors.append(_build_scoring_obs(
                instrument, MetricKind.KDJ_J, j_val,
                observed_at, DerivedAlgorithm.KDJ_CALCULATION, f"{kdj_n}_{kdj_k}_{kdj_d}",
            ))
            if j_val > 100:
                blocks.append("kdj_overbought")
        if k_val is not None and d_val is not None and k_val < d_val:
            blocks.append("kdj_bearish_cross")

    # ADX
    adx_result = adx(highs, lows, closes, adx_n)
    adx_val = adx_result["ADX"]
    if adx_val is not None:
        factors.append(_build_scoring_obs(
            instrument, MetricKind.ADX_14, adx_val,
            observed_at, DerivedAlgorithm.ADX_CALCULATION, f"{adx_n}_observations",
        ))
        if adx_val < 20:
            blocks.append("adx_ranging")
        # Check DI direction
        plus_di = adx_result["plus_DI"]
        minus_di = adx_result["minus_DI"]
        if plus_di is not None and minus_di is not None and minus_di > plus_di:
            blocks.append("di_bearish")

    # Volume: up/down ratio
    ud_ratio = up_down_volume_ratio(closes, vols, 10)
    if ud_ratio is not None:
        factors.append(_build_scoring_obs(
            instrument, MetricKind.UP_DOWN_VOLUME_RATIO, ud_ratio,
            observed_at, DerivedAlgorithm.EFFICIENCY_RATIO_CALC, "10_day",
        ))
        if ud_ratio < 0.7:
            blocks.append("distribution_volume")

    # Volume: OBV trend
    if len(closes) >= 30:
        obv_series = obv(closes, vols)
        if len(obv_series) >= 30:
            # OBV declining over last 30 days
            obv_recent = obv_series[-30:]
            if obv_recent[-1] < obv_recent[0]:
                blocks.append("obv_declining")

    # Efficiency ratio
    er = efficiency_ratio(closes, 30)
    if er is not None:
        factors.append(_build_scoring_obs(
            instrument, MetricKind.EFFICIENCY_RATIO, er,
            observed_at, DerivedAlgorithm.EFFICIENCY_RATIO_CALC, "30_day",
        ))

    return factors, blocks


# ---------------------------------------------------------------------------
# Layer 4: Entry Timing
# ---------------------------------------------------------------------------


def _layer4_entry_timing(
    instrument: InstrumentId,
    prices: tuple[PricePoint, ...],
    closes: list[float],
    highs: list[float],
    lows: list[float],
    vols: list[float],
    atr_period: int,
    ma_short: int,
) -> list[Observation]:
    """Classify entry timing and compute price levels."""
    factors: list[Observation] = []
    observed_at = max(p.observed_at for p in prices)
    last_close = closes[-1]

    # Compute ATR for price levels
    ohlcv_prices = [p for p in prices
                    if all(v is not None for v in (p.open, p.high, p.low, p.volume))]
    atr_val = atr(tuple(ohlcv_prices), atr_period) if len(ohlcv_prices) >= atr_period else None

    # Entry price: at-market for now
    entry_price = last_close
    factors.append(_build_scoring_obs(
        instrument, MetricKind.WORTH_BUY_ENTRY_PRICE, entry_price,
        observed_at, DerivedAlgorithm.WORTH_BUY_ENTRY_TIMING, "current_close",
    ))

    # Stop price: MA60 or recent swing low, whichever is lower
    ma_short_val = sma(closes, ma_short)
    swing_low = min(lows[-20:]) if len(lows) >= 20 else min(lows)
    stop_price = min(
        ma_short_val if ma_short_val is not None else swing_low,
        swing_low,
    )
    if atr_val is not None:
        stop_price -= atr_val  # Add ATR buffer below stop
    factors.append(_build_scoring_obs(
        instrument, MetricKind.WORTH_BUY_STOP_PRICE, round(stop_price, 2),
        observed_at, DerivedAlgorithm.WORTH_BUY_ENTRY_TIMING,
        f"below_ma{ma_short}_or_swing_low",
    ))

    # Target price: recent 52-week high
    recent_high = max(highs[-252:]) if len(highs) >= 252 else max(highs)
    factors.append(_build_scoring_obs(
        instrument, MetricKind.WORTH_BUY_TARGET_PRICE, recent_high,
        observed_at, DerivedAlgorithm.WORTH_BUY_ENTRY_TIMING, "52_week_high",
    ))

    # Entry classification
    entry_class = _classify_entry(closes, highs, lows, vols, atr_val, ma_short)
    factors.append(_build_scoring_obs(
        instrument, MetricKind.WORTH_BUY_ENTRY_CLASSIFICATION, float(entry_class),
        observed_at, DerivedAlgorithm.WORTH_BUY_ENTRY_TIMING,
        _ENTRY_CLASSES.get(entry_class, "unknown"),
    ))

    return factors


def _classify_entry(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    vols: list[float],
    atr_val: float | None,
    ma_short: int,
) -> int:
    """Classify entry setup type. Returns integer code 0-5."""
    ma_short_val = sma(closes, ma_short)
    ma_long_val = sma(closes, 200)
    last_close = closes[-1]

    # Check for trend broken
    if (ma_short_val is not None and ma_long_val is not None
            and last_close < ma_short_val < ma_long_val):
        # Check for recovery reversal
        high_30d = max(highs[-30:]) if len(highs) >= 30 else max(highs)
        depth = (last_close - high_30d) / high_30d
        rsi6 = rsi(closes, 6) if len(closes) >= 7 else 50.0
        if depth < -0.12 and rsi6 > 50:
            return 5  # recovery_reversal
        return 0  # trend_broken

    # Overheating check
    ma20 = sma(closes, 20)
    if atr_val is not None and ma20 is not None:
        rsi14 = rsi(closes, 14) if len(closes) >= 15 else 50.0
        ext_atr = (last_close - ma20) / atr_val
        if (ext_atr > 3.0 or rsi14 > 80):
            return 1  # overextended
        if ext_atr > 2.2 or rsi14 > 75:
            return 1  # overextended (soft threshold)

    # Pullback detection
    high_30d = max(highs[-30:]) if len(highs) >= 30 else max(highs)
    low_10d = min(lows[-10:]) if len(lows) >= 10 else min(lows)
    depth = (last_close - high_30d) / high_30d if high_30d > 0 else 0

    is_pullback = depth < -0.04 and low_10d < ma20 if ma20 is not None else False

    if is_pullback:
        # Check for reversal signals
        reversal_signals = _count_reversal_signals(closes, highs, lows, vols, ma_short)
        if reversal_signals >= 2:
            return 4  # pullback_reversal
        return 2  # pullback_no_trigger

    # Trend continuation
    if ma20 is not None and last_close > ma20:
        macd_vals = macd_series(closes, 12, 26) if len(closes) >= 27 else []
        signal_vals = ema_series(macd_vals, 9) if macd_vals else []
        adx_val = adx(highs, lows, closes, 14)["ADX"]
        if (signal_vals and macd_vals[-1] > signal_vals[-1]
                and adx_val is not None and adx_val > 25):
            return 3  # trend_continuation

    return 2  # default to pullback_no_trigger


def _count_reversal_signals(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    vols: list[float],
    ma_short: int,
) -> int:
    """Count bullish reversal confirmation signals (0-6)."""
    signals = 0
    last_close = closes[-1]
    last_vol = vols[-1] if vols else 0
    prev_close = closes[-2] if len(closes) >= 2 else last_close

    # 1. Bounce day: closes in upper half of range
    day_range = highs[-1] - lows[-1] if len(highs) >= 1 and len(lows) >= 1 else 0
    if day_range > 0 and last_close > (highs[-1] + lows[-1]) / 2:
        signals += 1

    # 2. Volume expansion vs 20-day average
    if len(vols) >= 21:
        avg_vol_20 = statistics_mean(vols[-21:-1])
        if avg_vol_20 > 0 and last_vol > avg_vol_20 * 1.5:
            signals += 1

    # 3. Reclaim MA10
    if len(closes) >= 11:
        ma10 = sma(closes, 10)
        ma10_prev = sma(closes[:-1], 10)
        if (ma10 is not None and ma10_prev is not None
                and last_close > ma10 and prev_close <= ma10_prev):
            signals += 1

    # 4. Reclaim MA20
    if len(closes) >= 21:
        ma20 = sma(closes, 20)
        ma20_prev = sma(closes[:-1], 20)
        if (ma20 is not None and ma20_prev is not None
                and last_close > ma20 and prev_close <= ma20_prev):
            signals += 1

    # 5. KDJ golden cross within last 3 days
    kdj_result = kdj(highs, lows, closes, 9, 3, 3)
    k_val2 = kdj_result["K"]
    d_val2 = kdj_result["D"]
    if k_val2 is not None and d_val2 is not None and k_val2 > d_val2:
        signals += 1

    # 6. MACD histogram rising
    if len(closes) >= 27:
        macd_vals = macd_series(closes, 12, 26)
        signal_vals = ema_series(macd_vals, 9)
        if len(macd_vals) >= 3 and len(signal_vals) >= 3:
            hist_now = macd_vals[-1] - signal_vals[-1]
            hist_prev = macd_vals[-3] - signal_vals[-3]
            if hist_now > hist_prev:
                signals += 1

    return signals


# ---------------------------------------------------------------------------
# Verdict determination
# ---------------------------------------------------------------------------


def _determine_verdict(
    composite: float,
    risk_score: float,
    tech_blocks: list[str],
    n_bars: int,
    min_bars_full: int,
) -> tuple[str, str]:
    """Map numeric scores + technical blocks to a trading discipline verdict."""
    if n_bars < 30:
        return _VERDICT_CANNOT_SCORE, "insufficient data"

    # Critical blocks = cannot buy regardless of score
    critical_blocks = {"rsi_overbought", "kdj_overbought"}
    has_critical = bool(set(tech_blocks) & critical_blocks)
    block_count = len(tech_blocks)

    if composite >= 70 and risk_score < 30 and not has_critical and block_count <= 1:
        return _VERDICT_BUY, "strong composite, low risk, no critical blocks"
    if composite >= 50 and risk_score < 50 and not has_critical and block_count <= 2:
        return _VERDICT_WATCH, "moderate composite, manageable risk, monitor"
    if risk_score >= 50 or block_count >= 3:
        return _VERDICT_NO, f"high risk ({risk_score:.0f}) or many blocks ({block_count})"
    if composite >= 40 and risk_score < 40:
        return _VERDICT_WATCH, "borderline composite, low risk"
    if risk_score >= 40:
        return _VERDICT_REDUCE_RISK, f"elevated risk ({risk_score:.0f}), reduce if held"

    return _VERDICT_NO, f"low composite ({composite:.0f}), high risk ({risk_score:.0f})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_scoring_obs(
    instrument: InstrumentId,
    metric: MetricKind,
    value: float,
    observed_at: datetime,
    algorithm: DerivedAlgorithm,
    window: str,
) -> Observation:
    """Build a single derived Observation for scoring outputs."""
    return Observation(
        instrument=instrument,
        metric=metric,
        value=round(value, 10),
        source="derived",
        observed_at=observed_at,
        provenance={
            "algorithm": algorithm.value,
            "window": window,
        },
    )


def _empty_result(
    instrument: InstrumentId, name: str, missing: list[str]
) -> AnalystResult:
    return AnalystResult(
        analyst=name,
        instrument=instrument,
        summary=_summary_fn(missing),
        status=ReportStatus.PARTIAL,
        missing_metrics=missing_metric_kinds(missing),
        limitations=_limitations_fn(missing),
        observations=(),
    )


def statistics_mean(values: list[float]) -> float:
    """Safe mean that avoids importing statistics at module level for frozen dataclass."""
    import statistics
    return statistics.fmean(values)
