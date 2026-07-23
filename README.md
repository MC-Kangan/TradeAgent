# Trade Research

A **typed, local-first investment research framework** for analysts and LLM agents.
It fetches market data (prices, fundamentals, SEC filings) from public or internal sources,
runs deterministic quantitative analysis, and produces auditable `ResearchReport` objects
in JSON and Markdown. **Analytics only** — it never places trades, connects to brokers,
or holds positions.

Every derived number is traceable back to its source via a closed provenance schema and
SHA-256 content hashes. The framework is designed to be consumed *by* LLM agents (via MCP)
rather than using LLMs internally — no LLM inference calls exist in the codebase.

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  Entry points                                        │
│  CLI (Typer)  │  HTTP REST (FastAPI)  │  MCP stdio   │
└──────────────────────────────────────────────────────┘
                         │
              ResearchApplication
              (transport-neutral use cases)
                         │
               ResearchEngine (async)
              /                      \
    SkillRegistry (frozen)     ProviderRegistry
    FundamentalSkill           (validates contracts)
    TechnicalSkill             YahooPriceProvider
    FilingsSkill               CcxtPriceProvider
                               SecFilingsProvider
                               LocalCsv/Parquet/Sql providers
                         │
              sanitize_report (closed-enum projection)
                         │
              ResearchReport → ReportStore (.json + .md)
