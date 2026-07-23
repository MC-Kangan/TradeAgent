# TradeAgent (trade-research) — Project Description

## What it is

A **typed, local-first investment research framework** for analysts and LLM agents. It
fetches market data (prices, fundamentals, SEC filings) from public or internal sources,
runs deterministic quantitative analysis, and produces auditable `ResearchReport` objects
in JSON and Markdown. It **never places trades**, connects to brokers, or holds positions —
analytics only.

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
                               StooqPriceProvider
                               CcxtPriceProvider
                               SecFilingsProvider
                               LocalCsv/Parquet/Sql providers
                         │
              sanitize_report (closed-enum projection)
                         │
              ResearchReport → ReportStore (.json + .md)
                         │
              (optional) DiscordNotifier
```

Key packages under `src/trade_research/`:

| Module | Role |
|---|---|
| `domain/` | Immutable Pydantic types, provenance schema, closed enums |
| `skills/core.py` | `FundamentalSkill`, `TechnicalSkill`, `SkillRegistry` |
| `providers/` | Protocol contracts, local and remote data fetchers |
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
Computes 11 accounting ratios from financial statement data:

| Metric | Formula |
|---|---|
| `revenue_growth` | (revenue_current − revenue_prior) / revenue_prior |
| `earnings_growth` | YoY/QoQ net income growth |
| `operating_margin` | operating_income / revenue |
| `net_margin` | net_income / revenue |
| `return_on_equity` | net_income / shareholders_equity |
| `free_cash_flow` | direct FCF value |
| `free_cash_flow_margin` | FCF / revenue |
| `leverage` | total_debt / equity |
| `price_to_earnings` | market_cap / net_income |
| `enterprise_value_to_ebitda` | EV / EBITDA |
| `free_cash_flow_yield` | FCF / market_cap |

Requires a `FUNDAMENTALS`-capable provider (local CSV/Parquet/SQL or Bloomberg adapter).

### `technical`
Computes 13 price-based indicators from OHLCV history (default 20-bar window):

| Metric | Parameters |
|---|---|
| `price_return` | Full history close-to-close return |
| `simple_moving_average` | SMA(20) |
| `exponential_moving_average_20` | EMA(20) |
| `bollinger_middle/upper/lower_20` | SMA ± 2σ over 20 bars |
| `relative_strength_index_14` | Wilder RSI(14) |
| `macd_12_26` + `macd_signal_9` + `macd_histogram` | Standard MACD |
| `average_true_range_14` | Wilder ATR(14) |
| `momentum_10` | 10-period price return |
| `annualized_volatility_20` | 20-day returns × √252 |
| `volume_trend_20` | Last 20 bars vs prior 20 bars avg volume |

Requires a `PRICES`-capable provider (Yahoo, Stooq, CCXT, local files).

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
5. ResearchReviewer
        │  flags results with no observations as partial
        ▼
6. sanitize_report
        │  closed-enum projection — only known algorithm/window/
        │  currency/status/signal values survive
        ▼
7. ResearchReport saved to .trade-research/reports/ as .json + .md
        │
        └─► (optional) DiscordNotifier sends canned "report ready" webhook

Async queue path:
  start_research → SQLite WAL queue → worker claims → runs steps 3-7 → marks complete
  HTTP: GET /research/{id}/status, GET /research/{id}/result
```

---

## How to run

### Windows compatibility

> **The bootstrap script does not support Windows.** `scripts/bootstrap.py` line 42–43
> hard-exits on any platform that is not `darwin` or `linux`. Additionally, all path
> references in `AGENTS.md` and bootstrap use Unix conventions (`.venv/bin/python`,
> forward slashes). The CLI and tests themselves are cross-platform once the environment
> is installed — only the bootstrap and docs are Unix-only.
>
> **Manual setup on Windows (PowerShell):**
> ```powershell
> py -3.12 -m venv .venv
> .venv\Scripts\pip install -r requirements.lock
> .venv\Scripts\pip install -r requirements-dev.lock
> .venv\Scripts\pip install --no-deps --no-build-isolation -e .
> ```
> Replace `.venv/bin/trade-research` with `.venv\Scripts\trade-research` (or just
> `trade-research` if the venv is activated) throughout.
>
> **Known Windows-specific bug:** `trade-research doctor` crashes with
> `"Error: request could not be processed"`. The exception is swallowed by the CLI's
> fail-closed handler. The most likely cause is a Windows-incompatible path or
> subprocess call inside `run_doctor()`.

### Dependency lock files

The project ships two lock files that together pin every dependency to an exact version:

- **`requirements.lock`** — runtime dependencies only (FastAPI, Pydantic, MCP SDK,
  PyArrow, Typer, uvicorn, etc.). Everything needed to run the server, CLI, and MCP
  adapter. Install this in production.

- **`requirements-dev.lock`** — additional development/test dependencies (pytest,
  mypy, ruff, build tooling). Never needed in production. Install this when doing
  development or running the test suite.

Both files list exact `==` pins (no ranges) so the environment is fully reproducible
across machines. They are generated from `pyproject.toml` and should be regenerated
whenever dependencies change. The comment at the top of `requirements.lock` says these
are for Python 3.12/3.13 on macOS and Linux — Windows compatibility of individual
packages is not guaranteed.

