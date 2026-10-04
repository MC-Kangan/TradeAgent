# Corporate installation: Windows, JFrog and Bloomberg

This guide targets a Windows x64 company laptop with Python 3.12. Python 3.13 is
also declared supported, but use one agreed interpreter for the entire installation.
The current application/test validation was performed on macOS, not Windows or a
live corporate Bloomberg session. The steps below are the Windows acceptance path.

## What to transfer

Obtain approved source copies of **both** repositories and keep them as siblings:

```text
C:\Research\TradeAgent\pyproject.toml
C:\Research\FactorPlayground\app.py
```

A Git clone or source ZIP is sufficient. Do not transfer `.venv`, caches, credentials,
licensed observations or another machine's compiled packages. FactorPlayground is a
separate repository; cloning TradeAgent alone does not include it. Record both commit
IDs for reproducibility. Updating only one checkout may break the app/backend contract.
No Git access is needed at runtime. Docker, Node/npm and Claude Code are optional.

## Configure JFrog before installation

Use your firm's existing pip configuration, authentication and corporate CA setup.
If IT asks you to supply an index URL, the credential-free PowerShell shape is:

```powershell
$env:PIP_INDEX_URL = "https://YOUR-FIRM/artifactory/api/pypi/YOUR-VIRTUAL-REPO/simple"
```

Replace placeholders with the approved virtual repository. Do not put passwords or
access tokens in commands, documentation or Git. Use the firm's credential tooling.
Do not add public PyPI as `--extra-index-url`, bypass TLS checks or install around an
approval block. If an index is already managed, do not override it. IT should configure
approved CA trust for both pip and Python HTTP clients if TLS inspection is used.

