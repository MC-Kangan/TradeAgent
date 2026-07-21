# Local Research Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a research-only, extensible Python 3.12 framework for US, UK, EU, ETF, and crypto analysis, callable natively, through Docker, and by Codex, Claude Code, or Hermes.

**Architecture:** A typed application core owns instruments, evidence, analyst results, and reports. Replaceable providers feed immutable analyst skills; an asyncio engine coordinates them and exposes the same behavior through Python, CLI, HTTP, and MCP. SQLite stores jobs, Parquet stores observations, and no execution capability exists.

**Tech Stack:** Python 3.12, Pydantic 2, pandas, pyarrow, aiosqlite, Typer, FastAPI, uvicorn, httpx, MCP Python SDK, pytest, pytest-asyncio, ruff, mypy.

## Global Constraints

- Research and analytics only: no broker clients, order types, or transaction endpoints.
- Support US, UK, EU, ETF, and optional crypto; explicitly reject Asian exchanges in scope.
- Secrets, account identifiers, client IPs, and raw positions must not enter logs, reports, notifications, or persisted job inputs.
- Skills are immutable at runtime and external agents receive bounded typed tools only.
- Native and Docker deployments use the same Python package and report schema.
- Default analysts are fundamental and technical; one or more analysts may be selected without a fixed debate topology.
- Third-party review checkouts remain ignored reference material.

---

### Task 1: Package foundation, domain models, and storage

**Files:** Create project configuration, `src/trade_research/domain/`, `src/trade_research/storage/`, and focused tests under `tests/`.

**Interfaces:** Produce `InstrumentId`, `AnalysisRequest`, `Observation`, `Evidence`, `AnalystResult`, `ResearchReport`, `Position`, market validation, `RunStore`, and `ObservationStore` for later tasks.

- [ ] Write failing tests for typed serialization, default analysts, market acceptance/rejection, SQLite WAL persistence, secret-free persisted requests, and Parquet observation round trips.
- [ ] Run those tests and confirm failures are caused by missing implementation.
- [ ] Add package metadata, pinned compatible dependencies, configuration, domain models, market validation, SQLite run storage, Parquet observation storage, and ignore rules.
- [ ] Run task tests, ruff, and mypy; correct failures without broadening scope.
- [ ] Commit the independently working foundation.

### Task 2: Provider contracts and analyst skills

**Files:** Create `src/trade_research/providers/`, `src/trade_research/skills/`, and provider/skill tests.

**Interfaces:** Produce provider protocols and registries, local CSV/Parquet/SQL and portfolio providers, Yahoo/Stooq and SEC adapters, optional CCXT adapter, `SkillRegistry`, fundamental and technical skills, compiler, and reviewer.

- [ ] Write failing contract and numerical tests using deterministic fixtures, including a Bloomberg-shaped mock provider.
- [ ] Confirm the tests fail for missing provider and skill behavior.
- [ ] Implement typed provider boundaries and the basic fundamental and technical factors from the specification, with provenance on every observation.
- [ ] Implement immutable skill discovery, compilation, review, and partial-data labelling.
- [ ] Run task tests, ruff, and mypy and commit.

### Task 3: Engine and bounded interfaces

**Files:** Create the engine, reporting, CLI, HTTP, MCP, worker, notification modules, and interface/security tests.

**Interfaces:** Produce `ResearchEngine.from_settings()` / `analyze()`, the specified CLI commands, authenticated HTTP endpoints, bounded MCP tools, a SQLite worker, and redacted Discord summaries.

- [ ] Write failing tests for analyst selection/concurrency, partial failures, persisted queue recovery, equivalent CLI/HTTP/MCP results, bearer authentication, prompt-injection isolation, and sensitive-data redaction.
- [ ] Confirm failures, then implement the minimum shared application services and adapters required by all interfaces.
- [ ] Ensure MCP/HTTP cannot run shell, arbitrary Python, arbitrary SQL, filesystem operations, or execution functions.
- [ ] Disable access logs and avoid client-IP persistence; validate all external content as evidence rather than instructions.
- [ ] Run task tests, ruff, mypy, and commit.

### Task 4: Portable setup, Docker, plugins, and documentation

**Files:** Create bootstrap/doctor tooling, Docker assets, `AGENTS.md`, `CLAUDE.md`, the dual Codex/Claude plugin, Hermes guide, migration guide, and deployment tests.

**Interfaces:** Produce `python3.12 scripts/bootstrap.py --dev`, `trade-research doctor`, Compose `research-api` and `research-worker` services with optional Hermes profile, and plugin setup/research skills with a portable MCP launcher.

- [ ] Write failing tests for bootstrap dry-run behavior, doctor redaction, Compose secure defaults, plugin manifest/skill discovery, MCP launcher resolution, and absence of execution dependencies or APIs.
- [ ] Confirm failures and implement native setup and locked dependency workflows for macOS/Linux.
- [ ] Implement Docker using the same package and storage schema, with no published port by default and an explicit localhost/VPN override.
- [ ] Add agent instructions, plugin manifests, setup/analysis/create-skill workflows, Hermes CLI/stdio/HTTP guidance, and Bloomberg/internal database migration documentation.
- [ ] Run the complete test, lint, typing, build, Compose validation, and static security suite; commit.
