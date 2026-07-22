"""Deterministic, redacted Markdown and JSON research reporting."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from trade_research.domain import InstrumentId, ResearchReport
from trade_research.redaction import redact_json, redact_text


def sanitize_report(report: ResearchReport) -> ResearchReport:
    """Return a report safe for rendering and durable storage."""

    safe_instrument = _sanitize_instrument(report.instrument)
    return report.model_copy(
        update={
            "instrument": safe_instrument,
            "results": tuple(
                result.model_copy(
                    update={
                        "instrument": _sanitize_instrument(result.instrument),
                        "summary": redact_text(result.summary),
                        "observations": tuple(
                            observation.model_copy(
                                update={
                                    "instrument": _sanitize_instrument(observation.instrument),
                                    "value": redact_json(observation.value),
                                }
                            )
                            for observation in result.observations
                        ),
                        "evidence": tuple(
                            evidence.model_copy(
                                update={
                                    "source": redact_text(evidence.source),
                                    "content": redact_text(evidence.content),
                                }
                            )
                            for evidence in result.evidence
                        ),
                    }
                )
                for result in report.results
            ),
        }
    )


def render_json(report: ResearchReport) -> str:
    """Serialize with stable key ordering and no sensitive evidence text."""

    payload = sanitize_report(report).model_dump(mode="json")
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_markdown(report: ResearchReport) -> str:
    """Render a stable Markdown representation of the common report schema."""

    safe = sanitize_report(report)
    lines = [
        f"# Research report: {safe.instrument.symbol}",
        "",
        f"- Market: {safe.instrument.market}",
        f"- Request: {safe.request_id}",
        f"- Generated: {safe.generated_at.isoformat()}",
    ]
    for result in safe.results:
        lines.extend(("", f"## {result.analyst}", "", result.summary))
        if result.observations:
            lines.extend(("", "| Metric | Value | As of |", "| --- | ---: | --- |"))
            for observation in result.observations:
                value = json.dumps(observation.value, sort_keys=True)
                observed_at = observation.observed_at.isoformat()
                lines.append(f"| {observation.metric} | {value} | {observed_at} |")
        if result.evidence:
            lines.extend(("", "### Evidence"))
            for evidence in result.evidence:
                evidence_lines = evidence.content.splitlines() or [""]
                source_lines = evidence.source.splitlines() or [""]
                lines.extend(("", "> [UNTRUSTED EVIDENCE]"))
                lines.extend(f"> {line}" for line in evidence_lines)
                lines.extend(
                    f"> {'Source: ' if index == 0 else ''}{line}"
                    for index, line in enumerate(source_lines)
                )
    return "\n".join(lines) + "\n"


def _sanitize_instrument(instrument: InstrumentId) -> InstrumentId:
    return instrument.model_copy(update={"symbol": redact_text(instrument.symbol)})


class ReportStore:
    """Store reports under UUID-derived filenames only."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, report: ResearchReport) -> str:
        safe = sanitize_report(report)
        identifier = str(safe.request_id)
        self._path(identifier, ".json").write_text(render_json(safe), encoding="utf-8")
        self._path(identifier, ".md").write_text(render_markdown(safe), encoding="utf-8")
        return f"report:{identifier}"

    def list_reports(self) -> tuple[str, ...]:
        identifiers: list[str] = []
        for path in self.directory.glob("*.json"):
            try:
                identifiers.append(str(UUID(path.stem)))
            except ValueError:
                continue
        return tuple(sorted(identifiers))

    def get(self, reference: str | UUID) -> ResearchReport:
        identifier = self._identifier(reference)
        path = self._path(identifier, ".json")
        if not path.is_file():
            raise KeyError(f"unknown report: {identifier}")
        return ResearchReport.model_validate_json(path.read_text(encoding="utf-8"))

    def render(self, reference: str | UUID, format_name: str = "markdown") -> str:
        report = self.get(reference)
        if format_name == "markdown":
            return render_markdown(report)
        if format_name == "json":
            return render_json(report)
        raise ValueError("format must be 'markdown' or 'json'")

    def _path(self, identifier: str, suffix: str) -> Path:
        return self.directory / f"{self._identifier(identifier)}{suffix}"

    @staticmethod
    def _identifier(reference: str | UUID) -> str:
        value = str(reference)
        if value.startswith("report:"):
            value = value.removeprefix("report:")
        try:
            return str(UUID(value))
        except ValueError as error:
            raise ValueError("report reference must contain a valid UUID") from error
