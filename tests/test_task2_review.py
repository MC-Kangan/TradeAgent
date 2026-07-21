from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite

import pytest

from trade_research.domain import InstrumentId, Observation
from trade_research.providers import (
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
        return self.prices


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

        def analyze(
            self, instrument: InstrumentId, providers: ProviderRegistry
        ) -> object:  # pragma: no cover - rejected before invocation
            raise AssertionError

    with pytest.raises(TypeError, match="immutable fields"):
        SkillRegistry((MutableDefinition([20]),))  # type: ignore[arg-type]

    registry = SkillRegistry((FundamentalSkill(), TechnicalSkill()))
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        registry._names = ("changed",)  # type: ignore[misc]


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
            "period": "current",
            "observed_at": current.observed_at.isoformat(),
            "value": 120.0,
            "source": "bloomberg-mock",
            "provider_reference": {"field": "REVENUE_CURRENT"},
        },
        {
            "metric": "revenue",
            "period": "prior",
            "observed_at": prior.observed_at.isoformat(),
            "value": 100.0,
            "source": "bloomberg-mock",
            "provider_reference": {"field": "REVENUE_PRIOR"},
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

    result = TechnicalSkill().analyze(instrument, ProviderRegistry({"prices": provider}))
    factors = {item.metric: item.value for item in result.observations}

    assert factors["price_return"] == 0.41
    assert result.summary.startswith("partial data")
    assert "discarded invalid or duplicate OHLCV" in result.summary
    assert all(item.observed_at <= valid[-1].observed_at for item in result.observations)


def test_incomplete_ohlcv_produces_available_close_factors_and_partial_summary() -> None:
    instrument = InstrumentId(symbol="ACME", market="US")
    close_only = tuple(
        PricePoint(
            observed_at=AS_OF + timedelta(days=index),
            close=float(index + 1),
            source="legacy-provider",
            provenance={"field": "PX_LAST"},
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


@pytest.mark.parametrize(
    ("provider", "instrument", "expected"),
    [
        ("yahoo", InstrumentId(symbol="AAPL", market="US"), "AAPL"),
        ("yahoo", InstrumentId(symbol="VOD", market="UK"), "VOD.L"),
        ("yahoo", InstrumentId(symbol="SAP", market="XETRA"), "SAP.DE"),
        ("yahoo", InstrumentId(symbol="SPY", market="ETF"), "SPY"),
        ("stooq", InstrumentId(symbol="AAPL", market="NASDAQ"), "aapl.us"),
        ("stooq", InstrumentId(symbol="VOD", market="LSE"), "vod.uk"),
        ("stooq", InstrumentId(symbol="SAP", market="XETRA"), "sap.de"),
        ("stooq", InstrumentId(symbol="SPY", market="ETF"), "spy.us"),
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
) -> Observation:
    provenance = {}
    if period is not None:
        provenance["period"] = period
    if field is not None:
        provenance["field"] = field
    return Observation(
        instrument=instrument,
        metric=metric,
        value=value,
        source="bloomberg-mock",
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
        provenance={"field": "OHLCV", "row": str(index)},
    )
