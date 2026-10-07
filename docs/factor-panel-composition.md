# Combining research factors and Bloomberg packs

Select Fama–French plus one or more enabled Bloomberg YAML packs in Factor Playground,
then select the individual factors required. Use the configured Bloomberg source for
level packs. Both the data inspector and regression use the same backend preparation.
No new dependency is required.

## Input contract

- Providers return validated `ResearchFactorPanel` values at the requested native daily
  or monthly frequency. A request-scoped composite selects the requested columns.
- French columns use the official USD convention. Mixed French studies default to USD
  stock excess returns even though the UI preset becomes `custom`. The stock's local
  return is converted with the configured FX provider before subtracting French cash.
- Bloomberg YAML factors retain the transformation, unit and quotation currency of
  their inputs. A EUR index change or USD/barrel diesel change can explain USD stock
  returns; those explanatory changes are not relabelled or automatically FX-converted.
  This is different from a funded asset-return factor, which must already match the
  study currency when supplied inside a research panel. The asset-return provider path
  continues to apply its existing FX conversion.
- Only the primary panel supplies the risk-free rate. Cash is subtracted once from
  the stock and funded benchmarks, never from price changes or long-short factors.
- Selected factor IDs and research keys must be unique across sources. YAML level
  selections carry `research_source: pack`; French presets carry
  `research_source: kenneth_french`. A standalone pack may use a name such as momentum
  without being mistaken for French momentum. When combining both, give the pack
  factor a distinct key (the MSCI template uses `msci_momentum`). Unknown columns
  and incompatible region/frequency metadata fail before estimation.
- Daily joins compare both starting and ending session dates, so differing holiday
  calendars cannot silently align returns over unequal intervals. Monthly joins use
  complete calendar-month labels; providers remain responsible for complete periods.
- There is no forward filling, interpolation, or gap bridging. The intersection is
  used for all columns. Empty intersections produce no fit.
- Source hashes, retrieval times and observation counts remain separately visible.
  Source coverage records available observations and losses from composition/final
  alignment. The expected count for composed sources is the union of supplied periods,
  not a claim that each vendor should publish on every exchange holiday.

## Scope and interpretation

This is historical attribution. Different closing times remain a limitation even after
matching intervals; the existing nonsynchronous-close warning remains active. French
revisions, return-convention differences and publication lag remain visible. Monthly
analysis is often easier to interpret across regions.

Bloomberg-only pack studies do not download French files. Mixed studies require access
to both Bloomberg and the official Kenneth French download site; no provider failure is
silently replaced with demo data. BPIPE authentication remains firm-specific.

## Design references and verification

The composition follows the explicit intersection and duplicate-column checks described
by [pandas concat](https://pandas.pydata.org/docs/reference/api/pandas.concat.html), using
small typed dictionaries for the existing bounded panel contract rather than adding a
research framework. The [French international-factor documentation](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/f-f_developed_mom.html)
states the USD, non-continuously-compounded return convention.

Tests use synthetic observations only. They check period intersection, daily holiday
mismatches, duplicate IDs, frequency/region and currency conflicts, risk-free ownership,
EUR stock conversion before cash subtraction, preview/regression sample equality, and
that pack-only requests do not fetch French data. Live Bloomberg acceptance must still
be performed on an entitled workstation.
