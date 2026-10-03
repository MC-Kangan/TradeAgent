"""Same-sample comparisons and explicit, order-independent factor attribution."""

from __future__ import annotations

from datetime import date

import numpy as np
from numpy.typing import NDArray
from statsmodels.regression.linear_model import OLS

from trade_research.domain.models import (
    FactorDefinition,
    FactorModelCoefficient,
    FactorModelComparison,
    FactorModelSpec,
    FactorResidualization,
    FactorResidualizationSummary,
    FactorStability,
    RollingFactorFit,
)
from trade_research.skills.factor_study import StudyError


def high_collinearity(condition: float, vifs: NDArray[np.float64]) -> bool:
    return bool(condition > 30 or np.max(vifs) > 10)


def standardized_design(
    x: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Scale only the supplied sample and return the original-unit transform."""
    scale = x.std(axis=0, ddof=1)
    if np.any(scale <= 1e-14):
        raise StudyError("rank_deficient", "fit")
    means = x.mean(axis=0)
    design = np.column_stack((np.ones(len(x)), (x - means) / scale))
    if np.linalg.matrix_rank(design) < design.shape[1]:
        raise StudyError("rank_deficient", "fit")
    transform = np.eye(design.shape[1])
    transform[0, 1:] = -means / scale
    transform[1:, 1:] = np.diag(1 / scale)
    return design, transform, scale


def residualize(
    x: NDArray[np.float64],
    ids: list[str],
    rules: tuple[FactorResidualization, ...],
) -> tuple[NDArray[np.float64], tuple[FactorResidualizationSummary, ...]]:
    """Use the original controls simultaneously; never chain residualized factors."""
    transformed = x.copy()
    summaries = []
    for rule in rules:
        target = ids.index(rule.factor_id)
        controls = [ids.index(term) for term in rule.against]
        design, _, _ = standardized_design(x[:, controls])
        variance = float(np.var(x[:, target], ddof=1))
        if variance <= 1e-28:
            raise StudyError("rank_deficient", "fit")
        residual = OLS(x[:, target], design, missing="raise").fit().resid
        transformed[:, target] = residual
        summaries.append(
            FactorResidualizationSummary(
                **rule.model_dump(),
                remaining_variance_fraction=float(
                    np.clip(np.var(residual, ddof=1) / variance, 0, 1)
                ),
            )
        )
    return transformed, tuple(summaries)


def compare_models(
    x: NDArray[np.float64],
    y: NDArray[np.float64],
    definitions: tuple[FactorDefinition, ...],
    specifications: tuple[FactorModelSpec, ...],
) -> tuple[FactorModelComparison, ...]:
    """Compare original-factor models using exactly the full model's aligned rows."""
    ids = [d.id for d in definitions]
    full = FactorModelSpec(name="Full model", factor_ids=tuple(ids))
    results = []
    for spec in (full, *specifications):
        indices = [ids.index(term) for term in spec.factor_ids]
        design, transform, _ = standardized_design(x[:, indices])
        fit = OLS(y, design, missing="raise").fit()
        estimates = transform @ fit.params
        condition = float(np.linalg.cond(design))
        vifs = np.diag(np.linalg.inv(np.atleast_2d(np.corrcoef(x[:, indices], rowvar=False))))
        coefficients = (
            FactorModelCoefficient(
                term="intercept", label="Intercept", estimate=float(estimates[0])
            ),
            *(
                FactorModelCoefficient(
                    term=definitions[index].id,
                    label=definitions[index].label,
                    estimate=float(estimates[i + 1]),
                )
                for i, index in enumerate(indices)
            ),
        )
        results.append(
            FactorModelComparison(
                name=spec.name,
                factor_ids=spec.factor_ids,
                sample_count=len(y),
                r_squared=float(fit.rsquared),
                adjusted_r_squared=float(fit.rsquared_adj),
                residual_volatility=float(np.sqrt(fit.ssr / fit.df_resid)),
                condition_number=condition,
                variance_inflation_factors=tuple(float(v) for v in vifs),
                high_collinearity=high_collinearity(condition, vifs),
                coefficients=coefficients,
            )
        )
    return tuple(results)


def stability_summary(
    rolling: list[RollingFactorFit],
    definitions: tuple[FactorDefinition, ...],
    latest_expected_end: date,
) -> tuple[FactorStability, ...]:
    """Descriptive dispersion across overlapping windows; not confidence intervals."""
    if not rolling:
        return ()
    estimates = np.array([row.estimates for row in rolling])
    return tuple(
        FactorStability(
            term=d.id,
            label=d.label,
            window_count=len(rolling),
            minimum=float(estimates[:, i + 1].min()),
            maximum=float(estimates[:, i + 1].max()),
            median=float(np.median(estimates[:, i + 1])),
            latest=float(estimates[-1, i + 1]),
            latest_end_date=rolling[-1].end_date,
            latest_is_current=rolling[-1].end_date == latest_expected_end,
            standard_deviation=float(estimates[:, i + 1].std(ddof=1)) if len(rolling) > 1 else None,
            positive_fraction=float(np.mean(estimates[:, i + 1] > 0)),
            negative_fraction=float(np.mean(estimates[:, i + 1] < 0)),
        )
        for i, d in enumerate(definitions)
    )
