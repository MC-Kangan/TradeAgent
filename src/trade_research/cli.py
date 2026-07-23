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
from click import Abort, ClickException
from click.exceptions import Exit
from dotenv import dotenv_values, find_dotenv
from pydantic import ValidationError

from trade_research.application import ResearchApplication
from trade_research.diagnostics import run_doctor
from trade_research.domain import AnalysisRequest, InstrumentId
from trade_research.engine import ResearchEngine
from trade_research.http import validate_bind_host
from trade_research.providers import ProviderConfigurationError
from trade_research.queue import JobQueue, RequestConflict, ResearchWorker
from trade_research.reporting import ReportFormat, ReportStore
from trade_research.settings import Settings

app = typer.Typer(
    help="Local, research-only analysis tools.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)

_SETTINGS_ENVIRONMENT_NAMES = frozenset(
    {
        "TRADE_RESEARCH_CONFIG",
        "TRADE_RESEARCH_PRICE_PROVIDER",
        "TRADE_RESEARCH_PRICE_PATH",
        "TRADE_RESEARCH_FUNDAMENTAL_PROVIDER",
        "TRADE_RESEARCH_FUNDAMENTAL_PATH",
        "TRADE_RESEARCH_CCXT_EXCHANGE",
        "TRADE_RESEARCH_PROVIDER",
        "TRADE_RESEARCH_API_TOKEN",
        "TRADE_RESEARCH_DATA_ROOT",
        "TRADE_RESEARCH_SEC_USER_AGENT",
        "DISCORD_WEBHOOK_URL",
    }
)


@lru_cache(maxsize=1)
def get_application() -> ResearchApplication:
    data_directory = Path(os.environ.get("TRADE_RESEARCH_DATA_DIR", ".trade-research"))
    # Merge .env defaults with os.environ (explicit env vars take precedence).
    dotenv_path = find_dotenv(usecwd=True)
    merged_env: dict[str, str] = {}
    if dotenv_path:
        merged_env.update(dotenv_values(dotenv_path))
    merged_env.update(os.environ)
    settings_environment = {
        name: merged_env[name] for name in _SETTINGS_ENVIRONMENT_NAMES if name in merged_env
    }
    settings = Settings.from_environment(settings_environment)
    return ResearchApplication(
        ResearchEngine.from_settings(settings),
        ReportStore(data_directory / "reports"),
        JobQueue(data_directory / "jobs.sqlite3"),
    )


def _get_application_or_error() -> ResearchApplication:
    try:
        return get_application()
    except ProviderConfigurationError:
        raise typer.BadParameter("research configuration is invalid") from None


def _write_json(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _run_async(awaitable: Any) -> Any:
    """Run a CLI coroutine even when invoked by an async test harness."""

    if not _has_running_event_loop():
        return asyncio.run(awaitable)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, awaitable).result()


def _has_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@app.command("doctor")
def doctor() -> None:
    """Report local interface availability without printing configuration values."""

    _write_json(run_doctor())


@app.command("list-skills")
def list_skills() -> None:
    _write_json(_get_application_or_error().list_skills())


@app.command("run-skill")
def run_skill(
    name: str,
    symbol: str,
    market: Annotated[str, typer.Option("--market")] = "US",
) -> None:
    try:
        request = AnalysisRequest(
            instrument=InstrumentId(symbol=symbol, market=market),
            analysts=(name,),
        )
    except ValidationError:
        raise typer.BadParameter("invalid analysis request") from None
    try:
        result = _run_async(_get_application_or_error().run_skill(name, request))
    except KeyError:
        raise typer.BadParameter("unknown skill") from None
    except ProviderConfigurationError:
        raise typer.BadParameter("selected analyst capability is not configured") from None
    _write_json(result)


@app.command("research")
def research(
    symbol: str,
    market: Annotated[str, typer.Option("--market")] = "US",
    analyst: Annotated[list[str] | None, typer.Option("--analyst")] = None,
    request_id: Annotated[UUID | None, typer.Option("--request-id")] = None,
) -> None:
    try:
        kwargs: dict[str, Any] = {
            "instrument": InstrumentId(symbol=symbol, market=market)
        }
        if analyst:
            kwargs["analysts"] = tuple(analyst)
        if request_id is not None:
            kwargs["request_id"] = request_id
        request = AnalysisRequest(**kwargs)
    except ValidationError:
        raise typer.BadParameter("invalid analysis request") from None
    try:
        result = _run_async(_get_application_or_error().start_research(request))
    except KeyError:
        raise typer.BadParameter("unknown skill") from None
    except ProviderConfigurationError:
        raise typer.BadParameter("selected analyst capability is not configured") from None
    except RequestConflict as error:
        raise typer.BadParameter(str(error)) from None
    _write_json(result)


@app.command("report")
def report(
    request_id: str,
    format_name: Annotated[ReportFormat, typer.Option("--format")] = ReportFormat.MARKDOWN,
) -> None:
    try:
        compiled = _get_application_or_error().compile_report(request_id, format_name)
    except (KeyError, ValueError):
        raise typer.BadParameter("report unavailable") from None
    typer.echo(compiled, nl=False)


@app.command("serve")
def serve(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8000,
) -> None:
    from trade_research.http import run_server

    internal_container_bind = (
        os.environ.get("TRADE_RESEARCH_CONTAINER_INTERNAL_BIND") == "compose-internal-v1"
    )
    try:
        validated_host = validate_bind_host(
            host, allow_container_wildcard=internal_container_bind
        )
    except ValueError:
        raise typer.BadParameter("host must be a private IP literal") from None
    token = _read_secret("TRADE_RESEARCH_API_TOKEN")
    if not token:
        raise typer.BadParameter("TRADE_RESEARCH_API_TOKEN must be set") from None
    run_server(
        _get_application_or_error(),
        bearer_token=token,
        host=validated_host,
        port=port,
        allow_container_wildcard=internal_container_bind,
    )


@app.command("worker")
def worker(
    once: Annotated[bool, typer.Option("--once")] = False,
    poll_interval: Annotated[float, typer.Option("--poll-interval", min=0.1)] = 1.0,
) -> None:
    """Run the supervised SQLite research worker."""

    application = _get_application_or_error()
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

    run_mcp_server(_get_application_or_error())


def _read_secret(name: str) -> str:
    direct = os.environ.get(name, "")
    if direct:
        return direct
    secret_file = os.environ.get(f"{name}_FILE", "")
    if not secret_file:
        return ""
    try:
        value = Path(secret_file).read_text().strip()
    except OSError:
        raise typer.BadParameter(f"{name}_FILE is not readable") from None
    if len(value) > 4096:
        raise typer.BadParameter(f"{name}_FILE is too large")
    return value


def main() -> None:
    """Run the CLI behind a fail-closed exception and logging boundary."""

    try:
        exit_code = app(standalone_mode=False)
    except Exit as signal:
        raise SystemExit(signal.exit_code) from None
    except Abort:
        typer.echo("Aborted!", err=True)
        raise SystemExit(1) from None
    except ClickException as error:
        typer.echo("Error: invalid command or arguments", err=True)
        raise SystemExit(error.exit_code) from None
    except Exception:
        typer.echo("Error: request could not be processed", err=True)
        raise SystemExit(1) from None
    if isinstance(exit_code, int):
        raise SystemExit(exit_code) from None


if __name__ == "__main__":
    main()
