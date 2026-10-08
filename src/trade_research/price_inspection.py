"""Ephemeral vendor price levels; never reconstructed from model returns."""

from collections.abc import Sequence
from datetime import date

from pydantic import Field, FiniteFloat

from trade_research.domain import AnalysisRequest
from trade_research.domain.models import DomainModel
from trade_research.providers.contracts import ProviderConfigurationError
from trade_research.providers.factor_returns import (
    BloombergLevelMapping,
    BloombergReturnMapping,
    BloombergReturnProvider,
    YahooReturnProvider,
)
from trade_research.settings import Settings
from trade_research.skills.factor_parameters import FactorRegressionParameters


class PriceObservation(DomainModel):
    date: date
    value: FiniteFloat | None


class InspectionSeries(DomainModel):
    label: str
    source: str
    field: str
    quotation: str
    basis: str
    points: tuple[PriceObservation, ...] = Field(max_length=4097)


def inspect_prices(request: AnalysisRequest, settings: Settings) -> tuple[InspectionSeries, ...]:
    """Read selected stock, benchmark and spread-leg prices without FX or alignment.

    Bloomberg histories share one session per inspection call while retaining a separate
    bounded request for each mapping, which keeps event-stream ownership deterministic.
    """
    params = FactorRegressionParameters.model_validate(
        request.skill_parameters["factor-regression"]
    )
    start, end = params.start_date, params.end_date
    if start is None or end is None:
        raise ValueError("inspection requires explicit dates")
    instruments = [request.instrument]
    for factor in params.selected_factors():
        for asset in (factor.instrument, factor.short_instrument):
            if asset is not None and asset not in instruments:
                instruments.append(asset)
    result = []

    def append(label: str, source: str, field: str, quotation: str, basis: str,
               levels: Sequence[tuple[date, float | None]]) -> None:
        result.append(InspectionSeries(
            label=label, source=source, field=field, quotation=quotation, basis=basis,
            points=tuple(PriceObservation(date=day, value=value) for day, value in levels
                         if start <= day <= end),
        ))

    if settings.price_provider == "yahoo":
        yahoo = YahooReturnProvider()
        for asset in instruments:
            history = yahoo.price_history(asset, start, end)
            append(history.symbol, "yahoo", "ADJ_CLOSE", history.quotation,
                   "Dividend/split-adjusted price; original quotation, before FX",
                   history.adjusted)
    elif settings.price_provider == "bloomberg":
        bloomberg = BloombergReturnProvider(settings.bloomberg_return_mappings,
                                           host=settings.bloomberg_host,
                                           port=settings.bloomberg_port)
        mappings = {m.instrument: m for m in settings.bloomberg_return_mappings}
        definitions = []
        mappings_to_load: list[BloombergReturnMapping | BloombergLevelMapping] = []
        for asset in instruments:
            if asset not in mappings:
                raise ProviderConfigurationError("inspection requires a verified security mapping")
            mapping = mappings[asset]
            definitions.append((
                mapping.security,
                mapping.field,
                f"Vendor quotation; configured currency {mapping.currency}",
                ("Corporate-action-adjusted PX_LAST for equities; before FX"
                 if mapping.field == "PX_LAST" else "Vendor index levels; before FX"),
            ))
            mappings_to_load.append(mapping)
        pack = settings.bloomberg_factor_pack
        if pack:
            for key, item in pack.inputs.items():
                definitions.append((
                    f"{key} · {item.security}",
                    item.field,
                    f"Vendor quotation; configured {item.currency}/{item.unit}",
                    f"Before YAML scale {item.scale:g}, spreads and return transforms",
                ))
                mappings_to_load.append(item)
        histories = bloomberg.level_histories(mappings_to_load, start, end)
        for definition, levels in zip(definitions, histories, strict=True):
            label, field, quotation, basis = definition
            append(label, "bloomberg", field, quotation, basis, levels)
    else:
        raise ProviderConfigurationError("price inspection supports Bloomberg and Yahoo")
    return tuple(result)
