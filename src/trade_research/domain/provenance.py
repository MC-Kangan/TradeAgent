"""Closed, typed provenance schema shared by reports and persistence."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime
from enum import StrEnum
from itertools import islice
from typing import Any, Final, cast

from pydantic import JsonValue


class ProviderKind(StrEnum):
    """Provider identities permitted to cross report and persistence boundaries."""

    LOCAL_CSV = "local_csv"
    LOCAL_PARQUET = "local_parquet"
    LOCAL_SQL = "local_sql"
    YAHOO = "yahoo"
    STOOQ = "stooq"
    SEC = "sec"
    CCXT = "ccxt"
    BLOOMBERG = "bloomberg"
    INTERNAL = "internal"
    FIXTURE = "fixture"
    DERIVED = "derived"
    UNTRUSTED = "untrusted"


class MetricKind(StrEnum):
    """Raw and derived v1 metrics permitted in observations and provenance."""

    OPEN = "open"
    HIGH = "high"
    LOW = "low"
    CLOSE = "close"
    VOLUME = "volume"
    OHLCV = "ohlcv"
    REVENUE = "revenue"
    NET_INCOME = "net_income"
    EARNINGS = "earnings"
    OPERATING_INCOME = "operating_income"
    SHAREHOLDERS_EQUITY = "shareholders_equity"
    TOTAL_EQUITY = "total_equity"
    FREE_CASH_FLOW = "free_cash_flow"
    TOTAL_DEBT = "total_debt"
    MARKET_CAP = "market_cap"
    ENTERPRISE_VALUE = "enterprise_value"
    EBITDA = "ebitda"
    GROSS_PROFIT = "gross_profit"
    REVENUE_GROWTH = "revenue_growth"
    EARNINGS_GROWTH = "earnings_growth"
    OPERATING_MARGIN = "operating_margin"
    NET_MARGIN = "net_margin"
    RETURN_ON_EQUITY = "return_on_equity"
    FREE_CASH_FLOW_MARGIN = "free_cash_flow_margin"
    LEVERAGE = "leverage"
    PRICE_TO_EARNINGS = "price_to_earnings"
    ENTERPRISE_VALUE_TO_EBITDA = "enterprise_value_to_ebitda"
    FREE_CASH_FLOW_YIELD = "free_cash_flow_yield"
    PRICE_RETURN = "price_return"
    SIMPLE_MOVING_AVERAGE = "simple_moving_average"
    SIMPLE_MOVING_AVERAGE_20 = "simple_moving_average_20"
    EXPONENTIAL_MOVING_AVERAGE_20 = "exponential_moving_average_20"
    RELATIVE_STRENGTH_INDEX_14 = "relative_strength_index_14"
    MACD_12_26 = "macd_12_26"
    MACD_SIGNAL_9 = "macd_signal_9"
    MACD_HISTOGRAM = "macd_histogram"
    BOLLINGER_MIDDLE_20 = "bollinger_middle_20"
    BOLLINGER_UPPER_20_2 = "bollinger_upper_20_2"
    BOLLINGER_LOWER_20_2 = "bollinger_lower_20_2"
    AVERAGE_TRUE_RANGE_14 = "average_true_range_14"
    MOMENTUM_10 = "momentum_10"
    ANNUALIZED_VOLATILITY_20 = "annualized_volatility_20"
    VOLUME_TREND_20 = "volume_trend_20"
    RECENT_FILING_COUNT = "recent_filing_count"
    MATERIAL_EVENT_COUNT = "material_event_count"
    ANNUAL_REPORT_AGE_DAYS = "annual_report_age_days"
    QUARTERLY_REPORT_AGE_DAYS = "quarterly_report_age_days"
    # Worth-buy-stocks composite scoring
    WORTH_BUY_COMPOSITE = "worth_buy_composite"
    WORTH_BUY_VERDICT = "worth_buy_verdict"
    WORTH_BUY_MOMENTUM_SCORE = "worth_buy_momentum_score"
    WORTH_BUY_RELATIVE_STRENGTH = "worth_buy_relative_strength"
    WORTH_BUY_EFFICIENCY_SCORE = "worth_buy_efficiency_score"
    WORTH_BUY_RISK_VETO = "worth_buy_risk_veto"
    WORTH_BUY_ENTRY_CLASSIFICATION = "worth_buy_entry_classification"
    WORTH_BUY_ENTRY_PRICE = "worth_buy_entry_price"
    WORTH_BUY_STOP_PRICE = "worth_buy_stop_price"
    WORTH_BUY_TARGET_PRICE = "worth_buy_target_price"
    # Worth-buy-stocks supporting indicators
    KDJ_K = "kdj_k"
    KDJ_D = "kdj_d"
    KDJ_J = "kdj_j"
    ADX_14 = "adx_14"
    EFFICIENCY_RATIO = "efficiency_ratio"
    UP_DOWN_VOLUME_RATIO = "up_down_volume_ratio"
    MAX_DRAWDOWN = "max_drawdown"
    WEEKLY_BEARISH_ALIGNMENT = "weekly_bearish_alignment"


class VendorField(StrEnum):
    """Known v1 source fields; arbitrary vendor labels are never propagated."""

    OHLCV = "OHLCV"
    OPEN = "OPEN"
    HIGH = "HIGH"
    LOW = "LOW"
    CLOSE = "CLOSE"
    VOLUME = "VOLUME"
    PX_LAST = "PX_LAST"
    REVENUE = "REVENUE"
    REVENUE_CURRENT = "REVENUE_CURRENT"
    REVENUE_PRIOR = "REVENUE_PRIOR"
    NET_INCOME = "NET_INCOME"
    EARNINGS = "EARNINGS"
    OPERATING_INCOME = "OPERATING_INCOME"
    SHAREHOLDERS_EQUITY = "SHAREHOLDERS_EQUITY"
    TOTAL_EQUITY = "TOTAL_EQUITY"
    FREE_CASH_FLOW = "FREE_CASH_FLOW"
    TOTAL_DEBT = "TOTAL_DEBT"
    MARKET_CAP = "MARKET_CAP"
    ENTERPRISE_VALUE = "ENTERPRISE_VALUE"
    EBITDA = "EBITDA"
    GROSS_PROFIT = "GROSS_PROFIT"


class PeriodType(StrEnum):
    ANNUAL = "annual"
    QUARTERLY = "quarterly"
    TTM = "ttm"


class PeriodRole(StrEnum):
    CURRENT = "current"
    PRIOR = "prior"


class DerivedAlgorithm(StrEnum):
    DIRECT_VALUE = "direct_value"
    PERIOD_GROWTH = "period_growth"
    RATIO = "ratio"
    FULL_HISTORY_RETURN = "full_history_return"
    SIMPLE_MOVING_AVERAGE = "simple_moving_average"
    EXPONENTIAL_MOVING_AVERAGE = "exponential_moving_average"
    WILDER_RSI = "wilder_rsi"
    MACD = "macd"
    MACD_SIGNAL = "macd_signal"
    MACD_HISTOGRAM = "macd_histogram"
    BOLLINGER_BAND = "bollinger_band"
    WILDER_ATR = "wilder_atr"
    MOMENTUM = "momentum"
    ANNUALIZED_VOLATILITY = "annualized_volatility"
    VOLUME_TREND = "volume_trend"
    FILING_COUNT = "filing_count"
    FILING_AGE = "filing_age"
    # Worth-buy-stocks scoring layers
    WORTH_BUY_ALPHA_WEIGHTED = "worth_buy_alpha_weighted"
    WORTH_BUY_RISK_VETO = "worth_buy_risk_veto"
    WORTH_BUY_TECHNICAL_CONFIRMATION = "worth_buy_technical_confirmation"
    WORTH_BUY_ENTRY_TIMING = "worth_buy_entry_timing"
    # Additional indicator algorithms
    KAUFMAN_EFFICIENCY = "kaufman_efficiency"
    RELATIVE_STRENGTH = "relative_strength"
    KDJ_CALCULATION = "kdj_calculation"
    ADX_CALCULATION = "adx_calculation"
    EFFICIENCY_RATIO_CALC = "efficiency_ratio"


_REFERENCE_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_CURRENCY_PATTERN = re.compile(r"[A-Z]{3}")
_REFERENCE_KEYS = frozenset(
    {"reference", "period_ref", "prior_period_ref", "snapshot_ref", "series_ref"}
)
_TIMESTAMP_KEYS = frozenset(
    {"observed_at", "timestamp", "valuation_as_of", "start_at", "end_at"}
)
_CLOSED_SCALAR_KEYS = frozenset(
    {
        "provider_kind",
        "input_provider_kind",
        "metric",
        "vendor_field",
        "period_type",
        "period_role",
        "currency",
        "period_end",
        *_TIMESTAMP_KEYS,
        *_REFERENCE_KEYS,
        "algorithm",
        "window",
        "point_count",
    }
)
_REFERENCE_FIELDS = frozenset({"provider_kind", "vendor_field", "reference"})
_OHLCV_FIELDS = frozenset({"close", "high", "low", "open", "volume"})
MAX_PROVENANCE_DEPTH: Final = 3
MAX_PROVENANCE_ITEMS: Final = 64
MAX_PROVENANCE_INTEGER: Final = 10**308 - 1


def normalize_provider_kind(value: object) -> ProviderKind:
    """Normalize a known provider kind or reject a free-form source label."""

    if isinstance(value, ProviderKind):
        return value
    if not isinstance(value, str):
        raise ValueError("source must be a known provider kind")
    try:
        return ProviderKind(value.strip().lower())
    except ValueError as error:
        raise ValueError("source must be a known provider kind") from error


def normalize_metric_kind(value: object) -> MetricKind:
    """Normalize a known v1 metric or reject a free-form metric label."""

    if isinstance(value, MetricKind):
        return value
    if not isinstance(value, str):
        raise ValueError("metric must be a known v1 metric")
    try:
        return MetricKind(value.strip().lower())
    except ValueError as error:
        raise ValueError("metric must be a known v1 metric") from error


def sanitize_provenance(provenance: Mapping[str, object]) -> dict[str, JsonValue]:
    """Retain only values accepted by the closed typed provenance schema."""

    return _sanitize_provenance(provenance, depth=0)


def _sanitize_provenance(provenance: Mapping[Any, object], *, depth: int) -> dict[str, JsonValue]:
    sanitized: dict[str, JsonValue] = {}
    for key, value in islice(provenance.items(), MAX_PROVENANCE_ITEMS):
        if not isinstance(key, str):
            continue
        if key in _CLOSED_SCALAR_KEYS:
            validated = _sanitize_scalar(key, value)
            if validated is not None:
                sanitized[key] = validated
        elif key == "inputs" and depth < MAX_PROVENANCE_DEPTH and isinstance(value, list | tuple):
            inputs = [
                item
                for item in (
                    _sanitize_input(candidate, depth=depth + 1)
                    for candidate in islice(value, MAX_PROVENANCE_ITEMS)
                )
                if item is not None
            ]
            if inputs:
                sanitized["inputs"] = cast(JsonValue, inputs)
    return sanitized


def sanitize_provider_reference(provenance: Mapping[str, object]) -> dict[str, JsonValue]:
    """Retain only provider kind, known vendor field, and opaque content hash."""

    return _sanitize_provider_reference(provenance)


def _sanitize_provider_reference(provenance: Mapping[Any, object]) -> dict[str, JsonValue]:
    sanitized: dict[str, JsonValue] = {}
    for key, value in islice(provenance.items(), MAX_PROVENANCE_ITEMS):
        if not isinstance(key, str) or key not in _REFERENCE_FIELDS:
            continue
        validated = _sanitize_scalar(key, value)
        if validated is not None:
            sanitized[key] = validated
    return sanitized


def _sanitize_input(value: object, *, depth: int) -> dict[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    sanitized = _sanitize_provenance(value, depth=depth)
    if "value" in value:
        sanitized_value = _sanitize_input_value(value["value"])
        if sanitized_value is not None:
            sanitized["value"] = sanitized_value
    provider_reference = value.get("provider_reference")
    if isinstance(provider_reference, Mapping):
        safe_reference = _sanitize_provider_reference(provider_reference)
        if safe_reference:
            sanitized["provider_reference"] = safe_reference
    return sanitized or None


def _sanitize_input_value(value: object) -> JsonValue | None:
    if _is_finite_number(value):
        return cast(JsonValue, value)
    if isinstance(value, Mapping):
        return {
            str(key): cast(JsonValue, candidate)
            for key, candidate in islice(value.items(), MAX_PROVENANCE_ITEMS)
            if isinstance(key, str) and key in _OHLCV_FIELDS and _is_finite_number(candidate)
        }
    return None


def _enum_value(value: object, enum_type: type[StrEnum]) -> JsonValue | None:
    if not isinstance(value, str | StrEnum):
        return None
    text = str(value)
    try:
        return cast(JsonValue, enum_type(text).value)
    except ValueError:
        return None


def _sanitize_scalar(key: str, value: object) -> JsonValue | None:
    if key in {"provider_kind", "input_provider_kind"}:
        return _enum_value(value, ProviderKind)
    if key == "metric":
        return _enum_value(value, MetricKind)
    if key == "vendor_field":
        return _enum_value(value, VendorField)
    if key == "period_type":
        return _enum_value(value, PeriodType)
    if key == "period_role":
        return _enum_value(value, PeriodRole)
    if key == "algorithm":
        return _enum_value(value, DerivedAlgorithm)
    if key == "window":
        return (
            cast(JsonValue, value)
            if isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,64}", value)
            else None
        )
    if key == "point_count":
        return (
            cast(JsonValue, value)
            if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 4096
            else None
        )
    if key == "currency":
        return _pattern_value(value, _CURRENCY_PATTERN)
    if key == "period_end":
        return _iso_date(value)
    if key in _TIMESTAMP_KEYS:
        return _iso_timestamp(value)
    if key in _REFERENCE_KEYS:
        return _pattern_value(value, _REFERENCE_PATTERN)
    return None


def _pattern_value(value: object, pattern: re.Pattern[str]) -> JsonValue | None:
    return cast(JsonValue, value) if isinstance(value, str) and pattern.fullmatch(value) else None


def _iso_date(value: object) -> JsonValue | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return cast(JsonValue, parsed.isoformat()) if parsed.isoformat() == value else None


def _iso_timestamp(value: object) -> JsonValue | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return cast(JsonValue, parsed.isoformat())


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) <= MAX_PROVENANCE_INTEGER
    return isinstance(value, float) and math.isfinite(value)
