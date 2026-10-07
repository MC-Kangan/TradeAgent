"""Compose normalized research columns without changing their economic units."""

import hashlib
from datetime import date, timedelta

from trade_research.domain.models import (
    FactorCoverage,
    FactorDatasetSummary,
    FactorFrequency,
    FactorRegion,
    ResearchFactorPanel,
    ResearchFactorPoint,
)
from trade_research.providers.contracts import ResearchFactorProvider
from trade_research.skills.factor_data import session_dates


def merge_factor_panels(panels: tuple[ResearchFactorPanel, ...]) -> ResearchFactorPanel:
    """Inner join whole periods; the first panel owns study currency and cash."""
    if not panels:
        raise ValueError("at least one research panel is required")
    primary = panels[0]
    definitions = []
    rows = []
    datasets = []
    counts = []
    for index, panel in enumerate(panels):
        panel = ResearchFactorPanel.model_validate(panel.model_dump())
        if (panel.frequency, panel.region) != (primary.frequency, primary.region):
            raise ValueError("research panel region/frequency mismatch")
        if index and any(p.risk_free is not None for p in panel.points):
            raise ValueError("only the primary panel may supply risk-free returns")
        for definition in panel.definitions:
            currency = definition.currency or panel.currency
            if definition.kind != "change" and currency != primary.currency:
                raise ValueError("return factors must already match the study currency")
            definitions.append(definition.model_copy(update={"currency": currency}))
        if len({d.id for d in definitions}) != len(definitions):
            raise ValueError("duplicate research factor ID")
        previous = {}
        if panel.frequency == "daily" and panel.points:
            sessions = session_dates(
                panel.calendar, panel.points[0].date - timedelta(days=40), panel.points[-1].date
            )
            previous = dict(zip(sessions[1:], sessions[:-1], strict=True))
        indexed = {}
        for point in panel.points:
            if panel.frequency == "daily":
                start = point.start_date or previous.get(point.date)
                if start is None:
                    raise ValueError("research factor period cannot be resolved")
                key = (start, point.date)
            else:
                key = (date(point.date.year, point.date.month, 1), point.date)
            indexed[key] = point
        rows.append(indexed)
        counts.append(len(indexed))
        datasets.append(
            FactorDatasetSummary(
                source=panel.source,
                reference=panel.reference,
                retrieved_at=panel.retrieved_at,
                currency=panel.currency,
                label=f"Research panel {index + 1}: {panel.source.value}",
                observation_count=len(panel.points),
            )
        )
    common = sorted(set.intersection(*(set(row) for row in rows)))
    expected = len(set.union(*(set(row) for row in rows)))
    points = tuple(
        ResearchFactorPoint(
            date=end,
            start_date=start if primary.frequency == "daily" else None,
            values={key: value for row in rows for key, value in row[(start, end)].values.items()},
            risk_free=rows[0][(start, end)].risk_free,
        )
        for start, end in common
    )
    digest = hashlib.sha256("".join(p.model_dump_json() for p in panels).encode()).hexdigest()
    return ResearchFactorPanel(
        region=primary.region,
        frequency=primary.frequency,
        currency=primary.currency,
        calendar=primary.calendar,
        definitions=tuple(definitions),
        source="derived",
        reference="sha256:" + digest,
        retrieved_at=max(p.retrieved_at for p in panels),
        points=points,
        datasets=tuple(datasets),
        coverage=tuple(
            FactorCoverage(
                role=datasets[i].label,
                expected_periods=expected,
                available_periods=count,
                invalid_or_missing_periods=expected - count,
                alignment_losses=count - len(common),
            )
            for i, count in enumerate(counts)
        ),
    )


class CompositeResearchFactorProvider:
    """A request-scoped list of providers and explicitly selected columns."""

    def __init__(self, sources: tuple[tuple[ResearchFactorProvider, frozenset[str]], ...]):
        self.sources = sources

    def research_factors(
        self, region: FactorRegion, frequency: FactorFrequency, start: date, end: date
    ) -> ResearchFactorPanel:
        panels = []
        for provider, keys in self.sources:
            panel = provider.research_factors(region, frequency, start, end)
            if not keys <= {d.id for d in panel.definitions}:
                raise ValueError("requested research columns are unavailable")
            if not keys:
                raise ValueError("each composed provider needs at least one selected factor")
            panels.append(
                ResearchFactorPanel.model_validate(
                    panel.model_dump()
                    | {
                        "definitions": tuple(d for d in panel.definitions if d.id in keys),
                        "points": tuple(
                            p.model_copy(
                                update={"values": {k: v for k, v in p.values.items() if k in keys}}
                            )
                            for p in panel.points
                        ),
                    }
                )
            )
        return merge_factor_panels(tuple(panels))
