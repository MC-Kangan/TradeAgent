"""Data-only YAML packs, compiled to the existing normalized factor contracts."""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal, Self, cast

import yaml
from pydantic import Field, FiniteFloat, model_validator

from trade_research.domain.models import (
    MAX_FACTOR_COUNT,
    Currency,
    DomainModel,
    FactorDefinition,
    FactorFrequency,
    FactorId,
    FactorRegion,
    FactorSpec,
    ResearchFactorPanel,
    ResearchFactorPoint,
)
from trade_research.providers.factor_returns import BloombergLevelMapping
from trade_research.skills.factor_data import month_end, session_dates


class LevelInput(BloombergLevelMapping):
    """Scale converts the vendor quotation into the declared normalized unit."""

    currency: Currency
    unit: str = Field(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9_/%.-]+$")
    scale: FiniteFloat = Field(default=1, gt=0)


class LevelFactor(DomainModel):
    id: FactorId
    label: str = Field(min_length=1, max_length=96)
    legs: dict[FactorId, FiniteFloat] = Field(min_length=1, max_length=8)
    transform: Literal["simple_return", "log_return", "difference"]


class FactorPack(DomainModel):
    id: FactorId
    label: str = Field(min_length=1, max_length=96)
    enabled: bool = True
    description: str = Field(default="", max_length=2000)
    sources: tuple[Literal["demo", "yahoo", "configured"], ...] = Field(
        default=("demo", "yahoo", "configured"), min_length=1, max_length=3
    )
    factors: tuple[FactorSpec, ...] = Field(default=(), max_length=MAX_FACTOR_COUNT)
    inputs: dict[FactorId, LevelInput] = Field(default_factory=dict, max_length=128)
    level_factors: tuple[LevelFactor, ...] = Field(default=(), max_length=MAX_FACTOR_COUNT)
    currency: Currency | None = None
    calendar: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_pack(self) -> Self:
        ids = [f.id for f in self.factors] + [f.id for f in self.level_factors]
        if not 1 <= len(ids) <= MAX_FACTOR_COUNT or len(ids) != len(set(ids)) or "intercept" in ids:
            raise ValueError("pack factor IDs must be unique and within the factor limit")
        if any(f.kind == "research" for f in self.factors):
            raise ValueError("pack research columns are generated from level_factors")
        if self.level_factors:
            if self.sources != ("configured",):
                raise ValueError("Bloomberg level factors require sources: [configured]")
            if not self.currency or not self.calendar:
                raise ValueError("level factors require a study currency and calendar")
            session_dates(self.calendar, date(2024, 1, 1), date(2024, 1, 5))
        elif self.inputs or self.currency or self.calendar:
            raise ValueError("level metadata requires level factors")
        for f in self.level_factors:
            if not set(f.legs) <= self.inputs.keys():
                raise ValueError("unknown input in factor legs")
            inputs = [self.inputs[key] for key in f.legs]
            if len({item.unit for item in inputs}) != 1:
                raise ValueError("spread legs must have compatible normalized units")
            if len({item.currency for item in inputs}) != 1:
                raise ValueError("spread legs must have compatible currencies")
            if not any(f.legs.values()):
                raise ValueError("factor legs cannot all have zero weight")
        return self

    def selected_factors(self) -> tuple[FactorSpec, ...]:
        return self.factors + tuple(
            FactorSpec(
                id=f.id, label=f.label, kind="research", research_key=f.id,
                research_source="pack",
            )
            for f in self.level_factors
        )

    def subset(self, selected: set[str]) -> FactorPack:
        level_factors = [f for f in self.level_factors if f.id in selected]
        required_inputs = {key for factor in level_factors for key in factor.legs}
        updates: dict[str, object] = {
            "factors": [f for f in self.factors if f.id in selected],
            "level_factors": level_factors,
            "inputs": {key: item for key, item in self.inputs.items() if key in required_inputs},
            "calendar": self.calendar if level_factors else None,
            "currency": self.currency if level_factors else None,
        }
        return FactorPack.model_validate(
            self.model_dump() | updates
        )


def merge_factor_packs(
    packs: tuple[FactorPack, ...], *, study_currency: Currency
) -> FactorPack:
    """Combine selected pack subsets into one bounded research panel configuration."""
    if not packs:
        raise ValueError("choose at least one factor pack")
    factors = tuple(factor for pack in packs for factor in pack.factors)
    level_factors = tuple(factor for pack in packs for factor in pack.level_factors)
    factor_ids = [factor.id for factor in factors] + [factor.id for factor in level_factors]
    if len(factor_ids) != len(set(factor_ids)):
        raise ValueError("duplicate factor ID across selected packs")
    inputs: dict[FactorId, LevelInput] = {}
    for pack in packs:
        for key, item in pack.inputs.items():
            if key in inputs and inputs[key] != item:
                raise ValueError("duplicate level input ID across selected packs")
            inputs[key] = item
    calendars = {pack.calendar for pack in packs if pack.level_factors}
    if len(calendars) > 1:
        raise ValueError("selected level packs require the same calendar")
    sources = tuple(
        source
        for source in ("demo", "yahoo", "configured")
        if all(source in pack.sources for pack in packs)
    )
    if not sources:
        raise ValueError("selected factor packs have no common data source")
    return FactorPack(
        id="combined",
        label="Combined factor packs",
        enabled=all(pack.enabled for pack in packs),
        description=" + ".join(pack.label for pack in packs),
        sources=sources,
        factors=factors,
        inputs=inputs,
        level_factors=level_factors,
        currency=study_currency if level_factors else None,
        calendar=next(iter(calendars)) if calendars else None,
    )


