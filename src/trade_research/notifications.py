"""Redacted one-way Discord notification adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

import httpx

from trade_research.domain import ResearchReport
from trade_research.reporting import sanitize_report

NotificationSender = Callable[[str, dict[str, str]], Awaitable[None]]


class DiscordNotifier:
    """Send only analyst summaries and an opaque report reference."""

    def __init__(self, webhook_url: str, *, sender: NotificationSender | None = None) -> None:
        self._webhook_url = webhook_url
        self._sender = sender or self._post

    async def notify(self, report: ResearchReport, report_reference: str) -> None:
        safe = sanitize_report(report)
        reference = _safe_report_reference(report_reference)
        content = f"Research report ready: {safe.instrument.symbol} | {reference}"
        await self._sender(self._webhook_url, {"content": content})

    @staticmethod
    async def _post(target: str, payload: dict[str, str]) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(target, json=payload)
            response.raise_for_status()


def _safe_report_reference(reference: str) -> str:
    prefix = "report:"
    if not reference.startswith(prefix):
        raise ValueError("notification report reference must be a report UUID")
    try:
        identifier = UUID(reference.removeprefix(prefix))
    except ValueError as error:
        raise ValueError("notification report reference must be a report UUID") from error
    return f"{prefix}{identifier}"
