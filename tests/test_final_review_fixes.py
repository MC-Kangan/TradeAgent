from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import click
import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolRequest, CallToolRequestParams
from pydantic import ValidationError
from typer.testing import CliRunner

from trade_research import cli
from trade_research.application import ResearchApplication
from trade_research.cli import app, get_application
from trade_research.domain import (
    AnalysisRequest,
    AnalystResult,
    Evidence,
    InstrumentId,
    Observation,
    ResearchReport,
)
from trade_research.engine import ResearchEngine
from trade_research.http import create_app, validate_bind_host
from trade_research.mcp_server import BOUNDED_TOOL_NAMES, BoundedResearchTools, build_mcp_server
from trade_research.providers import (
    MAX_HTTP_BYTES,
    MAX_PRICE_POINTS,
    CapabilityName,
    LocalCsvParquetFundamentalProvider,
    PricePoint,
    ProviderConfigurationError,
    ProviderContractError,
    ProviderRegistry,
    ReadOnlySqlFundamentalProvider,
    SecFilingsProvider,
    YahooPriceProvider,
)
from trade_research.queue import JobQueue, LostLease, ResearchWorker
from trade_research.reporting import ReportStore, render_json, render_markdown
from trade_research.settings import Settings
from trade_research.skills import SkillRegistry, TechnicalSkill
from trade_research.storage import RunStore


def _ref(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _write_price_fixture(path: Path, *, points: int = 70) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("symbol", "observed_at", "open", "high", "low", "close", "volume"),
        )
        writer.writeheader()
        for index in range(points):
            close = 100 + index
            writer.writerow(
                {
                    "symbol": "ACME",
                    "observed_at": (
                        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
                    ).isoformat(),
                    "open": close - 0.5,
                    "high": close + 1,
                    "low": close - 1,
                    "close": close,
                    "volume": 100 if index < 35 else 200,
                }
            )


