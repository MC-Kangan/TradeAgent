# Dependency audit for corporate deployment

Audit date: 2026-10-04. The combined backend runtime/editable-build lock and Dash UI
pins contain **80 unique distributions**. Public PyPI metadata returned all exact
versions and at least one wheel for each. This checks public existence only: it does
not prove availability in your firm's JFrog mirror, target Windows wheel compatibility,
security approval or license approval. No access to your firm's mirror was available.

The backend's `requirements.lock` is authoritative for its dependencies; the app's
`requirements-ui.lock` adds 16 UI distributions. `requirements.txt` includes both
and the editable sibling backend. The split preserves every previous pinned version.
Dev, Bloomberg, crypto and Streamlit extras are outside this 80-package baseline.

## Packages to send IT first

| Package / group | Why present | Corporate action |
|---|---|---|
| `blpapi>=3.24` (optional) | Bloomberg Desktop/BPIPE sessions | Obtain and pin firm's approved Windows wheel/runtime and entitlements; not baseline-locked |
| `backtesting==0.6.6`, `bokeh==3.9.2` | Existing backtesting analyst; currently eagerly imported through the engine | Check AGPL-3.0 metadata for backtesting with your approval team; required by current full package even for factor use |
| `exchange-calendars==4.13.2` | Trading-session alignment/coverage | Check availability plus `pyluach`, `korean-lunar-calendar`, `toolz`, `tzdata`; apparently unrelated calendars are transitive requirements |
| `statsmodels==0.15.0` | Regression/HAC numerical primitives | Keep compatible SciPy/NumPy/pandas pins; check `formulaic`, `interface-meta`, `patsy`, `wrapt` too |
| `dash==4.4.1`, `plotly==7.1.0` | Standalone local UI | UI-only approval; Flask and other UI dependencies are in the UI lock |
| `mcp==1.12.4` | Existing assistant transport | Check Windows-only `pywin32`; retain resolved Windows dependencies |
| `pyarrow==18.1.0` | Existing Parquet provider | Large binary package; required by current package, though factor arithmetic does not need Parquet |
| `pydantic-core`, `rpds-py`, `numpy`, `scipy`, `pandas`, `pillow`, `contourpy`, `wrapt` | Validation/scientific/rendering dependencies | Use matching Windows/Python wheels; avoid an accidental compiler/toolchain requirement |
| `pyyaml==6.0.3` | Safe declarative factor packs | Already baseline-pinned; now declared directly by backend |

[backtesting's published metadata](https://pypi.org/project/backtesting/0.6.6/)
lists AGPL-3.0. This is an approval flag, not a determination of your firm's legal
obligations. Do not conceal or bypass it by omitting dependencies with `--no-deps`.
If blocked, a separate, tested change can make the backtesting skill optional with
conditional registration. That packaging work has not been implemented here.

## What we deliberately do not add

Yahoo uses the existing HTTP adapter, so `yfinance` is not required (having yfinance
approved does not establish outbound Yahoo access). YAML ideas were borrowed from
other projects without adding `xbbg`, Alphalens, Zipline or a large research framework.
PCA/scikit-learn is not a new dependency. PyYAML and statsmodels provide established
parsing and numerical primitives; replacing them with custom implementations would
increase maintenance and validation work.

`ccxt` is optional crypto support, Streamlit is an optional separate strategy showcase,
and dev tools are only needed to build/test. Skip these for a normal Dash installation.
Do not skip backend baseline packages without changing and testing package boundaries.

## Recommended distribution

Keep source and pins separate from approved binary wheels. Let JFrog resolve the
exact baseline and UI pins on Windows, retain the resolved conditional dependencies,
and use an IT-approved wheelhouse if the laptop is offline. Do not copy a Mac virtual
environment or bundle third-party libraries inside application source. A backend wheel
simplifies distributing backend code but does not eliminate dependency requirements.

See the [Windows/JFrog guide](corporate-installation.md) for commands, platform-marker
limitations and acceptance checks. The public metadata audit is not a vulnerability
scan, complete SBOM or successful fresh Windows installation. The firm should scan
the final resolved Windows environment using its own approved tooling.
