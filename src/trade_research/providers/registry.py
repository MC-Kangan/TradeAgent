"""Typed immutable provider capability bundle and return-contract validation."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, overload

from trade_research.domain import (
    Evidence,
    InstrumentId,
    Observation,
    OutcomeSeriesSpec,
)
from trade_research.domain.provenance import (
    normalize_provider_kind,
    sanitize_provenance,
    sanitize_provider_reference,
)
from trade_research.providers.contracts import (
    MAX_FILING_ROWS,
    MAX_FUNDAMENTAL_ROWS,
    MAX_OUTCOME_POINTS,
    MAX_PRICE_POINTS,
    FilingProvider,
    FundamentalProvider,
    OutcomePoint,
    OutcomeSeries,
    OutcomeSeriesProvider,
    PortfolioProvider,
    PricePoint,
    PriceProvider,
    ProviderConfigurationError,
    ProviderContractError,
)


class CapabilityName(StrEnum):
    PRICES = "prices"
    OUTCOMES = "outcomes"
    FUNDAMENTALS = "fundamentals"
    FILINGS = "filings"
    PORTFOLIO = "portfolio"


type CapabilityProvider = (
    PriceProvider
    | OutcomeSeriesProvider
    | FundamentalProvider
    | FilingProvider
    | PortfolioProvider
)


class ProviderRegistry:
    """Validated typed capabilities without a free-form object registry."""

    def __init__(self, providers: Mapping[str, CapabilityProvider]) -> None:
        validated: dict[CapabilityName, CapabilityProvider] = {}
        for raw_name, provider in providers.items():
            try:
                name = CapabilityName(raw_name)
            except ValueError:
                raise ProviderConfigurationError(
                    f"unknown provider capability '{raw_name}'"
                ) from None

            protocol = {
                CapabilityName.PRICES: PriceProvider,
                CapabilityName.OUTCOMES: OutcomeSeriesProvider,
                CapabilityName.FUNDAMENTALS: FundamentalProvider,
                CapabilityName.FILINGS: FilingProvider,
                CapabilityName.PORTFOLIO: PortfolioProvider,
            }[name]
            method_name = {
                CapabilityName.PRICES: "price_history",
                CapabilityName.OUTCOMES: "outcome_history",
                CapabilityName.FUNDAMENTALS: "fundamentals",
                CapabilityName.FILINGS: "filings",
                CapabilityName.PORTFOLIO: "positions",
            }[name]
            if not isinstance(provider, protocol) or not callable(
                getattr(provider, method_name, None)
            ):
                raise ProviderConfigurationError(f"{name} capability does not satisfy its protocol")
            validated[name] = provider
        self._providers: Mapping[CapabilityName, CapabilityProvider] = MappingProxyType(validated)

    @property
    def providers(self) -> Mapping[CapabilityName, CapabilityProvider]:
        return self._providers

    def has(self, name: CapabilityName | str) -> bool:
        try:
            capability = CapabilityName(name)
        except ValueError:
            return False
        if capability is CapabilityName.OUTCOMES:
            return (
                CapabilityName.OUTCOMES in self._providers
                or CapabilityName.PRICES in self._providers
            )
        return capability in self._providers

    def ensure_capabilities(self, names: tuple[CapabilityName, ...]) -> None:
        missing = tuple(name for name in names if not self.has(name))
        if missing:
            raise ProviderConfigurationError(
                f"selected analyst capability is not configured: {', '.join(missing)}"
            )

    @overload
    def require(self, name: Literal["prices"]) -> PriceProvider: ...

    @overload
    def require(self, name: Literal["outcomes"]) -> OutcomeSeriesProvider: ...

    @overload
    def require(self, name: Literal["fundamentals"]) -> FundamentalProvider: ...

    @overload
    def require(self, name: Literal["filings"]) -> FilingProvider: ...

    @overload
    def require(self, name: Literal["portfolio"]) -> PortfolioProvider: ...

    def require(self, name: CapabilityName | str) -> CapabilityProvider:
        try:
            capability = CapabilityName(name)
        except ValueError:
            raise ProviderConfigurationError(
                f"provider capability is not configured: {name}"
            ) from None
        try:
            return self._providers[capability]
        except KeyError:
            raise ProviderConfigurationError(
                f"provider capability is not configured: {name}"
            ) from None

    def prices(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        points = self.require("prices").price_history(instrument)
        if not isinstance(points, tuple):
            raise ProviderContractError("price provider must return a tuple")
        if len(points) > MAX_PRICE_POINTS:
            raise ProviderContractError("price provider exceeded the point limit")
        normalized: list[PricePoint] = []
        for point in points:
            if not isinstance(point, PricePoint):
                raise ProviderContractError("price provider returned a malformed point")
            if point.instrument is None:
                raise ProviderContractError("price provider omitted the requested instrument")
            if point.instrument != instrument:
                raise ProviderContractError("price provider returned the wrong instrument")
            if point.observed_at.tzinfo is None:
                raise ProviderContractError("price provider returned a naive timestamp")
            values = (point.close, point.open, point.high, point.low, point.volume)
            if any(value is not None and not _is_finite(value) for value in values):
                raise ProviderContractError("price provider returned a non-finite value")
            if dict(point.provenance) != sanitize_provenance(point.provenance):
                raise ProviderContractError("price provider returned non-closed provenance")
            source = normalize_provider_kind(point.source)
            if point.provenance.get("provider_kind") != source.value:
                raise ProviderContractError("price provider provenance does not match its source")
            _require_auditable_reference(point.provenance, "reference", "price")
            normalized.append(point)
        return tuple(normalized)

    def outcomes(
        self, instrument: InstrumentId, spec: OutcomeSeriesSpec
    ) -> OutcomeSeries:
        if CapabilityName.OUTCOMES in self._providers:
            provider = self.require("outcomes")
            series = provider.outcome_history(instrument, spec)
        else:
            series = self._price_outcome_series(instrument, spec)
        if not isinstance(series, OutcomeSeries):
            raise ProviderContractError("outcome provider returned a malformed series")
        if series.instrument != instrument or series.spec != spec:
            raise ProviderContractError("outcome provider returned the wrong series")
        if len(series.points) > MAX_OUTCOME_POINTS:
            raise ProviderContractError("outcome provider exceeded the point limit")
        if not isinstance(series.points, tuple) or any(
            not isinstance(point, OutcomePoint) for point in series.points
        ):
            raise ProviderContractError("outcome provider returned malformed points")
        if series.barrier_basis not in {"observed_value", "high_low"}:
            raise ProviderContractError("outcome provider returned an invalid barrier basis")
        series_source = normalize_provider_kind(series.source)
        if series.provenance.get("provider_kind") != series_source.value:
            raise ProviderContractError(
                "outcome provider provenance does not match its source"
            )
        _require_auditable_reference(series.provenance, "reference", "outcome")
        timestamps = [point.observed_at for point in series.points]
        if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
            raise ProviderContractError(
                "outcome provider must return ordered unique timestamps"
            )
        complete = [
            point.high is not None and point.low is not None for point in series.points
        ]
        partial = [
            (point.high is None) != (point.low is None) for point in series.points
        ]
        if any(partial):
            raise ProviderContractError("outcome high and low must be supplied together")
        if series.barrier_basis == "high_low" and not all(complete):
            raise ProviderContractError(
                "high_low outcome series requires high and low for every point"
            )
        if series.barrier_basis == "observed_value" and any(complete):
            raise ProviderContractError(
                "observed_value outcome series cannot contain high or low"
            )
        return series

    def _price_outcome_series(
        self, instrument: InstrumentId, spec: OutcomeSeriesSpec
    ) -> OutcomeSeries:
        if spec.kind != "price" or spec.name != "close":
            raise ProviderConfigurationError(
                "a configured outcome provider is required for this target series"
            )
        prices = tuple(sorted(self.prices(instrument), key=lambda point: point.observed_at))
        timestamps = [point.observed_at for point in prices]
        if len(set(timestamps)) != len(timestamps):
            raise ProviderContractError("price outcome series has duplicate timestamps")
        complete = [point.high is not None and point.low is not None for point in prices]
        partial = [(point.high is None) != (point.low is None) for point in prices]
        if any(partial) or (any(complete) and not all(complete)):
            raise ProviderContractError("price outcome series has mixed high/low coverage")
        barrier_basis: Literal["observed_value", "high_low"] = (
            "high_low" if prices and all(complete) else "observed_value"
        )
        sources = {point.source for point in prices}
        if len(sources) > 1:
            raise ProviderContractError("price outcome series has mixed provider sources")
        source = normalize_provider_kind(
            next(iter(sources), normalize_provider_kind("derived"))
        )
        rows = [
            {
                "observed_at": point.observed_at.isoformat(),
                "value": point.close,
                "high": point.high if barrier_basis == "high_low" else None,
                "low": point.low if barrier_basis == "high_low" else None,
                "reference": point.provenance.get("reference"),
            }
            for point in prices
        ]
        reference = _sha256(
            {
                "spec": spec.model_dump(mode="json"),
                "barrier_basis": barrier_basis,
                "points": rows,
            }
        )
        return OutcomeSeries(
            instrument=instrument,
            spec=spec,
            points=tuple(
                OutcomePoint(
                    observed_at=point.observed_at,
                    value=point.close,
                    high=point.high if barrier_basis == "high_low" else None,
                    low=point.low if barrier_basis == "high_low" else None,
                )
                for point in prices
            ),
            source=source,
            barrier_basis=barrier_basis,
            provenance={
                "provider_kind": source.value,
                "reference": reference,
                "series_ref": reference,
            },
        )

    def fundamentals(self, instrument: InstrumentId) -> tuple[Observation, ...]:
        observations = self.require("fundamentals").fundamentals(instrument)
        if not isinstance(observations, tuple):
            raise ProviderContractError("fundamental provider must return a tuple")
        if len(observations) > MAX_FUNDAMENTAL_ROWS:
            raise ProviderContractError("fundamental provider exceeded the row limit")
        for observation in observations:
            if not isinstance(observation, Observation):
                raise ProviderContractError("fundamental provider returned a malformed observation")
            if observation.instrument != instrument:
                raise ProviderContractError("fundamental provider returned the wrong instrument")
            if observation.observed_at.tzinfo is None:
                raise ProviderContractError("fundamental provider returned a naive timestamp")
            if isinstance(observation.value, bool) or not isinstance(
                observation.value, int | float
            ):
                raise ProviderContractError("fundamental provider returned a non-numeric value")
            if not _is_finite(observation.value):
                raise ProviderContractError("fundamental provider returned a non-finite value")
            if observation.provenance != sanitize_provenance(observation.provenance):
                raise ProviderContractError("fundamental provider returned non-closed provenance")
            _validate_fundamental_metadata(observation)
        return observations

    def filings(self, instrument: InstrumentId) -> tuple[Evidence, ...]:
        evidence = self.require("filings").filings(instrument)
        if not isinstance(evidence, tuple):
            raise ProviderContractError("filing provider must return a tuple")
        if len(evidence) > MAX_FILING_ROWS:
            raise ProviderContractError("filing provider exceeded the row limit")
        if any(not isinstance(item, Evidence) for item in evidence):
            raise ProviderContractError("filing provider returned malformed evidence")
        if any(item.collected_at.tzinfo is None for item in evidence):
            raise ProviderContractError("filing provider returned a naive timestamp")
        return evidence


def _is_finite(value: int | float) -> bool:
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


_STATEMENT_METRICS = frozenset(
    {
        "revenue",
        "net_income",
        "earnings",
        "operating_income",
        "shareholders_equity",
        "total_equity",
        "free_cash_flow",
        "total_debt",
        "ebitda",
        "gross_profit",
    }
)
_VALUATION_METRICS = frozenset({"market_cap", "enterprise_value"})


def _require_auditable_reference(
    provenance: Mapping[str, object], key: str, provider_label: str
) -> str:
    value = provenance.get(key)
    if (
        not isinstance(value, str)
        or sanitize_provider_reference({"reference": value}).get("reference") != value
    ):
        raise ProviderContractError(
            f"{provider_label} provider provenance requires a valid {key} reference"
        )
    return value


def _validate_fundamental_metadata(observation: Observation) -> None:
    provenance = observation.provenance
    source = normalize_provider_kind(observation.source)
    if provenance.get("provider_kind") != source.value:
        raise ProviderContractError("fundamental provider provenance does not match its source")
    _require_auditable_reference(provenance, "snapshot_ref", "fundamental")
    _require_auditable_reference(provenance, "reference", "fundamental")

    metric = observation.metric.value
    if metric in _STATEMENT_METRICS:
        _validate_statement_metadata(provenance, observation.observed_at)
        return
    if metric in _VALUATION_METRICS:
        _validate_valuation_metadata(provenance, observation.observed_at)
        return
    raise ProviderContractError("fundamental provider returned unsupported metadata")


def _validate_statement_metadata(provenance: Mapping[str, object], observed_at: datetime) -> None:
    if provenance.get("period_role") not in {"current", "prior"}:
        raise ProviderContractError("fundamental statement metadata requires a period role")
    if provenance.get("period_type") not in {"annual", "quarterly", "ttm"}:
        raise ProviderContractError("fundamental statement metadata requires a period type")
    period_end = provenance.get("period_end")
    if not isinstance(period_end, str):
        raise ProviderContractError("fundamental statement metadata requires a period end")
    try:
        parsed_period_end = date.fromisoformat(period_end)
    except ValueError:
        raise ProviderContractError(
            "fundamental statement metadata has an invalid period end"
        ) from None
    if parsed_period_end > observed_at.date():
        raise ProviderContractError("fundamental statement metadata is future-dated")
    _require_currency(provenance, "statement")
    _require_auditable_reference(provenance, "period_ref", "fundamental statement")
    if "prior_period_ref" in provenance:
        _require_auditable_reference(provenance, "prior_period_ref", "fundamental statement")


def _validate_valuation_metadata(provenance: Mapping[str, object], observed_at: datetime) -> None:
    valuation_as_of = provenance.get("valuation_as_of")
    if not isinstance(valuation_as_of, str):
        raise ProviderContractError("fundamental valuation metadata requires an as-of timestamp")
    try:
        parsed_as_of = datetime.fromisoformat(valuation_as_of.replace("Z", "+00:00"))
    except ValueError:
        raise ProviderContractError(
            "fundamental valuation metadata has an invalid timestamp"
        ) from None
    if parsed_as_of.tzinfo is None or parsed_as_of > observed_at:
        raise ProviderContractError("fundamental valuation metadata has an invalid timestamp")
    _require_currency(provenance, "valuation")


def _require_currency(provenance: Mapping[str, object], metadata_kind: str) -> None:
    currency = provenance.get("currency")
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isupper():
        raise ProviderContractError(f"fundamental {metadata_kind} metadata requires a currency")
