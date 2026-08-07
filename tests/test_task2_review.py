from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, datetime, timedelta
from math import isfinite

import pytest

from trade_research.domain import InstrumentId, Observation
from trade_research.providers import (
    CapabilityName,
    CcxtPriceProvider,
    PricePoint,
    ProviderConfigurationError,
    ProviderRegistry,
    resolve_provider_symbol,
)
from trade_research.skills import FundamentalSkill, SkillRegistry, TechnicalSkill

AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class StaticProvider:
    observations: tuple[Observation, ...] = ()
    prices: tuple[PricePoint, ...] = ()

    def fundamentals(self, instrument: InstrumentId) -> tuple[Observation, ...]:
        return self.observations

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        return tuple(replace(point, instrument=instrument) for point in self.prices)


def test_builtin_skills_and_discovered_definitions_are_immutable() -> None:
    fundamental = FundamentalSkill()
    technical = TechnicalSkill(window=20)
    registry = SkillRegistry((fundamental, technical))

    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        technical.window = 5  # type: ignore[misc]
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        fundamental.name = "changed"  # type: ignore[misc]

    assert registry.names == ("fundamental", "technical")
    assert registry.require("technical") == TechnicalSkill(window=20)


def test_skill_registry_rejects_nested_mutable_definitions_and_is_frozen() -> None:
    @dataclass(frozen=True)
    class MutableDefinition:
        name = "mutable"
        configuration: list[int]
        required_capabilities: tuple[CapabilityName, ...] = ()

        def analyze(
            self, instrument: InstrumentId, providers: ProviderRegistry
        ) -> object:  # pragma: no cover - rejected before invocation
            raise AssertionError

    with pytest.raises(TypeError, match="immutable fields"):
        SkillRegistry((MutableDefinition([20]),))  # type: ignore[arg-type]

    registry = SkillRegistry((FundamentalSkill(), TechnicalSkill()))
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        registry._names = ("changed",)  # type: ignore[misc]


def test_registry_snapshot_is_unaffected_by_builtin_class_reassignment() -> None:
    instrument = InstrumentId(symbol="ACME", market="US")
    prices = tuple(_price(index, float(index + 100), 100.0) for index in range(40))
    providers = ProviderRegistry(
        {
            "fundamentals": StaticProvider(),
            "prices": StaticProvider(prices=prices),
        }
    )
    registry = SkillRegistry((FundamentalSkill(), TechnicalSkill()))
    original_fundamental_name = FundamentalSkill.name
    original_technical_name = TechnicalSkill.name
    missing = object()
    original_rsi_window = getattr(TechnicalSkill, "RSI_WINDOW", missing)
    original_volume_window = getattr(TechnicalSkill, "VOLUME_WINDOW", missing)

    try:
        FundamentalSkill.name = "changed-fundamental"  # type: ignore[misc]
        TechnicalSkill.name = "changed-technical"  # type: ignore[misc]
        TechnicalSkill.RSI_WINDOW = 2  # type: ignore[attr-defined]
        TechnicalSkill.VOLUME_WINDOW = 2  # type: ignore[attr-defined]

        fundamental = registry.require("fundamental").analyze(instrument, providers)
        technical = registry.require("technical").analyze(instrument, providers)
    finally:
        FundamentalSkill.name = original_fundamental_name  # type: ignore[misc]
        TechnicalSkill.name = original_technical_name  # type: ignore[misc]
        if original_rsi_window is missing:
            delattr(TechnicalSkill, "RSI_WINDOW")
        else:
            TechnicalSkill.RSI_WINDOW = original_rsi_window  # type: ignore[attr-defined]
        if original_volume_window is missing:
            delattr(TechnicalSkill, "VOLUME_WINDOW")
        else:
            TechnicalSkill.VOLUME_WINDOW = original_volume_window  # type: ignore[attr-defined]

    assert registry.names == ("fundamental", "technical")
    assert fundamental.analyst == "fundamental"
    assert technical.analyst == "technical"
    assert any(item.metric == "relative_strength_index_14" for item in technical.observations)
    assert any(item.metric == "volume_trend_20" for item in technical.observations)


