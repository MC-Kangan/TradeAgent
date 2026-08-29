from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from typer.testing import CliRunner

from trade_research.cli import app, get_application
from trade_research.http import create_app
from trade_research.mcp_server import BOUNDED_TOOL_NAMES

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
    assert "editables==0.5" in production
    assert "build==1.3.0" in development
    assert configuration["project"]["scripts"]["trade-research"] == (
        "trade_research.cli:main"
    )


def test_bootstrap_dry_run_plans_recreate_for_stale_venv_without_mutation(
    tmp_path: Path,
) -> None:
    for name in ("pyproject.toml", "requirements.lock", "requirements-dev.lock"):
        shutil.copy2(ROOT / name, tmp_path / name)
    environment = tmp_path / ".venv"
    environment.mkdir()
    _write_fake_venv(environment, version=(3, 10))
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
    assert "create replacement virtual environment" in completed.stdout
    assert "atomically replace" in completed.stdout
    assert sentinel.read_text() == "untouched"


@pytest.mark.parametrize("configuration", [None, "version = malformed\n"])
def test_bootstrap_refuses_unknown_nonempty_venv_without_deleting_sentinel(
    tmp_path: Path, configuration: str | None
) -> None:
    _copy_bootstrap_inputs(tmp_path)
    environment = tmp_path / ".venv"
    environment.mkdir()
    sentinel = environment / "do-not-delete"
    sentinel.write_text("unrelated user data")
    if configuration is not None:
        (environment / "pyvenv.cfg").write_text(configuration)

    completed = _run_bootstrap_dry(tmp_path)

    assert completed.returncode != 0
    assert "unknown non-empty .venv" in completed.stderr
    assert sentinel.read_text() == "unrelated user data"
    assert "--clear" not in completed.stdout


