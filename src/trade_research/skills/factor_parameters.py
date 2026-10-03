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
    FactorRegion,
    FactorSpec,
    InstrumentId,
)


class FactorRegressionParameters(DomainModel):
    preset: Literal["us_etf", "custom", "french"] = "us_etf"
    factors: tuple[FactorSpec, ...] | None = Field(
        default=None, min_length=1, max_length=MAX_FACTOR_COUNT
    )
    frequency: FactorFrequency = "daily"
    region: FactorRegion = "US"
    return_mode: Literal["raw_total_return", "excess_return"] = "raw_total_return"
    stock_calendar: str | None = Field(default=None, max_length=32)
    baseline_factor_ids: tuple[str, ...] = ()
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
        if not set(self.baseline_factor_ids) < set(ids) and self.baseline_factor_ids:
            raise ValueError("baseline must be a strict subset of selected factor IDs")
        if len(set(self.baseline_factor_ids)) != len(self.baseline_factor_ids):
            raise ValueError("baseline factor IDs must be unique")
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