def test_fundamental_skill_calculates_complete_required_factor_set() -> None:
    instrument = InstrumentId(symbol="ACME", market="NASDAQ")
    observations = (
        _observation(instrument, "revenue", 120.0, period="current"),
        _observation(instrument, "revenue", 100.0, period="prior"),
        _observation(instrument, "net_income", 12.0, period="current"),
        _observation(instrument, "net_income", 10.0, period="prior"),
        _observation(instrument, "operating_income", 18.0, period="current"),
        _observation(instrument, "shareholders_equity", 60.0, period="current"),
        _observation(instrument, "free_cash_flow", 18.0, period="current"),
        _observation(instrument, "total_debt", 30.0, period="current"),
        _observation(instrument, "market_cap", 180.0),
        _observation(instrument, "enterprise_value", 240.0),
        _observation(instrument, "ebitda", 24.0, period="current"),
    )
    provider = StaticProvider(observations=observations)

    result = FundamentalSkill().analyze(instrument, ProviderRegistry({"fundamentals": provider}))

    assert {item.metric: item.value for item in result.observations} == {
        "earnings_growth": 0.2,
        "enterprise_value_to_ebitda": 10.0,
        "free_cash_flow": 18.0,
        "free_cash_flow_margin": 0.15,
        "free_cash_flow_yield": 0.1,
        "leverage": 0.5,
        "net_margin": 0.1,
        "operating_margin": 0.15,
        "price_to_earnings": 15.0,
        "return_on_equity": 0.2,
        "revenue_growth": 0.2,
    }
    assert result.summary == "complete data: all required inputs available"


def test_fundamental_skill_rejects_incompatible_periods_currencies_and_snapshots() -> None:
    instrument = InstrumentId(symbol="ACME", market="NASDAQ")
    observations = (
        _observation(instrument, "revenue", 120.0, period="current"),
        _observation(
            instrument,
            "revenue",
            100.0,
            period="prior",
            period_type="quarterly",
            period_id="2024-Q4",
        ),
        _observation(
            instrument,
            "operating_income",
            18.0,
            period="current",
            period_type="quarterly",
            period_id="2025-Q4",
        ),
        _observation(
            instrument,
            "net_income",
            12.0,
            period="current",
            currency="EUR",
        ),
        _observation(instrument, "shareholders_equity", 60.0, period="current"),
        _observation(instrument, "free_cash_flow", 18.0, period="current"),
        _observation(instrument, "total_debt", 30.0, period="current"),
        _observation(
            instrument,
            "market_cap",
            180.0,
            snapshot_id="valuation-snapshot",
        ),
        _observation(instrument, "enterprise_value", 240.0),
        _observation(instrument, "ebitda", 24.0, period="current"),
    )

    result = FundamentalSkill().analyze(
        instrument,
        ProviderRegistry({"fundamentals": StaticProvider(observations=observations)}),
    )
    metrics = {item.metric for item in result.observations}

    assert "revenue_growth" not in metrics
    assert "operating_margin" not in metrics
    assert "net_margin" not in metrics
    assert "return_on_equity" not in metrics
    assert "price_to_earnings" not in metrics
    assert result.summary.startswith("partial data")
    assert "incompatible" in result.summary


def test_factor_provenance_contains_exact_inputs_and_factor_specific_as_of() -> None:
    instrument = InstrumentId(symbol="ACME", market="US")
    prior = _observation(
        instrument,
        "revenue",
        100.0,
        period="prior",
        observed_at=AS_OF,
        field="REVENUE_PRIOR",
    )
    current = _observation(
        instrument,
        "revenue",
        120.0,
        period="current",
        observed_at=AS_OF + timedelta(days=1),
        field="REVENUE_CURRENT",
    )
    unrelated_future = _observation(
        instrument,
        "gross_profit",
        70.0,
        period="current",
        observed_at=AS_OF + timedelta(days=30),
        field="GROSS_PROFIT",
    )
    result = FundamentalSkill().analyze(
        instrument,
        ProviderRegistry(
            {"fundamentals": StaticProvider(observations=(unrelated_future, prior, current))}
        ),
    )
    factor = next(item for item in result.observations if item.metric == "revenue_growth")

    assert factor.observed_at == current.observed_at
    assert factor.provenance["inputs"] == [
        {
            "metric": "revenue",
            "period_role": "current",
            "period_end": "2025-12-31",
            "period_type": "annual",
            "period_ref": _reference("FY2025"),
            "prior_period_ref": _reference("FY2024"),
            "snapshot_ref": _reference("snapshot-2026-01-01"),
            "currency": "USD",
            "observed_at": current.observed_at.isoformat(),
            "value": 120.0,
            "provider_kind": "bloomberg",
            "provider_reference": {
                "provider_kind": "bloomberg",
                "vendor_field": "REVENUE_CURRENT",
                "reference": _reference(f"revenue:current:{current.observed_at.isoformat()}"),
            },
        },
        {
            "metric": "revenue",
            "period_role": "prior",
            "period_end": "2024-12-31",
            "period_type": "annual",
            "period_ref": _reference("FY2024"),
            "snapshot_ref": _reference("snapshot-2026-01-01"),
            "currency": "USD",
            "observed_at": prior.observed_at.isoformat(),
            "value": 100.0,
            "provider_kind": "bloomberg",
            "provider_reference": {
                "provider_kind": "bloomberg",
                "vendor_field": "REVENUE_PRIOR",
                "reference": _reference(f"revenue:prior:{prior.observed_at.isoformat()}"),
            },
        },
    ]


