from __future__ import annotations

import json
import time
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
from trade_research.providers import ProviderRegistry
from trade_research.queue import JobQueue, LostLease, RequestConflict
from trade_research.reporting import ReportStore, render_json, render_markdown
from trade_research.skills import SkillRegistry
from trade_research.storage import RunStore


@dataclass(frozen=True)
class FixedSkill:
    name: str = "fundamental"

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary="complete",
        )


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
    assert "unique" in str(captured.value)


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

    assert "request id conflicts with an existing request" in str(captured.value)
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
            "safeMetric": 42,
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
    assert '"safeMetric": 42' in outputs[0]


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
        ):
            assert forbidden not in output
        compacted = output.replace(" ", "")
        assert '"label":"ACME"' not in compacted
        assert '"unit":"shares"' not in compacted

    for output in outputs[:4]:
        assert "revenue_growth" in output
        assert "0.125" in output
        assert "short interest 10 percent" in output
        assert "long term 10 year growth" in output
        assert "token is bullish" in output
    assert set(sent[0]) == {"content"}
    assert "Research report ready" in sent[0]["content"]


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
