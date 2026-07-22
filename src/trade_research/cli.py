"""Typer CLI over the shared bounded research application."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer

from trade_research.application import ResearchApplication
from trade_research.diagnostics import run_doctor
from trade_research.domain import AnalysisRequest, InstrumentId
from trade_research.engine import ResearchEngine
from trade_research.queue import JobQueue, RequestConflict, ResearchWorker
from trade_research.reporting import ReportFormat, ReportStore

app = typer.Typer(help="Local, research-only analysis tools.", no_args_is_help=True)


@lru_cache(maxsize=1)
def get_application() -> ResearchApplication:
    data_directory = Path(os.environ.get("TRADE_RESEARCH_DATA_DIR", ".trade-research"))
    return ResearchApplication(
        ResearchEngine.from_settings(),
        ReportStore(data_directory / "reports"),
        JobQueue(data_directory / "jobs.sqlite3"),
    )


def _write_json(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _run_async(awaitable: Any) -> Any:
    """Run a CLI coroutine even when invoked by an async test harness."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, awaitable).result()


@app.command("doctor")
def doctor() -> None:
    """Report local interface availability without printing configuration values."""

    _write_json(run_doctor())


@app.command("list-skills")
def list_skills() -> None:
    _write_json(get_application().list_skills())


@app.command("run-skill")
def run_skill(
    name: str,
    symbol: str,
    market: Annotated[str, typer.Option("--market")] = "US",
) -> None:
    request = AnalysisRequest(
        instrument=InstrumentId(symbol=symbol, market=market),
        analysts=(name,),
    )
    _write_json(_run_async(get_application().run_skill(name, request)))


@app.command("research")
def research(
    symbol: str,
    market: Annotated[str, typer.Option("--market")] = "US",
    analyst: Annotated[list[str] | None, typer.Option("--analyst")] = None,
    request_id: Annotated[UUID | None, typer.Option("--request-id")] = None,
) -> None:
    kwargs: dict[str, Any] = {"instrument": InstrumentId(symbol=symbol, market=market)}
    if analyst:
        kwargs["analysts"] = tuple(analyst)
    if request_id is not None:
        kwargs["request_id"] = request_id
    request = AnalysisRequest(**kwargs)
    try:
        result = _run_async(get_application().start_research(request))
    except RequestConflict as error:
        raise typer.BadParameter(str(error)) from None
    _write_json(result)


@app.command("report")
def report(
    request_id: str,
    format_name: Annotated[ReportFormat, typer.Option("--format")] = ReportFormat.MARKDOWN,
) -> None:
    typer.echo(get_application().compile_report(request_id, format_name), nl=False)


@app.command("serve")
def serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8000,
) -> None:
    from trade_research.http import run_server

    token = _read_secret("TRADE_RESEARCH_API_TOKEN")
    if not token:
        raise typer.BadParameter("TRADE_RESEARCH_API_TOKEN must be set")
    run_server(get_application(), bearer_token=token, host=host, port=port)


@app.command("worker")
def worker(
    once: Annotated[bool, typer.Option("--once")] = False,
    poll_interval: Annotated[float, typer.Option("--poll-interval", min=0.1)] = 1.0,
) -> None:
    """Run the supervised SQLite research worker."""

    application = get_application()
    if application.queue is None:
        raise typer.BadParameter("research queue is unavailable")
    research_worker = ResearchWorker(application.queue, application.engine)
    try:
        while True:
            handled = bool(_run_async(research_worker.run_once()))
            if once:
                return
            if not handled:
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        return


@app.command("mcp")
def mcp_command() -> None:
    from trade_research.mcp_server import run_mcp_server

    run_mcp_server(get_application())


def _read_secret(name: str) -> str:
    direct = os.environ.get(name, "")
    if direct:
        return direct
    secret_file = os.environ.get(f"{name}_FILE", "")
    if not secret_file:
        return ""
    try:
        value = Path(secret_file).read_text().strip()
    except OSError as error:
        raise typer.BadParameter(f"{name}_FILE is not readable") from error
    if len(value) > 4096:
        raise typer.BadParameter(f"{name}_FILE is too large")
    return value


if __name__ == "__main__":
    app()
