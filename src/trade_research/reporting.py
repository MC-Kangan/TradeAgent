"""Deterministic Markdown and JSON reporting through a closed export schema."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from itertools import islice
from pathlib import Path
from typing import Final
from uuid import UUID

from trade_research.domain import (
    AnalysisMethod,
    AnalystResult,
    Citation,
    FailureCategory,
    InstrumentId,
    LimitationKind,
    Observation,
    ProviderKind,
    ReportStatus,
    ResearchReport,
)
from trade_research.domain.provenance import normalize_provider_kind
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
            _project_result(result, safe_instrument)
            for result in islice(report.results, MAX_EXPORT_RESULTS)
        ),
        generated_at=report.generated_at,
    )


def _project_result(result: AnalystResult, instrument: InstrumentId) -> AnalystResult:
    observations = tuple(
        _project_observation(item, instrument)
        for item in islice(result.observations, MAX_EXPORT_OBSERVATIONS)
    )
    if result.status is ReportStatus.FAILED:
        status = ReportStatus.FAILED
    elif result.status is ReportStatus.PARTIAL or not observations:
        status = ReportStatus.PARTIAL
    else:
        status = ReportStatus.COMPLETE
    failure_category = result.failure_category
    if status is ReportStatus.PARTIAL and failure_category is FailureCategory.NONE:
        failure_category = FailureCategory.INSUFFICIENT_DATA
    limitations = list(result.limitations)
    if not observations and LimitationKind.MISSING_INPUTS not in limitations:
        limitations.append(LimitationKind.MISSING_INPUTS)
    if result.evidence and LimitationKind.EVIDENCE_OMITTED not in limitations:
        limitations.append(LimitationKind.EVIDENCE_OMITTED)
    methods = list(result.methods)
    for observation in observations:
        algorithm = observation.provenance.get("algorithm")
        window = observation.provenance.get("window")
        if isinstance(algorithm, str) and isinstance(window, str):
            method = AnalysisMethod(algorithm=algorithm, window=window)
            if method not in methods:
                methods.append(method)
    citations = list(result.citations)
    for observation in observations:
        series_citation = _series_citation(observation)
        if series_citation is not None and series_citation not in citations:
            citations.append(series_citation)
        for citation in _input_citations(observation):
            if citation not in citations:
                citations.append(citation)
            if len(citations) >= MAX_EXPORT_EVIDENCE:
                break
        if len(citations) >= MAX_EXPORT_EVIDENCE:
            break
    for item in islice(result.evidence, MAX_EXPORT_EVIDENCE):
        try:
            provider = normalize_provider_kind(item.source)
        except ValueError:
            provider = ProviderKind.UNTRUSTED
        citation = Citation(
            provider=provider,
            reference=f"sha256:{hashlib.sha256(item.content.encode()).hexdigest()}",
            collected_at=item.collected_at,
        )
        if citation not in citations:
            citations.append(citation)
    return AnalystResult(
        analyst=result.analyst,
        instrument=instrument,
        summary=(
            f"{result.analyst} analysis {status.value} with "
            f"{len(observations)} numeric factors"
        ),
        status=status,
        missing_metrics=result.missing_metrics,
        failure_category=failure_category,
        limitations=tuple(limitations),
        methods=tuple(methods),
        inference=result.inference,
        signal=result.signal,
        citations=tuple(islice(citations, MAX_EXPORT_EVIDENCE)),
        observations=observations,
        evidence=(),
    )


def _series_citation(observation: Observation) -> Citation | None:
    provider_value = observation.provenance.get("input_provider_kind")
    reference = observation.provenance.get("series_ref")
    if not isinstance(reference, str):
        return None
    try:
        return Citation(
            provider=normalize_provider_kind(provider_value),
            reference=reference,
            collected_at=observation.observed_at,
        )
    except (TypeError, ValueError):
        return None


def _input_citations(observation: Observation) -> tuple[Citation, ...]:
    inputs = observation.provenance.get("inputs")
    if not isinstance(inputs, list):
        return ()
    citations: list[Citation] = []
    for item in islice(inputs, MAX_EXPORT_EVIDENCE):
        if not isinstance(item, Mapping):
            continue
        provider_reference = item.get("provider_reference")
        if not isinstance(provider_reference, Mapping):
            continue
        provider_value = provider_reference.get("provider_kind")
        if item.get("provider_kind") != provider_value:
            continue
        reference = provider_reference.get("reference")
        observed_at = item.get("observed_at")
        if not isinstance(reference, str) or not isinstance(observed_at, str):
            continue
        try:
            provider = normalize_provider_kind(provider_value)
            collected_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            citation = Citation(
                provider=provider,
                reference=reference,
                collected_at=collected_at,
            )
        except (TypeError, ValueError):
            continue
        if citation not in citations:
            citations.append(citation)
    return tuple(citations)


def _project_observation(observation: Observation, instrument: InstrumentId) -> Observation:
    return Observation(
        instrument=instrument,
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
        lines.extend(
            (
                "",
                f"## {result.analyst}",
                "",
                result.summary,
                "",
                f"- Status: {result.status.value}",
                f"- Failure category: {result.failure_category.value}",
                f"- Inference: {result.inference.value}",
                f"- Signal: {result.signal.value}",
                "- Missing metrics: "
                + (", ".join(item.value for item in result.missing_metrics) or "none"),
                "- Limitations: "
                + (", ".join(item.value for item in result.limitations) or "none"),
            )
        )
        if result.observations:
            lines.extend(
                (
                    "",
                    "| Metric | Value | As of | Source | Method | Window | Reference |",
                    "| --- | ---: | --- | --- | --- | --- | --- |",
                )
            )
            for observation in result.observations:
                value = json.dumps(observation.value, sort_keys=True)
                observed_at = observation.observed_at.isoformat()
                method = observation.provenance.get("algorithm", "-")
                window = observation.provenance.get("window", "-")
                reference = next(
                    (
                        observation.provenance[key]
                        for key in ("series_ref", "reference", "snapshot_ref")
                        if key in observation.provenance
                    ),
                    "-",
                )
                lines.append(
                    f"| {observation.metric} | {value} | {observed_at} | "
                    f"{observation.source.value} | {method} | {window} | {reference} |"
                )
        if result.citations:
            lines.extend(
                (
                    "",
                    "### Citations",
                    "",
                    "| Provider | Reference | Collected |",
                    "| --- | --- | --- |",
                )
            )
            lines.extend(
                f"| {citation.provider.value} | {citation.reference} | "
                f"{citation.collected_at.isoformat()} |"
                for citation in result.citations
            )
    return "\n".join(lines) + "\n"


def _sanitize_instrument(instrument: InstrumentId) -> InstrumentId:
    return InstrumentId.model_validate(instrument.model_dump())


class ReportStore:
    """Store reports under UUID-derived filenames only, with atomic writes."""

    _COMPLETION_SUFFIX = ".complete"

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, report: ResearchReport) -> str:
        """Write JSON and Markdown atomically with a completion marker.

        Reports are written to temporary files first, flushed, then atomically
        renamed to their final paths. A completion marker is written last.
        Readers only return reports with a valid completion marker, preventing
        half-written or mismatched output after a crash.
        """
        safe = sanitize_report(report)
        identifier = str(safe.request_id)
        json_final = self._path(identifier, ".json")
        md_final = self._path(identifier, ".md")
        json_tmp = self._path(identifier, ".json.tmp")
        md_tmp = self._path(identifier, ".md.tmp")
        try:
            json_tmp.write_text(render_json(safe), encoding="utf-8")
            md_tmp.write_text(render_markdown(safe), encoding="utf-8")
            json_tmp.replace(json_final)
            md_tmp.replace(md_final)
            self._path(identifier, self._COMPLETION_SUFFIX).touch()
        finally:
            json_tmp.unlink(missing_ok=True)
            md_tmp.unlink(missing_ok=True)
        return f"report:{identifier}"

    def list_reports(self) -> tuple[str, ...]:
        identifiers: list[str] = []
        for path in self.directory.glob("*.json"):
            try:
                parsed = UUID(path.stem)
            except ValueError:
                continue
            if self._path(str(parsed), self._COMPLETION_SUFFIX).is_file():
                identifiers.append(str(parsed))
        return tuple(sorted(identifiers))

    def get(self, reference: str | UUID) -> ResearchReport:
        identifier = self._identifier(reference)
        path = self._path(identifier, ".json")
        if not path.is_file():
            raise KeyError(f"unknown report: {identifier}")
        if not self._path(identifier, self._COMPLETION_SUFFIX).is_file():
            raise KeyError(f"report incomplete or corrupted: {identifier}")
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