def test_technical_skill_sorts_ohlcv_and_calculates_named_lookbacks() -> None:
    instrument = InstrumentId(symbol="ACME", market="NYSE")
    chronological = tuple(
        _price(
            index,
            close=float(index + 100),
            volume=100.0 if index < 20 else 200.0,
        )
        for index in range(40)
    )
    provider = StaticProvider(prices=tuple(reversed(chronological)))

    result = TechnicalSkill().analyze(instrument, ProviderRegistry({"prices": provider}))
    factors = {item.metric: item.value for item in result.observations}

    assert factors["price_return"] == 0.39
    assert factors["simple_moving_average_20"] == 129.5
    assert factors["exponential_moving_average_20"] == 129.5
    assert factors["relative_strength_index_14"] == 100.0
    assert factors["macd_12_26"] == 7.0
    assert factors["macd_signal_9"] == 7.0
    assert factors["macd_histogram"] == 0.0
    assert factors["bollinger_middle_20"] == 129.5
    assert factors["bollinger_upper_20_2"] == pytest.approx(141.0325625947)
    assert factors["bollinger_lower_20_2"] == pytest.approx(117.9674374053)
    assert factors["average_true_range_14"] == 2.0
    assert factors["momentum_10"] == pytest.approx(10 / 129)
    assert factors["annualized_volatility_20"] == pytest.approx(0.0057128038)
    assert factors["volume_trend_20"] == 1.0
    assert result.summary == "complete data: all required inputs available"
    assert all(isfinite(float(value)) for value in factors.values())


def test_technical_skill_skips_duplicate_and_non_finite_rows_and_reports_partial() -> None:
    instrument = InstrumentId(symbol="ACME", market="ETF")
    valid = tuple(_price(index, float(index + 100), 100.0) for index in range(42))
    duplicate = _price(10, 999.0, 100.0)
    non_finite = _price(50, float("nan"), 100.0)
    provider = StaticProvider(prices=(valid[-1], duplicate, non_finite, *valid[:-1]))

    with pytest.raises(ProviderConfigurationError, match="non-finite"):
        TechnicalSkill().analyze(instrument, ProviderRegistry({"prices": provider}))


def test_incomplete_ohlcv_produces_available_close_factors_and_partial_summary() -> None:
    instrument = InstrumentId(symbol="ACME", market="US")
    close_only = tuple(
        PricePoint(
            observed_at=AS_OF + timedelta(days=index),
            close=float(index + 1),
            source="internal",
            provenance={
                "provider_kind": "internal",
                "vendor_field": "PX_LAST",
                "reference": _reference(f"legacy:{index}"),
            },
        )
        for index in range(20)
    )

    result = TechnicalSkill().analyze(
        instrument,
        ProviderRegistry({"prices": StaticProvider(prices=close_only)}),
    )

    assert any(item.metric == "simple_moving_average_20" for item in result.observations)
    assert not any(item.metric == "average_true_range_14" for item in result.observations)
    assert result.summary.startswith("partial data")


