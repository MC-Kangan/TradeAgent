#!/usr/bin/env python3
"""Create the local virtual environment without collecting secrets."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from enum import Enum
from pathlib import Path

SUPPORTED_PLATFORMS = frozenset({"darwin", "linux"})
SUPPORTED_PYTHON = {(3, 12), (3, 13)}
_INTERPRETER_PROBE = (
    "import json,sys; "
    'print(json.dumps({"version": list(sys.version_info[:2]), "prefix": sys.prefix}))'
)


class EnvironmentState(Enum):
    ABSENT = "absent"
    EMPTY = "empty"
    CURRENT = "current"
    STALE = "stale"
    UNKNOWN = "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true", help="install development dependencies")
    parser.add_argument(
        "--dry-run", action="store_true", help="print commands without changing files"
    )
    parser.add_argument("--project-root", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args()

    if sys.platform not in SUPPORTED_PLATFORMS:
        parser.error("bootstrap supports macOS and Linux")
    if sys.version_info[:2] not in SUPPORTED_PYTHON:
        parser.error("bootstrap requires Python 3.12 or 3.13")

    project_root = (options.project_root or Path(__file__).resolve().parents[1]).resolve()
    _require_project(project_root)
    environment = project_root / ".venv"
    prepare_environment(environment, dry_run=options.dry_run)
    python = environment / "bin" / "python"
    commands = [
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--requirement",
            str(project_root / "requirements.lock"),
        ],
    ]
    if options.dev:
        commands.append(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--requirement",
                str(project_root / "requirements-dev.lock"),
            ]
        )
    commands.append(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "--editable",
            str(project_root),
        ]
    )

    for command in commands:
        print(shlex.join(command))
        if not options.dry_run:
            subprocess.run(command, check=True)
    return 0


def _require_project(project_root: Path) -> None:
    required = ("pyproject.toml", "requirements.lock", "requirements-dev.lock")
    missing = [name for name in required if not (project_root / name).is_file()]
    if missing:
        raise SystemExit(f"project root is missing required files: {', '.join(missing)}")


def prepare_environment(environment: Path, *, dry_run: bool) -> None:
    """Create or safely replace one interpreter-validated virtual environment."""

    state = _environment_state(environment)
    if state is EnvironmentState.UNKNOWN:
        raise SystemExit("refusing to modify unknown non-empty .venv directory")
    if state is EnvironmentState.CURRENT:
        print(f"reuse validated virtual environment: {environment}")
        return
    if state in {EnvironmentState.ABSENT, EnvironmentState.EMPTY}:
        command = [sys.executable, "-m", "venv", str(environment)]
        print(shlex.join(command))
        if not dry_run:
            subprocess.run(command, check=True)
            _require_current_environment(environment)
        return
    _replace_stale_environment(environment, dry_run=dry_run)


def _replace_stale_environment(environment: Path, *, dry_run: bool) -> None:
    token = uuid.uuid4().hex
    replacement = environment.parent / f".venv.bootstrap-new-{token}"
    backup = environment.parent / f".venv.bootstrap-backup-{token}"
    create = [sys.executable, "-m", "venv", str(replacement)]
    refresh = [sys.executable, "-m", "venv", "--upgrade", str(environment)]
    print(f"create replacement virtual environment: {shlex.join(create)}")
    print(f"atomically replace: {environment} -> {backup}; {replacement} -> {environment}")
    print(f"refresh replacement at final path: {shlex.join(refresh)}")
    print(f"remove validated backup after successful replacement: {backup}")
    if dry_run:
        return

    backup_created = False
    replacement_installed = False
    try:
        subprocess.run(create, check=True)
        _require_current_environment(replacement)
        environment.rename(backup)
        backup_created = True
        replacement.rename(environment)
        replacement_installed = True
        subprocess.run(refresh, check=True)
        _require_current_environment(environment)
    except Exception:
        if replacement_installed:
            if environment.exists() and not environment.is_symlink():
                shutil.rmtree(environment)
        if backup_created:
            backup.rename(environment)
        raise
    finally:
        if replacement.exists() and not replacement.is_symlink():
            shutil.rmtree(replacement)
    shutil.rmtree(backup)


def _environment_state(environment: Path) -> EnvironmentState:
    if not environment.exists() and not environment.is_symlink():
        return EnvironmentState.ABSENT
    if environment.is_symlink() or not environment.is_dir():
        return EnvironmentState.UNKNOWN
    try:
        if next(environment.iterdir(), None) is None:
            return EnvironmentState.EMPTY
    except OSError:
        return EnvironmentState.UNKNOWN
    configured = _configured_version(environment)
    actual = _probe_interpreter(environment)
    if configured is None or actual is None:
        return EnvironmentState.UNKNOWN
    actual_version, actual_prefix = actual
    try:
        prefix_matches = actual_prefix.resolve() == environment.resolve()
    except OSError:
        return EnvironmentState.UNKNOWN
    if not prefix_matches:
        return EnvironmentState.UNKNOWN
    if configured == sys.version_info[:2] and actual_version == sys.version_info[:2]:
        return EnvironmentState.CURRENT
    return EnvironmentState.STALE


def _configured_version(environment: Path) -> tuple[int, int] | None:
    try:
        lines = (environment / "pyvenv.cfg").read_text().splitlines()[:32]
    except OSError:
        return None
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip().lower() == "version":
            parts = value.strip().split(".")
            try:
                return int(parts[0]), int(parts[1])
            except (IndexError, ValueError):
                return None
    return None


def _probe_interpreter(environment: Path) -> tuple[tuple[int, int], Path] | None:
    interpreter = environment / "bin" / "python"
    if not interpreter.exists() or not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        return None
    try:
        completed = subprocess.run(
            [str(interpreter), "-c", _INTERPRETER_PROBE],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        payload = json.loads(completed.stdout)
        version = payload["version"]
        prefix = payload["prefix"]
        if not isinstance(version, list) or len(version) != 2 or not isinstance(prefix, str):
            return None
        parsed_version = int(version[0]), int(version[1])
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ):
        return None
    return parsed_version, Path(prefix)


def _require_current_environment(environment: Path) -> None:
    if _environment_state(environment) is not EnvironmentState.CURRENT:
        raise RuntimeError("new virtual environment failed interpreter validation")


if __name__ == "__main__":
    raise SystemExit(main())
