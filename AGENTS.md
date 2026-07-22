# Trade Research agent guide

This file is authoritative for this repository. Trade Research is analytics-only. Do not add broker clients, orders, transaction endpoints, arbitrary code or shell execution, arbitrary SQL, or runtime skill mutation. Do not log or persist secrets, account identifiers, client IPs, or raw positions.

## Setup

- Use Python 3.12 on macOS or Linux: `python3.12 scripts/bootstrap.py --dev`.
- Dependencies come from `requirements.lock` and `requirements-dev.lock`; keep exact pins compatible with `pyproject.toml`.
- Bootstrap creates `.venv` and installs the local package editable. It must never create `.env`, secret files, tokens, or credentials.
- Bootstrap refuses unknown non-empty `.venv` directories. It may atomically replace only an interpreter-validated stale virtual environment and must restore its validated backup on failure.
- Run `.venv/bin/trade-research doctor` after configuration. Treat its output as presence/status metadata only.

## Development and tests

- Follow test-driven development: add a focused failing test, verify the reason, implement the minimum change, then refactor while green.
- Before completion run: `.venv/bin/python -m pytest`, `.venv/bin/ruff check .`, `.venv/bin/mypy`, and `.venv/bin/python -m build` when `build` is installed.
- Validate Compose with `docker compose config` when Docker Compose exists. Do not start services as part of tests or validation.
- Preserve native/HTTP/MCP/Docker behavior through the same `trade_research` package and report schema.

## Providers, provenance, and skills

- Providers implement bounded typed protocols and return validated observations with source, timestamp, vendor field where licensed, and safe content hashes.
- Keep raw licensed payloads in the user's authorized system. Do not copy Bloomberg or internal data into fixtures, logs, reports, or commits.
- Analyst skills are fixed code registered at process startup. Runtime agents may select analysts but cannot write or load code.
- To add an analyst, write contract/numerical tests, implement against provider protocols, register a fixed instance, run all quality gates, then restart/rebuild and verify discovery.
- Runtime plugin workflows may call only bounded CLI/MCP setup, research, and report operations. `create-analysis-skill` is a development-only coding workflow used after an explicit change request; it never modifies a running registry.

## Secrets and deployment

- Reference secrets through environment variables or read-only secret files. Commit examples with names only, never values.
- Compose publishes no ports by default. `compose.override.local.yaml` binds loopback unless an explicit private VPN address is supplied.
- The optional Hermes profile stays disabled unless `HERMES_IMAGE` is an approved digest-pinned image; the sentinel default must fail closed.
- Release container builds must supply an approved digest-pinned `PYTHON_BASE_IMAGE`; never invent a digest.
- HTTP requires bearer authentication and access logs remain disabled. Never expose it on a public interface.