def load_factor_packs(directory: Path) -> dict[str, FactorPack]:
    """Read local administrator-owned files at startup, never user-supplied paths."""
    if not directory.is_dir():
        raise ValueError("factor pack directory does not exist")
    result: dict[str, FactorPack] = {}
    paths = sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")])
    if len(paths) > 128:
        raise ValueError("too many factor packs")
    for path in paths:
        try:
            if path.stat().st_size > 65536:
                raise ValueError("pack exceeds 64 KiB")
            # SafeLoader rejects executable tags. Reject aliases and duplicate keys too.
            text = path.read_text(encoding="utf-8")
            if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)):
                raise ValueError("YAML aliases are not supported")
            node = yaml.compose(text, Loader=yaml.SafeLoader)
            _unique_keys(node)
            pack = FactorPack.model_validate(yaml.safe_load(text))
        except (OSError, UnicodeError, ValueError, yaml.YAMLError, RecursionError) as error:
            raise ValueError(f"invalid factor pack: {path.name}") from error
        if pack.id in result:
            raise ValueError(f"duplicate factor pack ID: {pack.id}")
        result[pack.id] = pack
    return result


def _unique_keys(node: object) -> None:
    if isinstance(node, yaml.MappingNode):
        keys = [key.value for key, _ in node.value]
        if any(not isinstance(key, yaml.ScalarNode) for key, _ in node.value):
            raise ValueError("YAML keys must be scalars")
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate YAML key")
        for _, value in node.value:
            _unique_keys(value)
    elif isinstance(node, yaml.SequenceNode):
        for value in node.value:
            _unique_keys(value)


def transform_levels(
    pack: FactorPack,
    levels: dict[str, dict[date, float | None]],
    region: FactorRegion,
    frequency: FactorFrequency,
    start: date,
    end: date,
) -> ResearchFactorPanel:
    """Build spread levels before transforms; use exact complete calendar periods."""
    if not pack.calendar or not pack.currency or not pack.level_factors:
        raise ValueError("level factors, calendar and currency are required")
    days = session_dates(pack.calendar, start - timedelta(days=40), month_end(end))
    previous = dict(zip(days[1:], days[:-1], strict=True))
    periods: dict[date, list[date]] = {}
    if frequency == "daily":
        periods = {
            day: [previous[day], day]
            for day in days
            if day > start and day <= end and previous[day] >= start
        }
    else:
        months: dict[date, list[date]] = {}
        for day in days:
            months.setdefault(month_end(day), []).append(day)
        periods = {
            label: [previous[sessions[0]], *sessions]
            for label, sessions in months.items()
            if date(label.year, label.month, 1) >= start and label <= end
        }
    rows: dict[str, dict[date, float]] = {}
    definitions = []
    for factor in pack.level_factors:
        unit = pack.inputs[next(iter(factor.legs))].unit
        definitions.append(
            FactorDefinition(
                id=factor.id,
                currency=pack.inputs[next(iter(factor.legs))].currency,
                label=factor.label,
                kind="change",
                unit=unit
                if factor.transform == "difference"
                else "log_return"
                if factor.transform == "log_return"
                else "decimal_return",
            )
        )
        values: dict[date, float] = {}
        for day in days:
            parts = [levels[key].get(day) for key in factor.legs]
            if any(value is None for value in parts):
                continue
            for value in parts:
                if value is not None and not math.isfinite(value):
                    raise ValueError("nonfinite input level")
            normalized = sum(
                cast(float, levels[key][day]) * pack.inputs[key].scale * weight
                for key, weight in factor.legs.items()
            )
            if not math.isfinite(normalized):
                raise ValueError("nonfinite normalized level")
            if factor.transform != "difference" and normalized <= 0:
                raise ValueError("return transformations require positive levels")
            values[day] = normalized
        rows[factor.id] = {}
        for label, sessions in periods.items():
            if not all(day in values for day in sessions):
                continue
            a, b = values[sessions[0]], values[sessions[-1]]
            value = (
                b - a
                if factor.transform == "difference"
                else math.log(b) - math.log(a)
                if factor.transform == "log_return"
                else b / a - 1
            )
            rows[factor.id][label] = value
    common = sorted(set.intersection(*(set(row) for row in rows.values())))
    points = tuple(
        ResearchFactorPoint(
            date=day, start_date=periods[day][0],
            values={key: row[day] for key, row in rows.items()}
        )
        for day in common
    )
    digest = hashlib.sha256(pack.model_dump_json().encode())
    for key in sorted(levels):
        for day, value in sorted(levels[key].items()):
            digest.update(f"{key}:{day}:{value}".encode())
    return ResearchFactorPanel(
        region=region,
        frequency=frequency,
        currency=pack.currency,
        calendar=pack.calendar,
        definitions=tuple(definitions),
        source="bloomberg",
        retrieved_at=datetime.now(UTC),
        reference="sha256:" + digest.hexdigest(),
        points=points,
    )
