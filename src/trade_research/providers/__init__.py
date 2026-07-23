"""Provider protocols, registries, and optional local/remote adapters."""

from trade_research.providers.contracts import (
    MAX_FILING_ROWS,
    MAX_FUNDAMENTAL_ROWS,
    MAX_HTTP_BYTES,
    MAX_LOCAL_BYTES,
    MAX_PRICE_POINTS,
    FilingProvider,
    FundamentalProvider,
    FundamentalStatementMetadata,
    FundamentalValuationMetadata,
    OptionalProviderDependencyError,
    PortfolioProvider,
    PricePoint,
    PriceProvider,
    ProviderConfigurationError,
    ProviderContractError,
)
from trade_research.providers.local import (
    LocalCsvParquetFundamentalProvider,
    LocalCsvParquetPriceProvider,
    LocalPortfolioProvider,
    ReadOnlySqlFundamentalProvider,
    ReadOnlySqlPriceProvider,
)
from trade_research.providers.registry import CapabilityName, ProviderRegistry
from trade_research.providers.remote import (
    CcxtPriceProvider,
    YahooPriceProvider,
    resolve_provider_symbol,
)
from trade_research.providers.sec import (
    CikResolver,
    SecCompanyFactsProvider,
    SecFilingsProvider,
)

__all__ = [
    "CcxtPriceProvider",
    "CikResolver",
    "CapabilityName",
    "FilingProvider",
    "FundamentalProvider",
    "FundamentalStatementMetadata",
    "FundamentalValuationMetadata",
    "LocalCsvParquetFundamentalProvider",
    "LocalCsvParquetPriceProvider",
    "LocalPortfolioProvider",
    "MAX_FILING_ROWS",
    "MAX_FUNDAMENTAL_ROWS",
    "MAX_HTTP_BYTES",
    "MAX_LOCAL_BYTES",
    "MAX_PRICE_POINTS",
    "OptionalProviderDependencyError",
    "PortfolioProvider",
    "PricePoint",
    "PriceProvider",
    "ProviderConfigurationError",
    "ProviderContractError",
    "ProviderRegistry",
    "ReadOnlySqlFundamentalProvider",
    "ReadOnlySqlPriceProvider",
    "SecCompanyFactsProvider",
    "SecFilingsProvider",
    "YahooPriceProvider",
    "resolve_provider_symbol",
]
