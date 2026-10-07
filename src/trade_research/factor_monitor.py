"""Closing-data monitoring using the same normalized inputs and OLS/HAC estimator."""

import hashlib
from dataclasses import replace
from datetime import date

from pydantic import FiniteFloat

from trade_research.domain.models import DomainModel, FactorRegressionPresentation
from trade_research.skills.factor_parameters import FactorRegressionParameters
from trade_research.skills.factor_regression import fit_study
from trade_research.skills.factor_study import StudyData, StudyError


class MonitorContribution(DomainModel):
    factor_id: str
    label: str
    unit: str
    move: FiniteFloat
    beta: FiniteFloat
    contribution: FiniteFloat


class FactorMonitorSnapshot(DomainModel):
    period_start: date
    period_end: date
    actual: FiniteFloat
    intercept: FiniteFloat
    explained: FiniteFloat
    unexplained: FiniteFloat
    contributions: tuple[MonitorContribution, ...]
    model: FactorRegressionPresentation


def monitor_snapshot(data: StudyData, params: FactorRegressionParameters) -> FactorMonitorSnapshot:
    """Fit only preceding observations; the final aligned daily interval is held out.

    This is a contemporaneous explanation, not a forecast or point-in-time backtest.
    Revised vendor history may be used. No data or fitted models are persisted.
    """
    if params.frequency != "daily" or params.attribution_rules():
        raise ValueError("monitor requires daily original-factor attribution")
    if len(data.days) - 1 < params.minimum_observations:
        raise StudyError("insufficient_history", "align")
    training = replace(
        data, y=data.y[:-1], x=data.x[:-1], days=data.days[:-1], intervals=data.intervals[:-1]
    )
    # Only gaps inside the estimation sample affect its covariance diagnostics.
    # A gap between the model's last observation and the held-out day is not one.
    training.gaps = sum(
        previous[1] != current[0]
        for previous, current in zip(training.intervals[:-1], training.intervals[1:], strict=True)
    )
    if not training.gaps:
        training.warnings = [
            warning
            for warning in data.warnings
            if warning not in {"discontinuous_history", "hac_intervals_withheld"}
        ]
    assert params.start_date is not None and params.end_date is not None
    presentation = FactorRegressionPresentation(
        preset=params.preset,
        frequency=params.frequency,
        region=params.region,
        return_mode=params.return_mode,
        rolling_window=params.rolling_window,
        requested_start=params.start_date,
        requested_end=training.days[-1],
        actual_start=training.intervals[0][0],
        actual_end=training.days[-1],
        sample_count=len(training.days),
        minimum_observations=params.minimum_observations,
        hac_lags=params.hac_lags,
        configuration_ref="sha256:" + hashlib.sha256(params.model_dump_json().encode()).hexdigest(),
        study_currency=data.currency,
        discontinuity_count=training.gaps,
        diagnostics=tuple(training.warnings),
    )
    fitted = fit_study(training, params, presentation)
    coefficients = {c.term: c.estimate for c in fitted.coefficients}
    contributions = tuple(
        MonitorContribution(
            factor_id=d.id,
            label=d.label,
            unit=d.unit,
            move=float(data.x[-1, i]),
            beta=coefficients[d.id],
            contribution=float(data.x[-1, i]) * coefficients[d.id],
        )
        for i, d in enumerate(data.definitions)
    )
    intercept = coefficients["intercept"]
    explained = intercept + sum(c.contribution for c in contributions)
    return FactorMonitorSnapshot(
        period_start=data.intervals[-1][0],
        period_end=data.days[-1],
        actual=float(data.y[-1]),
        intercept=intercept,
        explained=explained,
        unexplained=float(data.y[-1]) - explained,
        contributions=contributions,
        model=fitted,
    )
