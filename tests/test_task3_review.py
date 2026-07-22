from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from trade_research.application import ResearchApplication
from trade_research.cli import app as cli_app
from trade_research.domain import (
    AnalysisRequest,
    AnalystResult,
    Evidence,
    InstrumentId,
    Observation,
    ResearchReport,
)
from trade_research.engine import ResearchEngine
from trade_research.http import create_app
from trade_research.mcp_server import build_mcp_server
from trade_research.notifications import DiscordNotifier
from trade_research.providers import CapabilityName, ProviderRegistry
from trade_research.queue import JobQueue, LostLease, RequestConflict
from trade_research.reporting import ReportStore, render_json, render_markdown
from trade_research.skills import SkillRegistry
from trade_research.storage import RunStore


@dataclass(frozen=True)
class FixedSkill:
    name: str = "fundamental"
    required_capabilities: tuple[CapabilityName, ...] = ()

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary="complete",
        )


class CountingMapping(Mapping[str, object]):
    def __init__(self, size: int) -> None:
        self.size = size
        self.iterations = 0

    def __getitem__(self, key: str) -> object:
        del key
        return "unknown narrative"

    def __iter__(self) -> Iterator[str]:
        for index in range(self.size):
            self.iterations += 1
            yield f"unknown-{index}"

    def __len__(self) -> int:
        return self.size


def _engine() -> ResearchEngine:
    return ResearchEngine.from_settings(
        skills=SkillRegistry((FixedSkill(),)),
        providers=ProviderRegistry({}),
        clock=lambda: datetime(2026, 7, 22, tzinfo=UTC),
    )


def _request(symbol: str = "ACME", *, request_id: object | None = None) -> AnalysisRequest:
    kwargs: dict[str, object] = {
        "instrument": InstrumentId(symbol=symbol, market="US"),
        "analysts": ("fundamental",),
    }
    if request_id is not None:
        kwargs["request_id"] = request_id
    return AnalysisRequest.model_validate(kwargs)


def _report(request: AnalysisRequest, *, result: AnalystResult | None = None) -> ResearchReport:
    return ResearchReport(
        request_id=request.request_id,
        instrument=request.instrument,
        results=(result or FixedSkill().analyze(request.instrument, ProviderRegistry({})),),
        generated_at=datetime(2026, 7, 22, tzinfo=UTC),
    )


