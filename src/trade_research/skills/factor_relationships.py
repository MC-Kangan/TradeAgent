"""Descriptive relationships on the fitted sample, never a factor selection rule."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray
from scipy.stats import rankdata
from statsmodels.regression.linear_model import OLS

from trade_research.domain.models import FactorDefinition, FactorRelationship
from trade_research.skills.factor_attribution import standardized_design


def correlation(a: NDArray[np.float64], b: NDArray[np.float64]) -> float | None:
    if np.std(a) <= 1e-14 or np.std(b) <= 1e-14:
        return None
    return float(np.clip(np.corrcoef(a, b)[0, 1], -1, 1))


def stock_correlations(
    x: NDArray[np.float64], y: NDArray[np.float64]
) -> tuple[tuple[float | None, ...], tuple[float | None, ...]]:
    return (
        tuple(correlation(x[:, i], y) for i in range(x.shape[1])),
        tuple(correlation(rankdata(x[:, i]), rankdata(y)) for i in range(x.shape[1])),
    )


def factor_relationships(
    x: NDArray[np.float64], y: NDArray[np.float64], definitions: tuple[FactorDefinition, ...]
) -> tuple[FactorRelationship, ...]:
    design, _, _ = standardized_design(x)
    full = OLS(y, design, missing="raise").fit()
    pearson, spearman = stock_correlations(x, y)
    rows = []
    for i, definition in enumerate(definitions):
        controls = np.delete(design, i + 1, axis=1)
        reduced = OLS(y, controls, missing="raise").fit()
        residual_factor = OLS(design[:, i + 1], controls, missing="raise").fit().resid
        residual_y = reduced.resid
        # Residual roundoff after a perfect reduced-model fit is not economic variation.
        partial = (
            None
            if np.std(residual_y) <= 1e-12 * np.std(y)
            else correlation(residual_y, residual_factor)
        )
        rows.append(
            FactorRelationship(
                term=definition.id,
                label=definition.label,
                pearson=pearson[i],
                spearman=spearman[i],
                partial_correlation=partial,
                incremental_r_squared=float(np.clip(full.rsquared - reduced.rsquared, 0, 1)),
            )
        )
    return tuple(rows)
