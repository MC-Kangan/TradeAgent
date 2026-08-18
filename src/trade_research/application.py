"""Shared bounded application service used by every transport."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from trade_research.domain import AnalysisRequest, ResearchReport
from trade_research.engine import ResearchEngine
from trade_research.queue import JobQueue
from trade_research.reporting import (
    ReportFormat,
    ReportStore,
    normalize_report_format,
    render_json,
    render_markdown,
    sanitize_report,
)
from trade_research.skills.parameters import (
    PORTFOLIO_SKILLS,
    SKILL_PARAMETER_SCHEMAS,
    configure_skill,
)

JsonObject = dict[str, Any]


class ResearchApplication:
    """Expose research use cases without transport or execution capabilities."""

    def __init__(
        self,
        engine: ResearchEngine,
        reports: ReportStore,
        queue: JobQueue | None = None,
    ) -> None:
        self.engine = engine
        self.reports = reports
        self.queue = queue

    def list_skills(self) -> list[JsonObject]:
        return [self.describe_skill(name) for name in self.engine.skills.names]

    def describe_skill(self, name: str) -> JsonObject:
        skill = self.engine.skills.require(name)
        missing_capabilities = self.engine.missing_service_capabilities(name)
        short_descriptions = {
            "fundamental": (
                "SEC-backed growth, profitability, cash-flow, leverage, "
                "and valuation factors."
            ),
            "filings": "SEC filing activity and reporting-recency checks.",
            "worth-buy-stocks": "Trend, relative strength, and risk checks.",
            "markov-method": "Bull, Bear, and Sideways regime detection.",
            "technical-basic": "EMA, ADX, RSI, Bollinger, OBV, and volume confirmation.",
            "risk-analysis": "Historical volatility, tail loss, drawdown, and return shape.",
            "volatility-regime": "Realized-volatility percentile and expansion state.",
            "correlation-analysis": "Aligned return correlations and diversification structure.",
            "asset-allocation": "Long-only price-derived allocation scenarios.",
            "backtesting": (
                "Daily fractional-tranche simulation for built-in or timestamped signals."
            ),
        }
        supported_asset_types = {
            "fundamental": ["equity"],
            "filings": ["equity"],
            "worth-buy-stocks": ["equity"],
            "technical": ["equity", "crypto"],
            "markov-method": ["equity", "crypto"],
            "technical-basic": ["equity", "crypto"],
            "risk-analysis": ["equity", "crypto"],
            "volatility-regime": ["equity", "crypto"],
            "correlation-analysis": ["equity", "crypto"],
            "asset-allocation": ["equity", "crypto"],
            "backtesting": ["equity", "crypto"],
        }
        scope = (
            "portfolio"
            if skill.name in {"correlation-analysis", "asset-allocation"}
            else "instrument"
        )
        return {
            "name": skill.name,
            "description": short_descriptions.get(
                skill.name,
                (type(skill).__doc__ or "Research analyst skill").strip(),
            ),
            "immutable": True,
            "parameters": SKILL_PARAMETER_SCHEMAS.get(name),
            "supported_asset_types": supported_asset_types.get(name, ["equity"]),
            "scope": scope,
            "available": not missing_capabilities,
            "missing_capabilities": [item.value for item in missing_capabilities],
        }

    async def run_skill(self, name: str, request: AnalysisRequest) -> JsonObject:
        if (name in PORTFOLIO_SKILLS) != (request.scope == "portfolio"):
            raise ValueError(
                f"Skill '{name}' does not support {request.scope}-scoped requests"
            )
        base_skill = self.engine.skills.require(name)
        params = request.skill_parameters.get(name, {})
        configured = configure_skill(
            base_skill,
            params,
            portfolio_instruments=request.portfolio_instruments,
        )
        result = await self.engine.run_configured_skill(configured, request)
        wrapped = ResearchReport(
            request_id=request.request_id,
            instrument=request.instrument,
            results=(result,),
            generated_at=self.engine.clock(),
        )
        sanitized = sanitize_report(wrapped)
        self.reports.save(sanitized)
        return _json_object(render_json(sanitized))

    async def research(self, request: AnalysisRequest) -> JsonObject:
        report = await self.engine.analyze(request)
        self.reports.save(report)
        return _json_object(render_json(report))

    async def start_research(self, request: AnalysisRequest) -> JsonObject:
        """Persist a safe queued request for a separately supervised worker."""

        if self.queue is None:
            raise RuntimeError("a job queue is required to start durable research")
        if request.price_series or "backtesting" in request.analysts:
            raise ValueError(
                "inline price series and backtesting are immediate-only; use run_skill or research"
            )
        self.engine.configure_request(request)
        self.engine.validate_analysts(request.analysts)
        submission = self.queue.enqueue(request)
        return {"request_id": str(submission.request_id), "status": submission.status}

    def get_research_status(self, request_id: str | UUID) -> JsonObject:
        try:
            identifier = UUID(str(request_id))
        except ValueError:
            return {"request_id": str(request_id), "status": "not_found"}
        if self.queue is not None:
            try:
                job = self.queue.get(identifier)
            except KeyError:
                pass
            else:
                return {"request_id": str(request_id), "status": job.status}
        try:
            self.reports.get(str(request_id))
        except KeyError:
            return {"request_id": str(request_id), "status": "not_found"}
        return {"request_id": str(request_id), "status": "succeeded"}

    def get_research_result(self, request_id: str | UUID) -> JsonObject:
        if self.queue is not None:
            try:
                job = self.queue.get(UUID(str(request_id)))
            except KeyError:
                pass
            else:
                if job.result is None:
                    raise KeyError(f"research result is not available: {request_id}")
                self.reports.save(job.result)
                return _json_object(render_json(job.result))
        return _json_object(render_json(self.reports.get(str(request_id))))

    def compile_report(
        self,
        request_id: str | UUID,
        format_name: ReportFormat = ReportFormat.MARKDOWN,
    ) -> str:
        format_name = normalize_report_format(format_name)
        try:
            return self.reports.render(str(request_id), format_name)
        except KeyError:
            report = self._queued_report(request_id)
            if format_name is ReportFormat.MARKDOWN:
                return render_markdown(report)
            return render_json(report)

    def list_reports(self) -> list[str]:
        identifiers = set(self.reports.list_reports())
        if self.queue is not None:
            identifiers.update(str(item) for item in self.queue.list_succeeded())
        return sorted(identifiers)

    def get_report(self, request_id: str | UUID) -> JsonObject:
        return self.get_research_result(request_id)

    def _queued_report(self, request_id: str | UUID) -> ResearchReport:
        if self.queue is None:
            raise KeyError(f"unknown report: {request_id}")
        job = self.queue.get(UUID(str(request_id)))
        if job.result is None:
            raise KeyError(f"report is not available: {request_id}")
        return job.result


def _json_object(payload: str) -> JsonObject:
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise TypeError("report payload must be a JSON object")
    return parsed