### Configuration via `.env` file

The project uses `python-dotenv` (included in `requirements.lock`). You can place a
`.env` file in the project root instead of setting shell environment variables. Example:

```dotenv
# .env  — never commit this file
TRADE_RESEARCH_PRICE_PROVIDER=yahoo
TRADE_RESEARCH_FUNDAMENTAL_PROVIDER=local_csv
TRADE_RESEARCH_FUNDAMENTAL_PATH=C:\data\fundamentals.csv
TRADE_RESEARCH_DATA_DIR=.trade-research
TRADE_RESEARCH_API_TOKEN=your-token-here
```

Variables in `.env` are loaded automatically by Pydantic settings. Shell environment
variables take precedence over `.env` values. Never commit `.env` to version control —
add it to `.gitignore`.

Alternatively, pass a JSON config file:
```dotenv
TRADE_RESEARCH_CONFIG=C:\Users\you\trade-research-config.json
```

The JSON config shape mirrors the env var names (lowercase, without the
`TRADE_RESEARCH_` prefix).

### Setup (macOS / Linux)

```sh
python3.12 scripts/bootstrap.py --dev
.venv/bin/trade-research doctor        # check which providers are available
```

### CLI commands

```sh
trade-research doctor                               # capability check
trade-research list-skills                          # list registered analysts
# note: describe-skill is MCP/HTTP only — no CLI command exists
trade-research run-skill technical AAPL             # run one skill synchronously
trade-research run-skill technical AAPL --market NASDAQ
trade-research research AAPL                        # enqueue async job (needs worker)
trade-research research AAPL --analyst technical    # enqueue with selected skill
trade-research report <UUID> --format markdown      # render saved report
trade-research serve --host 127.0.0.1 --port 8000  # start HTTP API (needs API token)
trade-research worker                               # start queue worker
trade-research mcp                                  # start MCP stdio server
```

`run-skill` runs analysis **synchronously** and prints the result immediately.
`research` only enqueues a job and returns a job ID — you need `worker` running in a
separate terminal to process it, then `report <UUID>` to retrieve the output.

### Key environment variables

| Variable | Purpose |
|---|---|
| `TRADE_RESEARCH_CONFIG` | Path to JSON config file |
| `TRADE_RESEARCH_PRICE_PROVIDER` | `yahoo`, `stooq`, `ccxt`, `local_csv`, `local_parquet`, `local_sql` |
| `TRADE_RESEARCH_PRICE_PATH` | Path to local price file (for local providers) |
| `TRADE_RESEARCH_FUNDAMENTAL_PROVIDER` | Same options as price provider |
| `TRADE_RESEARCH_FUNDAMENTAL_PATH` | Path to local fundamental file |
| `TRADE_RESEARCH_CCXT_EXCHANGE` | Exchange ID for CCXT, e.g. `binance` |
| `TRADE_RESEARCH_DATA_DIR` | Storage root (default: `.trade-research`) |
| `TRADE_RESEARCH_API_TOKEN` | Bearer token for HTTP API |
| `DISCORD_WEBHOOK_URL` | Discord notification webhook |

### Docker Compose

```sh
# Standard (no ports exposed by default):
TRADE_RESEARCH_SOURCE_DIR=/authorized/sources docker compose up --build

# Local workstation access (binds loopback):
docker compose -f compose.yaml -f compose.override.local.yaml up --build
```

### MCP with Claude Code

Register `plugins/trade-research/.mcp.json` in Claude Code's MCP settings. After that,
Claude has access to 9 tools: `list_skills`, `describe_skill`, `run_skill`,
`start_research`, `get_research_status`, `get_research_result`, `compile_report`,
`list_reports`, `get_report`.

---

## Adding a new skill

Skills are registered at `engine.py:67` inside `ResearchEngine.from_settings()`:

```python
default_skills = cast(tuple[ResearchSkill, ...], (FundamentalSkill(), TechnicalSkill()))
selected_skills = skills or SkillRegistry(default_skills)
```

To add a skill:

1. **Implement** a frozen dataclass in `src/trade_research/skills/core.py` (or a new
   module) implementing the `ResearchSkill` protocol:
   - `name: str` — unique lowercase kebab slug matching `ANALYST_PATTERN`
   - `required_capabilities: tuple[CapabilityName, ...]`
   - `analyze(instrument, providers) -> AnalystResult`
   - Must be `@dataclass(frozen=True)` with only immutable fields.

2. **Register** the instance in `engine.py` by adding it to `default_skills`:
   ```python
   default_skills = (FundamentalSkill(), TechnicalSkill(), MyNewSkill())
   ```

3. **Extend enums** if needed — new `MetricKind` values in
   `src/trade_research/domain/provenance.py`, new `CapabilityName` if a new provider
   type is required.

4. **Export** from `src/trade_research/skills/__init__.py`.

5. **Write tests** first (TDD per `AGENTS.md`), then run:
   ```sh
   .venv/bin/python -m pytest
   .venv/bin/ruff check .
   .venv/bin/mypy
   ```

