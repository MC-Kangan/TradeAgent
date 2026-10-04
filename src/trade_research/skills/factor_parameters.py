"""Validated study settings; presets expand to ordinary factor selections."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import Field, model_validator

from trade_research.domain.factors import preset_factors
from trade_research.domain.models import (
    MAX_FACTOR_COUNT,
    DomainModel,
    FactorFrequency,
    FactorId,
    FactorModelSpec,
    FactorRegion,
    FactorResidualization,
    FactorSpec,
    InstrumentId,
)


class FactorRegressionParameters(DomainModel):
    preset: Literal["us_etf", "msci_europe", "custom", "french"] = "us_etf"
    factors: tuple[FactorSpec, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_FACTOR_COUNT
    )
    frequency: FactorFrequency = "daily"
    region: FactorRegion = "US"
    return_mode: Literal["raw_total_return", "excess_return"] = "raw_total_return"
    stock_calendar: str | None = Field(default=None, max_length=32)
    comparisons: tuple[FactorModelSpec, ...] = Field(default=(), max_length=8)
    residualizations: tuple[FactorResidualization, ...] = Field(
        default=(), max_length=MAX_FACTOR_COUNT
    )
    sequential_order: tuple[FactorId, ...] = Field(default=(), max_length=MAX_FACTOR_COUNT)
    max_factors: int = Field(default=32, ge=1, le=MAX_FACTOR_COUNT)
    rolling_window: int = Field(default=252, ge=24, le=2520)
    start_date: date | None = None
    end_date: date | None = None
    minimum_observations: int = Field(default=252, ge=60, le=2520)
    hac_lags: int = Field(default=5, ge=0, le=60)

    @model_validator(mode="before")
    @classmethod
    def defaults(cls, values: object) -> object:
        if isinstance(values, dict):
            values = dict(values)
            if values.get("frequency") == "monthly":
                for name, value in (
                    ("minimum_observations", 60),
                    ("hac_lags", 3),
                    ("rolling_window", 60),
                ):
                    values.setdefault(name, value)
            if values.get("preset") == "french":
                values.setdefault("return_mode", "excess_return")
        return values

    @model_validator(mode="after")
    def validate_study(self) -> FactorRegressionParameters:
        factors = self.selected_factors()
        ids = [f.id for f in factors]
        if not factors or len(set(ids)) != len(ids) or len(ids) > self.max_factors:
            raise ValueError("select unique factor IDs within the configured factor limit")
        if self.sequential_order:
            if self.residualizations:
                raise ValueError("choose sequential attribution or explicit control rules")
            if len(self.sequential_order) != len(ids) or set(self.sequential_order) != set(ids):
                raise ValueError("sequential order must contain every selected factor exactly once")
        names = [m.name.strip().casefold() for m in self.comparisons]
        if len(names) != len(set(names)) or "full model" in names or "" in names:
            raise ValueError("comparison names must be unique and not Full model")
        for model in self.comparisons:
            if len(set(model.factor_ids)) != len(model.factor_ids) or not set(
                model.factor_ids
            ) < set(ids):
                raise ValueError("comparison factors must be a unique strict subset")
        targets = [r.factor_id for r in self.residualizations]
        if len(targets) != len(set(targets)) or not set(targets) <= set(ids):
            raise ValueError("residualization targets must be unique selected factors")
        for rule in self.residualizations:
            if (
                len(set(rule.against)) != len(rule.against)
                or not set(rule.against) <= set(ids)
                or set(rule.against) & set(targets)
            ):
                raise ValueError("residualization controls must be unique untransformed factors")
        if (
            self.start_date
            and self.end_date
            and not 0 < (self.end_date - self.start_date).days <= 3660
        ):
            raise ValueError("study dates must increase and span at most ten years")
        if min(self.rolling_window, self.minimum_observations) <= len(ids) + 1 + self.hac_lags:
            raise ValueError("sample and rolling window must exceed parameters plus HAC lags")
        return self

    def selected_factors(self) -> tuple[FactorSpec, ...]:
        return self.factors if self.factors is not None else preset_factors(self.preset)

    def attribution_rules(self) -> tuple[FactorResidualization, ...]:
        if not self.sequential_order:
            return self.residualizations
        return tuple(
            FactorResidualization(factor_id=key, against=self.sequential_order[:i])
            for i, key in enumerate(self.sequential_order)
            if i
        )

    def benchmarks(self) -> tuple[InstrumentId, ...]:
        return tuple(
            dict.fromkeys(
                i
                for f in self.selected_factors()
                for i in (f.instrument, f.short_instrument)
                if i is not None
            )
        )

    def resolve_dates(self, today: date) -> FactorRegressionParameters:
        end = self.end_date or today - timedelta(days=1)
        start = self.start_date or end - timedelta(
            days=3652 if self.frequency == "monthly" else 1096
        )
        if not 0 < (end - start).days <= 3660 or end >= today:
            raise ValueError(
                "factor dates require a completed historical window of at most ten years"
            )
        return self.model_copy(update={"start_date": start, "end_date": end})
