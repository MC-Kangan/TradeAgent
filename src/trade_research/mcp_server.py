"""Official MCP SDK adapter with an exact, bounded tool surface."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ContentBlock

from trade_research.application import ResearchApplication
from trade_research.domain import AnalysisRequest
from trade_research.reporting import ReportFormat, normalize_report_format

BOUNDED_TOOL_NAMES = (
    "list_skills",
    "describe_skill",
    "run_skill",
    "start_research",
    "get_research_status",
    "get_research_result",
    "compile_report",
    "list_reports",
    "get_report",
)


class BoundedFastMCP(FastMCP):
    """FastMCP boundary that never exposes rejected tool inputs or exception values."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        try:
            result = await super().call_tool(name, arguments)
        except Exception:
            pass
        else:
            return result
        raise ToolError("tool request rejected") from None


class BoundedResearchTools:
    """Directly testable implementations registered as MCP tools."""

    def __init__(self, application: ResearchApplication) -> None:
        self._application = application

    def list_skills(self) -> list[dict[str, Any]]:
        return self._application.list_skills()

    def describe_skill(self, name: str) -> dict[str, Any]:
        return self._application.describe_skill(name)

    async def run_skill(self, name: str, request: AnalysisRequest) -> dict[str, Any]:
        return await self._application.run_skill(name, request)

    async def start_research(self, request: AnalysisRequest) -> dict[str, Any]:
        return await self._application.start_research(request)

    def get_research_status(self, request_id: str) -> dict[str, Any]:
        return self._application.get_research_status(request_id)

    def get_research_result(self, request_id: str) -> dict[str, Any]:
        return self._application.get_research_result(request_id)

    def compile_report(
        self,
        request_id: str,
        format_name: ReportFormat = ReportFormat.MARKDOWN,
    ) -> dict[str, str]:
        format_name = normalize_report_format(format_name)
        return {
            "format": format_name.value,
            "content": self._application.compile_report(request_id, format_name),
        }

    def list_reports(self) -> list[str]:
        return self._application.list_reports()

    def get_report(self, request_id: str) -> dict[str, Any]:
        return self._application.get_report(request_id)


def build_mcp_server(application: ResearchApplication) -> BoundedFastMCP:
    """Register the exact public tool tuple with the official Python SDK."""

    tools = BoundedResearchTools(application)
    server = BoundedFastMCP(
        "trade-research",
        instructions="Bounded research and report retrieval only.",
        log_level="ERROR",
    )
    for name in BOUNDED_TOOL_NAMES:
        server.tool(name=name)(getattr(tools, name))
    return server


def run_mcp_server(application: ResearchApplication) -> None:
    build_mcp_server(application).run(transport="stdio")
