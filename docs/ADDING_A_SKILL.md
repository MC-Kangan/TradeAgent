# Adding a New Skill to TradeAgent

This guide documents the exact steps to integrate a new analysis skill into the
TradeAgent research framework. It is based on the experience of integrating the
`worth-buy-stocks` trend-scoring pipeline — a ~650-line third-party algorithm
that required zero provider changes and slotted into the existing architecture
cleanly.

## Architecture Overview

A skill is a **frozen dataclass** that implements the `ResearchSkill` protocol.
It declares which provider capabilities it needs, and the engine supplies them
at runtime. The skill runs purely computational logic on provider-supplied data.

```
┌─────────────────────────────────────────────────┐
│                  ResearchEngine                  │
│  selects skills by name → calls skill.analyze() │
└──────────────────┬──────────────────────────────┘
                   │
    ┌──────────────┴──────────────┐
    │     SkillRegistry           │
    │  validates: frozen?         │
    │            immutable fields?│
    │            unique name?     │
    │            capabilities?    │
    └──────────────┬──────────────┘
                   │
    ┌──────────────┴──────────────┐
    │     ResearchSkill Protocol  │
    │  .name: str                 │
    │  .required_capabilities     │
    │  .analyze(instrument,       │
    │           providers)        │
    │      → AnalystResult        │
    └─────────────────────────────┘
```

Skills are **auto-discovered** by the CLI, MCP server, and HTTP API — you never
need to register new tools, routes, or endpoints.

## Checklist (9 files, ~3 new)

### Step 1: Define new metrics and algorithms

**File:** `src/trade_research/domain/provenance.py`

Every derived value your skill produces needs:
- A `MetricKind` enum value — the thing being measured
- A `DerivedAlgorithm` enum value — how it was computed

```python
# In MetricKind (StrEnum):
MY_NEW_COMPOSITE = "my_new_composite"
MY_NEW_SIGNAL = "my_new_signal"

# In DerivedAlgorithm (StrEnum):
MY_NEW_ALGORITHM = "my_new_algorithm"
```

**Rule:** Metrics are what you measured; algorithms are how you computed it.
Every `Observation` your skill emits carries both.

### Step 2: Add any new indicator functions (optional)

**File:** `src/trade_research/skills/indicators.py` (or create if needed)

If your skill needs math functions not already in `indicators.py`, add them as
**public** (no underscore prefix) functions here. Existing functions include
`sma`, `ema_series`, `rsi`, `macd_series`, `kdj`, `adx`, `atr`, `obv`,
`efficiency_ratio`, `max_drawdown`, `momentum_12_1`, `weekly_bearish_check`,
`validated_prices`, and the provenance helpers (`summary`, `limitations`,
`missing_metric_kinds`, `sanitize_text`, `algorithm_for_metric`).

Keep these functions **pure** — they take numbers in, return numbers out.
No provider calls, no side effects.

Update `algorithm_for_metric()` if your new metric prefixes need mapping to
their algorithms. This function is used by the provenance system to map
metric kind strings to canonical `DerivedAlgorithm` labels.

### Step 3: Create the skill class

**File:** `src/trade_research/skills/my_new_skill.py` (new)

Follow the frozen-dataclass pattern exactly:

```python
from dataclasses import dataclass, field
from trade_research.domain import (
    AnalysisMethod, AnalystResult, InstrumentId, MetricKind,
    Observation, ReportStatus, SignalKind,
)
from trade_research.domain.provenance import DerivedAlgorithm
from trade_research.providers import CapabilityName, ProviderRegistry
from trade_research.skills.indicators import (
    summary as _summary_fn,
    limitations as _limitations_fn,
    sanitize_text,
    missing_metric_kinds,
    validated_prices,
    # ... your indicator imports
)


@dataclass(frozen=True, slots=True)
class MyNewSkill:
    """One-line description of what this skill does."""

    # -- public configuration fields (settable at construction) --
    some_config: str = "default_value"

    # -- internal fields (init=False → not part of the constructor) --
    _name: str = field(default="my-new-skill", init=False, repr=False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        return (CapabilityName.PRICES,)  # or FUNDAMENTALS, FILINGS, etc.

    def __getattribute__(self, attribute: str) -> object:
        # Required: route "name" → _name for SkillRegistry validation
        if attribute == "name":
            return object.__getattribute__(self, "_name")
        return object.__getattribute__(self, attribute)

    def analyze(
        self, instrument: InstrumentId, providers: ProviderRegistry
    ) -> AnalystResult:
        # 1. Fetch data via providers
        # 2. Run your algorithm
        # 3. Build Observations with source="derived"
        # 4. Return AnalystResult
        ...
```

**Critical rules:**
- `@dataclass(frozen=True, slots=True)` — non-negotiable
- `_name` must match `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$` (kebab-case)
- `required_capabilities` must be a `tuple[CapabilityName, ...]` with no duplicates
- All fields must be deeply immutable (primitives, tuples, frozen dataclasses)
- `__getattribute__` override is required for `name` → `_name` routing
- Every `Observation` must have `source="derived"` and closed-schema provenance

**Naming convention:**
- Skill class: `PascalCase` (e.g., `WorthBuyStocksSkill`)
- `_name`: kebab-case (e.g., `"worth-buy-stocks"`)
- File name: snake_case matching the skill name (e.g., `worth_buy_stocks.py`)