6. Restart the process / rebuild Docker — `SkillRegistry` is frozen at startup.

> **You cannot add skills at runtime.** The registry's `__init__` validates that every
> skill is a frozen dataclass and rejects any attempt to mutate it afterward.

---

## Use cases

| Use case | Skills needed | Provider needed |
|---|---|---|
| Equity screening (FCF yield, P/E, ROE) | `fundamental` | `FUNDAMENTALS` (local or Bloomberg) |
| Technical entry/exit signals (RSI, MACD, Bollinger) | `technical` | `PRICES` (Yahoo or Stooq — no local data required) |
| Combined research note | both | both |
| Crypto technical analysis | `technical` | `PRICES` via CCXT (e.g. Binance) |
| SEC filing review | (no skill yet, data layer exists) | `SecFilingsProvider` |
| LLM-driven analysis via Claude/Hermes | any | any | 

---

## Known issues and limitations

### Platform

- **Bootstrap is macOS/Linux only.** Windows requires manual venv setup (see above).
- The bootstrap also hardcodes `.venv/bin/python` (Unix path). On Windows the
  interpreter is at `.venv\Scripts\python.exe`. The `doctor` and test commands in
  `AGENTS.md` also use Unix paths.

### Providers and data

- **`fundamental` skill has no free remote provider.** Yahoo and Stooq only supply
  prices. The fundamental skill is only usable with local CSV/Parquet/SQL data or a
  Bloomberg adapter (documented in `docs/adapters.md` but not shipped). Running
  `trade-research research AAPL` without a fundamentals source will produce a
  `FAILED` result for the fundamental analyst.
- **`SecFilingsProvider` feeds no skill.** Filing data is fetched and normalized but
  no analyst skill consumes it yet — it is an unused data layer.
- **`CCXT` is an optional dependency.** `CcxtPriceProvider` does a guarded import;
  missing `ccxt` package gives a configuration error at runtime rather than import
  time, which may be surprising.
- **Yahoo and Stooq providers fail in practice.** Both use Python's built-in `urllib`
  with minimal headers (no `User-Agent`, no cookies). Yahoo's chart API and Stooq now
  block or return empty data for such requests. Neither `yfinance` nor any other
  third-party data library is in `requirements.lock`.

  **Why raw `urllib` instead of `yfinance`?** The docs cite licensing and redistribution
  control — `yfinance` may cache raw vendor payloads internally, conflicting with the
  project's rule that raw licensed data must not be persisted outside its authorized
  system. However, **this reasoning is uncertain** (?). It's equally plausible the
  choice was simply to keep the dependency footprint minimal, or that the raw endpoint
  was accessible when the code was written and has since been restricted. The actual
  intent is not documented in the code or plan. Before adding `yfinance` or another
  library, it's worth clarifying whether the `urllib` approach was a deliberate
  licensing boundary or just a lightweight choice that has aged poorly.

  **Practical fix (no new dependencies):** adding a `User-Agent` header to
  `_http_get` in `providers/remote.py` is the smallest change and may unblock Stooq.
  Yahoo additionally requires a crumb/cookie handshake that `urllib` alone cannot
  easily replicate.

### Security

- **Prompt injection via market data.** Free-text string fields fetched from external
  APIs (company names, filing titles from SEC EDGAR) may flow into the Markdown report
  that the MCP server returns to an LLM agent. The closed provenance schema protects
  numeric/enum fields but does not sanitize arbitrary text fields. A malicious party
  could embed instruction text in a ticker's display name or SEC filing title.

- **Symbol validation at the domain level is weak.** `providers/remote.py` does apply
  `urllib.parse.quote()` before building Yahoo and Stooq URLs. However, the
  `InstrumentId.symbol` regex (`[A-Za-z0-9^][A-Za-z0-9._:/^-]{0,31}`) allows
  characters like `/` and `:` that are meaningful in URLs. A crafted symbol could
  still produce unexpected request paths after encoding.

- **Discord webhook URL and API bearer token should use `SecretStr`.** If the
  `Settings` object is ever printed, logged, or included in a traceback, raw secret
  values could appear. Confirm that `settings.py` uses `pydantic.SecretStr` for these
  fields and that `doctor` output masks them.

- **Hermes sidecar image is not digest-pinned in practice.** The `compose.yaml`
  documents a sentinel placeholder that is intentionally not runnable, but digest
  pinning is not enforced by CI. A substituted Hermes image could exhaust the API or
  exfiltrate reports.

- **Local file provider path traversal.** `LocalCsvParquetPriceProvider` accepts a
  file path from settings without validating it is within a designated data root.

### Design constraints (intentional, not bugs)

- Skills are immutable at runtime — no plugin loading, no hot-reload.
- HTTP binds loopback/private only — not suitable as a public API without a reverse
  proxy and additional auth.
- No LLM inference is built in — the project is a *tool for* LLM agents, not an
  agent itself.
- Asian market symbols (`ASX`, `HKEX`, `JPX`, etc.) are defined in `ASIAN_MARKETS`
  in `domain/models.py` but are not included in `SUPPORTED_MARKETS` — requests
  specifying those markets will fail validation.