def test_derived_provenance_allow_list_drops_sensitive_structured_fields() -> None:
    instrument = InstrumentId(symbol="ACME", market="US")
    prices = tuple(
        PricePoint(
            observed_at=AS_OF + timedelta(days=index),
            open=float(index + 99.5),
            high=float(index + 101),
            low=float(index + 99),
            close=float(index + 100),
            volume=100.0,
            source="fixture",
            provenance={
                "provider_kind": "fixture",
                "vendor_field": "OHLCV",
                "reference": _reference(f"safe-fixture:{index}"),
                "path": "/Users/alice-account/private/prices.csv",
                "account_id": "broker-account-123",
                "api_key": "secret-token",
                "client_ip": "203.0.113.9",
                "positions": "ACME:1000",
            },
        )
        for index in range(40)
    )

    result = TechnicalSkill().analyze(
        instrument, ProviderRegistry({"prices": StaticProvider(prices=prices)})
    )
    payload = result.model_dump_json()

    assert "fixture" in payload
    assert "sha256:" in payload
    for sensitive in (
        "alice-account",
        "broker-account-123",
        "secret-token",
        "203.0.113.9",
        "ACME:1000",
    ):
        assert sensitive not in payload


@pytest.mark.parametrize(
    ("provider", "instrument", "expected"),
    [
        ("yahoo", InstrumentId(symbol="AAPL", market="US"), "AAPL"),
        ("yahoo", InstrumentId(symbol="VOD", market="UK"), "VOD.L"),
        ("yahoo", InstrumentId(symbol="SAP", market="XETRA"), "SAP.DE"),
        ("yahoo", InstrumentId(symbol="SPY", market="ETF"), "SPY"),
        ("yahoo", InstrumentId(symbol="^STOXX", market="INDEX"), "^STOXX"),
        ("ccxt", InstrumentId(symbol="BTC/USDT", market="CRYPTO"), "BTC/USDT"),
    ],
)
def test_provider_symbol_resolution_is_bounded_and_market_aware(
    provider: str, instrument: InstrumentId, expected: str
) -> None:
    assert resolve_provider_symbol(provider, instrument) == expected


def test_symbol_resolution_and_ccxt_reject_incompatible_markets_before_io() -> None:
    us_equity = InstrumentId(symbol="ACME", market="US")

    with pytest.raises(ProviderConfigurationError, match="CRYPTO"):
        resolve_provider_symbol("ccxt", us_equity)
    with pytest.raises(ProviderConfigurationError, match="CRYPTO"):
        CcxtPriceProvider("kraken").price_history(us_equity)
    with pytest.raises(ProviderConfigurationError, match="provider"):
        resolve_provider_symbol("caller-controlled", us_equity)


def _observation(
    instrument: InstrumentId,
    metric: str,
    value: float,
    *,
    period: str | None = None,
    observed_at: datetime = AS_OF,
    field: str | None = None,
    period_end: str | None = None,
    period_type: str = "annual",
    period_id: str | None = None,
    prior_period_id: str | None = None,
    snapshot_id: str = "snapshot-2026-01-01",
    currency: str = "USD",
    valuation_as_of: str | None = None,
) -> Observation:
    provenance = {
        "provider_kind": "bloomberg",
        "snapshot_ref": _reference(snapshot_id),
        "currency": currency,
        "reference": _reference(f"{metric}:{period or 'valuation'}:{observed_at.isoformat()}"),
    }
    if period is not None:
        provenance["period_role"] = period
        provenance["period_end"] = period_end or (
            "2025-12-31" if period == "current" else "2024-12-31"
        )
        provenance["period_type"] = period_type
        provenance["period_ref"] = _reference(
            period_id or ("FY2025" if period == "current" else "FY2024")
        )
        if period == "current":
            provenance["prior_period_ref"] = _reference(prior_period_id or "FY2024")
    else:
        provenance["valuation_as_of"] = valuation_as_of or observed_at.isoformat()
    if field is not None:
        provenance["vendor_field"] = field
    else:
        provenance["vendor_field"] = metric.upper()
    return Observation(
        instrument=instrument,
        metric=metric,
        value=value,
        source="bloomberg",
        observed_at=observed_at,
        provenance=provenance,
    )


def _price(index: int, close: float, volume: float) -> PricePoint:
    return PricePoint(
        observed_at=AS_OF + timedelta(days=index),
        open=close - 0.5,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=volume,
        source="fixture",
        provenance={
            "provider_kind": "fixture",
            "vendor_field": "OHLCV",
            "reference": _reference(f"price:{index}"),
        },
    )


def _reference(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"
