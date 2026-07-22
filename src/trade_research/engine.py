"""Async coordination of immutable analyst skills."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast

from trade_research.domain import AnalysisRequest, AnalystResult, ResearchReport
from trade_research.providers import ProviderRegistry
from trade_research.skills import (
    FundamentalSkill,
    ResearchReviewer,
    ResearchSkill,
    SkillRegistry,
    TechnicalSkill,
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
        settings: object | None = None,
        *,
        skills: SkillRegistry | None = None,
        providers: ProviderRegistry | Mapping[str, object] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> ResearchEngine:
        """Compose safe defaults while permitting typed dependency injection.

        ``settings`` is deliberately not interpreted as research evidence. It is
        accepted for a stable composition-root API; callers inject registries via
        explicit keyword arguments.
        """

        del settings
        default_skills = cast(tuple[ResearchSkill, ...], (FundamentalSkill(), TechnicalSkill()))
        selected_skills = skills or SkillRegistry(default_skills)
        if providers is None:
            selected_providers = ProviderRegistry({})
        elif isinstance(providers, ProviderRegistry):
            selected_providers = providers
        else:
            selected_providers = ProviderRegistry(providers)
        return cls(selected_skills, selected_providers, clock=clock)

    @property
    def skills(self) -> SkillRegistry:
        return self._skills

    async def analyze(self, request: AnalysisRequest) -> ResearchReport:
        """Analyze one request without allowing evidence to affect selection."""

        selected = self._skills.discover(request.analysts)
        outcomes = await asyncio.gather(
            *(self._run_skill(skill, request) for skill in selected),
            return_exceptions=True,
        )
        results: list[AnalystResult] = []
        for skill, outcome in zip(selected, outcomes, strict=True):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                results.append(
                    AnalystResult(
                        analyst=skill.name,
                        instrument=request.instrument,
                        summary=f"partial data: analyst failed ({type(outcome).__name__})",
                    )
                )
            else:
                results.append(outcome)
        return ResearchReport(
            request_id=request.request_id,
            instrument=request.instrument,
            results=self._reviewer.review(results),
            generated_at=self._clock(),
        )

    async def _run_skill(self, skill: ResearchSkill, request: AnalysisRequest) -> AnalystResult:
        return await asyncio.to_thread(skill.analyze, request.instrument, self._providers)
