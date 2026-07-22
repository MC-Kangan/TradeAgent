"""Redacted one-way Discord notification adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

from trade_research.domain import ResearchReport
from trade_research.redaction import redact_text

NotificationSender = Callable[[str, dict[str, str]], Awaitable[None]]


class DiscordNotifier:
    """Send only analyst summaries and an opaque report reference."""

    def __init__(self, webhook_url: str, *, sender: NotificationSender | None = None) -> None:
        self._webhook_url = webhook_url
        self._sender = sender or self._post

    async def notify(self, report: ResearchReport, report_reference: str) -> None:
        summaries = "; ".join(redact_text(result.summary) for result in report.results)
        content = redact_text(
            f"Research {report.instrument.symbol}: {summaries} | {report_reference}"
        )
        await self._sender(self._webhook_url, {"content": content})

    @staticmethod
    async def _post(target: str, payload: dict[str, str]) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(target, json=payload)
            response.raise_for_status()
