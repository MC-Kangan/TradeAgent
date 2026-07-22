from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml
from typer.testing import CliRunner

from trade_research.cli import app, get_application

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "trade-research"


def test_bootstrap_dev_dry_run_is_portable_and_non_mutating(tmp_path: Path) -> None:
    for name in ("pyproject.toml", "requirements.lock", "requirements-dev.lock"):
        shutil.copy2(ROOT / name, tmp_path / name)

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "bootstrap.py"),
            "--dev",
            "--dry-run",
            "--project-root",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert not (tmp_path / ".venv").exists()
    assert "requirements.lock" in completed.stdout
    assert "requirements-dev.lock" in completed.stdout
    assert "--editable" in completed.stdout
    assert "--no-build-isolation" in completed.stdout
    assert all("==" in line for line in _requirements(ROOT / "requirements.lock"))
    assert all("==" in line for line in _requirements(ROOT / "requirements-dev.lock"))
    assert not any((tmp_path / name).exists() for name in (".env", "secrets"))


def test_lockfiles_cover_all_declared_dependencies_and_build_tool() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text())
    production = set(_requirements(ROOT / "requirements.lock"))
    development = set(_requirements(ROOT / "requirements-dev.lock"))

    assert set(configuration["project"]["dependencies"]) <= production
    assert set(configuration["project"]["optional-dependencies"]["dev"]) <= development
    assert "build==1.3.0" in development


def test_bootstrap_dry_run_plans_recreate_for_stale_venv_without_mutation(
    tmp_path: Path,
) -> None:
    for name in ("pyproject.toml", "requirements.lock", "requirements-dev.lock"):
        shutil.copy2(ROOT / name, tmp_path / name)
    environment = tmp_path / ".venv"
    environment.mkdir()
    (environment / "pyvenv.cfg").write_text("version = 3.10.6\n")
    sentinel = environment / "keep-in-dry-run"
    sentinel.write_text("untouched")

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "bootstrap.py"),
            "--dry-run",
            "--project-root",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert " -m venv --clear " in completed.stdout
    assert sentinel.read_text() == "untouched"


def test_doctor_reports_capabilities_without_echoing_secret_values(tmp_path: Path) -> None:
    secret_values = {
        "TRADE_RESEARCH_API_TOKEN": "api-token-do-not-print",
        "OPENAI_API_KEY": "llm-key-do-not-print",
        "DISCORD_WEBHOOK_URL": "https://discord.invalid/secret-webhook-token",
        "TRADE_RESEARCH_PROVIDER": "yahoo-secret-label",
    }
    environment = {**secret_values, "TRADE_RESEARCH_DATA_DIR": str(tmp_path / "private")}
    get_application.cache_clear()
    result = CliRunner().invoke(app, ["doctor"], env=environment)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    serialized = result.stdout
    assert payload["research_only"] is True
    assert payload["python"]["supported"] is True
    assert payload["storage"]["writable"] is True
    assert payload["storage"]["sqlite_wal"] is True
    assert payload["configuration"]["api_token_configured"] is True
    assert payload["providers"]["configured"] is True
    assert payload["llm"]["configured"] is True
    assert payload["discord"]["configured"] is True
    assert str(tmp_path) not in serialized
    for secret in secret_values.values():
        assert secret not in serialized


def test_compose_defaults_are_non_public_and_share_named_storage() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = compose["services"]

    assert set(services) == {"research-api", "research-worker"}
    assert "ports" not in services["research-api"]
    assert "ports" not in services["research-worker"]
    assert compose["volumes"]["research-data"] is None
    for service in services.values():
        assert "research-data:/var/lib/trade-research" in service["volumes"]
        assert service.get("privileged") is not True
        assert service.get("read_only") is True
        assert service.get("network_mode") != "host"
    assert services["research-api"]["command"][:2] == ["trade-research", "serve"]
    assert services["research-worker"]["command"][:2] == ["trade-research", "worker"]
    assert services["research-api"]["environment"]["TRADE_RESEARCH_API_TOKEN_FILE"].startswith(
        "/run/secrets/"
    )
    assert services["research-api"]["secrets"][0]["mode"] == 0o444


def test_local_compose_override_binds_loopback_by_default() -> None:
    override = yaml.safe_load((ROOT / "compose.override.local.yaml").read_text())
    ports = override["services"]["research-api"]["ports"]
    assert ports == ["${TRADE_RESEARCH_BIND_ADDRESS:-127.0.0.1}:${TRADE_RESEARCH_PORT:-8000}:8000"]


