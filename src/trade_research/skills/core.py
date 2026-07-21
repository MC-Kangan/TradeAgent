"""Immutable research skills and the small composition layer that invokes them."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Protocol, cast

from trade_research.domain import AnalystResult, InstrumentId, Observation
from trade_research.providers import (
    FundamentalProvider,
    PricePoint,
    PriceProvider,
    ProviderRegistry,
)


class ResearchSkill(Protocol):
    """An immutable analyst capability selected by its stable name."""

    name: str

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult: ...


class SkillRegistry:
    """Discover a fixed set of skills without runtime registration or mutation."""

    def __init__(self, skills: Iterable[ResearchSkill]) -> None:
        skill_items = tuple(skills)
        discovered = {skill.name: skill for skill in skill_items}
        if len(discovered) != len(skill_items):
            raise ValueError("skill names must be unique")
        self._skills: Mapping[str, ResearchSkill] = MappingProxyType(discovered)
        self._names = tuple(discovered)

    @property
    def names(self) -> tuple[str, ...]:
        return self._names

    def require(self, name: str) -> ResearchSkill:
        try:
            return self._skills[name]
        except KeyError as error:
            raise KeyError(name) from error

    def discover(self, selected: Sequence[str]) -> tuple[ResearchSkill, ...]:
        return tuple(self.require(name) for name in selected)


class FundamentalSkill:
    """Calculate transparent revenue, earnings, and cash-flow factors."""

    name = "fundamental"

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        provider = cast(FundamentalProvider, providers.require("fundamentals"))
        observations = provider.fundamentals(instrument)
        as_of = _latest_observed_at(observations) if observations else None
        factors: list[Observation] = []
        missing: list[str] = []

        revenue = _metric_value(observations, "revenue", "current")
        prior_revenue = _metric_value(observations, "revenue", "prior")
        if revenue is not None and prior_revenue is not None and prior_revenue != 0:
            factors.append(
                _factor(
                    instrument,
                    "revenue_growth",
                    (revenue - prior_revenue) / prior_revenue,
                    _require_as_of(as_of),
                    observations,
                    ("revenue:current", "revenue:prior"),
                )
            )
        else:
            missing.append("revenue current/prior")

        net_income = _metric_value(observations, "net_income")
        market_cap = _metric_value(observations, "market_cap")
        if market_cap is not None and net_income is not None and net_income != 0:
            factors.append(
                _factor(
                    instrument,
                    "price_to_earnings",
                    market_cap / net_income,
                    _require_as_of(as_of),
                    observations,
                    ("market_cap", "net_income"),
                )
            )
        else:
            missing.append("market cap/net income")

        free_cash_flow = _metric_value(observations, "free_cash_flow")
        if revenue is not None and revenue != 0 and free_cash_flow is not None:
            factors.append(
                _factor(
                    instrument,
                    "free_cash_flow_margin",
                    free_cash_flow / revenue,
                    _require_as_of(as_of),
                    observations,
                    ("free_cash_flow", "revenue:current"),
                )
            )
        else:
            missing.append("free cash flow/revenue")

        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary=_summary(missing),
            observations=tuple(factors),
        )


class TechnicalSkill:
    """Calculate a trailing return and simple moving average from price history."""

    name = "technical"

    def __init__(self, window: int = 20) -> None:
        if window < 2:
            raise ValueError("technical window must be at least two observations")
        self._window = window

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        provider = cast(PriceProvider, providers.require("prices"))
        prices = provider.price_history(instrument)
        missing: list[str] = []
        factors: list[Observation] = []
        if len(prices) >= 2 and prices[0].close != 0:
            factors.append(
                _price_factor(
                    instrument,
                    "price_return",
                    (prices[-1].close / prices[0].close) - 1,
                    prices,
                    ("first_close", "last_close"),
                )
            )
        else:
            missing.append("two non-zero prices")
        if len(prices) >= self._window:
            factors.append(
                _price_factor(
                    instrument,
                    "simple_moving_average",
                    sum(point.close for point in prices[-self._window :]) / self._window,
                    prices,
                    (f"last_{self._window}_closes",),
                )
            )
        else:
            missing.append(f"{self._window} prices")
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary=_summary(missing),
            observations=tuple(factors),
        )


class ResearchCompiler:
    """Compile only the user-selected analyst set; no debate topology is imposed."""

    def __init__(self, skills: SkillRegistry, providers: ProviderRegistry) -> None:
        self._skills = skills
        self._providers = providers

    def compile(
        self, instrument: InstrumentId, selected: Sequence[str]
    ) -> tuple[AnalystResult, ...]:
        return tuple(
            skill.analyze(instrument, self._providers) for skill in self._skills.discover(selected)
        )


class ResearchReviewer:
    """Preserve analyst conclusions while making incomplete coverage explicit."""

    def review(self, results: Sequence[AnalystResult]) -> tuple[AnalystResult, ...]:
        reviewed: list[AnalystResult] = []
        for result in results:
            summary = result.summary
            if not result.observations and not summary.startswith("partial data"):
                summary = "partial data: no derived observations"
            reviewed.append(result.model_copy(update={"summary": summary}))
        return tuple(reviewed)


def _metric_value(
    observations: Sequence[Observation], metric: str, period: str | None = None
) -> float | None:
    for observation in observations:
        matches_period = period is None or observation.provenance.get("period") == period
        if observation.metric == metric and matches_period:
            if isinstance(observation.value, int | float):
                return float(observation.value)
    return None


def _latest_observed_at(observations: Sequence[Observation]) -> datetime:
    if not observations:
        raise ValueError("fundamental provider returned no observations")
    return max(observation.observed_at for observation in observations)


def _factor(
    instrument: InstrumentId,
    metric: str,
    value: float,
    as_of: datetime,
    inputs: Sequence[Observation],
    input_names: tuple[str, ...],
) -> Observation:
    return Observation(
        instrument=instrument,
        metric=metric,
        value=round(value, 10),
        source="fundamental-skill",
        observed_at=as_of,
        provenance={
            "inputs": list(input_names),
            "sources": sorted({observation.source for observation in inputs}),
        },
    )


def _price_factor(
    instrument: InstrumentId,
    metric: str,
    value: float,
    prices: Sequence[PricePoint],
    input_names: tuple[str, ...],
) -> Observation:
    observed_at = prices[-1].observed_at
    sources = sorted({price.source for price in prices})
    return Observation(
        instrument=instrument,
        metric=metric,
        value=round(value, 10),
        source="technical-skill",
        observed_at=observed_at,
        provenance={"inputs": list(input_names), "sources": sources},
    )


def _summary(missing: Sequence[str]) -> str:
    if missing:
        return f"partial data: missing {', '.join(missing)}"
    return "complete data: all required inputs available"


def _require_as_of(value: datetime | None) -> datetime:
    if value is None:
        raise RuntimeError("derived observations require source observations")
    return value
