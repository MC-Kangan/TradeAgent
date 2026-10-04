# YAML factor packs

Factor Playground discovers `*.yaml` and `*.yml` in its `factor_packs/` directory at
startup. Set `FACTOR_PLAYGROUND_PACK_DIR` to an absolute directory to use a firm's
pack repository instead. Restart the app after changing files. Packs are data,
not Python plugins. Duplicate IDs/keys, unknown properties and executable YAML tags
are rejected. Pack files contain definitions, never credentials or downloaded data.

Choose a named pack in **Starting factor set**, then remove any unwanted factors
from **Factors in this run**. Choose the source and daily/monthly frequency and run.
The pack summary shows the transforms and spread legs. Manual additions remain
optional; JSON overrides require the **Custom factors** selection.

## Two types of input

A public equity/ETF pack uses the existing canonical instrument contract:

```yaml
id: energy_public
label: Energy proxies
sources: [demo, yahoo]
factors:
  - id: brent
    label: Brent ETP return
    kind: asset_return
    instrument: {market: US, symbol: BNO}
```

The Yahoo adapter supplies dividend-adjusted simple returns. These ETP returns
include product and futures-roll effects; they are not spot commodity prices.
Existing `spread` factors still mean a difference of funded simple returns.

A Bloomberg level pack contains exact security/field mappings and explicit
transforms. The example below is deliberately disabled and uses placeholders.
Replace them with verified entitled instruments and review the unit scales and
calendar before setting `enabled: true`:

```yaml
id: refinery
label: Refinery drivers
sources: [configured]
enabled: false
currency: USD
calendar: weekdays
inputs:
  brent:
    security: REPLACE_BRENT Comdty
    field: PX_LAST
    currency: USD
    unit: USD/barrel
    scale: 1
  diesel:
    security: REPLACE_DIESEL Comdty
    field: PX_LAST
    currency: USD
    unit: USD/barrel
    scale: 1  # Replace if the vendor quotation is not already USD/barrel.
level_factors:
  - id: oil
    label: Brent log change
    legs: {brent: 1}
    transform: log_return
  - id: crack
    label: Diesel minus Brent crack change
    legs: {diesel: 1, brent: -1}
    transform: difference
```

`unit` describes the normalized level after multiplying the vendor value by
`scale`. Converting diesel from USD/tonne to USD/barrel requires an explicitly
verified conversion based on the relevant product specification; no universal
conversion is assumed. All legs of a spread must have the same normalized unit
and currency. All level inputs must share the pack's study currency. `calendar`
must describe the publication sessions of those inputs; `weekdays` is only an
example and includes weekday holidays, which may cause missing periods.

The processing order is:

1. Retrieve bounded daily levels through the existing Bloomberg adapter.
2. Multiply each input by its unit conversion scale.
3. Form each weighted level basket, such as diesel minus Brent.
4. At the selected frequency, calculate `simple_return` = end/start − 1,
   `log_return` = log(end) − log(start), or `difference` = end − start.
5. Send the typed research panel through the existing regression, diagnostics and
   reporting pipeline. Reports retain units and a hash, not raw vendor payloads.

Daily observations require both adjacent scheduled closes. Monthly observations
require the previous month's closing level and every scheduled session in the
month. Partial months and missing sessions are dropped, with no forward filling
or bridging of gaps. Monthly log changes and price differences use endpoints;
they are never compounded as if they were simple returns. Nonpositive basket
levels are rejected for simple/log returns; differences allow zero and negative
levels, which are meaningful for crack spreads.

The dependent stock remains a simple total return. Choosing a log factor does not
change the target into log returns. These are explanatory regressions, and a
coefficient on a price change has units of stock return per price unit. The model
intercept is not risk-adjusted alpha unless a cash/excess-return model is supplied.
Generic commodity futures may jump on contract rolls; these packs do not invent
roll adjustments. Bloomberg total-return mappings for the stock and funded
benchmarks remain in `TRADE_RESEARCH_CONFIG`. Credentials, identity and B-PIPE
entitlements remain adapter/session configuration, outside the pack.

For Repsol with USD commodity inputs, use a verified USD-normalized stock history
or inject the firm's FX provider. The configured Bloomberg workflow does not
silently fetch Yahoo FX. An EUR MSCI pack can use EUR Repsol data directly when
return conventions, intervals and calendars align. Exact entitled MSCI identifiers
are deliberately not guessed. Disabled Bloomberg templates ship with the app.

## Backend use

`load_factor_packs(Path(...))` returns validated `FactorPack` objects. Select a
pack (or call `pack.subset(...)`), set `Settings(price_provider="bloomberg",
bloomberg_factor_pack=pack, bloomberg_return_mappings=...)`, and build a custom
factor request using `pack.selected_factors()`. This composes a `PackFactorProvider`
into the same engine used by native, HTTP and MCP entry points. The UI only selects
configuration; transformations remain backend code. A firm can instead inject a
normalized research-factor provider using the existing contract.

## Ideas borrowed and validation

- [xbbg](https://github.com/underloam/xbbg): declarative market-data configuration;
  retain our existing Bloomberg request/session adapter instead of adopting a
  second client/framework.
- [PyYAML](https://pyyaml.org/wiki/PyYAMLDocumentation): SafeLoader for data-only
  YAML. PyYAML was already pinned in the lock; it is now a direct dependency.
- [Dash Dropdown](https://dash.plotly.com/dash-core-components/dropdown): use
  `searchable=False` to show choices directly without a search input.

Focused tests cover daily/monthly transforms, missing dates, unit conversion,
negative spreads, nonpositive log inputs, invalid YAML and engine integration with
synthetic Bloomberg histories. Live firm Bloomberg access still requires validation
on an entitled terminal or authorized B-PIPE session.