def _write_fundamental_fixture(path: Path) -> None:
    statement_values = {
        ("revenue", "current"): 120,
        ("revenue", "prior"): 100,
        ("net_income", "current"): 12,
        ("net_income", "prior"): 10,
        ("operating_income", "current"): 18,
        ("shareholders_equity", "current"): 60,
        ("free_cash_flow", "current"): 18,
        ("total_debt", "current"): 30,
        ("ebitda", "current"): 24,
    }
    fields = (
        "symbol",
        "metric",
        "value",
        "observed_at",
        "period_role",
        "period_end",
        "period_type",
        "period_ref",
        "prior_period_ref",
        "snapshot_ref",
        "currency",
        "valuation_as_of",
        "vendor_field",
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        snapshot = _ref("snapshot")
        for (metric, role), value in statement_values.items():
            current = role == "current"
            writer.writerow(
                {
                    "symbol": "ACME",
                    "metric": metric,
                    "value": value,
                    "observed_at": "2026-01-02T00:00:00+00:00",
                    "period_role": role,
                    "period_end": "2025-12-31" if current else "2024-12-31",
                    "period_type": "annual",
                    "period_ref": _ref("FY2025" if current else "FY2024"),
                    "prior_period_ref": _ref("FY2024") if current else "",
                    "snapshot_ref": snapshot,
                    "currency": "USD",
                    "valuation_as_of": "",
                    "vendor_field": metric.upper(),
                }
            )
        for metric, value in (("market_cap", 180), ("enterprise_value", 240)):
            writer.writerow(
                {
                    "symbol": "ACME",
                    "metric": metric,
                    "value": value,
                    "observed_at": "2026-01-02T00:00:00+00:00",
                    "period_role": "",
                    "period_end": "",
                    "period_type": "",
                    "period_ref": "",
                    "prior_period_ref": "",
                    "snapshot_ref": snapshot,
                    "currency": "USD",
                    "valuation_as_of": "2026-01-01T00:00:00+00:00",
                    "vendor_field": metric.upper(),
                }
            )


def _configured_environment(tmp_path: Path) -> dict[str, str]:
    prices = tmp_path / "prices.csv"
    fundamentals = tmp_path / "fundamentals.csv"
    _write_price_fixture(prices)
    _write_fundamental_fixture(fundamentals)
    return {
        "TRADE_RESEARCH_DATA_DIR": str(tmp_path / "data"),
        "TRADE_RESEARCH_PRICE_PROVIDER": "local_csv",
        "TRADE_RESEARCH_PRICE_PATH": str(prices),
        "TRADE_RESEARCH_FUNDAMENTAL_PROVIDER": "local_csv",
        "TRADE_RESEARCH_FUNDAMENTAL_PATH": str(fundamentals),
    }


def test_settings_are_typed_allowlisted_and_compose_real_local_capabilities(
    tmp_path: Path,
) -> None:
    environment = _configured_environment(tmp_path)
    settings = Settings.from_environment(environment)
    engine = ResearchEngine.from_settings(settings)

    engine.validate_analysts(("fundamental", "technical"))
    with pytest.raises(ProviderConfigurationError, match="unknown setting"):
        Settings.from_environment({"TRADE_RESEARCH_PRICE_PROVIDER": "arbitrary"})
    with pytest.raises(ProviderConfigurationError, match="invalid configuration"):
        Settings.from_environment(
            {
                "TRADE_RESEARCH_PRICE_PROVIDER": "local_csv",
                "TRADE_RESEARCH_PRICE_PATH": str(tmp_path / "missing.csv"),
            }
        )
    with pytest.raises(ProviderConfigurationError, match="capability"):
        ResearchEngine.from_settings(Settings()).validate_analysts(("technical",))


@pytest.mark.asyncio
async def test_real_composition_runs_through_cli_worker_http_and_direct_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in _configured_environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    get_application.cache_clear()
    runner = CliRunner()

    submitted = runner.invoke(app, ["research", "ACME", "--market", "US"])
    assert submitted.exit_code == 0, submitted.output
    request_id = json.loads(submitted.output)["request_id"]
    worked = runner.invoke(app, ["worker", "--once"])
    assert worked.exit_code == 0, worked.output
    report = json.loads(runner.invoke(app, ["report", request_id, "--format", "json"]).output)
    assert {result["status"] for result in report["results"]} == {"complete"}
    assert all(result["observations"] for result in report["results"])
    fundamental = next(result for result in report["results"] if result["analyst"] == "fundamental")
    assert {method["algorithm"] for method in fundamental["methods"]} == {
        "direct_value",
        "period_growth",
        "ratio",
    }
    markdown = runner.invoke(app, ["report", request_id, "--format", "markdown"])
    assert markdown.exit_code == 0, markdown.output
    assert "local_csv" in markdown.output
    assert "sha256:" in markdown.output

    application = get_application()
    client = TestClient(create_app(application, bearer_token="test-token"))
    response = client.post(
        "/skills/technical/run",
        headers={"Authorization": "Bearer test-token"},
        json={"instrument": {"symbol": "ACME", "market": "US"}, "analysts": ["technical"]},
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["observations"]

    mcp_report = await BoundedResearchTools(application).run_skill(
        "fundamental",
        AnalysisRequest(
            instrument=InstrumentId(symbol="ACME", market="US"), analysts=("fundamental",)
        ),
    )
    assert mcp_report["results"][0]["observations"]
    get_application.cache_clear()


def test_provider_registry_validates_capabilities_identity_and_cardinality() -> None:
    instrument = InstrumentId(symbol="ACME", market="US")
    other = InstrumentId(symbol="OTHER", market="US")

    class WrongFundamentals:
        def fundamentals(self, requested: InstrumentId) -> tuple[Observation, ...]:
            return (
                Observation(
                    instrument=other,
                    metric="revenue",
                    value=1.0,
                    source="fixture",
                    observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            )

    with pytest.raises(ProviderConfigurationError, match="prices capability"):
        ProviderRegistry({"prices": object()})

    class MalformedFilings:
        def filings(self, requested: InstrumentId) -> tuple[object, ...]:
            del requested
            return (object(),)

    with pytest.raises(ProviderContractError, match="malformed evidence"):
        ProviderRegistry({"filings": MalformedFilings()}).filings(instrument)  # type: ignore[dict-item]
    with pytest.raises(ProviderContractError, match="instrument"):
        ProviderRegistry({"fundamentals": WrongFundamentals()}).fundamentals(instrument)

    class OverflowingFundamentals:
        def fundamentals(self, requested: InstrumentId) -> tuple[Observation, ...]:
            return (
                Observation(
                    instrument=requested,
                    metric="revenue",
                    value=10**4000,
                    source="fixture",
                    observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            )

    with pytest.raises(ProviderContractError, match="finite"):
        ProviderRegistry({"fundamentals": OverflowingFundamentals()}).fundamentals(instrument)

    points = tuple(
        PricePoint(
            instrument=instrument,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
            close=float(index + 1),
            source="fixture",
            provenance={"provider_kind": "fixture", "reference": _ref(str(index))},
        )
        for index in range(MAX_PRICE_POINTS + 1)
    )

    class OversizePrices:
        def price_history(self, requested: InstrumentId) -> tuple[PricePoint, ...]:
            return points

    with pytest.raises(ProviderContractError, match="point limit"):
        ProviderRegistry({"prices": OversizePrices()}).prices(instrument)


@pytest.mark.parametrize(
    "provenance",
    (
        {},
        {"provider_kind": "fixture"},
        {"provider_kind": "internal", "reference": _ref("price")},
        {"provider_kind": "fixture", "reference": "not-a-reference"},
    ),
)
def test_price_registry_requires_matching_provider_and_opaque_reference(
    provenance: dict[str, str],
) -> None:
    instrument = InstrumentId(symbol="ACME", market="US")

    class Prices:
        def price_history(self, requested: InstrumentId) -> tuple[PricePoint, ...]:
            return (
                PricePoint(
                    instrument=requested,
                    observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                    close=100.0,
                    source="fixture",
                    provenance=provenance,
                ),
            )

    with pytest.raises(ProviderContractError, match="provenance|reference"):
        ProviderRegistry({"prices": Prices()}).prices(instrument)


@pytest.mark.parametrize(
    "metric, provenance",
    (
        ("revenue", {}),
        (
            "revenue",
            {
                "provider_kind": "internal",
                "snapshot_ref": _ref("snapshot"),
                "reference": _ref("revenue"),
                "period_role": "current",
                "period_end": "2025-12-31",
                "period_type": "annual",
                "period_ref": _ref("FY2025"),
                "currency": "USD",
            },
        ),
        (
            "revenue",
            {
                "provider_kind": "fixture",
                "snapshot_ref": "bad",
                "reference": _ref("revenue"),
                "period_role": "current",
                "period_end": "2025-12-31",
                "period_type": "annual",
                "period_ref": _ref("FY2025"),
                "currency": "USD",
            },
        ),
        (
            "market_cap",
            {
                "provider_kind": "fixture",
                "snapshot_ref": _ref("snapshot"),
                "reference": _ref("market-cap"),
                "valuation_as_of": "not-a-timestamp",
                "currency": "USD",
            },
        ),
    ),
)
def test_fundamental_registry_requires_typed_auditable_metadata(
    metric: str, provenance: dict[str, str]
) -> None:
    instrument = InstrumentId(symbol="ACME", market="US")

    class Fundamentals:
        def fundamentals(self, requested: InstrumentId) -> tuple[Observation, ...]:
            return (
                Observation(
                    instrument=requested,
                    metric=metric,
                    value=100.0,
                    source="fixture",
                    observed_at=datetime(2026, 1, 2, tzinfo=UTC),
                    provenance=provenance,
                ),
            )

    with pytest.raises(ProviderContractError, match="provenance|metadata|reference"):
        ProviderRegistry({"fundamentals": Fundamentals()}).fundamentals(instrument)


def test_adapters_reject_oversized_bodies_and_tables(tmp_path: Path) -> None:
    instrument = InstrumentId(symbol="ACME", market="US")
    yahoo = YahooPriceProvider(http_get=lambda _url, _headers: "x" * (MAX_HTTP_BYTES + 1))
    with pytest.raises(ProviderContractError, match="byte limit"):
        yahoo.price_history(instrument)

    path = tmp_path / "fundamentals.csv"
    path.write_text(
        "symbol,metric,value,observed_at\n"
        "ACME,revenue,1,2026-01-01T00:00:00+00:00\n"
        "ACME,revenue,2,2026-01-02T00:00:00+00:00\n",
        encoding="utf-8",
    )
    with pytest.raises(ProviderContractError, match="row limit"):
        LocalCsvParquetFundamentalProvider(path, max_rows=1).fundamentals(instrument)

    price_database = tmp_path / "prices.sqlite3"
    with sqlite3.connect(price_database) as connection:
        connection.execute("CREATE TABLE prices (symbol TEXT, observed_at TEXT, close REAL)")
        connection.executemany(
            "INSERT INTO prices VALUES (?, ?, ?)",
            [
                ("ACME", "2026-01-01T00:00:00+00:00", 1.0),
                ("ACME", "2026-01-02T00:00:00+00:00", 2.0),
            ],
        )
    from trade_research.providers import ReadOnlySqlPriceProvider

    with pytest.raises(ProviderContractError, match="row limit"):
        ReadOnlySqlPriceProvider(price_database, max_rows=1).price_history(instrument)


def test_sec_adapter_discards_raw_payload_and_keeps_bounded_metadata_refs() -> None:
    payload = json.dumps(
        {
            "secret_raw_field": "sk-live-must-not-survive",
            "filings": {
                "recent": {
                    "form": ["10-K"],
                    "filingDate": ["2026-01-01"],
                    "accessionNumber": ["0000000000-26-000001"],
                }
            },
        }
    )
    provider = SecFilingsProvider(
        {"ACME": "1"},
        "research@example.test",
        http_get=lambda _url, _headers: payload,
    )

    evidence = provider.filings(InstrumentId(symbol="ACME", market="US"))
    normalized = json.loads(evidence[0].content)
    assert normalized == {
        "filing_date": "2026-01-01",
        "form": "10-K",
        "reference": normalized["reference"],
    }
    assert normalized["reference"].startswith("sha256:")
    assert "sk-live" not in evidence[0].content


def test_normalized_fundamentals_support_parquet_and_fixed_read_only_sql(tmp_path: Path) -> None:
    import pyarrow.csv as arrow_csv
    import pyarrow.parquet as parquet

    instrument = InstrumentId(symbol="ACME", market="US")
    csv_path = tmp_path / "fundamentals.csv"
    parquet_path = tmp_path / "fundamentals.parquet"
    _write_fundamental_fixture(csv_path)
    parquet.write_table(arrow_csv.read_csv(csv_path), parquet_path)

    parquet_rows = LocalCsvParquetFundamentalProvider(parquet_path).fundamentals(instrument)
    assert len(parquet_rows) == 11
    assert all(item.instrument == instrument for item in parquet_rows)

    database = tmp_path / "fundamentals.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE fundamentals "
            "(symbol TEXT, metric TEXT, value REAL, observed_at TEXT)"
        )
        connection.execute(
            "INSERT INTO fundamentals VALUES (?, ?, ?, ?)",
            ("ACME", "revenue", 120.0, "2026-01-01T00:00:00+00:00"),
        )
    sql_rows = ReadOnlySqlFundamentalProvider(database).fundamentals(instrument)
    assert len(sql_rows) == 1
    assert sql_rows[0].source == "local_sql"


def test_recursive_history_provenance_has_complete_series_hash_not_oldest_truncation() -> None:
    instrument = InstrumentId(symbol="ACME", market="US")
    prices = tuple(
        PricePoint(
            instrument=instrument,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
            close=100.0 + index,
            open=99.5 + index,
            high=101.0 + index,
            low=99.0 + index,
            volume=100.0,
            source="fixture",
            provenance={"provider_kind": "fixture", "reference": _ref(str(index))},
        )
        for index in range(100)
    )

    class Prices:
        def price_history(self, requested: InstrumentId) -> tuple[PricePoint, ...]:
            return prices

    result = TechnicalSkill().analyze(instrument, ProviderRegistry({"prices": Prices()}))
    rsi = next(item for item in result.observations if item.metric == "relative_strength_index_14")

    assert rsi.provenance["algorithm"] == "wilder_rsi"
    assert rsi.provenance["window"] == "wilder_14_observations"
    assert rsi.provenance["point_count"] == 100
    assert rsi.provenance["start_at"] == prices[0].observed_at.isoformat()
    assert rsi.provenance["end_at"] == prices[-1].observed_at.isoformat()
    assert str(rsi.provenance["series_ref"]).startswith("sha256:")
    assert rsi.provenance["input_provider_kind"] == "fixture"
    assert "inputs" not in rsi.provenance

    report = ResearchReport(
        request_id=uuid4(),
        instrument=instrument,
        results=(result,),
        generated_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    markdown = render_markdown(report)
    assert "| fixture |" in markdown
    assert str(rsi.provenance["series_ref"]) in markdown


@dataclass(frozen=True)
class _UnsafeSkill:
    name: str = "fundamental"
    required_capabilities: tuple[CapabilityName, ...] = ()

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary="api_key=sk-live-12345678 arbitrary external narrative",
            evidence=(
                Evidence(
                    source="https://unsafe.example/account/acct-123",
                    content="token=sk-live-12345678 raw provider body",
                    collected_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            ),
        )


@pytest.mark.asyncio
async def test_public_python_report_is_safe_structured_and_informative() -> None:
    engine = ResearchEngine.from_settings(
        skills=SkillRegistry((_UnsafeSkill(),)), providers=ProviderRegistry({})
    )
    report = await engine.analyze(
        AnalysisRequest(
            instrument=InstrumentId(symbol="ACME", market="US"), analysts=("fundamental",)
        )
    )
    payload = report.model_dump_json()

    assert "sk-live" not in payload
    assert "acct-123" not in payload
    assert report.results[0].status == "partial"
    assert report.results[0].failure_category == "insufficient_data"
    assert report.results[0].limitations
    assert report.results[0].citations[0].reference.startswith("sha256:")
    assert report.results[0].evidence == ()
    assert "Failure category" in render_markdown(report)
    assert "citations" in render_json(report)


def test_skill_registry_requires_typed_capability_declaration() -> None:
    @dataclass(frozen=True)
    class MissingCapabilityDeclaration:
        name: str = "missing-capabilities"

        def analyze(
            self, instrument: InstrumentId, providers: ProviderRegistry
        ) -> AnalystResult:
            del providers
            return AnalystResult(
                analyst=self.name,
                instrument=instrument,
                summary="complete",
            )

    with pytest.raises(TypeError, match="required_capabilities"):
        SkillRegistry((MissingCapabilityDeclaration(),))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "symbol",
    (
        "sk-live-12345678",
        "pk_test_1234567890",
        "token-prod-1234567890",
        "eyJhbGciOiJIUzI1NiJ9.abc.def",
    ),
)
def test_market_symbol_grammar_rejects_common_credential_shapes(symbol: str) -> None:
    with pytest.raises(ValueError):
        InstrumentId(symbol=symbol, market="US")


@pytest.mark.parametrize(
    "host",
    (
        "0.0.0.0",
        "::",
        "8.8.8.8",
        "203.0.113.8",
        "2001:db8::8",
        "2001:4860:4860::8888",
        "example.com",
    ),
)
def test_bind_validation_rejects_wildcard_public_and_hostname_targets(host: str) -> None:
    with pytest.raises(ValueError, match="private"):
        validate_bind_host(host)


@pytest.mark.parametrize("host", ("127.0.0.1", "::1", "10.0.0.7", "192.168.1.8", "100.64.0.9"))
def test_bind_validation_accepts_loopback_private_and_vpn_literals(host: str) -> None:
    assert validate_bind_host(host) == host


def test_container_internal_wildcard_requires_explicit_non_public_mode() -> None:
    assert validate_bind_host("0.0.0.0", allow_container_wildcard=True) == "0.0.0.0"


@pytest.mark.parametrize(
    "symbol",
    (
        "AKIA1234567890ABCDEF",
        "ASIA1234567890ABCDEF",
        "akia1234567890abcdef",
        "AsIa1234567890aBcDeF",
    ),
)
def test_aws_access_key_ids_fail_python_run_store_and_queue(
    tmp_path: Path, symbol: str
) -> None:
    with pytest.raises(ValidationError, match="credential"):
        InstrumentId(symbol=symbol, market="CRYPTO")
    with pytest.raises(ValidationError, match="credential"):
        InstrumentId.model_validate(
            InstrumentId.model_construct(symbol=symbol, market="CRYPTO").model_dump()
        )

    unsafe_instrument = InstrumentId.model_construct(symbol=symbol, market="CRYPTO")
    unsafe_request = AnalysisRequest.model_construct(
        request_id=uuid4(),
        instrument=unsafe_instrument,
        analysts=("fundamental",),
        metadata={},
        positions=(),
    )
    run_store = RunStore(tmp_path / "runs.sqlite3")
    queue = JobQueue(tmp_path / "jobs.sqlite3")
    with pytest.raises(ValidationError, match="credential"):
        run_store.save_request(unsafe_request)
    with pytest.raises(ValidationError, match="credential"):
        queue.enqueue(unsafe_request)
    assert symbol not in (tmp_path / "runs.sqlite3").read_bytes().decode(
        "utf-8", errors="ignore"
    )
    assert symbol not in (tmp_path / "jobs.sqlite3").read_bytes().decode(
        "utf-8", errors="ignore"
    )


@pytest.mark.parametrize(
    "symbol",
    (
        "AKIA1234567890ABCDEF",
        "akia1234567890abcdef",
        "AkIa1234567890AbCdEf",
    ),
)
@pytest.mark.asyncio
async def test_aws_access_key_ids_fail_http_and_direct_mcp_without_echo(
    tmp_path: Path, symbol: str
) -> None:
    application = ResearchApplication(
        ResearchEngine.from_settings(
            skills=SkillRegistry((_UnsafeSkill(),)), providers=ProviderRegistry({})
        ),
        ReportStore(tmp_path / "reports"),
        JobQueue(tmp_path / "jobs.sqlite3"),
    )
    client = TestClient(
        create_app(application, bearer_token="test-token"),
        raise_server_exceptions=False,
    )
    response = client.post(
        "/research",
        headers={"Authorization": "Bearer test-token"},
        json={
            "instrument": {"symbol": symbol, "market": "CRYPTO"},
            "analysts": ["fundamental"],
        },
    )
    assert response.status_code == 422
    assert symbol not in response.text

    unsafe_request = AnalysisRequest.model_construct(
        request_id=uuid4(),
        instrument=InstrumentId.model_construct(symbol=symbol, market="CRYPTO"),
        analysts=("fundamental",),
        metadata={},
        positions=(),
    )
    with pytest.raises(ValidationError, match="credential"):
        await BoundedResearchTools(application).start_research(unsafe_request)


def test_http_cans_late_application_validation_errors(tmp_path: Path) -> None:
    sentinel = "sk-live-LATE-VALIDATION-SENTINEL-12345678"

    class LateValidationApplication(ResearchApplication):
        async def start_research(self, request: AnalysisRequest) -> dict[str, object]:
            del request
            InstrumentId(symbol=sentinel, market="US")
            raise AssertionError("unreachable")

    application = LateValidationApplication(
        ResearchEngine.from_settings(
            skills=SkillRegistry((_UnsafeSkill(),)), providers=ProviderRegistry({})
        ),
        ReportStore(tmp_path / "reports"),
        JobQueue(tmp_path / "jobs.sqlite3"),
    )
    response = TestClient(
        create_app(application, bearer_token="test-token"),
        raise_server_exceptions=False,
    ).post(
        "/research",
        headers={"Authorization": "Bearer test-token"},
        json={
            "instrument": {"symbol": "ACME", "market": "US"},
            "analysts": ["fundamental"],
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid request"}
    assert sentinel not in response.text


@pytest.mark.parametrize(
    "symbol",
    (
        "AKIA1234567890ABCDEF",
        "akia1234567890abcdef",
        "AkIa1234567890AbCdEf",
    ),
)
@pytest.mark.asyncio
async def test_fastmcp_call_tool_cans_aws_validation_errors(
    tmp_path: Path, symbol: str
) -> None:
    application = ResearchApplication(
        ResearchEngine.from_settings(
            skills=SkillRegistry((_UnsafeSkill(),)), providers=ProviderRegistry({})
        ),
        ReportStore(tmp_path / "reports"),
        JobQueue(tmp_path / "jobs.sqlite3"),
    )
    server = build_mcp_server(application)

    with pytest.raises(ToolError) as captured:
        await server.call_tool(
            "start_research",
            {
                "request": {
                    "instrument": {"symbol": symbol, "market": "CRYPTO"},
                    "analysts": ["fundamental"],
                }
            },
        )

    assert str(captured.value) == "tool request rejected"
    assert captured.value.__context__ is None
    assert symbol not in str(captured.value)
    assert "AKIA1234567890ABCDEF" not in str(captured.value)

    protocol_handler = server._mcp_server.request_handlers[CallToolRequest]
    protocol_result = await protocol_handler(
        CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="start_research",
                arguments={
                    "request": {
                        "instrument": {"symbol": symbol, "market": "CRYPTO"},
                        "analysts": ["fundamental"],
                    }
                },
            ),
        )
    )
    assert protocol_result.root.isError is True
    assert protocol_result.root.content[0].text == "tool request rejected"  # type: ignore[union-attr]
    assert symbol not in str(protocol_result)


@pytest.mark.asyncio
async def test_fastmcp_call_tool_cans_other_malformed_inputs_and_keeps_valid_tools(
    tmp_path: Path,
) -> None:
    sentinel = "sk-live-MCP-TOOL-SENTINEL-12345678"
    application = ResearchApplication(
        ResearchEngine.from_settings(
            skills=SkillRegistry((_UnsafeSkill(),)), providers=ProviderRegistry({})
        ),
        ReportStore(tmp_path / "reports"),
        JobQueue(tmp_path / "jobs.sqlite3"),
    )
    server = build_mcp_server(application)

    assert tuple(tool.name for tool in await server.list_tools()) == BOUNDED_TOOL_NAMES
    assert await server.call_tool("list_skills", {})
    valid_result = await server.call_tool(
        "start_research",
        {
            "request": {
                "instrument": {"symbol": "ACME", "market": "US"},
                "analysts": ["fundamental"],
            }
        },
    )
    assert isinstance(valid_result, tuple)
    assert valid_result[1]["status"] == "queued"
    with pytest.raises(ToolError) as captured:
        await server.call_tool(sentinel, {})
    assert str(captured.value) == "tool request rejected"
    assert captured.value.__context__ is None
    assert sentinel not in str(captured.value)


class _SlowEngine:
    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def analyze(self, request: AnalysisRequest):  # type: ignore[no-untyped-def]
        await asyncio.sleep(self.delay)
        return await ResearchEngine.from_settings(
            skills=SkillRegistry((_UnsafeSkill(),)), providers=ProviderRegistry({})
        ).analyze(request)


@pytest.mark.asyncio
async def test_worker_heartbeat_prevents_recovery_across_threshold(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite3", recovery_after_seconds=0.006)
    request = AnalysisRequest(
        instrument=InstrumentId(symbol="ACME", market="US"), analysts=("fundamental",)
    )
    queue.enqueue(request)
    worker = ResearchWorker(queue, _SlowEngine(0.03))  # type: ignore[arg-type]

    task = asyncio.create_task(worker.run_once())
    await asyncio.sleep(0.008)
    assert JobQueue(tmp_path / "jobs.sqlite3", recovery_after_seconds=0.006).claim_next() is None
    assert await task is True
    assert queue.get(request.request_id).status == "succeeded"


@pytest.mark.asyncio
async def test_worker_discards_result_after_lost_lease_without_crashing(tmp_path: Path) -> None:
    queue = JobQueue(tmp_path / "jobs.sqlite3", recovery_after_seconds=0.02)
    request = AnalysisRequest(
        instrument=InstrumentId(symbol="ACME", market="US"), analysts=("fundamental",)
    )
    queue.enqueue(request)
    worker = ResearchWorker(queue, _SlowEngine(0.08), heartbeat_interval_seconds=1.0)  # type: ignore[arg-type]

    task = asyncio.create_task(worker.run_once())
    await asyncio.sleep(0.04)
    current = JobQueue(tmp_path / "jobs.sqlite3", recovery_after_seconds=0.02).claim_next()
    assert current is not None and current.claim_token is not None
    assert await task is True
    assert queue.get(request.request_id).status == "running"
    with pytest.raises(LostLease):
        queue.renew(request.request_id, current.claim_token.__class__(int=0))


def _bounded_cli_environment(tmp_path: Path, sentinel_token: str) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("TRADE_RESEARCH_")
    }
    environment.update(
        {
            "TRADE_RESEARCH_API_TOKEN": sentinel_token,
            "TRADE_RESEARCH_DATA_DIR": str(tmp_path / "data"),
        }
    )
    return environment


@pytest.mark.parametrize("case", ("malformed_config", "missing_capability", "public_bind"))
def test_cli_expected_errors_never_emit_tracebacks_secrets_or_paths(
    tmp_path: Path, case: str
) -> None:
    sentinel_token = "sk-live-CLI-SENTINEL-12345678"
    sentinel_path = tmp_path / "private-account-sentinel" / "missing.csv"
    environment = _bounded_cli_environment(tmp_path, sentinel_token)
    if case == "malformed_config":
        environment.update(
            {
                "TRADE_RESEARCH_PRICE_PROVIDER": "local_csv",
                "TRADE_RESEARCH_PRICE_PATH": str(sentinel_path),
            }
        )
        arguments = ("list-skills",)
    elif case == "missing_capability":
        arguments = ("run-skill", "technical", "ACME", "--market", "US")
    else:
        arguments = ("serve", "--host", "0.0.0.0")
    executable = Path(sys.executable).with_name("trade-research")

    completed = subprocess.run(
        [str(executable), *arguments],
        cwd=Path(__file__).parents[1],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = completed.stdout + completed.stderr

    assert completed.returncode != 0
    assert sentinel_token not in output
    assert str(sentinel_path) not in output
    assert "Traceback" not in output


@pytest.mark.parametrize(
    "case",
    (
        "credential_symbol",
        "credential_analyst",
        "unknown_skill",
        "invalid_market",
        "invalid_report",
        "aws_access_key",
        "aws_access_key_lower",
        "aws_access_key_mixed",
    ),
)
def test_cli_main_never_echoes_rejected_inputs_paths_or_tracebacks(
    tmp_path: Path, case: str
) -> None:
    sentinel = "sk-live-ARGUMENT-SENTINEL-12345678"
    private_path = tmp_path / "private-report-sentinel"
    cases = {
        "credential_symbol": ("research", sentinel, "--market", "US"),
        "credential_analyst": ("run-skill", sentinel, "ACME"),
        "unknown_skill": ("run-skill", "unknown-skill", "ACME"),
        "invalid_market": ("research", "ACME", "--market", sentinel),
        "invalid_report": ("report", str(private_path)),
        "aws_access_key": (
            "research",
            "AKIA1234567890ABCDEF",
            "--market",
            "CRYPTO",
        ),
        "aws_access_key_lower": (
            "research",
            "akia1234567890abcdef",
            "--market",
            "CRYPTO",
        ),
        "aws_access_key_mixed": (
            "research",
            "AkIa1234567890AbCdEf",
            "--market",
            "CRYPTO",
        ),
    }
    executable = Path(sys.executable).with_name("trade-research")
    completed = subprocess.run(
        [str(executable), *cases[case]],
        cwd=Path(__file__).parents[1],
        env=_bounded_cli_environment(tmp_path, "api-token-not-printed"),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = completed.stdout + completed.stderr

    assert completed.returncode != 0
    assert sentinel not in output
    assert "AKIA1234567890ABCDEF" not in output
    assert "akia1234567890abcdef" not in output
    assert "AkIa1234567890AbCdEf" not in output
    assert str(private_path) not in output
    assert "Traceback" not in output
    assert output.strip() == "Error: invalid command or arguments"


def test_cli_main_cans_unexpected_errors_and_disables_standalone_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = "sk-live-OUTERMOST-SENTINEL-12345678"

    def fail(*, standalone_mode: bool) -> None:
        assert standalone_mode is False
        raise RuntimeError(sentinel)

    monkeypatch.setattr(cli, "app", fail)
    with pytest.raises(SystemExit) as raised:
        cli.main()

    output = capsys.readouterr()
    assert raised.value.code == 1
    assert "request could not be processed" in output.err
    assert sentinel not in output.err
    assert "Traceback" not in output.err


@pytest.mark.parametrize("kind", ("command", "option", "path"))
def test_installed_cli_cans_sensitive_click_usage_errors(tmp_path: Path, kind: str) -> None:
    sentinel = "sk-live-CLICK-SENTINEL-12345678"
    private_path = tmp_path / "private-click-command"
    arguments = {
        "command": (sentinel,),
        "option": ("list-skills", f"--{sentinel}"),
        "path": (str(private_path),),
    }[kind]
    executable = Path(sys.executable).with_name("trade-research")
    completed = subprocess.run(
        [str(executable), *arguments],
        cwd=Path(__file__).parents[1],
        env=_bounded_cli_environment(tmp_path, "api-token-not-printed"),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = completed.stdout + completed.stderr

    assert completed.returncode == 2
    assert output.strip() == "Error: invalid command or arguments"
    assert sentinel not in output
    assert str(private_path) not in output
    assert "Traceback" not in output


def test_run_async_does_not_retain_the_loop_probe_exception() -> None:
    async def fail() -> None:
        raise KeyError("bounded failure")

    with pytest.raises(KeyError) as raised:
        cli._run_async(fail())

    assert raised.value.__context__ is None


@pytest.mark.parametrize("behavior", ("usage", "exit", "abort"))
def test_cli_main_preserves_click_usage_exit_and_abort(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    behavior: str,
) -> None:
    def invoke(*, standalone_mode: bool) -> int | None:
        assert standalone_mode is False
        if behavior == "usage":
            raise click.UsageError("bounded usage error")
        if behavior == "abort":
            raise click.Abort
        return 7

    monkeypatch.setattr(cli, "app", invoke)
    with pytest.raises(SystemExit) as raised:
        cli.main()

    output = capsys.readouterr()
    if behavior == "usage":
        assert raised.value.code == 2
        assert output.err.strip() == "Error: invalid command or arguments"
        assert "bounded usage error" not in output.err
    elif behavior == "abort":
        assert raised.value.code == 1
        assert "Aborted!" in output.err
    else:
        assert raised.value.code == 7
