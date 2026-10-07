"""Bloomberg levels normalized into an ordinary research panel by a YAML pack."""

from datetime import date, timedelta

from trade_research.domain.models import FactorFrequency, FactorRegion, ResearchFactorPanel
from trade_research.factor_packs import FactorPack, transform_levels
from trade_research.providers.factor_returns import BloombergReturnProvider


class PackFactorProvider:
    def __init__(self, pack: FactorPack, provider: BloombergReturnProvider) -> None:
        self._pack, self._provider = pack, provider

    @property
    def factor_ids(self) -> frozenset[str]:
        return frozenset(f.id for f in self._pack.level_factors)

    def selected(self, keys: frozenset[str]) -> "PackFactorProvider":
        if not keys <= {f.id for f in self._pack.level_factors}:
            raise ValueError("requested Bloomberg factor columns are unavailable")
        return PackFactorProvider(self._pack.subset(set(keys)), self._provider)

    def research_factors(
        self, region: FactorRegion, frequency: FactorFrequency, start: date, end: date
    ) -> ResearchFactorPanel:
        required = {key for factor in self._pack.level_factors for key in factor.legs}
        levels = {
            key: dict(
                self._provider.level_history(
                    self._pack.inputs[key], start - timedelta(days=40), end
                )
            )
            for key in sorted(required)
        }
        return transform_levels(self._pack, levels, region, frequency, start, end)
