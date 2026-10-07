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
from trade_research.domain.factors import FRENCH_DEFINITIONS
from trade_research.domain.models import (
    FactorStudyPreview,
    FactorStudyPreviewColumn,
    FactorStudyPreviewRow,
)
from trade_research.factor_monitor import FactorMonitorSnapshot, monitor_snapshot
from trade_research.providers import (
    CcxtPriceProvider,
    CikResolver,
    InlineOutcomeProvider,
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
from trade_research.providers.contracts import ResearchFactorProvider
from trade_research.providers.factor_composition import CompositeResearchFactorProvider
from trade_research.providers.factor_fx import (
    BloombergFxProvider,
    InlineFxProvider,
    YahooFxProvider,
)
from trade_research.providers.factor_returns import (
    BloombergReturnProvider,
    InlineReturnProvider,
    YahooReturnProvider,
)
from trade_research.providers.french import FrenchFactorProvider, InlineResearchFactorProvider
from trade_research.providers.pack_factors import PackFactorProvider
from trade_research.providers.registry import CapabilityName, CapabilityProvider, ProviderRegistry
from trade_research.reporting import sanitize_report
from trade_research.settings import Settings
from trade_research.skills import (
    AssetAllocationSkill,
    BacktestingSkill,
    CorrelationAnalysisSkill,
    CrossSectionalSignalSkill,
    FilingsSkill,
    FundamentalSkill,
    MarkovMethodSkill,
    PriceActionStructureSkill,
    ResearchReviewer,
    ResearchSkill,
    RiskAnalysisSkill,
    SignalEvaluationSkill,
    SkillRegistry,
    TechnicalBasicSkill,
    TechnicalSkill,
    VolatilityRegimeSkill,
    WorthBuyStocksSkill,
)
from trade_research.skills.factor_parameters import FactorRegressionParameters
from trade_research.skills.factor_regression import FactorRegressionSkill
from trade_research.skills.factor_study import prepare_study
from trade_research.skills.parameters import PORTFOLIO_SKILLS, configure_skill


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
                FactorRegressionSkill(),
                CrossSectionalSignalSkill(),
                FundamentalSkill(),
                TechnicalSkill(),
                FilingsSkill(),
                WorthBuyStocksSkill(),
                MarkovMethodSkill(),
                TechnicalBasicSkill(),
                RiskAnalysisSkill(),
                VolatilityRegimeSkill(),
                PriceActionStructureSkill(),
                SignalEvaluationSkill(),
                CorrelationAnalysisSkill(),
                AssetAllocationSkill(),
                BacktestingSkill(),
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

    def missing_service_capabilities(self, skill_name: str) -> tuple[CapabilityName, ...]:
        """Report capabilities that cannot be supplied inline with a request."""

        skill = self._skills.require(skill_name)
        request_capabilities = {
            CapabilityName.PRICES,
            CapabilityName.OUTCOMES,
            CapabilityName.FACTOR_RETURNS,
        }
        return tuple(
            capability
            for capability in skill.required_capabilities
            if capability not in request_capabilities and not self._providers.has(capability)
        )

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

    def factor_monitor_snapshot(self, request: AnalysisRequest) -> FactorMonitorSnapshot:
        """Return the latest aligned close with betas fitted strictly before that interval."""
        request = AnalysisRequest.model_validate(request.model_dump())
        selected = self.configure_request(request)
        if len(selected) != 1 or not isinstance(selected[0], FactorRegressionSkill):
            raise ValueError("monitor requires exactly one factor-regression analyst")
        skill = selected[0]
        params = skill.parameters.resolve_dates(skill.as_of or self._clock().date())
        if params.frequency != "daily" or params.attribution_rules():
            raise ValueError("monitor requires daily original-factor attribution")
        providers = self._providers_for(request)
        self.validate_analysts(request.analysts, providers)
        return monitor_snapshot(prepare_study(request.instrument, params, providers), params)

    def factor_study_preview(self, request: AnalysisRequest) -> FactorStudyPreview:
        """Fetch normalized aligned inputs for an ephemeral local data inspection."""
        request = AnalysisRequest.model_validate(request.model_dump())
        selected = self.configure_request(request)
        if len(selected) != 1 or not isinstance(selected[0], FactorRegressionSkill):
            raise ValueError("factor preview requires exactly one factor-regression analyst")
        providers = self._providers_for(request)
        self.validate_analysts(request.analysts, providers)
        skill = selected[0]
        params = skill.parameters.resolve_dates(skill.as_of or self._clock().date())
        data = prepare_study(request.instrument, params, providers)
        columns = (
            FactorStudyPreviewColumn(
                term="stock",
                label=f"{request.instrument.market}:{request.instrument.symbol}",
                unit="decimal_return",
                currency=data.currency,
            ),
            *(
                FactorStudyPreviewColumn(
                    term=item.id, label=item.label, unit=item.unit, currency=item.currency
                )
                for item in data.definitions
            ),
        )
        rows = tuple(
            FactorStudyPreviewRow(
                start_date=start,
                end_date=end,
                values=(float(data.y[index]), *(float(value) for value in data.x[index])),
            )
            for index, (start, end) in enumerate(data.intervals)
        )
        return FactorStudyPreview(
            columns=columns,
            rows=rows,
            inputs=data.inputs,
            datasets=data.datasets,
            coverage=data.coverage,
        )

    async def analyze(self, request: AnalysisRequest) -> ResearchReport:
        """Analyze one request without allowing evidence to affect selection."""

        request = AnalysisRequest.model_validate(request.model_dump())
        providers = self._providers_for(request)
        selected = self.configure_request(request)
        self.validate_analysts(request.analysts, providers)
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

    def configure_request(self, request: AnalysisRequest) -> tuple[ResearchSkill, ...]:
        """Validate request scope and return immutable per-run analyst instances."""
        selected_portfolio_skills = set(request.analysts) & PORTFOLIO_SKILLS
        if request.scope == "portfolio" and selected_portfolio_skills != set(request.analysts):
            raise ValueError("portfolio requests may select only portfolio-scoped skills")
        if request.scope == "instrument" and selected_portfolio_skills:
            raise ValueError("instrument requests cannot select portfolio-scoped skills")
        as_of = self._clock().astimezone(UTC).date()
        return tuple(
            configure_skill(
                skill,
                request.skill_parameters.get(skill.name, {}),
                portfolio_instruments=request.portfolio_instruments,
                as_of=as_of,
            )
            for skill in self._skills.discover(request.analysts)
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
        providers: dict[str, CapabilityProvider] = {
            name.value: provider for name, provider in self._providers.providers.items()
        }
        research = providers.get("research_factors")
        if (
            request.research_factors is None
            and "factor-regression" in request.analysts
            and isinstance(research, PackFactorProvider)
        ):
            params = FactorRegressionParameters.model_validate(
                request.skill_parameters.get("factor-regression", {})
            )
            keys = frozenset(
                f.research_key for f in params.selected_factors() if f.research_key is not None
            )
            french_keys = frozenset(
                f.research_key
                for f in params.selected_factors()
                if f.research_source != "pack"
                and f.research_key in {d.id for d in FRENCH_DEFINITIONS}
            )
            explicit_pack_keys = {
                f.research_key
                for f in params.selected_factors()
                if f.research_source == "pack" and f.research_key is not None
            }
            if french_keys & explicit_pack_keys:
                raise ValueError("Selected research keys conflict; give pack factors unique IDs")
            pack_keys = keys - french_keys
            if french_keys or params.return_mode == "excess_return":
                sources: list[tuple[ResearchFactorProvider, frozenset[str]]] = [
                    (FrenchFactorProvider(), french_keys or frozenset({"market_excess"}))
                ]
                if pack_keys:
                    sources.append((research.selected(pack_keys), pack_keys))
                providers["research_factors"] = CompositeResearchFactorProvider(tuple(sources))
            elif pack_keys:
                providers["research_factors"] = research.selected(pack_keys)
        if request.research_factors is not None:
            providers["research_factors"] = InlineResearchFactorProvider(request.research_factors)
        if request.fx_series:
            providers["fx"] = InlineFxProvider(request.fx_series)
        if request.factor_series:
            providers[CapabilityName.FACTOR_RETURNS.value] = InlineReturnProvider(
                request.factor_series
            )
        if request.price_series:
            providers[CapabilityName.PRICES.value] = InlinePriceProvider(request.price_series)
        if request.outcome_series:
            providers[CapabilityName.OUTCOMES.value] = InlineOutcomeProvider(request.outcome_series)
        return ProviderRegistry(providers)


def _compose_providers(settings: Settings) -> dict[str, CapabilityProvider]:
    providers: dict[str, CapabilityProvider] = {"research_factors": FrenchFactorProvider()}
    if settings.price_provider in {"local_csv", "local_parquet"}:
        assert settings.price_path is not None
        providers["prices"] = LocalCsvParquetPriceProvider(settings.price_path)
    elif settings.price_provider == "local_sql":
        assert settings.price_path is not None
        providers["prices"] = ReadOnlySqlPriceProvider(settings.price_path)
    elif settings.price_provider == "yahoo":
        providers["prices"] = YahooPriceProvider()
        providers["factor_returns"] = YahooReturnProvider()
        providers["fx"] = YahooFxProvider()
    elif settings.price_provider == "ccxt":
        assert settings.ccxt_exchange is not None
        providers["prices"] = CcxtPriceProvider(settings.ccxt_exchange)
    elif settings.price_provider == "bloomberg":
        from trade_research.providers.remote import BloombergPriceProvider

        providers["prices"] = BloombergPriceProvider(
            host=settings.bloomberg_host,
            port=settings.bloomberg_port,
        )

        if settings.bloomberg_return_mappings or settings.bloomberg_factor_pack:
            return_provider = BloombergReturnProvider(
                settings.bloomberg_return_mappings,
                host=settings.bloomberg_host,
                port=settings.bloomberg_port,
            )
            providers["factor_returns"] = return_provider
            providers["fx"] = BloombergFxProvider(return_provider)
            if settings.bloomberg_factor_pack and settings.bloomberg_factor_pack.level_factors:
                providers["research_factors"] = PackFactorProvider(
                    settings.bloomberg_factor_pack, return_provider
                )

    if settings.fundamental_provider in {"local_csv", "local_parquet"}:
        assert settings.fundamental_path is not None
        providers["fundamentals"] = LocalCsvParquetFundamentalProvider(settings.fundamental_path)
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
