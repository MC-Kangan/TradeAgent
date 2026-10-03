"""One OLS/HAC estimator for any selected, normalized factor matrix."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import uuid4

import numpy as np
from statsmodels.regression.linear_model import OLS

from trade_research.domain import (
    AnalystResult,
    Citation,
    InstrumentId,
    LimitationKind,
    MetricKind,
    Observation,
    ReportStatus,
)
from trade_research.domain.models import (
    FactorCoefficient,
    FactorDiagnostic,
    FactorRegressionPresentation,
    RollingFactorFit,
)
from trade_research.providers import CapabilityName, ProviderRegistry
from trade_research.providers.contracts import ProviderConfigurationError, ProviderContractError
from trade_research.skills.factor_attribution import (
    compare_models,
    high_collinearity,
    residualize,
    stability_summary,
    standardized_design,
)
from trade_research.skills.factor_parameters import (
    FactorRegressionParameters as FactorRegressionParameters,
)
from trade_research.skills.factor_study import StudyData, StudyError, prepare_study


def fit_study(
    data: StudyData, params: FactorRegressionParameters, presentation: FactorRegressionPresentation
) -> FactorRegressionPresentation:
    ids = [d.id for d in data.definitions]
    x, residualizations = residualize(data.x, ids, params.residualizations)
    y = data.y
    definitions = tuple(
        d.model_copy(update={"label": d.label + " (residualized)"})
        if d.id in {r.factor_id for r in params.residualizations}
        else d
        for d in data.definitions
    )
    standardized, transform, scale = standardized_design(x)
    if y.std(ddof=1) <= 1e-14:
        raise StudyError("constant_target", "fit")
    fit = OLS(y, standardized, missing="raise").fit(
        cov_type="HAC", cov_kwds={"maxlags": params.hac_lags, "use_correction": True}, use_t=True
    )
    estimates = transform @ fit.params
    intervals = fit.t_test(transform).conf_int()
    errors = np.sqrt(np.diag(transform @ fit.cov_params() @ transform.T))
    correlation = np.atleast_2d(np.corrcoef(x, rowvar=False))
    vifs = np.diag(np.linalg.inv(correlation))
    condition = float(np.linalg.cond(standardized))
    warnings = list(data.warnings)
    comparisons = compare_models(data.x, y, data.definitions, params.comparisons)
    if params.residualizations and comparisons[0].high_collinearity:
        warnings.append("original_high_collinearity")
    if high_collinearity(condition, vifs):
        warnings.append("high_collinearity")
    coefficients = tuple(
        FactorCoefficient(
            term="intercept" if i == 0 else definitions[i - 1].id,
            label="Intercept" if i == 0 else definitions[i - 1].label,
            unit="decimal_return" if i == 0 else definitions[i - 1].unit,
            estimate=float(value),
            standard_error=None if data.gaps else float(errors[i]),
            lower_95=None if data.gaps else float(intervals[i, 0]),
            upper_95=None if data.gaps else float(intervals[i, 1]),
            standardized_effect=None if i == 0 else float(value * scale[i - 1] / y.std(ddof=1)),
        )
        for i, value in enumerate(estimates)
    )
    month_last = {d.replace(day=1): i for i, d in enumerate(data.days)}
    rolling = []
    skipped = []
    for i in month_last.values():
        lo = i + 1 - params.rolling_window
        if lo < 0:
            continue
        try:
            window_x, _ = residualize(data.x[lo : i + 1], ids, params.residualizations)
            window, window_transform, _ = standardized_design(window_x)
            if y[lo : i + 1].std() <= 1e-14:
                raise StudyError("constant_target", "fit")
        except StudyError:
            skipped.append(data.intervals[i][1])
            if "rolling_windows_skipped" not in warnings:
                warnings.append("rolling_windows_skipped")
            continue
        fitted = OLS(y[lo : i + 1], window).fit()
        rolling.append(
            RollingFactorFit(
                start_date=data.intervals[lo][0],
                end_date=data.intervals[i][1],
                sample_count=i + 1 - lo,
                estimates=tuple(float(v) for v in window_transform @ fitted.params),
                r_squared=float(fitted.rsquared),
            )
        )
    influential = int(np.sum(fit.get_influence().cooks_distance[0] > 4 / len(y)))
    if influential:
        warnings.append("influential_observations")
    ac = (
        float(np.corrcoef(fit.resid[:-1], fit.resid[1:])[0, 1])
        if not data.gaps and np.std(fit.resid) > 1e-14
        else None
    )
    return FactorRegressionPresentation.model_validate(
        {
            **presentation.model_dump(),
            "coefficients": coefficients,
            "r_squared": float(fit.rsquared),
            "adjusted_r_squared": float(fit.rsquared_adj),
            "residual_volatility": float(np.sqrt(fit.ssr / fit.df_resid)),
            "factor_correlations": tuple(tuple(float(v) for v in row) for row in correlation),
            "variance_inflation_factors": tuple(float(v) for v in vifs),
            "condition_number": condition,
            "rolling": tuple(rolling),
            "comparisons": comparisons,
            "rolling_skipped_end_dates": tuple(skipped),
            "residualizations": residualizations,
            "stability": stability_summary(rolling, definitions, data.intervals[-1][1]),
            "influential_count": influential,
            "residual_autocorrelation": ac,
            "diagnostics": tuple(warnings),
            "covariance": "withheld_irregular_spacing"
            if data.gaps
            else "HAC_bartlett_small_sample_t",
        }
    )


@dataclass(frozen=True, slots=True)
class FactorRegressionSkill:
    parameters: FactorRegressionParameters = field(default_factory=FactorRegressionParameters)
    as_of: date | None = field(default=None, repr=False)
    name: str = field(default="factor-regression", init=False)
    required_capabilities: tuple[CapabilityName, ...] = (CapabilityName.FACTOR_RETURNS,)

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        params = self.parameters.resolve_dates(self.as_of or datetime.now(UTC).date())
        assert params.start_date is not None and params.end_date is not None
        reference = "sha256:" + hashlib.sha256(params.model_dump_json().encode()).hexdigest()
        presentation = FactorRegressionPresentation(
            preset=params.preset,
            frequency=params.frequency,
            region=params.region,
            return_mode=params.return_mode,
            rolling_window=params.rolling_window,
            requested_start=params.start_date,
            requested_end=params.end_date,
            minimum_observations=params.minimum_observations,
            hac_lags=params.hac_lags,
            configuration_ref=reference,
        )
        stage = "fetch"
        try:
            data = prepare_study(instrument, params, providers)
            presentation = presentation.model_copy(
                update={
                    "sample_count": len(data.days),
                    "actual_start": data.intervals[0][0] if data.days else None,
                    "actual_end": data.intervals[-1][1] if data.days else None,
                    "dropped_interval_count": data.expected - len(data.days),
                    "discontinuity_count": data.gaps,
                    "inputs": data.inputs,
                    "datasets": data.datasets,
                    "coverage": data.coverage,
                    "study_currency": data.currency,
                    "diagnostics": tuple(data.warnings),
                }
            )
            if len(data.days) < params.minimum_observations:
                raise StudyError("insufficient_history", "align")
            stage = "fit"
            presentation = fit_study(data, params, presentation)
        except StudyError as error:
            return _partial(instrument, presentation, error.diagnostic, error.stage)
        except (ProviderConfigurationError, ProviderContractError) as error:
            return _partial(
                instrument, presentation, "factor_definition_mismatch", stage, type(error).__name__
            )
        except (ValueError, np.linalg.LinAlgError, FloatingPointError):
            return _partial(instrument, presentation, "numerical_failure", stage)
        observations = (
            Observation(
                instrument=instrument,
                metric=MetricKind.FACTOR_R_SQUARED,
                value=presentation.r_squared,
                source="derived",
                observed_at=datetime.combine(
                    data.intervals[-1][1], datetime.min.time(), tzinfo=UTC
                ),
                provenance={
                    "algorithm": "factor_ols_hac",
                    "window": f"{len(data.days)}_return_intervals",
                    "configuration_ref": reference,
                    "point_count": len(data.days),
                },
            ),
        )
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary="Historical factor explanation; coefficients are not forecasts.",
            observations=observations,
            presentation=presentation,
            citations=tuple(
                Citation(provider=i.source, reference=i.reference, collected_at=i.retrieved_at)
                for i in data.inputs
            )
            + tuple(
                Citation(provider=i.source, reference=i.reference, collected_at=i.retrieved_at)
                for i in data.datasets
            ),
        )


def _partial(
    instrument: InstrumentId,
    presentation: FactorRegressionPresentation,
    reason: FactorDiagnostic,
    stage: str,
    code: str | None = None,
) -> AnalystResult:
    return AnalystResult(
        analyst="factor-regression",
        instrument=instrument,
        summary="Factor study unavailable.",
        status=ReportStatus.PARTIAL,
        limitations=(
            LimitationKind.INSUFFICIENT_HISTORY
            if reason == "insufficient_history"
            else LimitationKind.INCOMPATIBLE_INPUTS,
        ),
        presentation=presentation.model_copy(
            update={
                "diagnostics": (*presentation.diagnostics, reason),
                "error_stage": stage,
                "error_code": code or reason,
                "error_id": str(uuid4()),
            }
        ),
    )
