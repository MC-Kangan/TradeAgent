"""Small data-only presets. Estimation never branches on these factor names."""

from trade_research.domain.models import FactorDefinition, FactorSpec, InstrumentId

FRENCH_DEFINITIONS = tuple(
    FactorDefinition(id=key, label=label, kind=kind)
    for key, label, kind in (
        ("market_excess", "Market minus cash", "excess_return"),
        ("smb", "Size (SMB)", "spread"),
        ("hml", "Value (HML)", "spread"),
        ("rmw", "Profitability (RMW)", "spread"),
        ("cma", "Investment (CMA)", "spread"),
        ("momentum", "Momentum", "spread"),
    )
)


def preset_factors(preset: str) -> tuple[FactorSpec, ...]:
    if preset == "french":
        return tuple(
            FactorSpec(id=d.id, label=d.label, kind="research", research_key=d.id)
            for d in FRENCH_DEFINITIONS
        )
    if preset == "us_etf":

        def asset(symbol: str) -> InstrumentId:
            return InstrumentId(symbol=symbol, market="US")

        return (
            FactorSpec(id="market", label="Market", kind="asset_return", instrument=asset("IWB")),
            FactorSpec(
                id="growth_minus_value",
                label="Growth minus value",
                kind="spread",
                instrument=asset("IWF"),
                short_instrument=asset("IWD"),
            ),
            FactorSpec(
                id="momentum_minus_market",
                label="Momentum minus market",
                kind="spread",
                instrument=asset("MTUM"),
                short_instrument=asset("IWB"),
            ),
        )
    return ()
