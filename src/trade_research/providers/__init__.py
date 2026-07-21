"""Provider protocols, registries, and optional local/remote adapters."""

from trade_research.providers.contracts import (
    FilingProvider,
    FundamentalProvider,
    OptionalProviderDependencyError,
    PortfolioProvider,
    PricePoint,
    PriceProvider,
    ProviderConfigurationError,
)
from trade_research.providers.local import (
    LocalCsvParquetPriceProvider,
    LocalPortfolioProvider,
    ReadOnlySqlPriceProvider,
)
from trade_research.providers.registry import ProviderRegistry
from trade_research.providers.remote import (
    CcxtPriceProvider,
    SecFilingsProvider,
    StooqPriceProvider,
    YahooPriceProvider,
    resolve_provider_symbol,
)

__all__ = [
    "CcxtPriceProvider",
    "FilingProvider",
    "FundamentalProvider",
    "LocalCsvParquetPriceProvider",
    "LocalPortfolioProvider",
    "OptionalProviderDependencyError",
    "PortfolioProvider",
    "PricePoint",
    "PriceProvider",
    "ProviderConfigurationError",
    "ProviderRegistry",
    "ReadOnlySqlPriceProvider",
    "SecFilingsProvider",
    "StooqPriceProvider",
    "YahooPriceProvider",
    "resolve_provider_symbol",
]