def test_claim_next_reaps_expired_lease_and_fences_stale_workers(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite3", recovery_after_seconds=0.01)
    request = _request()
    queue.enqueue(request)
    stale = queue.claim_next()
    assert stale is not None and stale.claim_token is not None

    time.sleep(0.02)
    current = queue.claim_next()

    assert current is not None and current.claim_token is not None
    assert current.claim_token != stale.claim_token
    assert current.generation == stale.generation + 1
    with pytest.raises(LostLease):
        queue.complete(request.request_id, _report(request), stale.claim_token)
    queue.complete(request.request_id, _report(request), current.claim_token)
    with pytest.raises(LostLease):
        queue.fail(request.request_id, RuntimeError("late secret"), stale.claim_token)
    assert queue.get(request.request_id).status == "succeeded"


@pytest.mark.parametrize(
    "symbol",
    [
        "API_KEY=QUEUE-SECRET",
        "accountId",
        "acct-123",
        "userAcctId",
        "203.0.113.9",
        "2001:db8::1",
        '{"position":{"quantity":900}}',
        "position: ACME 900 shares",
        "ABC DEF",
        "ABC\nDEF",
        "A" * 33,
    ],
)
def test_symbol_boundary_rejects_sensitive_or_non_symbol_values(
    tmp_path: Path, symbol: str
) -> None:
    with pytest.raises(ValidationError):
        InstrumentId(symbol=symbol, market="US")

    unsafe_instrument = InstrumentId.model_construct(symbol=symbol, market="US")
    unsafe_request = AnalysisRequest.model_construct(
        request_id=uuid4(),
        instrument=unsafe_instrument,
        analysts=("fundamental",),
        metadata={},
        positions=(),
    )
    with pytest.raises(ValidationError):
        RunStore(tmp_path / "runs.sqlite3").save_request(unsafe_request)
    with pytest.raises(ValidationError):
        JobQueue(tmp_path / "jobs.sqlite3").enqueue(unsafe_request)


@pytest.mark.parametrize("symbol", ["BRK.B", "VOD.L", "BTC/USD", "ETH-USD", "^SPX"])
def test_symbol_boundary_preserves_valid_exchange_and_crypto_symbols(symbol: str) -> None:
    assert InstrumentId(symbol=symbol, market="CRYPTO" if "/" in symbol else "US").symbol == symbol


@pytest.mark.parametrize(
    "analysts",
    [
        ("fundamental", "fundamental"),
        tuple(f"analyst-{index}" for index in range(17)),
    ],
)
def test_analysts_are_a_unique_bounded_subset(analysts: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        AnalysisRequest(instrument=InstrumentId(symbol="ACME", market="US"), analysts=analysts)


@pytest.mark.asyncio
async def test_engine_revalidates_constructed_duplicate_analysts() -> None:
    request = AnalysisRequest.model_construct(
        request_id=uuid4(),
        instrument=InstrumentId(symbol="ACME", market="US"),
        analysts=("fundamental", "fundamental"),
        metadata={},
        positions=(),
    )

    with pytest.raises(ValidationError, match="unique"):
        await _engine().analyze(request)


def test_duplicate_analysts_fail_http_cli_mcp_and_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    application = ResearchApplication(_engine(), ReportStore(tmp_path / "reports"), queue)
    client = TestClient(create_app(application, bearer_token="token"))
    payload = {
        "instrument": {"symbol": "ACME", "market": "US"},
        "analysts": ["fundamental", "fundamental"],
    }
    assert (
        client.post(
            "/research", json=payload, headers={"Authorization": "Bearer token"}
        ).status_code
        == 422
    )

    import trade_research.cli as cli_module

    monkeypatch.setattr(cli_module, "get_application", lambda: application)
    cli_result = CliRunner().invoke(
        cli_app,
        [
            "research",
            "ACME",
            "--analyst",
            "fundamental",
            "--analyst",
            "fundamental",
        ],
    )
    assert cli_result.exit_code != 0

    unsafe = AnalysisRequest.model_construct(
        request_id=uuid4(),
        instrument=InstrumentId(symbol="ACME", market="US"),
        analysts=("fundamental", "fundamental"),
        metadata={},
        positions=(),
    )
    with pytest.raises(ValidationError, match="unique"):
        RunStore(tmp_path / "runs.sqlite3").save_request(unsafe)


@pytest.mark.asyncio
async def test_duplicate_analysts_fail_mcp_validation(tmp_path: Path) -> None:
    application = ResearchApplication(
        _engine(), ReportStore(tmp_path / "reports"), JobQueue(tmp_path / "jobs.sqlite3")
    )
    with pytest.raises(Exception) as captured:
        await build_mcp_server(application).call_tool(
            "start_research",
            {
                "request": {
                    "instrument": {"symbol": "ACME", "market": "US"},
                    "analysts": ["fundamental", "fundamental"],
                }
            },
        )
    assert str(captured.value) == "tool request rejected"


@pytest.mark.asyncio
async def test_duplicate_request_id_is_idempotent_only_for_identical_safe_request(
    tmp_path: Path,
) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    request_id = uuid4()
    original = _request("ACME", request_id=request_id)

    first = queue.enqueue(original)
    claimed = queue.claim_next()
    assert claimed is not None
    replay = queue.enqueue(original)

    assert first.status == "queued"
    assert replay.status == "running"
    with pytest.raises(RequestConflict):
        queue.enqueue(_request("OTHER", request_id=request_id))

    application = ResearchApplication(_engine(), ReportStore(tmp_path / "reports"), queue)
    assert (await application.start_research(original))["status"] == "running"


@pytest.mark.parametrize("terminal_status", ["queued", "running", "succeeded", "failed"])
def test_identical_replay_returns_each_actual_job_status(
    tmp_path: Path, terminal_status: str
) -> None:
    queue = JobQueue(tmp_path / f"{terminal_status}.sqlite3")
    request = _request()
    queue.enqueue(request)
    if terminal_status != "queued":
        claimed = queue.claim_next()
        assert claimed is not None and claimed.claim_token is not None
        if terminal_status == "succeeded":
            queue.complete(request.request_id, _report(request), claimed.claim_token)
        elif terminal_status == "failed":
            queue.fail(request.request_id, RuntimeError("bounded"), claimed.claim_token)

    assert queue.enqueue(request).status == terminal_status


def test_http_maps_request_conflict_to_409(tmp_path: Path) -> None:
    request_id = uuid4()
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(_request("ACME", request_id=request_id))
    application = ResearchApplication(_engine(), ReportStore(tmp_path / "reports"), queue)
    client = TestClient(create_app(application, bearer_token="token"))

    response = client.post(
        "/research",
        json={
            "request_id": str(request_id),
            "instrument": {"symbol": "OTHER", "market": "US"},
            "analysts": ["fundamental"],
        },
        headers={"Authorization": "Bearer token"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "request id conflicts with an existing request"}


def test_cli_reports_bounded_duplicate_request_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = uuid4()
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(_request("ACME", request_id=request_id))
    application = ResearchApplication(_engine(), ReportStore(tmp_path / "reports"), queue)
    import trade_research.cli as cli_module

    monkeypatch.setattr(cli_module, "get_application", lambda: application)
    result = CliRunner().invoke(
        cli_app,
        [
            "research",
            "OTHER",
            "--market",
            "US",
            "--analyst",
            "fundamental",
            "--request-id",
            str(request_id),
        ],
    )

    assert result.exit_code != 0
    assert "request id conflicts with an existing request" in result.output
    assert "ACME" not in result.output


@pytest.mark.asyncio
async def test_mcp_conflict_error_is_bounded(tmp_path: Path) -> None:
    request_id = uuid4()
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(_request("ACME", request_id=request_id))
    application = ResearchApplication(_engine(), ReportStore(tmp_path / "reports"), queue)
    server = build_mcp_server(application)

    with pytest.raises(Exception) as captured:
        await server.call_tool(
            "start_research",
            {
                "request": {
                    "request_id": str(request_id),
                    "instrument": {"symbol": "OTHER", "market": "US"},
                    "analysts": ["fundamental"],
                }
            },
        )

    assert str(captured.value) == "tool request rejected"
    assert "ACME" not in str(captured.value)


@pytest.mark.asyncio
async def test_closed_output_projection_drops_sensitive_families_everywhere(
    tmp_path: Path,
) -> None:
    request = _request()
    observation = Observation(
        instrument=request.instrument,
        metric="close",
        value={
            "score": 42,
            "position": {"quantity": 900, "average_cost": 2},
            "positions": ["ACME 900"],
            "portfolio": {"value": 1},
            "holding": "ACME",
            "holdings": ["ACME"],
            "exposure": 0.9,
            "accountId": "acct-123",
            "acctId": "acct-direct-789",
            "account_number": "999",
            "brokerAccountId": "broker-acct-456",
            "quantity": 900,
            "averageCost": 2,
            "weight": 0.9,
            "note": "client address 2001:db8::1",
            "payload": ["long ACME 900 @ 2", "short ACME 300 at 4.25"],
            "203.0.113.8": "address-key",
            "nested": {"clientIp": "203.0.113.8", "safe": True},
        },
        source="fixture",
        observed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    result = AnalystResult(
        analyst="fundamental",
        instrument=request.instrument,
        summary="position: ACME 900 shares at 2; account acct-123",
        observations=(observation,),
        evidence=(
            Evidence(
                source="fixture",
                content="qty 900 ACME avg cost 2",
                collected_at=datetime(2026, 7, 22, tzinfo=UTC),
            ),
        ),
    )
    account_result = AnalystResult(
        analyst="technical",
        instrument=request.instrument,
        summary=(
            "brokerAccountId=broker-acct-456 account_number=999; "
            "acct_id=acct-direct-789; acct id is acct-natural-1"
        ),
    )
    address_result = AnalystResult(
        analyst="address-review",
        instrument=request.instrument,
        summary="client address 2001:db8::1",
    )
    position_result = AnalystResult(
        analyst="position-review",
        instrument=request.instrument,
        summary="short ACME 300 at 4.25",
    )
    report = _report(request, result=result).model_copy(
        update={"results": (result, account_result, address_result, position_result)}
    )
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    submission = queue.enqueue(request)
    claimed = queue.claim_next()
    assert claimed is not None and claimed.claim_token is not None
    queue.complete(request.request_id, report, claimed.claim_token)
    store = ReportStore(tmp_path / "reports")
    store.save(report)

    sent: list[dict[str, str]] = []

    async def sender(target: str, payload: dict[str, str]) -> None:
        del target
        sent.append(payload)

    await DiscordNotifier("https://discord.test/webhook", sender=sender).notify(
        report, f"report:{request.request_id}"
    )

    outputs = (
        render_json(report),
        render_markdown(report),
        queue.get(submission.request_id).result.model_dump_json(),  # type: ignore[union-attr]
        store.get(str(request.request_id)).model_dump_json(),
        json.dumps(sent[0]),
    )
    forbidden = (
        "quantity",
        "average_cost",
        "ACME 900",
        "acct-123",
        "broker-acct-456",
        "acct-direct-789",
        "acct-natural-1",
        "203.0.113.8",
        "2001:db8::1",
        "long ACME 900",
        "short ACME 300",
        "qty 900 ACME",
        '"position"',
        '"holdings"',
        '"exposure"',
        '"accountId"',
        '"averageCost"',
        '"weight"',
    )
    for output in outputs:
        for value in forbidden:
            assert value not in output
    assert '"score": 42' in outputs[0]


@pytest.mark.asyncio
async def test_semantic_projection_blocks_credentials_and_shorthand_positions(
    tmp_path: Path,
) -> None:
    request = _request()
    semantic_position = Observation(
        instrument=request.instrument,
        metric="close",
        value={
            "note": "api key is sk-live-12345678",
            "label": "ACME",
            "value": 900,
            "unit": "shares",
        },
        source="fixture",
        observed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    lowercase_position = Observation(
        instrument=request.instrument,
        metric="close",
        value={"label": "acme", "value": 900, "unit": "shares"},
        source="fixture",
        observed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    string_quantity_position = Observation(
        instrument=request.instrument,
        metric="close",
        value={"label": "ACME", "value": "900", "unit": "shares"},
        source="fixture",
        observed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    safe_factor = Observation(
        instrument=request.instrument,
        metric="revenue_growth",
        value={"metric": "revenue_growth", "value": 0.125, "unit": "ratio"},
        source="fixture",
        observed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    results = (
        AnalystResult(
            analyst="credential",
            instrument=request.instrument,
            summary="api key is sk-live-12345678",
            evidence=(
                Evidence(
                    source="fixture",
                    content="password is hunter2-secret",
                    collected_at=datetime(2026, 7, 22, tzinfo=UTC),
                ),
            ),
        ),
        AnalystResult(
            analyst="shorthand",
            instrument=request.instrument,
            summary="ACME 900 @ 2",
        ),
        AnalystResult(
            analyst="ownership",
            instrument=request.instrument,
            summary="owned 900 ACME at average cost 2",
        ),
        AnalystResult(
            analyst="factor",
            instrument=request.instrument,
            summary="factor calculation complete",
            observations=(
                semantic_position,
                lowercase_position,
                string_quantity_position,
                safe_factor,
            ),
        ),
        AnalystResult(
            analyst="safe-language",
            instrument=request.instrument,
            summary="short interest 10 percent; long term 10 year growth; token is bullish",
        ),
    )
    report = ResearchReport(
        request_id=request.request_id,
        instrument=request.instrument,
        results=results,
        generated_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    submission = queue.enqueue(request)
    claim = queue.claim_next()
    assert claim is not None and claim.claim_token is not None
    queue.complete(request.request_id, report, claim.claim_token)
    store = ReportStore(tmp_path / "reports")
    store.save(report)

    sent: list[dict[str, str]] = []

    async def sender(target: str, payload: dict[str, str]) -> None:
        del target
        sent.append(payload)

    await DiscordNotifier("https://discord.test/webhook", sender=sender).notify(
        report, f"report:{request.request_id}"
    )

    outputs = (
        render_json(report),
        render_markdown(report),
        queue.get(submission.request_id).result.model_dump_json(),  # type: ignore[union-attr]
        store.get(str(request.request_id)).model_dump_json(),
        json.dumps(sent[0]),
    )
    for output in outputs:
        for forbidden in (
            "sk-live-12345678",
            "hunter2-secret",
            "ACME 900 @ 2",
            "owned 900 ACME at average cost 2",
            "short interest 10 percent",
            "long term 10 year growth",
            "token is bullish",
        ):
            assert forbidden not in output
        compacted = output.replace(" ", "")
        assert '"label":"ACME"' not in compacted
        assert '"unit":"shares"' not in compacted

    for output in outputs[:4]:
        assert "revenue_growth" in output
        assert "0.125" in output
        assert "safe-language analysis partial with 0 numeric factors" in output
    assert set(sent[0]) == {"content"}
    assert "Research report ready" in sent[0]["content"]


@pytest.mark.parametrize(
    "analyst",
    (
        "Fundamental",
        "risk analyst",
        "risk_analyst",
        "token=is-secret",
        "-risk",
        "risk-",
        "a" * 65,
    ),
)
def test_analyst_names_are_bounded_slug_identifiers(analyst: str) -> None:
    instrument = InstrumentId(symbol="ACME", market="US")

    with pytest.raises(ValidationError):
        AnalysisRequest(instrument=instrument, analysts=(analyst,))
    with pytest.raises(ValidationError):
        AnalystResult(
            analyst=analyst,
            instrument=instrument,
            summary="complete",
        )

    unsafe_result = AnalystResult.model_construct(
        analyst=analyst,
        instrument=instrument,
        summary="complete",
        observations=(),
        evidence=(),
    )
    unsafe_report = ResearchReport(
        request_id=uuid4(),
        instrument=instrument,
        results=(unsafe_result,),
        generated_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    with pytest.raises(ValidationError):
        render_json(unsafe_report)


@pytest.mark.asyncio
async def test_export_projection_only_serializes_closed_research_values(tmp_path: Path) -> None:
    request = _request()
    forbidden = (
        "token is token-live-987654",
        "secret is moonbase-secret",
        "bought 900 ACME at 2",
        "900 stock units of ACME",
        "unclassified provider narrative",
        "unknown-shaped-leaf",
    )
    factor = Observation(
        instrument=request.instrument,
        metric="revenue_growth",
        value={
            "value": 0.125,
            "values": [
                1.0,
                True,
                None,
                {"score": 0.75, "status": "partial"},
                forbidden[-1],
                {"value": forbidden[4]},
                float("nan"),
                float("inf"),
                *range(100),
            ],
            "status": "complete",
            "signal": "bullish",
            "currency": "USD",
            "label": "ACME",
            "note": forbidden[0],
            "payload": list(forbidden[1:]),
            "unit": "stock",
            "arbitrary": {"value": 900, "label": forbidden[-1]},
        },
        source="fixture",
        observed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    narrative = Observation(
        instrument=request.instrument,
        metric="close",
        value="; ".join(forbidden),
        source="fixture",
        observed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    result = AnalystResult(
        analyst="event-driven",
        instrument=request.instrument,
        summary="; ".join(forbidden),
        observations=(factor, narrative),
        evidence=(
            Evidence(
                source=f"provider {forbidden[0]}",
                content="; ".join(forbidden),
                collected_at=datetime(2026, 7, 22, tzinfo=UTC),
            ),
        ),
    )
    report = ResearchReport(
        request_id=request.request_id,
        instrument=request.instrument,
        results=(result,),
        generated_at=datetime(2026, 7, 22, tzinfo=UTC),
    )

    queue_path = tmp_path / "jobs.sqlite3"
    queue = JobQueue(queue_path)
    submission = queue.enqueue(request)
    claim = queue.claim_next()
    assert claim is not None and claim.claim_token is not None
    queue.complete(request.request_id, report, claim.claim_token)
    with sqlite3.connect(queue_path) as connection:
        queued_json = connection.execute(
            "SELECT result_json FROM jobs WHERE request_id = ?", (str(request.request_id),)
        ).fetchone()[0]

    report_directory = tmp_path / "reports"
    store = ReportStore(report_directory)
    store.save(report)

    sent: list[dict[str, str]] = []

    async def sender(target: str, payload: dict[str, str]) -> None:
        del target
        sent.append(payload)

    await DiscordNotifier("https://discord.test/webhook", sender=sender).notify(
        report, f"report:{request.request_id}"
    )

    serialized_outputs = (
        render_json(report),
        render_markdown(report),
        queued_json,
        queue.get(submission.request_id).result.model_dump_json(),  # type: ignore[union-attr]
        (report_directory / f"{request.request_id}.json").read_text(encoding="utf-8"),
        (report_directory / f"{request.request_id}.md").read_text(encoding="utf-8"),
        store.get(str(request.request_id)).model_dump_json(),
        json.dumps(sent[0]),
    )
    for output in serialized_outputs:
        for narrative_text in forbidden:
            assert narrative_text not in output

    for output in serialized_outputs[:-1]:
        assert "event-driven analysis complete with 2 numeric factors" in output
        assert "revenue_growth" in output
        assert "0.125" in output
        assert "complete" in output
        assert "bullish" in output
        assert "USD" in output
        assert "untrusted" in output
        assert "sha256:" in output
        assert "NaN" not in output
        assert "Infinity" not in output
    exported = json.loads(serialized_outputs[0])
    exported_observations = exported["results"][0]["observations"]
    assert exported_observations[0]["value"] == {
        "currency": "USD",
        "signal": "bullish",
        "status": "complete",
        "value": 0.125,
        "values": [1.0, True, None, {"score": 0.75, "status": "partial"}, {}, *range(56)],
    }
    assert exported_observations[1]["value"] is None
    assert sent == [{"content": f"Research report ready: ACME | report:{request.request_id}"}]


def test_export_projection_bounds_integer_magnitude_for_persistence(tmp_path: Path) -> None:
    request = _request()
    observation = Observation(
        instrument=request.instrument,
        metric="revenue_growth",
        value={"value": 10**5000, "score": 1},
        source="fixture",
        observed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    result = AnalystResult(
        analyst="fundamental",
        instrument=request.instrument,
        summary="provider supplied an oversized integer",
        observations=(observation,),
    )
    report = ResearchReport(
        request_id=request.request_id,
        instrument=request.instrument,
        results=(result,),
        generated_at=datetime(2026, 7, 22, tzinfo=UTC),
    )

    rendered = render_json(report)
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    queue.enqueue(request)
    claim = queue.claim_next()
    assert claim is not None and claim.claim_token is not None
    queue.complete(request.request_id, report, claim.claim_token)
    store = ReportStore(tmp_path / "reports")
    store.save(report)

    direct_value = json.loads(rendered)["results"][0]["observations"][0]["value"]
    queued = queue.get(request.request_id)
    stored = store.get(str(request.request_id))
    assert direct_value == {"score": 1}
    assert queued.status == "succeeded"
    assert queued.result is not None
    assert queued.result.results[0].observations[0].value == {"score": 1}
    assert stored.results[0].observations[0].value == {"score": 1}


def test_export_projection_caps_report_collections_in_raw_storage(tmp_path: Path) -> None:
    request = _request()
    observation = Observation(
        instrument=request.instrument,
        metric="revenue_growth",
        value=0.125,
        source="fixture",
        observed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    evidence = Evidence(
        source="provider narrative",
        content="unknown narrative",
        collected_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    results = tuple(
        AnalystResult(
            analyst=f"analyst-{index}",
            instrument=request.instrument,
            summary="provider narrative",
            observations=(observation,) * 300,
            evidence=(evidence,) * 70,
        )
        for index in range(20)
    )
    report = ResearchReport(
        request_id=request.request_id,
        instrument=request.instrument,
        results=results,
        generated_at=datetime(2026, 7, 22, tzinfo=UTC),
    )

    rendered = render_json(report)
    queue_path = tmp_path / "jobs.sqlite3"
    queue = JobQueue(queue_path)
    queue.enqueue(request)
    claim = queue.claim_next()
    assert claim is not None and claim.claim_token is not None
    queue.complete(request.request_id, report, claim.claim_token)
    with sqlite3.connect(queue_path) as connection:
        queued_json = connection.execute(
            "SELECT result_json FROM jobs WHERE request_id = ?", (str(request.request_id),)
        ).fetchone()[0]

    report_directory = tmp_path / "reports"
    ReportStore(report_directory).save(report)
    stored_json = (report_directory / f"{request.request_id}.json").read_text(encoding="utf-8")

    for payload in map(json.loads, (rendered, queued_json, stored_json)):
        assert len(payload["results"]) == 16
        assert all(len(result["observations"]) == 256 for result in payload["results"])
        assert all(result["evidence"] == [] for result in payload["results"])
        assert all(len(result["citations"]) == 1 for result in payload["results"])


def test_export_projection_bounds_provenance_in_raw_storage(tmp_path: Path) -> None:
    request = _request()
    bounded = Observation(
        instrument=request.instrument,
        metric="revenue_growth",
        value=0.125,
        source="fixture",
        observed_at=datetime(2026, 7, 22, tzinfo=UTC),
        provenance={
            "inputs": [
                {
                    "metric": "revenue_growth",
                    "value": index,
                    "inputs": [{"value": index}] * 100,
                }
                for index in range(1000)
            ]
        },
    )
    constructed = bounded.model_copy(
        update={
            "provenance": {
                "inputs": [
                    {"value": 10**5000},
                    {"inputs": [{"inputs": [{"inputs": [{"value": 42}]}]}]},
                ]
            }
        }
    )
    result = AnalystResult(
        analyst="fundamental",
        instrument=request.instrument,
        summary="provider narrative",
        observations=(bounded, constructed),
    )
    report = ResearchReport(
        request_id=request.request_id,
        instrument=request.instrument,
        results=(result,),
        generated_at=datetime(2026, 7, 22, tzinfo=UTC),
    )

    rendered = render_json(report)
    queue_path = tmp_path / "jobs.sqlite3"
    queue = JobQueue(queue_path)
    queue.enqueue(request)
    claim = queue.claim_next()
    assert claim is not None and claim.claim_token is not None
    queue.complete(request.request_id, report, claim.claim_token)
    with sqlite3.connect(queue_path) as connection:
        queued_json = connection.execute(
            "SELECT result_json FROM jobs WHERE request_id = ?", (str(request.request_id),)
        ).fetchone()[0]

    report_directory = tmp_path / "reports"
    ReportStore(report_directory).save(report)
    stored_json = (report_directory / f"{request.request_id}.json").read_text(encoding="utf-8")

    for payload in map(json.loads, (rendered, queued_json, stored_json)):
        observations = payload["results"][0]["observations"]
        inputs = observations[0]["provenance"]["inputs"]
        assert len(inputs) == 64
        assert all(len(item["inputs"]) == 64 for item in inputs)
        assert observations[1]["provenance"] == {}


def test_export_projection_bounds_provider_reference_mapping_iteration() -> None:
    request = _request()
    provider_reference = CountingMapping(10_000)
    observation = Observation(
        instrument=request.instrument,
        metric="revenue_growth",
        value=0.125,
        source="fixture",
        observed_at=datetime(2026, 7, 22, tzinfo=UTC),
    ).model_copy(
        update={
            "provenance": {
                "inputs": [{"provider_reference": provider_reference}],
            }
        }
    )
    result = AnalystResult(
        analyst="fundamental",
        instrument=request.instrument,
        summary="provider narrative",
        observations=(observation,),
    )
    report = ResearchReport(
        request_id=request.request_id,
        instrument=request.instrument,
        results=(result,),
        generated_at=datetime(2026, 7, 22, tzinfo=UTC),
    )

    payload = json.loads(render_json(report))

    assert provider_reference.iterations == 64
    assert payload["results"][0]["observations"][0]["provenance"] == {}


def test_report_format_is_closed_and_http_distinguishes_422_from_404(tmp_path: Path) -> None:
    application = ResearchApplication(_engine(), ReportStore(tmp_path / "reports"))
    report = _report(_request())
    application.reports.save(report)
    client = TestClient(create_app(application, bearer_token="token"))
    headers = {"Authorization": "Bearer token"}

    invalid = client.post(f"/reports/{report.request_id}?format=html", headers=headers)
    missing = client.post(f"/reports/{uuid4()}?format=json", headers=headers)

    assert invalid.status_code == 422
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_mcp_report_format_schema_is_closed(tmp_path: Path) -> None:
    application = ResearchApplication(_engine(), ReportStore(tmp_path / "reports"))
    tools = {tool.name: tool for tool in await build_mcp_server(application).list_tools()}
    schema = tools["compile_report"].inputSchema["$defs"]["ReportFormat"]

    assert schema["enum"] == ["markdown", "json"]
