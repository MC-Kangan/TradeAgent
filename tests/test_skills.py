from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trade_research.domain import InstrumentId, Observation
from trade_research.providers import PricePoint, ProviderRegistry
from trade_research.skills import (
    FundamentalSkill,
    ResearchCompiler,
    ResearchReviewer,
    SkillRegistry,
    TechnicalSkill,
)


@dataclass(frozen=True)
class BloombergShapedMock:
    """A Bloomberg-like implementation which only satisfies public protocols."""

    as_of: datetime

    def fundamentals(self, instrument: InstrumentId) -> tuple[Observation, ...]:
        return (
            _observation(instrument, "revenue", 120.0, self.as_of, {"period": "current"}),
            _observation(instrument, "revenue", 100.0, self.as_of, {"period": "prior"}),
            _observation(instrument, "net_income", 12.0, self.as_of, {"period": "current"}),
            _observation(instrument, "market_cap", 180.0, self.as_of),
            _observation(instrument, "free_cash_flow", 18.0, self.as_of, {"period": "current"}),
        )

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        return tuple(
            PricePoint(
                instrument=instrument,
                observed_at=self.as_of + timedelta(days=index),
                close=close,
                source="bloomberg",
                provenance={
                    "provider_kind": "bloomberg",
                    "vendor_field": "PX_LAST",
                    "reference": _reference(f"price:{index}"),
                },
            )
            for index, close in enumerate((100.0, 105.0, 110.0))
        )


def test_skills_depend_only_on_provider_protocols_and_calculate_factors() -> None:
    instrument = InstrumentId(symbol="ACME", market="NASDAQ")
    mock = BloombergShapedMock(datetime(2026, 1, 1, tzinfo=UTC))
    providers = ProviderRegistry({"fundamentals": mock, "prices": mock})

    result = FundamentalSkill().analyze(instrument, providers)

    factors = {observation.metric: observation.value for observation in result.observations}
    assert {
        metric: factors[metric]
        for metric in ("free_cash_flow_margin", "price_to_earnings", "revenue_growth")
    } == {
        "free_cash_flow_margin": 0.15,
        "price_to_earnings": 15.0,
        "revenue_growth": 0.2,
    }
    assert all(observation.provenance["inputs"] for observation in result.observations)


def test_compiler_discovers_immutable_skills_and_reviewer_labels_partial_data() -> None:
    instrument = InstrumentId(symbol="ACME", market="NASDAQ")
    mock = BloombergShapedMock(datetime(2026, 1, 1, tzinfo=UTC))
    skills = SkillRegistry((FundamentalSkill(), TechnicalSkill(window=3)))

    providers = ProviderRegistry({"fundamentals": mock, "prices": mock})
    results = ResearchCompiler(skills, providers).compile(instrument, ("technical",))
    reviewed = ResearchReviewer().review(results)

    assert skills.names == ("fundamental", "technical")
    assert [result.analyst for result in reviewed] == ["technical"]
    factors = {item.metric: item.value for item in reviewed[0].observations}
    assert {metric: factors[metric] for metric in ("price_return", "simple_moving_average")} == {
        "price_return": 0.1,
        "simple_moving_average": 105.0,
    }
    assert reviewed[0].summary.startswith("partial data")


def test_fundamental_skill_labels_missing_inputs_as_partial_data() -> None:
    instrument = InstrumentId(symbol="ACME", market="NASDAQ")

    class SparseFundamentals:
        def fundamentals(self, provided: InstrumentId) -> tuple[Observation, ...]:
            return (
                _observation(
                    provided,
                    "net_income",
                    10.0,
                    datetime(2026, 1, 1, tzinfo=UTC),
                    {"period": "current"},
                ),
            )

    providers = ProviderRegistry({"fundamentals": SparseFundamentals()})
    result = FundamentalSkill().analyze(instrument, providers)

    assert result.summary.startswith("partial data")
    assert result.observations == ()


def _observation(
    instrument: InstrumentId,
    metric: str,
    value: float,
    observed_at: datetime,
    provenance: dict[str, str] | None = None,
) -> Observation:
    metadata = dict(provenance or {})
    period = metadata.pop("period", None)
    metadata.update(
        {
            "provider_kind": "bloomberg",
            "snapshot_ref": _reference("snapshot-2026-01-01"),
            "currency": "USD",
            "vendor_field": metric.upper(),
            "reference": _reference(f"{metric}:{period or 'valuation'}"),
        }
    )
    if period in {"current", "prior"}:
        metadata.update(
            {
                "period_role": period,
                "period_end": "2025-12-31" if period == "current" else "2024-12-31",
                "period_type": "annual",
                "period_ref": _reference("FY2025" if period == "current" else "FY2024"),
            }
        )
        if period == "current":
            metadata["prior_period_ref"] = _reference("FY2024")
    else:
        metadata["valuation_as_of"] = observed_at.isoformat()
    return Observation(
        instrument=instrument,
        metric=metric,
        value=value,
        source="bloomberg",
        observed_at=observed_at,
        provenance=metadata,
    )


def _reference(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"
