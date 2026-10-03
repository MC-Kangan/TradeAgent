"""Fetch and align one study. No fitting or preset-specific factor names here."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import cast

import numpy as np
from numpy.typing import NDArray

from trade_research.domain import InstrumentId
from trade_research.domain.models import (
    FactorCoverage,
    FactorDatasetSummary,
    FactorDefinition,
    FactorDiagnostic,
    FactorInputSummary,
    FactorReturnSeries,
    FxLevelSeries,
)
from trade_research.providers import ProviderRegistry
from trade_research.skills.factor_data import convert_periods, prepare_returns, session_dates
from trade_research.skills.factor_parameters import FactorRegressionParameters


class StudyError(ValueError):
    def __init__(self, diagnostic: FactorDiagnostic, stage: str):
        super().__init__(diagnostic)
        self.diagnostic, self.stage = diagnostic, stage


@dataclass
class StudyData:
    y: NDArray[np.float64]
    x: NDArray[np.float64]
    days: list[date]
    intervals: list[tuple[date, date]]
    definitions: tuple[FactorDefinition, ...]
    inputs: tuple[FactorInputSummary, ...]
    datasets: tuple[FactorDatasetSummary, ...]
    coverage: tuple[FactorCoverage, ...]
    warnings: list[FactorDiagnostic]
    currency: str
    gaps: int
    expected: int


def _hash(series: FactorReturnSeries | FxLevelSeries) -> str:
    return "sha256:" + hashlib.sha256(series.model_dump_json().encode()).hexdigest()


def prepare_study(
    instrument: InstrumentId, params: FactorRegressionParameters, providers: ProviderRegistry
) -> StudyData:
    start, end = params.start_date, params.end_date
    assert start is not None and end is not None
    selected = params.selected_factors()
    calendars = {instrument: params.stock_calendar}
    roles = {instrument: "stock"}
    for f in selected:
        for asset, cal, role in (
            (f.instrument, f.calendar, f.id),
            (f.short_instrument, f.short_calendar, f.id + "_short"),
        ):
            if asset is None:
                continue
            if calendars.get(asset) is not None and calendars[asset] != cal and cal is not None:
                raise StudyError("factor_definition_mismatch", "normalize")
            calendars[asset] = cal or calendars.get(asset)
            roles.setdefault(asset, role)
    histories = {
        asset: providers.factor_returns(
            asset, start - timedelta(days=40) if params.frequency == "monthly" else start, end
        )
        for asset in calendars
    }
    needs_panel = params.return_mode == "excess_return" or any(
        f.kind == "research" for f in selected
    )
    panel = (
        providers.research_factors(params.region, params.frequency, start, end)
        if needs_panel
        else None
    )
    currency = panel.currency if panel else histories[instrument].currency
    warnings: list[FactorDiagnostic] = ["nonsynchronous_closes"]
    if params.return_mode == "raw_total_return":
        warnings.append("raw_returns_not_alpha")
    if len({h.return_basis for h in histories.values()}) != 1:
        raise StudyError("return_basis_mismatch", "normalize")
    prepared = {}
    for asset, h in histories.items():
        if h.frequency == "monthly" and params.frequency == "daily":
            raise StudyError("unsupported_frequency", "normalize")
        try:
            prepared[asset] = prepare_returns(
                h, params.frequency, start, end, calendar_market=calendars[asset]
            )
        except ValueError as error:
            raise StudyError("calendar_unavailable", "normalize") from error
    rows = {asset: dict(p.rows) for asset, p in prepared.items()}
    datasets: list[FactorDatasetSummary] = []
    fx_cache: dict[str, FxLevelSeries] = {}
    fx_losses = {asset: 0 for asset in histories}
    # Derive required FX endpoints from prepared intervals, shared across same-currency assets.
    for base in sorted({h.currency for h in histories.values()} - {currency}):
        relevant = [
            r
            for asset, rr in rows.items()
            if histories[asset].currency == base
            for r in rr.values()
        ]
        if not relevant:
            continue
        fx = providers.fx_history(
            base, min(r[0] for r in relevant), max(r[1] for r in relevant), currency
        )
        fx_cache[base] = fx
        datasets.append(
            FactorDatasetSummary(
                source=fx.source,
                reference=_hash(fx),
                retrieved_at=fx.retrieved_at,
                currency=currency,
                label=f"{base} to {currency}",
                observation_count=len(fx.points),
            )
        )
    for asset, h in histories.items():
        if h.currency == currency or not rows[asset]:
            continue
        before = len(rows[asset])
        rows[asset] = convert_periods(rows[asset], fx_cache[h.currency])
        fx_losses[asset] = before - len(rows[asset])
    if fx_cache:
        warnings.append("fx_conversion")
    if any(fx_losses.values()):
        warnings.append("fx_missing")
    factor_points = (
        {} if panel is None else {p.date: p for p in panel.points if start <= p.date <= end}
    )
    declared = {} if panel is None else {d.id: d for d in panel.definitions}
    definitions = []
    for f in selected:
        if f.kind == "research":
            if f.research_key not in declared:
                raise StudyError("factor_definition_mismatch", "normalize")
            d = declared[f.research_key]
            definitions.append(d.model_copy(update={"id": f.id, "label": f.label}))
        else:
            definitions.append(FactorDefinition(id=f.id, label=f.label, kind=f.kind))
    common = set.intersection(*(set(r) for r in rows.values()))
    if params.frequency == "daily":
        common = {day for day in common if len({r[day][:2] for r in rows.values()}) == 1}
    if panel:
        datasets.append(
            FactorDatasetSummary(
                source=panel.source,
                reference=panel.reference,
                retrieved_at=panel.retrieved_at,
                currency=panel.currency,
                label="Research factors",
                observation_count=len(panel.points),
            )
        )
        common &= factor_points.keys()
        if params.return_mode == "excess_return":
            common = {day for day in common if factor_points[day].risk_free is not None}
        if params.frequency == "daily":
            try:
                dates = session_dates(panel.calendar, start - timedelta(days=10), end)
            except ValueError as error:
                raise StudyError("calendar_unavailable", "normalize") from error
            prior = dict(zip(dates[1:], dates[:-1], strict=True))
            common = {d for d in common if rows[instrument][d][0] == prior.get(d)}
        if panel.source.value == "kenneth_french":
            warnings.extend(("research_data_revised", "return_convention_difference"))
        if factor_points and max(factor_points) < end - timedelta(
            days=7 if params.frequency == "daily" else 32
        ):
            warnings.append("research_data_lag")
    days = sorted(common)
    intervals = [(rows[instrument][d][0], rows[instrument][d][1]) for d in days]
    positions = {d: i for i, d in enumerate(prepared[instrument].expected)}
    gaps = sum(positions[b] - positions[a] != 1 for a, b in zip(days[:-1], days[1:], strict=True))
    coverage = tuple(
        FactorCoverage(
            role=roles[asset],
            expected_periods=p.expected_count,
            available_periods=len(p.rows),
            invalid_or_missing_periods=p.invalid_count,
            fx_endpoint_losses=fx_losses[asset],
            alignment_losses=len(rows[asset]) - len(days),
        )
        for asset, p in prepared.items()
    )
    if any(
        c.invalid_or_missing_periods or c.fx_endpoint_losses or c.alignment_losses for c in coverage
    ):
        warnings.append("missing_intervals")
    if gaps:
        warnings.extend(("discontinuous_history", "hac_intervals_withheld"))
    # Every additional funded benchmark may contain the target; no historical weights are assumed.
    if any(d.kind == "asset_return" for d in definitions):
        warnings.append("benchmark_self_inclusion_unchecked")
    cash = np.array(
        [
            cast(float, factor_points[d].risk_free)
            if params.return_mode == "excess_return"
            else 0.0
            for d in days
        ]
    )
    y = np.array([rows[instrument][d][2] for d in days]) - cash
    columns = []
    for f, definition in zip(selected, definitions, strict=True):
        if f.kind == "research":
            values = np.array([factor_points[d].values[cast(str, f.research_key)] for d in days])
            if definition.kind == "asset_return":
                values = values - cash
        else:
            assert f.instrument is not None
            values = np.array([rows[f.instrument][d][2] for d in days])
            if f.kind == "spread":
                assert f.short_instrument is not None
                values -= np.array([rows[f.short_instrument][d][2] for d in days])
            else:
                values -= cash
        columns.append(values)
    inputs = tuple(
        FactorInputSummary(
            role=roles[a],
            instrument=a,
            source=h.source,
            currency=h.currency,
            return_basis=h.return_basis,
            vendor_field=h.vendor_field,
            reference=_hash(h),
            retrieved_at=h.retrieved_at,
            interval_count=len(h.points),
        )
        for a, h in histories.items()
    )
    return StudyData(
        y,
        np.column_stack(columns),
        days,
        intervals,
        tuple(definitions),
        inputs,
        tuple(datasets),
        coverage,
        warnings,
        currency,
        gaps,
        prepared[instrument].expected_count,
    )
