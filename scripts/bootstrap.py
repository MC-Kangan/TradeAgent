#!/usr/bin/env python3
"""Create the local virtual environment without collecting secrets."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

SUPPORTED_PLATFORMS = frozenset({"darwin", "linux"})
SUPPORTED_PYTHON = {(3, 12), (3, 13)}


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
    python = environment / "bin" / "python"
    commands = [
        _venv_command(environment),
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


def _venv_command(environment: Path) -> list[str]:
    if environment.is_symlink():
        raise SystemExit("refusing to use a symlinked .venv directory")
    if environment.exists() and not environment.is_dir():
        raise SystemExit("refusing to replace a non-directory .venv path")
    command = [sys.executable, "-m", "venv"]
    if environment.exists() and _venv_version(environment) != sys.version_info[:2]:
        command.append("--clear")
    command.append(str(environment))
    return command


def _venv_version(environment: Path) -> tuple[int, int] | None:
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


if __name__ == "__main__":
    raise SystemExit(main())
