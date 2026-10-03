"""Offline European energy illustration of the sector-independent factor API."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import numpy as np

from trade_research.domain import AnalysisRequest, InstrumentId
from trade_research.domain.models import (
    FactorDefinition,
    FactorReturnPoint,
    FactorReturnSeries,
    ResearchFactorPanel,
    ResearchFactorPoint,
)
from trade_research.engine import ResearchEngine
from trade_research.reporting import render_markdown
from trade_research.skills.factor_data import month_end, session_dates


def example_request() -> AnalysisRequest:
    """All series are synthetic, including the illustrative oil/gas changes."""
    dates = session_dates("XETRA", date(2016, 12, 1), date(2024, 12, 31))
    closes = list({month_end(d): d for d in dates}.items())
    rng = np.random.default_rng(42)
    n = len(closes) - 1
    market = rng.normal(0.004, 0.04, n)
    oil = rng.normal(0, 6, n)  # percentage-point changes, not return-series levels
    gas = rng.normal(0, 12, n)
    sector = 0.8 * market + 0.002 * oil + rng.normal(0, 0.025, n)
    target = (
        0.001 + 1.1 * market + 0.6 * sector + 0.001 * oil + 0.0002 * gas + rng.normal(0, 0.01, n)
    )
    definitions = (
        FactorDefinition(id="market", label="European market", kind="asset_return"),
        FactorDefinition(id="sector", label="European energy sector", kind="asset_return"),
        FactorDefinition(
            id="oil", label="Oil price change", kind="change", unit="percentage_points"
        ),
        FactorDefinition(
            id="gas", label="Gas price change", kind="change", unit="percentage_points"
        ),
    )
    matrix = np.column_stack((market, sector, oil, gas))
    instrument = InstrumentId(symbol="DEMO", market="XETRA")
    retrieved = datetime(2025, 1, 1, tzinfo=UTC)
    returns = FactorReturnSeries(
        instrument=instrument,
        currency="EUR",
        source="fixture",
        frequency="monthly",
        return_basis="gross_total_return",
        vendor_field="TOTAL_RETURN",
        retrieved_at=retrieved,
        points=tuple(
            FactorReturnPoint(
                start_date=closes[i][1], end_date=closes[i + 1][1], value=float(target[i])
            )
            for i in range(n)
        ),
    )
    panel = ResearchFactorPanel(
        currency="EUR",
        region="Europe",
        frequency="monthly",
        calendar="XETRA",
        definitions=definitions,
        source="fixture",
        retrieved_at=retrieved,
        reference="sha256:" + "0" * 64,
        points=tuple(
            ResearchFactorPoint(
                date=closes[i + 1][0],
                values={d.id: float(matrix[i, j]) for j, d in enumerate(definitions)},
            )
            for i in range(n)
        ),
    )
    return AnalysisRequest(
        instrument=instrument,
        analysts=("factor-regression",),
        factor_series=(returns,),
        research_factors=panel,
        skill_parameters={
            "factor-regression": {
                "preset": "custom",
                "frequency": "monthly",
                "region": "Europe",
                "start_date": "2017-01-01",
                "end_date": "2024-12-31",
                "factors": [
                    {"id": d.id, "label": d.label, "kind": "research", "research_key": d.id}
                    for d in definitions
                ],
                "comparisons": [
                    {"name": "Market", "factor_ids": ["market"]},
                    {"name": "Market and sector", "factor_ids": ["market", "sector"]},
                    {"name": "Market and business drivers", "factor_ids": ["market", "oil", "gas"]},
                ],
                "residualizations": [{"factor_id": "sector", "against": ["market"]}],
            }
        },
    )


if __name__ == "__main__":
    print(render_markdown(asyncio.run(ResearchEngine.from_settings().analyze(example_request()))))