def test_plugin_manifests_and_skills_are_discoverable() -> None:
    codex = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())
    claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    mcp = json.loads((PLUGIN / ".mcp.json").read_text())

    assert codex["name"] == claude["name"] == "trade-research"
    assert codex["version"] == claude["version"]
    assert codex["skills"] == "./skills/"
    assert codex["mcpServers"] == "./.mcp.json"
    assert mcp["mcpServers"]["trade-research"]["command"] == "./scripts/launch-mcp"
    assert mcp["mcpServers"]["trade-research"]["cwd"] == "."
    expected = {
        "setup-and-diagnose",
        "fundamental-analysis",
        "technical-analysis",
        "company-research",
        "create-analysis-skill",
    }
    discovered = {path.parent.name for path in (PLUGIN / "skills").glob("*/SKILL.md")}
    assert discovered == expected
    for name in expected:
        content = (PLUGIN / "skills" / name / "SKILL.md").read_text()
        assert content.startswith("---\nname: " + name + "\n")
        assert "runtime skills are immutable" in content.lower()
        assert not any(term in content for term in ("subprocess.run", "os.system", "exec("))
    company = (PLUGIN / "skills" / "company-research" / "SKILL.md").read_text().lower()
    extension = (PLUGIN / "skills" / "create-analysis-skill" / "SKILL.md").read_text()
    assert "worker" in company and "deadline" in company
    assert "ResearchSkill" in extension and "frozen dataclass" in extension
    assert "python -m build" in extension and "docker compose config" in extension


def test_launcher_resolution_precedence_and_exact_bootstrap_failure(tmp_path: Path) -> None:
    launcher = PLUGIN / "scripts" / "launch-mcp"
    assert launcher.stat().st_mode & stat.S_IXUSR
    home = _fake_project(tmp_path / "home", executable=True)
    claude = _fake_project(tmp_path / "claude", executable=True)

    selected = subprocess.run(
        [str(launcher), "--print-resolution"],
        env={
            **os.environ,
            "TRADE_RESEARCH_HOME": str(home),
            "CLAUDE_PROJECT_DIR": str(claude),
        },
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert selected.returncode == 0
    assert selected.stdout.strip() == str(home.resolve())

    missing = _fake_project(tmp_path / "missing", executable=False)
    failed = subprocess.run(
        [str(launcher), "--print-resolution"],
        env={**os.environ, "TRADE_RESEARCH_HOME": str(missing)},
        check=False,
        capture_output=True,
        text=True,
        cwd=missing,
    )
    assert failed.returncode == 78
    assert failed.stderr.strip() == (
        f"trade-research is not bootstrapped at {missing.resolve()}; run: "
        "python3.12 scripts/bootstrap.py --dev"
    )

    stale = _fake_project(tmp_path / "stale", executable=True)
    python = stale / ".venv" / "bin" / "python"
    python.unlink()
    python.write_text("#!/bin/sh\nexit 1\n")
    python.chmod(0o755)
    rejected = subprocess.run(
        [str(launcher), "--print-resolution"],
        env={**os.environ, "TRADE_RESEARCH_HOME": str(stale)},
        check=False,
        capture_output=True,
        text=True,
        cwd=stale,
    )
    assert rejected.returncode == 78
    assert "python3.12 scripts/bootstrap.py --dev" in rejected.stderr


def test_no_execution_dependencies_or_public_execution_apis() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text().lower()
    source = "\n".join(path.read_text() for path in (ROOT / "src").rglob("*.py")).lower()
    manifests = "\n".join(path.read_text().lower() for path in PLUGIN.rglob("*.[jm][sd][o][nn]"))
    forbidden_dependencies = ("alpaca", "ib_insync", "interactive-brokers", "ccxt==")
    forbidden_apis = ("place_order", "submit_order", "cancel_order", '"/execute', '"/trade')

    assert not any(item in pyproject for item in forbidden_dependencies)
    assert not any(item in source for item in forbidden_apis)
    assert not any(item in manifests for item in forbidden_apis)


def test_docker_and_native_entrypoints_use_the_same_distribution() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    pyproject = (ROOT / "pyproject.toml").read_text()

    assert 'trade-research = "trade_research.cli:app"' in pyproject
    assert "requirements.lock" in dockerfile
    assert "pip install" in dockerfile
    assert "--no-build-isolation" in dockerfile
    assert "src/trade_research" not in dockerfile.split("ENTRYPOINT")[-1]
    for service in compose["services"].values():
        assert service["build"] == "."
        assert service["command"][0] == "trade-research"


def _requirements(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _fake_project(path: Path, *, executable: bool) -> Path:
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text('[project]\nname = "trade-research"\n')
    if executable:
        binary_directory = path / ".venv" / "bin"
        binary_directory.mkdir(parents=True)
        command = binary_directory / "trade-research"
        command.write_text("#!/bin/sh\nexit 0\n")
        command.chmod(0o755)
        (binary_directory / "python").symlink_to(sys.executable)
    return path
