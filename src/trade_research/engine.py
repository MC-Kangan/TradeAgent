"""Async coordination of immutable analyst skills."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from trade_research.domain import (
    AnalysisRequest,
    AnalystResult,
    FailureCategory,
    LimitationKind,
    ReportStatus,
    ResearchReport,
)
from trade_research.providers import (
    CcxtPriceProvider,
    CikResolver,
    InlinePriceProvider,
    LocalCsvParquetFundamentalProvider,
    LocalCsvParquetPriceProvider,
    ProviderConfigurationError,
    ProviderContractError,
    ReadOnlySqlFundamentalProvider,
    ReadOnlySqlPriceProvider,
    SecCompanyFactsProvider,
    SecFilingsProvider,
    YahooPriceProvider,
)
from trade_research.providers.registry import CapabilityName, CapabilityProvider, ProviderRegistry
from trade_research.reporting import sanitize_report
from trade_research.settings import Settings
from trade_research.skills import (
    FilingsSkill,
    FundamentalSkill,
    MarkovMethodSkill,
    ResearchReviewer,
    ResearchSkill,
    RiskAnalysisSkill,
    SkillRegistry,
    TechnicalBasicSkill,
    TechnicalSkill,
    VolatilityRegimeSkill,
    WorthBuyStocksSkill,
)


class ResearchEngine:
    """Run only selected skills concurrently and preserve partial results."""

    def __init__(
        self,
        skills: SkillRegistry,
        providers: ProviderRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._skills = skills
        self._providers = providers
        self._clock = clock or (lambda: datetime.now(UTC))
        self._reviewer = ResearchReviewer()

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        skills: SkillRegistry | None = None,
        providers: ProviderRegistry | Mapping[str, CapabilityProvider] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> ResearchEngine:
        """Compose configured bounded providers while permitting typed test injection."""

        default_skills = cast(
            tuple[ResearchSkill, ...],
            (
                FundamentalSkill(),
                TechnicalSkill(),
                FilingsSkill(),
                WorthBuyStocksSkill(),
                MarkovMethodSkill(),
                TechnicalBasicSkill(),
                RiskAnalysisSkill(),
                VolatilityRegimeSkill(),
            ),
        )
        selected_skills = skills or SkillRegistry(default_skills)
        if providers is None:
            selected_providers = ProviderRegistry(_compose_providers(settings or Settings()))
        elif isinstance(providers, ProviderRegistry):
            selected_providers = providers
        else:
            selected_providers = ProviderRegistry(providers)
        return cls(selected_skills, selected_providers, clock=clock)

    @property
    def skills(self) -> SkillRegistry:
        return self._skills

    @property
    def clock(self) -> Callable[[], datetime]:
        return self._clock

    def validate_analysts(
        self,
        selected: Sequence[str],
        providers: ProviderRegistry | None = None,
    ) -> None:
        active_providers = providers or self._providers
        capabilities: list[CapabilityName] = []
        for skill in self._skills.discover(selected):
            capabilities.extend(skill.required_capabilities)
        active_providers.ensure_capabilities(tuple(dict.fromkeys(capabilities)))

    async def analyze(self, request: AnalysisRequest) -> ResearchReport:
        """Analyze one request without allowing evidence to affect selection."""

        request = AnalysisRequest.model_validate(request.model_dump())
        providers = self._providers_for(request)
        self.validate_analysts(request.analysts, providers)
        selected = self._skills.discover(request.analysts)
        outcomes = await asyncio.gather(
            *(self._run_skill(skill, request, providers) for skill in selected),
            return_exceptions=True,
        )
        results: list[AnalystResult] = []
        for skill, outcome in zip(selected, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                if isinstance(outcome, ProviderContractError):
                    category = FailureCategory.PROVIDER_CONTRACT
                elif isinstance(outcome, ProviderConfigurationError):
                    category = FailureCategory.PROVIDER_CONFIGURATION
                else:
                    category = FailureCategory.ANALYST_ERROR
                results.append(
                    AnalystResult(
                        analyst=skill.name,
                        instrument=request.instrument,
                        summary=f"partial data: analyst failed ({type(outcome).__name__})",
                        status=ReportStatus.FAILED,
                        failure_category=category,
                        limitations=(LimitationKind.ANALYST_FAILURE,),
                    )
                )
            else:
                results.append(outcome)
        return sanitize_report(
            ResearchReport(
                request_id=request.request_id,
                instrument=request.instrument,
                results=self._reviewer.review(results),
                generated_at=self._clock(),
            )
        )

    async def _run_skill(
        self,
        skill: ResearchSkill,
        request: AnalysisRequest,
        providers: ProviderRegistry | None = None,
    ) -> AnalystResult:
        return await asyncio.to_thread(
            skill.analyze, request.instrument, providers or self._providers
        )

    async def run_configured_skill(
        self, skill: ResearchSkill, request: AnalysisRequest
    ) -> AnalystResult:
        """Run a single pre-configured skill, bypassing the frozen registry.

        Unlike ``analyze()`` which rediscovers skills from the registry,
        this method accepts an already-configured skill instance (e.g. from
        ``configure_skill()``) and runs it directly.
        """
        providers = self._providers_for(request)
        self.validate_analysts((skill.name,), providers)
        return await self._run_skill(skill, request, providers)

    def _providers_for(self, request: AnalysisRequest) -> ProviderRegistry:
        if not request.price_series:
            return self._providers
        providers: dict[str, CapabilityProvider] = {
            name.value: provider for name, provider in self._providers.providers.items()
        }
        providers[CapabilityName.PRICES.value] = InlinePriceProvider(request.price_series)
        return ProviderRegistry(providers)


def _compose_providers(settings: Settings) -> dict[str, CapabilityProvider]:
    providers: dict[str, CapabilityProvider] = {}
    if settings.price_provider in {"local_csv", "local_parquet"}:
        assert settings.price_path is not None
        providers["prices"] = LocalCsvParquetPriceProvider(settings.price_path)
    elif settings.price_provider == "local_sql":
        assert settings.price_path is not None
        providers["prices"] = ReadOnlySqlPriceProvider(settings.price_path)
    elif settings.price_provider == "yahoo":
        providers["prices"] = YahooPriceProvider()
    elif settings.price_provider == "ccxt":
        assert settings.ccxt_exchange is not None
        providers["prices"] = CcxtPriceProvider(settings.ccxt_exchange)
    elif settings.price_provider == "bloomberg":
        from trade_research.providers.remote import BloombergPriceProvider

        providers["prices"] = BloombergPriceProvider(
            host=settings.bloomberg_host,
            port=settings.bloomberg_port,
        )

    if settings.fundamental_provider in {"local_csv", "local_parquet"}:
        assert settings.fundamental_path is not None
        providers["fundamentals"] = LocalCsvParquetFundamentalProvider(
            settings.fundamental_path
        )
    elif settings.fundamental_provider == "local_sql":
        assert settings.fundamental_path is not None
        providers["fundamentals"] = ReadOnlySqlFundamentalProvider(settings.fundamental_path)

    sec_resolver: CikResolver | None = None
    if settings.sec_user_agent:
        data_dir = settings.data_root or Path(".trade-research")
        overrides = dict(settings.sec_cik_map)
        overrides.update(settings.sec_cik_overrides)
        sec_resolver = CikResolver(
            user_agent=settings.sec_user_agent,
            cache_dir=data_dir,
            overrides=overrides,
        )

        if settings.fundamental_provider == "sec_company_facts":
            providers["fundamentals"] = SecCompanyFactsProvider(
                resolver=sec_resolver,
                user_agent=settings.sec_user_agent,
            )

        providers["filings"] = SecFilingsProvider(
            resolver=sec_resolver,
            user_agent=settings.sec_user_agent,
        )

    return providers