JFrog's [PyPI repository documentation](https://docs.jfrog.com/artifactory/docs/pypi-repositories)
describes the endpoint. Existing JFrog CLI users can follow the firm's
[`jf pip` configuration](https://docs.jfrog.com/artifactory/docs/jf-pip); this project
does not require installing that CLI.

## Install the app and backend together

Run in PowerShell. Calling Python by path avoids activation/execution-policy changes.

```powershell
cd C:\Research\FactorPlayground
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: -r ..\TradeAgent\requirements.lock -r requirements-ui.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e ..\TradeAgent
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\trade-research.exe doctor
.\.venv\Scripts\python.exe app.py --open-browser
```

The first install supplies runtime and editable-build dependencies. Only then is
`--no-deps --no-build-isolation` appropriate: it prevents a second build environment
from fetching packages. Do not use it to skip unavailable required packages.
`doctor` reports configuration presence/status; it does not prove data entitlement.
Open http://127.0.0.1:8050, choose **Synthetic demo**, and run both workspaces.
Stop with Ctrl+C. On subsequent launches run only the final command.

For backend-only use, create `.venv` inside TradeAgent, install `requirements.lock`,
then install `-e .` with the same flags. The `scripts/bootstrap.py` helper is for
macOS/Linux; use these manual steps on Windows. No HTTP token/server is needed for
Dash, which imports the backend directly. The authenticated HTTP API is a separate
entry point; see [deployment](deployment.md) before enabling it.

## Preflight and offline installation

From FactorPlayground, on an approved machine matching the target OS, architecture
and Python version, ask pip to resolve and download through JFrog without installing:

```powershell
py -3.12 -m pip download --only-binary=:all: --dest wheelhouse -r ..\TradeAgent\requirements.lock -r requirements-ui.lock
```

A successful download is the practical mirror-and-wheel availability check. The
baseline lock was produced for macOS/Linux: Windows-only dependencies, notably
MCP's `pywin32` and terminal support such as `colorama`, can be added by the resolver.
Have IT retain the resolved Windows wheelhouse and a version inventory; these
conditional packages are not yet a tested Windows lock in this repository.
Do not assume downloading Mac wheels prepares Windows. Cross-platform pip download
flags exist, but a native Windows preflight also checks platform markers correctly.
See [pip download](https://pip.pypa.io/en/stable/cli/pip_download/).

Transfer the approved wheelhouse with both source trees. Replace the first install with:

```powershell
.\.venv\Scripts\python.exe -m pip install --no-index --find-links .\wheelhouse -r ..\TradeAgent\requirements.lock -r requirements-ui.lock
.\.venv\Scripts\python.exe -m pip install --no-index --no-deps --no-build-isolation -e ..\TradeAgent
.\.venv\Scripts\python.exe -m pip check
```

The wheelhouse must include every transitive dependency. Optional Bloomberg and dev
packages need separate approval/download too. Record the accepted environment with
`python -m pip list --format=freeze` using the environment's Python; keep that inventory
in your firm's approved location. A source ZIP alone is not an offline Python installer.
For internal distribution, TradeAgent can be built as a wheel with `python -m build
--wheel --no-isolation` after installing runtime and dev locks. A project wheel does
not bundle its dependencies or the separate Dash source. Do not vendor the entire
scientific stack into this repository to evade package approval.

## Bloomberg configuration

Ask IT for a compatible, approved `blpapi` wheel/runtime and the permitted Desktop API
or authorized BPIPE connection. The optional package constraint is `blpapi>=3.24`;
production should pin the exact version IT validates. It is not in the baseline lock.
Use the approved JFrog package or approved local wheel, never a guessed public URL.
The [Bloomberg API support page](https://www.bloomberg.com/professional/support/api-library/)
is the vendor reference. Terminal/API entitlements and package installation are separate.

Save a non-secret JSON configuration outside the source repositories. This example
illustrates a Repsol mapping; verify the field, currency and return convention with
Bloomberg before using it:

```json
{
  "price_provider": "bloomberg",
  "bloomberg_host": "localhost",
  "bloomberg_port": 8194,
  "bloomberg_return_mappings": [
    {
      "instrument": {"market": "BME", "symbol": "REP"},
      "security": "REP SM Equity",
      "field": "TOTAL_RETURN_INDEX_GROSS_DVDS",
      "currency": "EUR",
      "return_basis": "gross_total_return"
    }
  ]
}
```

Set configuration in the same shell used to launch Dash:

```powershell
$env:TRADE_RESEARCH_CONFIG = "C:\Research\private-config\research.json"
$env:FACTOR_PLAYGROUND_PACK_DIR = "C:\Research\private-config\factor_packs"
.\.venv\Scripts\python.exe app.py --open-browser
```

Omit the second variable to use bundled packs. Select **Configured backend** in the
app. Dash reads process environment, not `.env`; the backend CLI separately supports
`.env`. JSON settings are allowlisted, and explicit environment settings override them.
Keep credentials/session identity outside YAML and JSON examples.

Host/port alone is not a universal BPIPE integration. Firm-specific authorization,
identity and entitlements may require a dedicated authorized session injected through
the existing adapter. Validate that integration with IT before claiming live readiness.
Configured Bloomberg does not silently fall back to Yahoo for FX. EUR Repsol and USD
commodities require an authorized FX provider or genuinely USD-normalized stock data;
relabelling currency does not convert prices.

## Factor packs and analysis settings

Follow the [YAML schema and examples](factor-yaml-packs.md). The bundled energy and
MSCI Bloomberg templates are disabled until entitled securities, units, calendars and
scales are verified. Enable reviewed packs and restart. Select packs in **Starting
factor set**, then select individual factors, daily/monthly frequency and dates.

Price baskets are formed after unit normalization and before `simple_return`,
`log_return` or `difference`. Crack spreads need compatible units; generic futures
can include contract-roll jumps. MSCI price and total-return indexes are not interchangeable.
The Yahoo energy pack uses ETP return proxies, not spot diesel or gas. French research
factors download at native daily/monthly frequency when selected; package installation
does not supply their data.

Start with original joint regression. Use selective residualization for an explicit
question (for example diesel exposure after oil) or sequential attribution with a
predeclared economic order. The order changes attribution, not full-model fitted
returns. Significance-based ordering can overfit and is not the default. PCA is not
exposed as a production estimator in this slice. See [attribution diagnostics](factor-attribution-diagnostics.md)
for correlations, partial relationships, VIF, rolling behavior and interpretation.

Cross-sectional analysis currently evaluates 12–1 momentum with monthly formation;
it is not an arbitrary multi-signal builder. Validate universe membership, currency,
return basis and holdout settings before interpreting IC or quantile performance.

## Network access is separate from package access

| Function | Connection to approve when used |
|---|---|
| Installation | Firm JFrog endpoint and its authentication infrastructure |
| Yahoo equity/ETF/FX | `query1.finance.yahoo.com` HTTPS |
| French research factors | `mba.tuck.dartmouth.edu` HTTPS |
| Bloomberg | Approved Desktop/BPIPE endpoint and session, commonly port 8194 |
| SEC research | `data.sec.gov` / `www.sec.gov` HTTPS |
| Dash UI | Browser to loopback `127.0.0.1`, default port 8050 |

The synthetic demo needs no market-data network access. No `yfinance`, `xbbg`,
Alphalens or scikit-learn installation is required. Do not copy raw licensed data
into support logs, fixtures, commits or external AI tools.

## Claude Code (if that is the coding tool you meant)

Claude Code is optional; the app and backend perform no LLM inference themselves.
Follow your firm's policy before exposing research outputs to any assistant. To use
an approved local stdio MCP integration, point to the already installed executable:

```powershell
claude mcp add --transport stdio --scope local trade-research -- "C:\Research\FactorPlayground\.venv\Scripts\trade-research.exe" mcp
```

Set backend environment variables before starting the assistant. This avoids runtime
`uvx`/`npx` downloads. See [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
Other coding assistants can use the same executable/`mcp` argument if they support
stdio MCP. An assistant is not required to start Dash or configure factor packs.

## Acceptance checklist and troubleshooting

1. `pip check` passes; record Python version, both source commits and resolved packages.
2. `doctor` runs; missing provider configuration is not a failed offline install.
3. Both synthetic workspaces run and charts keep a stable height after tab changes.
4. If approved, a short Yahoo/French study confirms data egress separately.
5. One entitled Bloomberg stock and one reviewed pack pass a small-history study;
   inspect coverage, currency, units and missing periods before expanding the universe.

If pip cannot find a version, send IT the failing name/version and both lockfiles.
If wheels/DLLs fail, verify x64 Python version and approved runtime components. Do
not downgrade one numerical dependency in isolation. If Bloomberg fails, separate
package import, session connectivity, entitlement and identifier/field validation.
If plots look old, stop old servers, restart from the current checkout and refresh.
Tests use synthetic/mocked data and cannot establish Bloomberg access or Windows support.