def test_bootstrap_allows_empty_venv_without_clear(tmp_path: Path) -> None:
    _copy_bootstrap_inputs(tmp_path)
    (tmp_path / ".venv").mkdir()

    completed = _run_bootstrap_dry(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert " -m venv " in completed.stdout
    assert "--clear" not in completed.stdout


def test_bootstrap_uses_actual_interpreter_not_current_claim_in_cfg(tmp_path: Path) -> None:
    _copy_bootstrap_inputs(tmp_path)
    environment = tmp_path / ".venv"
    _write_fake_venv(environment, version=(3, 10), configured=sys.version_info[:2])

    completed = _run_bootstrap_dry(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert "create replacement virtual environment" in completed.stdout
    assert "atomically replace" in completed.stdout


def test_bootstrap_atomically_rebuilds_only_recognized_stale_venv(tmp_path: Path) -> None:
    environment = tmp_path / ".venv"
    _write_fake_venv(environment, version=(3, 10))
    bootstrap = _load_bootstrap()

    bootstrap.prepare_environment(environment, dry_run=False)

    probe = subprocess.run(
        [
            str(environment / "bin" / "python"),
            "-c",
            "import json,sys; print(json.dumps([list(sys.version_info[:2]), sys.prefix]))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    version, prefix = json.loads(probe.stdout)
    assert tuple(version) == sys.version_info[:2]
    assert Path(prefix).resolve() == environment.resolve()
    assert not list(tmp_path.glob(".venv.bootstrap-*"))


def test_bootstrap_restores_validated_backup_if_atomic_install_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = tmp_path / ".venv"
    _write_fake_venv(environment, version=(3, 10))
    bootstrap = _load_bootstrap()
    original_rename = Path.rename

    def fail_replacement(source: Path, target: Path) -> Path:
        if source.name.startswith(".venv.bootstrap-new-"):
            raise OSError("injected atomic install failure")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", fail_replacement)

    with pytest.raises(OSError, match="injected atomic install failure"):
        bootstrap.prepare_environment(environment, dry_run=False)

    assert environment.is_dir()
    assert bootstrap._environment_state(environment) is bootstrap.EnvironmentState.STALE
    assert not list(tmp_path.glob(".venv.bootstrap-*"))


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
    assert (
        payload["skills"]["price_action_structure_analysis"]
        == payload["skills"]["technical_analysis"]
    )
    assert payload["llm"]["configured"] is True
    assert payload["discord"]["configured"] is True
    assert str(tmp_path) not in serialized
    for secret in secret_values.values():
        assert secret not in serialized


def test_compose_defaults_are_non_public_and_share_named_storage() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = compose["services"]

    assert set(services) == {"research-api", "research-worker", "hermes"}
    assert "ports" not in services["research-api"]
    assert "ports" not in services["research-worker"]
    assert compose["volumes"]["research-data"] is None
    for service in (services["research-api"], services["research-worker"]):
        assert "research-data:/var/lib/trade-research" in service["volumes"]
        source_mount = next(
            volume
            for volume in service["volumes"]
            if isinstance(volume, dict)
            and volume.get("target") == "/var/lib/trade-research-sources"
        )
        assert source_mount["type"] == "bind"
        assert source_mount["read_only"] is True
        assert source_mount["source"] == "${TRADE_RESEARCH_SOURCE_DIR:-./sources}"
        assert service["environment"]["TRADE_RESEARCH_CONFIG"] == (
            "${TRADE_RESEARCH_CONFIG:-/var/lib/trade-research-sources/config.json}"
        )
        assert service["environment"]["TRADE_RESEARCH_SEC_USER_AGENT"] == (
            "${TRADE_RESEARCH_SEC_USER_AGENT:-}"
        )
        assert service["environment"]["TRADE_RESEARCH_DATA_ROOT"] == (
            "/var/lib/trade-research"
        )
        assert service.get("privileged") is not True
        assert service.get("read_only") is True
        assert service.get("network_mode") != "host"
    assert services["research-api"]["command"][:2] == ["trade-research", "serve"]
    assert services["research-worker"]["command"][:2] == ["trade-research", "worker"]
    assert services["research-api"]["environment"]["TRADE_RESEARCH_API_TOKEN_FILE"].startswith(
        "/run/secrets/"
    )
    assert services["research-api"]["secrets"][0]["mode"] == 0o444
    hermes = services["hermes"]
    assert hermes["profiles"] == ["hermes"]
    assert hermes["image"] == "${HERMES_IMAGE:-trade-research:local}"
    assert "ports" not in hermes
    assert hermes["read_only"] is True
    assert hermes["environment"]["TRADE_RESEARCH_API_URL"] == "http://research-api:8000"
    assert hermes["environment"]["TRADE_RESEARCH_API_TOKEN_FILE"] == "/run/secrets/api-token"
    assert hermes["environment"]["HERMES_IMAGE_CONFIGURED"] == "${HERMES_IMAGE:-}"


def test_rendered_compose_keeps_authorized_sources_read_only_for_api_and_worker() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is unavailable")
    completed = subprocess.run(
        ["docker", "compose", "config"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    rendered = yaml.safe_load(completed.stdout)
    expected_source = str((ROOT / "sources").resolve())
    for service_name in ("research-api", "research-worker"):
        service = rendered["services"][service_name]
        source_mount = next(
            volume
            for volume in service["volumes"]
            if volume.get("target") == "/var/lib/trade-research-sources"
        )
        assert source_mount == {
            "type": "bind",
            "source": expected_source,
            "target": "/var/lib/trade-research-sources",
            "read_only": True,
        }
        assert (
            service["environment"]["TRADE_RESEARCH_CONFIG"]
            == "/var/lib/trade-research-sources/config.json"
        )


def test_hermes_profile_preflight_and_compose_render(tmp_path: Path) -> None:
    entrypoint = ROOT / "scripts" / "hermes-entrypoint"
    missing = subprocess.run(
        [str(entrypoint), "sh", "-c", "exit 0"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 78
    assert missing.stderr.strip() == (
        "HERMES_IMAGE must name an explicitly configured trusted digest-pinned image"
    )
    configured = subprocess.run(
        [str(entrypoint), "sh", "-c", "exit 0"],
        env={**os.environ, "HERMES_IMAGE_CONFIGURED": "registry.invalid/hermes@sha256:" + "a" * 64},
        check=False,
    )
    assert configured.returncode == 0

    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is not installed")
    default = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "hermes" not in json.loads(default.stdout)["services"]
    image = "registry.invalid/hermes@sha256:" + "b" * 64
    profile = subprocess.run(
        ["docker", "compose", "--profile", "hermes", "config", "--format", "json"],
        cwd=ROOT,
        env={**os.environ, "HERMES_IMAGE": image},
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = json.loads(profile.stdout)["services"]["hermes"]
    assert rendered["image"] == image
    assert "ports" not in rendered
    assert rendered["environment"]["HERMES_IMAGE_CONFIGURED"] == image


def test_local_compose_override_binds_loopback_by_default() -> None:
    override = yaml.safe_load((ROOT / "compose.override.local.yaml").read_text())
    ports = override["services"]["research-api"]["ports"]
    assert ports == ["127.0.0.1:${TRADE_RESEARCH_PORT:-8000}:8000"]


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


def test_launcher_executes_from_resolved_project_root_not_external_cwd(tmp_path: Path) -> None:
    project = _fake_project(tmp_path / "project", executable=True)
    command = project / ".venv" / "bin" / "trade-research"
    command.write_text(
        "#!/bin/sh\n"
        "mkdir -p .trade-research\n"
        "pwd > .trade-research/launcher-cwd\n"
    )
    command.chmod(0o755)
    caller = tmp_path / "external-caller"
    caller.mkdir()

    completed = subprocess.run(
        [str(PLUGIN / "scripts" / "launch-mcp")],
        cwd=caller,
        env={**os.environ, "TRADE_RESEARCH_HOME": str(project)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert (project / ".trade-research" / "launcher-cwd").read_text().strip() == str(
        project.resolve()
    )
    assert not (caller / ".trade-research").exists()
    assert not (PLUGIN / ".trade-research").exists()


def test_public_interfaces_and_dependencies_match_exact_research_only_allowlists() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert set(configuration["project"]["dependencies"]) == {
        "backtesting==0.6.6",
        "fastapi==0.116.1",
        "httpx==0.28.1",
        "mcp==1.12.4",
        "pydantic==2.11.7",
        "pyarrow==18.1.0",
        "typer==0.16.0",
        "uvicorn==0.35.0",
    }
    assert {command.name for command in app.registered_commands} == {
        "doctor",
        "list-skills",
        "run-skill",
        "research",
        "report",
        "serve",
        "worker",
        "mcp",
    }
    http_routes = {
        (method, route.path)
        for route in create_app(object(), bearer_token="test-token").routes  # type: ignore[arg-type]
        for method in route.methods or set()
    }
    assert http_routes == {
        ("GET", "/skills"),
        ("GET", "/skills/{name}"),
        ("POST", "/skills/{name}/run"),
        ("POST", "/analyze"),
        ("POST", "/research"),
        ("GET", "/research/{request_id}/status"),
        ("GET", "/research/{request_id}/result"),
        ("POST", "/reports/{request_id}"),
        ("GET", "/reports"),
        ("GET", "/reports/{request_id}"),
    }
    assert BOUNDED_TOOL_NAMES == (
        "list_skills",
        "describe_skill",
        "run_skill",
        "start_research",
        "get_research_status",
        "get_research_result",
        "compile_report",
        "list_reports",
        "get_report",
    )


def test_plugin_artifacts_contain_no_prohibited_runtime_surface() -> None:
    artifacts = [
        *PLUGIN.glob(".*-plugin/plugin.json"),
        PLUGIN / ".mcp.json",
        *PLUGIN.glob("skills/*/SKILL.md"),
    ]
    content = "\n".join(path.read_text().lower() for path in artifacts)
    prohibited = {
        "place_order",
        "submit_order",
        "cancel_order",
        "broker_client",
        '"/execute',
        '"/trade',
        "arbitrary shell tool",
        "arbitrary python tool",
        "arbitrary sql tool",
        "filesystem tool",
        "runtime self-modification",
    }
    assert not prohibited.intersection(content.split())
    for term in prohibited:
        assert term not in content


def test_docker_and_native_entrypoints_use_the_same_distribution() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    pyproject = (ROOT / "pyproject.toml").read_text()

    assert 'trade-research = "trade_research.cli:main"' in pyproject
    assert "requirements.lock" in dockerfile
    assert "pip install" in dockerfile
    assert "--no-build-isolation" in dockerfile
    assert dockerfile.startswith(
        "ARG PYTHON_BASE_IMAGE=python:3.12-slim\nFROM ${PYTHON_BASE_IMAGE}"
    )
    assert "PYTHON_BASE_IMAGE" in (ROOT / "docs" / "deployment.md").read_text()
    assert "@sha256:" in (ROOT / "docs" / "deployment.md").read_text()
    assert "src/trade_research" not in dockerfile.split("ENTRYPOINT")[-1]
    for service in (compose["services"]["research-api"], compose["services"]["research-worker"]):
        assert service["build"]["context"] == "."
        assert service["build"]["args"]["PYTHON_BASE_IMAGE"] == (
            "${PYTHON_BASE_IMAGE:-python:3.12-slim}"
        )
        assert service["command"][0] == "trade-research"


def _requirements(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _copy_bootstrap_inputs(path: Path) -> None:
    for name in ("pyproject.toml", "requirements.lock", "requirements-dev.lock"):
        shutil.copy2(ROOT / name, path / name)


def _run_bootstrap_dry(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "bootstrap.py"),
            "--dry-run",
            "--project-root",
            str(project),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _load_bootstrap() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "trade_research_bootstrap_test", ROOT / "scripts" / "bootstrap.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_fake_venv(
    environment: Path,
    *,
    version: tuple[int, int],
    configured: tuple[int, int] | None = None,
) -> None:
    binary = environment / "bin"
    binary.mkdir(parents=True)
    configured = version if configured is None else configured
    (environment / "pyvenv.cfg").write_text(
        f"version = {configured[0]}.{configured[1]}.0\n"
    )
    interpreter = binary / "python"
    interpreter.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' '{{\"version\":[{version[0]},{version[1]}],"
        f"\"prefix\":\"{environment.resolve()}\"}}'\n"
    )
    interpreter.chmod(0o755)


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
