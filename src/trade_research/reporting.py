"""Deterministic Markdown and JSON reporting through a closed export schema."""

from __future__ import annotations

import json
from enum import Enum
from itertools import islice
from pathlib import Path
from typing import Final
from uuid import UUID

from trade_research.domain import (
    AnalystResult,
    Evidence,
    InstrumentId,
    Observation,
    ResearchReport,
)
from trade_research.projection import project_observation_value

MAX_EXPORT_RESULTS: Final = 16
MAX_EXPORT_OBSERVATIONS: Final = 256
MAX_EXPORT_EVIDENCE: Final = 64


class ReportFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


def normalize_report_format(value: object) -> ReportFormat:
    try:
        return ReportFormat(value)
    except (TypeError, ValueError) as error:
        raise ValueError("format must be 'markdown' or 'json'") from error


def sanitize_report(report: ResearchReport) -> ResearchReport:
    """Return a report safe for rendering and durable storage."""

    safe_instrument = _sanitize_instrument(report.instrument)
    return ResearchReport(
        request_id=report.request_id,
        instrument=safe_instrument,
        results=tuple(
            _project_result(result) for result in islice(report.results, MAX_EXPORT_RESULTS)
        ),
        generated_at=report.generated_at,
    )


def _project_result(result: AnalystResult) -> AnalystResult:
    observations = tuple(
        _project_observation(item) for item in islice(result.observations, MAX_EXPORT_OBSERVATIONS)
    )
    partial_summary = isinstance(result.summary, str) and (
        result.summary.startswith("partial data")
        or result.summary.startswith(f"{result.analyst} analysis partial with ")
    )
    state = "partial" if partial_summary else "complete"
    return AnalystResult(
        analyst=result.analyst,
        instrument=_sanitize_instrument(result.instrument),
        summary=f"{result.analyst} analysis {state} with {len(observations)} observations",
        observations=observations,
        evidence=tuple(
            Evidence(
                source="untrusted",
                content="[CONTENT OMITTED]",
                collected_at=item.collected_at,
            )
            for item in islice(result.evidence, MAX_EXPORT_EVIDENCE)
        ),
    )


def _project_observation(observation: Observation) -> Observation:
    return Observation(
        instrument=_sanitize_instrument(observation.instrument),
        metric=observation.metric,
        value=project_observation_value(observation.value),
        source=observation.source,
        observed_at=observation.observed_at,
        provenance=observation.provenance,
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
    return InstrumentId.model_validate(instrument.model_dump())


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

    def render(
        self,
        reference: str | UUID,
        format_name: ReportFormat = ReportFormat.MARKDOWN,
    ) -> str:
        format_name = normalize_report_format(format_name)
        report = self.get(reference)
        if format_name is ReportFormat.MARKDOWN:
            return render_markdown(report)
        if format_name is ReportFormat.JSON:
            return render_json(report)
        raise AssertionError("unreachable report format")

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
