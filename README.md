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
    Price-series skills        CcxtPriceProvider
    FilingsSkill               InlinePriceProvider
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
| `skills/price_series.py` | Cross-asset technical confirmation, historical risk, and volatility-regime skills |
| `skills/backtesting.py` | Bounded daily long-only tranche simulation |
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

Requires a `FUNDAMENTALS`-capable provider: `sec_company_facts` (automatic SEC EDGAR,
no local dataset needed) or local CSV/Parquet/SQL.

### `technical`

Computes 14 price-based indicators from OHLCV history (default 20-bar window): price
return, SMA, EMA, Bollinger Bands, RSI(14), MACD(12/26/9), ATR(14), momentum(10),
annualized volatility, and volume trend.

Requires a `PRICES`-capable provider (Yahoo, CCXT, local files).

### `filings`

Analyzes SEC EDGAR filing history: recent filing count, material-event (8-K) count,
days since latest 10-K, and days since latest 10-Q.

Requires `sec_user_agent` to be set. Ticker-to-CIK resolution is automatic — no
manual CIK mapping is needed. Override unusual symbols with `sec_cik_overrides`.

### `worth-buy-stocks`

A 4-layer trend-scoring pipeline producing trading discipline verdicts
(是/观察/否) with entry/stop/target price levels. Computes ALPHA-weighted
composite scores (momentum, relative strength, efficiency), risk veto
checks, and technical confirmation. [Original algorithm.](https://github.com/starriv/worth-buy-stocks)

Requires a `PRICES`-capable provider.

### `markov-method`

Markov regime detection: labels each day Bull/Bear/Sideways via rolling
returns, builds a 3×3 transition matrix, computes the stationary distribution,
and emits a signed signal (−1 to +1) usable as a direction filter or
standalone signal. Works on any asset. [Original algorithm.](https://github.com/jackson-video-resources/markov-hedge-fund-method)

Requires a `PRICES`-capable provider.

### `technical-basic`

A compact cross-asset confirmation score built from EMA(12/26), ADX/DMI,
RSI(14), Bollinger Bands, OBV, and current volume versus its 20-day average.
It requires complete daily OHLCV bars and reports explicit partial results
when history or volume fields are unavailable.

Supports equities and spot crypto through the same `PRICES` capability.

### `risk-analysis`

Descriptive historical risk statistics from daily closes: annualized and
downside volatility, maximum drawdown, historical 95% VaR/CVaR, return
skewness and excess kurtosis, plus best and worst daily returns. Downside
volatility is measured as shortfall deviation from zero. Equities use 252-day
annualization and crypto uses 365-day annualization.

This skill is non-directional and supports equities and spot crypto.

### `volatility-regime`

Classifies 20-day realized volatility as compressed, normal, or elevated
against its trailing 120-observation distribution. Percentiles use mid-ranks
for ties, so a flat series is neutral rather than incorrectly elevated. It
also reports whether realized volatility is expanding, stable, or contracting.

This skill is non-directional and supports equities and spot crypto.

### `correlation-analysis`

Aligns 2–9 equity and/or crypto daily-return series and returns a bounded
correlation matrix, average pairwise correlation, and per-asset annualized
volatility. Mixed portfolios use the common trading calendar without filling
stock weekends as zero returns.

### `asset-allocation`

Builds read-only, long-only allocation scenarios using equal weight, inverse
volatility, risk parity, or maximum diversification. It reports weights, risk
contributions, portfolio volatility, diversification ratio, and effective asset
count. The price-only methodology is adapted from the allocation frameworks
documented by [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading/blob/main/agent/src/skills/asset-allocation/SKILL.md).

Return-forecast mean variance, Black–Litterman, and turnover-aware optimization
remain out of scope until explicit forecast/view, current-weight, and cost inputs
are available. Outputs are mathematical scenarios, never orders or personalized
target allocations.

### `backtesting`

Runs reproducible, instrument-scoped daily backtests through pinned
`backtesting.py` 0.6.6. Built-in strategies are SMA crossover, MACD crossover,
RSI mean reversion, and the existing Markov regime method. Vibe Research may
instead send an ordered list of timestamped `add_long`, `reduce_long`, and
`exit_long` events in `skill_parameters.backtesting.strategy`. Additions create
fractional lots, reductions remove the oldest eligible lot, and exits flatten
all eligible lots.

Execution is deliberately fixed: long-only with no leverage, signals on bar
*t* fill at bar *t+1* open, and each signal acts on a fractional-share tranche
whose target notional is a configured percentage of starting capital. Early
additions respect a cooldown and a maximum allocation; exits respect the
configured minimum holding bars. Optional commission, spread, stop-loss, and
take-profit assumptions are explicit.
An optional start date separates causal warm-up from measured performance.
Results include core observations plus bounded price, indicator, equity,
drawdown, closed-trade, and open-position data with the assumptions needed to
reproduce the run. Every market uses the same fractional-unit execution model.
Protective levels are anchored to the actual next-open fill price. It does not expose Python,
optimization, plotting, brokers, or order APIs. Backtests and inline series are
immediate-only (`run_skill`, `/analyze`, or MCP equivalents), because the safe
job queue intentionally does not persist price bars or external signals.

> **Detailed specifications:** See `skills/*/SKILL.md` for full algorithm descriptions,
> input schemas, edge cases, and examples.

### Vibe Research integration

Vibe Research can supply bounded daily series directly through
`AnalysisRequest.price_series`. The shared instrument contract is
`{"symbol": "...", "market": "..."}`; the caller selects equity or crypto,
while TradeAgent derives calendar conventions from the market (for example,
`CRYPTO`). Inline sources currently include Yahoo, Tencent, Mootdx, and
Coinbase. Coinbase volume remains fractional.

The HTTP `/skills` catalog exposes `supported_asset_types` and `scope`. Price-series
technical, Markov, risk, and volatility-regime skills support both
`equity` and `crypto`; fundamentals, filings, and `worth-buy-stocks`
remain equity-only.
The `backtesting` skill supports both equity and crypto daily series and accepts
Vibe Research signals as bounded data rather than runtime strategy code.
Portfolio-scoped correlation and allocation skills accept 2–9 bounded inline
series in the same authenticated `/analyze` request and never receive broker
account identifiers or raw positions.

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
| `TRADE_RESEARCH_PRICE_PROVIDER` | `yahoo`, `ccxt`, `bloomberg`, `local_csv`, `local_parquet`, `local_sql` |
| `TRADE_RESEARCH_PRICE_PATH` | Path to local price file (for local providers) |
| `TRADE_RESEARCH_FUNDAMENTAL_PROVIDER` | `sec_company_facts`, `local_csv`, `local_parquet`, `local_sql` |
| `TRADE_RESEARCH_FUNDAMENTAL_PATH` | Path to local fundamental file (not needed for `sec_company_facts`) |
| `TRADE_RESEARCH_CCXT_EXCHANGE` | Exchange ID for CCXT, e.g. `binance` |
| `TRADE_RESEARCH_BLOOMBERG_HOST` | Optional BPIPE host when `TRADE_RESEARCH_PRICE_PROVIDER=bloomberg` |
| `TRADE_RESEARCH_BLOOMBERG_PORT` | Optional BPIPE port when `TRADE_RESEARCH_PRICE_PROVIDER=bloomberg`; default `8194` |
| `TRADE_RESEARCH_SEC_USER_AGENT` | User-Agent for SEC EDGAR (required for `sec_company_facts` and filings) |
| `TRADE_RESEARCH_DATA_ROOT` | Root directory that must contain all local provider paths |
| `TRADE_RESEARCH_API_TOKEN` | Bearer token for HTTP API |
| `DISCORD_WEBHOOK_URL` | Discord notification webhook |

### JSON config file

```json
{
  "price_provider": "yahoo",
  "fundamental_provider": "sec_company_facts",
  "data_root": ".trade-research",
  "api_token": "your-token-here",
  "sec_user_agent": "Your Org (contact@example.com)",
  "sec_cik_overrides": {
    "BRK.A": "0001067983",
    "BF.A": "0000018230"
  }
}
```

When `fundamental_provider` is `"sec_company_facts"`, no `fundamental_path` is needed —
data is fetched live from SEC EDGAR. `sec_cik_overrides` is optional; use it only for
unusual ticker symbols the automatic resolver can't find.

Set `TRADE_RESEARCH_CONFIG=/path/to/config.json`. Environment variables take precedence
over JSON config values for the same field.

### `.env` file

Place a `.env` file in the project root (never commit it):

```dotenv
TRADE_RESEARCH_PRICE_PROVIDER=yahoo
TRADE_RESEARCH_FUNDAMENTAL_PROVIDER=sec_company_facts
TRADE_RESEARCH_SEC_USER_AGENT=Your Org (contact@example.com)
TRADE_RESEARCH_DATA_ROOT=.trade-research
TRADE_RESEARCH_API_TOKEN=your-token-here
```

### SEC EDGAR quick start

Both `fundamental` (via `sec_company_facts`) and `filings` providers fetch data from
the SEC EDGAR system. The only hard requirement is a **User-Agent string** identifying
your organization — the SEC requires this per [EDGAR policy](https://www.sec.gov/os/accessing-edgar-data).

```sh
# The one required setting:
export TRADE_RESEARCH_SEC_USER_AGENT="Your Org (contact@example.com)"

# Use SEC Company Facts for fundamentals (no local dataset needed):
export TRADE_RESEARCH_FUNDAMENTAL_PROVIDER=sec_company_facts

# Optional: Yahoo for price data (needed for technical analysis):
export TRADE_RESEARCH_PRICE_PROVIDER=yahoo
```

**How it works:**

1. **Automatic CIK resolution** — On first run, the `CikResolver` downloads the SEC's
   `company_tickers.json` and builds a ticker→CIK map. The map is cached locally in
   `.trade-research/sec_cik_map.json` and reused for 24 hours. You never need to look
   up or type a CIK manually.

2. **Fundamentals** — The `SecCompanyFactsProvider` fetches XBRL Company Facts from
   `data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json` and extracts 10 US-GAAP concepts:
   revenue, net income, operating income, shareholders' equity, long-term debt,
   short-term borrowings, operating cash flow, capex, gross profit, and derived free
   cash flow. Each is surfaced as an `Observation` with statement provenance (fiscal
   year, period type, filing accession).

3. **Filings** — The `SecFilingsProvider` fetches recent submission history from
   `data.sec.gov/submissions/CIK{cik}.json` and enriches each filing with its
   `accession_number` and `is_amendment` flag.

**Overriding CIK for unusual symbols** (JSON config only):

```json
{
  "sec_user_agent": "Your Org (contact@example.com)",
  "sec_cik_overrides": {
    "BRK.A": "0001067983"
  }
}
```

**Verifying setup:**

```sh
.venv/bin/trade-research doctor
```

The `doctor` output includes per-skill readiness — each skill reports `"ready"` or
`"unavailable — <reason>"`. For SEC-based skills, look for:

```json
{
  "skills": {
    "fundamental_analysis": "ready",
    "filings_analysis": "ready"
  }
}
```

**Rate limits:** The SEC allows approximately 10 requests/second. The HTTP client uses
exponential backoff (3 retries, 0.5s base) for transient failures.

## CLI commands

```sh
trade-research doctor                               # capability check
trade-research list-skills                          # list registered analysts

# Run one skill synchronously (--market is required for market-aware providers):
trade-research run-skill technical AAPL --market NASDAQ
trade-research run-skill technical-basic AAPL --market NASDAQ
trade-research run-skill risk-analysis BTC-USD --market CRYPTO
trade-research run-skill volatility-regime BTC-USD --market CRYPTO
trade-research run-skill fundamental AAPL --market NASDAQ
trade-research run-skill filings AAPL --market NASDAQ

# Async job queue:
trade-research research AAPL                        # enqueue async job
trade-research research AAPL --analyst technical,fundamental
trade-research report <UUID> --format markdown      # render saved report

# Server / worker / MCP:
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
| Equity screening (FCF yield, P/E, ROE) | `fundamental` | `FUNDAMENTALS` (SEC EDGAR or local data) |
| Technical entry/exit signals (RSI, MACD, Bollinger) | `technical` | `PRICES` (Yahoo, CCXT, local) |
| Compact cross-asset confirmation | `technical-basic` | `PRICES` (inline, Yahoo, CCXT, local) |
| Historical tail and drawdown profile | `risk-analysis` | `PRICES` (daily closes) |
| Realized-volatility state | `volatility-regime` | `PRICES` (daily closes) |
| SEC filing review (recency, 8-K activity) | `filings` | `FILINGS` (SEC EDGAR, auto CIK) |
| Combined research note | all | multiple |
| Crypto price-series analysis | `technical`, `technical-basic`, `markov-method`, `risk-analysis`, `volatility-regime` | `PRICES` via inline Coinbase data or CCXT |
| LLM-driven analysis via Claude | any | any |

---

## Development

### Adding a new skill

See **[docs/ADDING_A_SKILL.md](docs/ADDING_A_SKILL.md)** for the complete
step-by-step guide.  Quick summary:

1. Define `MetricKind` + `DerivedAlgorithm` enums in `domain/provenance.py`
2. Add indicator functions to `skills/indicators.py` (if needed) and update
   `algorithm_for_metric()` for new metric prefixes
3. Create a frozen dataclass in `skills/` implementing the `ResearchSkill` protocol
4. Export from `skills/__init__.py` and register in `engine.py:default_skills`
5. Add a doctor diagnostics entry in `diagnostics.py`
6. Write `SKILL.md` and `README.md` in `skills/<name>/`
7. Write tests (8 minimum test types)
8. Update `skills/README.md` and this README
9. Run verification: `pytest`, `ruff`, `mypy`, `list-skills`, `doctor`, `run-skill`

**Skills are immutable at runtime.** The registry validates that every skill is a frozen
dataclass and rejects any attempt to mutate it after startup. You cannot add skills
without restarting the process.

### Quality gates

```sh
.venv/bin/python -m pytest   # 439 tests
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
| [skills/worth-buy-stocks/SKILL.md](skills/worth-buy-stocks/SKILL.md) | Worth-buy-stocks trend-scoring spec |
| [skills/markov-method/SKILL.md](skills/markov-method/SKILL.md) | Markov regime detection spec |
| [docs/EXPLAINING_SKILL_OUTPUTS.md](docs/EXPLAINING_SKILL_OUTPUTS.md) | Agent guide for natural-language report explanations |
| [docs/ADDING_A_SKILL.md](docs/ADDING_A_SKILL.md) | Guide to adding new skills |
| [docs/fundamental-data-schema.md](docs/fundamental-data-schema.md) | CSV/SQL schema for fundamental data |
| [docs/deployment.md](docs/deployment.md) | Native and Docker deployment |
| [docs/adapters.md](docs/adapters.md) | Enterprise data adapters |
| [docs/provenance-and-licensing.md](docs/provenance-and-licensing.md) | Provenance schema and licensing |
| [docs/provider-contract.md](docs/provider-contract.md) | Provider protocol contracts |
| [docs/hermes.md](docs/hermes.md) | Hermes agent integration |

---

## License

This project is research-only. See the repository license for details.