### Step 4: Export the skill

**File:** `src/trade_research/skills/__init__.py`

```python
from trade_research.skills.my_new_skill import MyNewSkill

__all__ = [
    ...,
    "MyNewSkill",
]
```

### Step 5: Register in the engine

**File:** `src/trade_research/engine.py`

Two changes:
1. Add the import
2. Add the skill instance to `default_skills`

```python
# Import:
from trade_research.skills import (
    ...,
    MyNewSkill,
)

# In ResearchEngine.from_settings(), in the default_skills tuple:
skills = SkillRegistry(
    (
        TechnicalSkill(),
        FundamentalSkill(),
        FilingsSkill(),
        MyNewSkill(),           # ← add here
    )
)
```

That's it — the engine, CLI, MCP server, and HTTP API all discover it
automatically through `SkillRegistry.names`.

### Step 6: Add doctor diagnostics

**File:** `src/trade_research/diagnostics.py`

Add an entry to the `"skills"` dict in `run_doctor()`:

```python
"my_new_skill_analysis": (
    "ready" if prices_available else
    "unavailable — price provider not configured"
),
```

The readiness condition should match your skill's `required_capabilities`.
- `PRICES` → check `prices_available`
- `FUNDAMENTALS` → check `fundamentals_available`
- `FILINGS` → check `filings_available`

### Step 7: Write the spec

**File:** `skills/my-new-skill/SKILL.md` (new)

Follow the same format as existing specs. Must include:
- **Name** and **Required capability**
- **Purpose** section
- **Algorithm** description with formulas (if applicable)
- **Metrics computed** table
- **Input requirements** (minimum bars, provider data needed)
- **Output format** (what the `AnalystResult` contains)
- **Failure modes** table

### Step 8: Write a README for the skill

**File:** `skills/my-new-skill/README.md` (new)

Each skill directory should have a brief README with:

- Skill name and one-line purpose
- Link to the original algorithm repository (if applicable)
- Quick-start command
- Summary of output
- Link to the full `SKILL.md` spec for details

Keep it short — ~30 lines. The `SKILL.md` is the authoritative spec; the
README is a quick entry point for discovery.

### Step 9: Write tests

**File:** `tests/test_my_new_skill.py` (new)

Minimum test coverage:

| Test | What it verifies |
|---|---|
| `test_is_frozen_dataclass` | `SkillRegistry` accepts it |
| `test_required_capabilities` | Correct capability tuple |
| `test_skill_name_matches_pattern` | Kebab-case regex |
| `test_insufficient_data_returns_partial` | Graceful degradation |
| `test_normal_case_produces_observations` | Happy-path output |
| `test_all_observations_have_derived_source` | Provenance hygiene |
| `test_skill_instance_is_hashable` | Frozen-dataclass contract |
| `test_verdict_methods_present` | AnalysisMethod entries complete |

Use synthetic `PricePoint` fixtures via `_price_points()` and a mock
`ProviderRegistry` — see `tests/test_worth_buy_stocks.py` for the pattern.

## Verification Checklist

After completing the steps, run:

```sh
# 1. New skill tests
.venv/bin/python -m pytest tests/test_my_new_skill.py -v

# 2. Full regression suite
.venv/bin/python -m pytest tests/ -q

# 3. Lint + types
.venv/bin/ruff check src/
.venv/bin/mypy

# 4. CLI discovery
.venv/bin/trade-research list-skills
# → should list "my-new-skill"

# 5. Doctor readiness
.venv/bin/trade-research doctor
# → my_new_skill_analysis should report "ready" or a clear reason

# 6. Live run (requires configured provider)
.venv/bin/trade-research run-skill my-new-skill AAPL
```

After the skill is verified, also update:

- **`skills/README.md`** — add the new skill to the contents table
- **`README.md`** (project root) — add the skill to the Skills section and
  documentation index

## Design Principles

1. **Provider data is your only input.** Skills don't make network calls,
   read files, or access environment variables. They call `providers.*()`
   and compute.

2. **Later layers can only downgrade.** If your algorithm has multiple
   stages, earlier optimism should be tempered — not amplified — by later
   checks. This prevents "talking yourself into a trade."

3. **Degrade gracefully.** If optional data is missing (benchmarks,
   certain metrics), produce `PARTIAL` status with a `LimitationKind`
   note — never crash.

4. **Closed provenance.** Every derived `Observation` must use
   `source="derived"`, a `MetricKind`, a `DerivedAlgorithm`, and a
   provenance dict with only the known keys (`algorithm`, `window`,
   `reference`).

5. **Frozen and immutable.** The skill instance must be hashable, deeply
   immutable, and side-effect-free. The engine may reuse a single
   instance across many concurrent calls.

## What You Never Need to Change

When adding a new skill, you do **not** touch:
- Provider contracts or registry (`src/trade_research/providers/`)
- MCP server (`src/trade_research/mcp_server.py`)
- HTTP API (`src/trade_research/http.py`)
- CLI commands (`src/trade_research/cli.py`) — `run-skill` and `list-skills` work
  with any registered skill automatically
- Queue/worker (`src/trade_research/queue.py`)
- Settings (`src/trade_research/settings.py`)
