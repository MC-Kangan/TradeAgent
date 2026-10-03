"""Small, fixed three-factor historical return regression using statsmodels."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Literal

import numpy as np
from pydantic import Field, model_validator
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
    DomainModel,
    FactorCoefficient,
    FactorDiagnostic,
    FactorInputRole,
    FactorInputSummary,
    FactorRegressionPresentation,
    FactorTerm,
)
from trade_research.providers import CapabilityName, ProviderRegistry


class FactorRegressionParameters(DomainModel):
    preset: Literal["us_etf", "custom"] = "us_etf"
    market_benchmark: InstrumentId | None = None
    growth_benchmark: InstrumentId | None = None
    value_benchmark: InstrumentId | None = None
    momentum_benchmark: InstrumentId | None = None
    start_date: date | None = None
    end_date: date | None = None
    minimum_observations: int = Field(default=252, ge=60, le=2520)
    hac_lags: int = Field(default=5, ge=0, le=60)

    @model_validator(mode="after")
    def validate_study(self) -> FactorRegressionParameters:
        benchmarks = (
            self.market_benchmark,
            self.growth_benchmark,
            self.value_benchmark,
            self.momentum_benchmark,
        )
        if self.preset == "custom":
            if any(item is None for item in benchmarks) or len(set(benchmarks)) != 4:
                raise ValueError("custom studies require four distinct benchmarks")
        elif any(item is not None for item in benchmarks):
            raise ValueError("benchmark overrides require the custom preset")
        if self.start_date and self.end_date:
            if not 0 < (self.end_date - self.start_date).days <= 3660:
                raise ValueError("study dates must increase and span at most ten years")
        if self.hac_lags >= self.minimum_observations - 4:
            raise ValueError("HAC lag count must leave residual degrees of freedom")
        return self

    def resolve_dates(self, today: date) -> FactorRegressionParameters:
        end = self.end_date or (today - timedelta(days=1))
        start = self.start_date or (end - timedelta(days=1096))
        if not 0 < (end - start).days <= 3660 or end >= today:
            raise ValueError(
                "factor dates require a completed historical window of at most ten years"
            )
        return self.model_copy(update={"start_date": start, "end_date": end})

    def benchmarks(self) -> tuple[InstrumentId, ...]:
        if self.preset == "us_etf":
            return tuple(
                InstrumentId(symbol=name, market="US") for name in ("IWB", "IWF", "IWD", "MTUM")
            )
        return tuple(
            item
            for item in (
                self.market_benchmark,
                self.growth_benchmark,
                self.value_benchmark,
                self.momentum_benchmark,
            )
            if item is not None
        )


@dataclass(frozen=True, slots=True)
class FactorRegressionSkill:
    """Explain total returns with market, growth-minus-value and momentum-minus-market."""

    parameters: FactorRegressionParameters = field(default_factory=FactorRegressionParameters)
    as_of: date | None = field(default=None, repr=False)
    name: str = field(default="factor-regression", init=False)
    required_capabilities: tuple[CapabilityName, ...] = (CapabilityName.FACTOR_RETURNS,)

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        params = self.parameters.resolve_dates(self.as_of or datetime.now(UTC).date())
        start, end = params.start_date, params.end_date
        assert start is not None and end is not None
        manifest = params
        reference = "sha256:" + hashlib.sha256(manifest.model_dump_json().encode()).hexdigest()
        presentation = FactorRegressionPresentation(
            preset=params.preset,
            requested_start=start,
            requested_end=end,
            minimum_observations=params.minimum_observations,
            hac_lags=params.hac_lags,
            configuration_ref=reference,
        )
        if instrument.market in {"CRYPTO", "PORTFOLIO", "SSE", "SZSE", "BJSE"} or (
            params.preset == "us_etf"
            and instrument.market not in {"US", "NASDAQ", "NYSE", "AMEX", "OTC", "ETF"}
        ):
            return _partial(instrument, presentation, "unsupported_market")
        identities = (instrument, *params.benchmarks())
        # A repeated identity must use exactly one snapshot within the study.
        fetched = {
            item: providers.factor_returns(item, start, end) for item in dict.fromkeys(identities)
        }
        histories = tuple(fetched[item] for item in identities)
        roles: tuple[FactorInputRole, ...] = ("stock", "market", "growth", "value", "momentum")
        inputs = tuple(
            FactorInputSummary(
                role=role,
                instrument=item.instrument,
                source=item.source,
                currency=item.currency,
                return_basis=item.return_basis,
                vendor_field=item.vendor_field,
                reference="sha256:" + hashlib.sha256(item.model_dump_json().encode()).hexdigest(),
                retrieved_at=item.retrieved_at,
                interval_count=len(item.points),
            )
            for role, item in zip(roles, histories, strict=True)
        )
        presentation = presentation.model_copy(update={"inputs": inputs})
        if len({item.currency for item in histories}) != 1:
            return _partial(instrument, presentation, "currency_mismatch")
        if len({item.return_basis for item in histories}) != 1:
            return _partial(instrument, presentation, "return_basis_mismatch")
        rows = [
            {
                (p.start_date, p.end_date): p.value
                for p in item.points
                if start <= p.start_date < p.end_date <= end
            }
            for item in histories
        ]
        union = set.union(*(set(item) for item in rows))
        common = set.intersection(*(set(item) for item in rows))
        # This MVP handles daily session returns. Never bridge missing endpoints or
        # silently fit long outages as daily returns. Four days permits holiday weekends.
        intervals = sorted(pair for pair in common if (pair[1] - pair[0]).days <= 4)
        discontinuities = sum(
            current[0] > previous[1] for previous, current in pairwise(intervals)
        )
        warnings: list[FactorDiagnostic] = ["raw_returns_not_alpha", "nonsynchronous_closes"]
        if discontinuities:
            warnings.append("discontinuous_history")
        if len(intervals) != len(union):
            warnings.append("missing_intervals")
        if len(intervals) != len(common):
            warnings.append("long_intervals_excluded")
        presentation = presentation.model_copy(
            update={
                "sample_count": len(intervals),
                "discontinuity_count": discontinuities,
                "dropped_interval_count": len(union) - len(intervals),
                "actual_start": intervals[0][0] if intervals else None,
                "actual_end": intervals[-1][1] if intervals else None,
                "diagnostics": tuple(warnings),
            }
        )
        if len(intervals) < params.minimum_observations:
            return _partial(instrument, presentation, "insufficient_history")
        values = np.array([[row[pair] for row in rows] for pair in intervals], dtype=float)
        y = values[:, 0]
        x = np.column_stack(
            (values[:, 1], values[:, 2] - values[:, 3], values[:, 4] - values[:, 1])
        )
        scale = x.std(axis=0, ddof=1)
        if np.any(scale <= 1e-14):
            return _partial(instrument, presentation, "rank_deficient")
        z = (x - x.mean(axis=0)) / scale
        design = np.column_stack((np.ones(len(x)), x))
        standardized = np.column_stack((np.ones(len(x)), z))
        if np.linalg.matrix_rank(standardized) < 4:
            return _partial(instrument, presentation, "rank_deficient")
        if y.std(ddof=1) <= 1e-14:
            return _partial(instrument, presentation, "constant_target")
        try:
            fit = OLS(y, design, missing="raise").fit(
                cov_type="HAC",
                cov_kwds={"maxlags": params.hac_lags, "use_correction": True},
                use_t=True,
            )
            bounds = fit.conf_int(alpha=0.05)
            correlation = np.corrcoef(x, rowvar=False)
            vifs = np.diag(np.linalg.inv(correlation))
            condition = float(np.linalg.cond(standardized))
            terms: tuple[FactorTerm, ...] = (
                "intercept",
                "market",
                "growth_minus_value",
                "momentum_minus_market",
            )
            coefficients = tuple(
                FactorCoefficient(
                    term=term,
                    estimate=float(fit.params[i]),
                    standard_error=float(fit.bse[i]),
                    lower_95=float(bounds[i, 0]),
                    upper_95=float(bounds[i, 1]),
                    standardized_effect=None
                    if i == 0
                    else float(fit.params[i] * scale[i - 1] / y.std(ddof=1)),
                )
                for i, term in enumerate(terms)
            )
            if condition > 30 or max(vifs) > 10:
                warnings.append("high_collinearity")
            if len(y) < 252:
                warnings.append("short_history")
            presentation = FactorRegressionPresentation.model_validate(
                {
                    **presentation.model_dump(),
                    "coefficients": coefficients,
                    "r_squared": float(fit.rsquared),
                    "adjusted_r_squared": float(fit.rsquared_adj),
                    "residual_volatility": float(np.sqrt(fit.ssr / fit.df_resid)),
                    "condition_number": condition,
                    "factor_correlations": tuple(
                        tuple(float(v) for v in row) for row in correlation
                    ),
                    "variance_inflation_factors": tuple(float(v) for v in vifs),
                    "diagnostics": tuple(warnings),
                }
            )
        except (ValueError, np.linalg.LinAlgError, FloatingPointError):
            return _partial(instrument, presentation, "numerical_failure")
        metrics = (
            MetricKind.FACTOR_R_SQUARED,
            MetricKind.FACTOR_MARKET_BETA,
            MetricKind.FACTOR_STYLE_BETA,
            MetricKind.FACTOR_MOMENTUM_BETA,
        )
        outputs = (float(fit.rsquared), *(float(v) for v in fit.params[1:]))
        observations = tuple(
            Observation(
                instrument=instrument,
                metric=metric,
                value=value,
                source="derived",
                observed_at=datetime.combine(intervals[-1][1], datetime.min.time(), tzinfo=UTC),
                provenance={
                    "algorithm": "factor_ols_hac",
                    "window": f"{len(y)}_return_intervals",
                    "configuration_ref": reference,
                    "point_count": len(y),
                },
            )
            for metric, value in zip(metrics, outputs, strict=True)
        )
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary="Historical three-factor explanation; coefficients are not forecasts.",
            observations=observations,
            presentation=presentation,
            citations=tuple(
                Citation(
                    provider=item.source, reference=item.reference, collected_at=item.retrieved_at
                )
                for item in inputs
            ),
        )


def _partial(
    instrument: InstrumentId, presentation: FactorRegressionPresentation, reason: FactorDiagnostic
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
            update={"diagnostics": (*presentation.diagnostics, reason)}
        ),
    )