```

Key packages under `src/trade_research/`:

| Module | Role |
|---|---|
| `domain/` | Immutable Pydantic types, provenance schema, closed enums |
| `skills/core.py` | `FundamentalSkill`, `TechnicalSkill`, `FilingsSkill`, `SkillRegistry` |
| `providers/` | Protocol contracts, local/remote data fetchers, registry |
| `engine.py` | Async concurrent skill execution, partial-result handling |
| `application.py` | Transport-neutral use cases |
| `queue.py` | WAL SQLite job queue + async worker |
| `reporting.py` | JSON/Markdown renderers, `sanitize_report` |
| `http.py` | FastAPI bearer-auth REST API (loopback/private only) |
| `mcp_server.py` | MCP stdio server — 9 bounded tools for LLM agents |
| `cli.py` | Typer CLI entry point |
| `settings.py` | Pydantic settings from env vars / JSON config file |

---

## Skills

Skills are **immutable frozen dataclasses** registered once at startup. The registry is
read-only at runtime — agents can select skills but cannot add or modify them.

### `fundamental`

Computes 11 accounting ratios from financial statement data: revenue growth, earnings
growth, operating margin, net margin, ROE, free cash flow, FCF margin, leverage, P/E,
EV/EBITDA, and FCF yield.

Requires a `FUNDAMENTALS`-capable provider (local CSV/Parquet/SQL).

### `technical`

Computes 14 price-based indicators from OHLCV history (default 20-bar window): price
return, SMA, EMA, Bollinger Bands, RSI(14), MACD(12/26/9), ATR(14), momentum(10),
annualized volatility, and volume trend.

Requires a `PRICES`-capable provider (Yahoo, CCXT, local files).

### `filings`

Analyzes SEC EDGAR filing history: recent filing count, material-event (8-K) count,
days since latest 10-K, and days since latest 10-Q.

Requires a `FILINGS`-capable provider (SecFilingsProvider with CIK map).

> **Detailed specifications:** See `skills/*/SKILL.md` for full algorithm descriptions,
> input schemas, edge cases, and examples.

---

## Workflow

```
1. AnalysisRequest arrives (symbol + analyst list)
        │  via CLI, HTTP POST, or MCP tool call
        ▼
2. ResearchEngine.validate_analysts()
        │  checks all required provider capabilities are registered
        │  fails immediately with typed error if not
        ▼
3. ProviderRegistry fetches data
        │  enforces: instrument identity, tz-aware timestamps,
        │  finite values, closed provenance, SHA-256 refs, size limits
        ▼
4. Skills run concurrently (asyncio.gather)
        │  pure numerical computation, partial results preserved on failure
        ▼
5. ResearchReviewer → sanitize_report
        │  closed-enum projection — only known algorithm/window/
        │  currency/status/signal values survive
        ▼
6. ResearchReport saved as .json + .md in .trade-research/reports/
        │
        └─► (optional) DiscordNotifier sends "report ready" webhook
```

---

## Setup

### Prerequisites

- **Python 3.12 or 3.13** (3.14+ not yet supported)
- **Requirements:** `requirements.lock` (runtime) and `requirements-dev.lock` (dev/test)

### macOS / Linux

```sh
# Clone and bootstrap (installs exact pinned dependencies):
python3.12 scripts/bootstrap.py --dev

# Verify:
.venv/bin/trade-research doctor
```

The bootstrap script:
- Creates `.venv` with the Python interpreter found at `python3.12`
- Installs from `requirements.lock` and `requirements-dev.lock`
- Installs the local package in editable mode
- Never creates `.env`, secret files, tokens, or credentials
- Refuses to touch unknown non-empty `.venv` directories

### Windows

The bootstrap script is macOS/Linux only. Manual setup (PowerShell):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.lock
.venv\Scripts\pip install -r requirements-dev.lock
.venv\Scripts\pip install --no-deps --no-build-isolation -e .
```

Replace `.venv/bin/trade-research` with `.venv\Scripts\trade-research` throughout the
documentation (or just `trade-research` if the venv is activated).

> **Known Windows issue:** `trade-research doctor` may crash with a swallowed error on
> Windows. The CLI and test suite are otherwise cross-platform once the environment is
> installed.

### Docker

```sh
# Build and run (no ports exposed by default):
TRADE_RESEARCH_SOURCE_DIR=/authorized/sources docker compose up --build

# Local workstation access (binds loopback):
docker compose -f compose.yaml -f compose.override.local.yaml up --build

# Validate Compose file:
docker compose config
```

### Optional: crypto support

```sh
pip install '.[crypto]'   # installs ccxt for crypto price data
```

---

## Configuration

### Environment variables

| Variable | Purpose |
|---|---|
| `TRADE_RESEARCH_CONFIG` | Path to JSON config file |
| `TRADE_RESEARCH_PRICE_PROVIDER` | `yahoo`, `ccxt`, `local_csv`, `local_parquet`, `local_sql` |
| `TRADE_RESEARCH_PRICE_PATH` | Path to local price file (for local providers) |
| `TRADE_RESEARCH_FUNDAMENTAL_PROVIDER` | `local_csv`, `local_parquet`, `local_sql` |
| `TRADE_RESEARCH_FUNDAMENTAL_PATH` | Path to local fundamental file |
| `TRADE_RESEARCH_CCXT_EXCHANGE` | Exchange ID for CCXT, e.g. `binance` |
| `TRADE_RESEARCH_SEC_USER_AGENT` | User-Agent for SEC EDGAR requests (required for filings) |
| `TRADE_RESEARCH_DATA_ROOT` | Root directory that must contain all local provider paths |
| `TRADE_RESEARCH_API_TOKEN` | Bearer token for HTTP API |
| `DISCORD_WEBHOOK_URL` | Discord notification webhook |

### JSON config file

```json
{
  "price_provider": "yahoo",
  "fundamental_provider": "local_csv",
  "fundamental_path": "/data/fundamentals.csv",
  "data_root": "/data",
  "api_token": "your-token-here",
  "sec_user_agent": "Your Org (contact@example.com)",
  "sec_cik_map": {
    "AAPL": "0000320193",
    "MSFT": "0000789019"
  }
}
```

Set `TRADE_RESEARCH_CONFIG=/path/to/config.json`. Environment variables take precedence
over JSON config values for the same field.

### `.env` file

Place a `.env` file in the project root (never commit it):

```dotenv
TRADE_RESEARCH_PRICE_PROVIDER=yahoo
TRADE_RESEARCH_FUNDAMENTAL_PROVIDER=local_csv
TRADE_RESEARCH_FUNDAMENTAL_PATH=/data/fundamentals.csv
TRADE_RESEARCH_DATA_ROOT=/data
TRADE_RESEARCH_API_TOKEN=your-token-here
TRADE_RESEARCH_SEC_USER_AGENT=Your Org (contact@example.com)
```

## CLI commands

```sh
trade-research doctor                               # capability check
trade-research list-skills                          # list registered analysts
trade-research run-skill technical AAPL             # run one skill synchronously
trade-research run-skill technical AAPL --market NASDAQ
trade-research run-skill filings AAPL               # SEC filing analysis
trade-research research AAPL                        # enqueue async job
trade-research research AAPL --analyst technical,fundamental
trade-research report <UUID> --format markdown      # render saved report
trade-research serve --host 127.0.0.1 --port 8000  # start HTTP API
trade-research worker                               # start queue worker
trade-research mcp                                  # start MCP stdio server
```

`run-skill` runs analysis **synchronously** and prints the result immediately.
`research` enqueues a job — you need `worker` running in a separate terminal to process
it, then `report <UUID>` to retrieve the output.

---

## MCP with Claude Code

Register `plugins/trade-research/.mcp.json` in Claude Code's MCP settings. After that,
Claude has access to 9 bounded tools:

| Tool | Purpose |
|---|---|
| `list_skills` | List available analysts |
| `describe_skill` | Get a skill's description, required inputs, and metrics |
| `run_skill` | Execute one skill synchronously and return results |
| `start_research` | Enqueue an async research job |
| `get_research_status` | Check job progress |
| `get_research_result` | Retrieve completed job results |
| `compile_report` | Compile analysis into a formatted report |
| `list_reports` | List saved reports |
| `get_report` | Retrieve a saved report by UUID |

---

## Use cases

| Use case | Skills | Provider |
|---|---|---|
| Equity screening (FCF yield, P/E, ROE) | `fundamental` | `FUNDAMENTALS` (local data) |
| Technical entry/exit signals (RSI, MACD, Bollinger) | `technical` | `PRICES` (Yahoo, CCXT, local) |
| SEC filing review (recency, 8-K activity) | `filings` | `FILINGS` (SEC EDGAR) |
| Combined research note | all | multiple |
| Crypto technical analysis | `technical` | `PRICES` via CCXT |
| LLM-driven analysis via Claude | any | any |

---

## Development

### Adding a new skill

1. Write documentation: `skills/<name>/SKILL.md` and `examples.md`
2. Implement a frozen dataclass in `src/trade_research/skills/core.py` implementing the
   `ResearchSkill` protocol
3. Extend enums in `src/trade_research/domain/provenance.py` if new metrics are needed
4. Register the instance in `src/trade_research/engine.py:default_skills`
5. Export from `src/trade_research/skills/__init__.py`
6. Write tests (TDD per `AGENTS.md`)
7. Run verification:
   ```sh
   .venv/bin/python -m pytest
   .venv/bin/ruff check .
   .venv/bin/mypy
   ```

**Skills are immutable at runtime.** The registry validates that every skill is a frozen
dataclass and rejects any attempt to mutate it after startup. You cannot add skills
without restarting the process.

### Quality gates

```sh
.venv/bin/python -m pytest   # ~298 tests
.venv/bin/ruff check .        # linting (E, F, I, UP, B rules)
.venv/bin/mypy                # strict type checking
```

### Design principles

- **No LLM inference** — the project is a tool *for* LLM agents, not an agent itself
- **Closed provenance** — every derived number traces back to a SHA-256 content hash
- **Frozen at runtime** — skills, providers, and settings are immutable after startup
- **Secret-free logs** — credentials use `SecretStr`, paths use content hashes
- **Bounded everything** — row limits, byte limits, point limits on all providers
- **Fail partial** — if one skill fails, others still complete; if one metric is
  unavailable, others still compute

### Security

See `AGENTS.md` for the full security policy. Key points:
- No broker clients, orders, transactions, or arbitrary code execution
- HTTP binds loopback/private only; bearer auth required
- CLI/MCP tools are bounded — agents can select skills, not write them
- Credentials in env vars or read-only secret files, never persisted in reports
- Path traversal prevented via `data_root` containment check
- Control characters and bidi overrides stripped from all free-text fields

---

## Documentation index

| Document | Content |
|---|---|
| [AGENTS.md](AGENTS.md) | Authoritative development guide |
| [skills/README.md](skills/README.md) | Skill documentation index |
| [skills/fundamental-analysis/SKILL.md](skills/fundamental-analysis/SKILL.md) | Fundamental skill spec |
| [skills/technical-analysis/SKILL.md](skills/technical-analysis/SKILL.md) | Technical skill spec |
| [skills/filings-analysis/SKILL.md](skills/filings-analysis/SKILL.md) | Filings skill spec |
| [skills/review/SKILL.md](skills/review/SKILL.md) | Reviewer spec |
| [docs/fundamental-data-schema.md](docs/fundamental-data-schema.md) | CSV/SQL schema for fundamental data |
| [docs/deployment.md](docs/deployment.md) | Native and Docker deployment |
| [docs/adapters.md](docs/adapters.md) | Enterprise data adapters |
| [docs/provenance-and-licensing.md](docs/provenance-and-licensing.md) | Provenance schema and licensing |
| [docs/provider-contract.md](docs/provider-contract.md) | Provider protocol contracts |
| [docs/hermes.md](docs/hermes.md) | Hermes agent integration |

---

## License

This project is research-only. See the repository license for details.
