# Skills

This directory contains Markdown specifications for each analysis skill in Trade Research.
These files are **developer and agent documentation** — they describe what the Python
implementations in `src/trade_research/skills/core.py` are intended to do.

**These SKILL.md files are NOT executable.** Skills are implemented as immutable frozen
dataclasses in Python. At runtime, `SkillRegistry` discovers and validates registered
skills. The runtime cannot load Markdown as code.

## Contents

| Directory | Skill | Required provider |
|---|---|---|
| [fundamental-analysis/](fundamental-analysis/SKILL.md) | `fundamental` | `FUNDAMENTALS` |
| [technical-analysis/](technical-analysis/SKILL.md) | `technical` | `PRICES` |
| [filings-analysis/](filings-analysis/SKILL.md) | `filings` | `FILINGS` |
| [worth-buy-stocks/](worth-buy-stocks/SKILL.md) | `worth-buy-stocks` | `PRICES` |
| [markov-method/](markov-method/SKILL.md) | `markov-method` | `PRICES` |
| [review/](review/SKILL.md) | built-in reviewer | (none — post-processing) |

For plain-English report narration, see
[../docs/EXPLAINING_SKILL_OUTPUTS.md](../docs/EXPLAINING_SKILL_OUTPUTS.md).
Worth-buy-stocks and Markov also have local `EXPLANATION.md` notes in their
skill directories.

## Adding a new skill

See **[../docs/ADDING_A_SKILL.md](../docs/ADDING_A_SKILL.md)** for the complete step-by-step
integration guide. Quick summary of the files involved:

1. Write a SKILL.md and examples.md in a new subdirectory (documentation first)
2. Implement a frozen dataclass in `src/trade_research/skills/core.py`
3. Register the instance in `src/trade_research/engine.py:default_skills`
4. Extend enums in `src/trade_research/domain/provenance.py` if needed
5. Export from `src/trade_research/skills/__init__.py`
6. Write tests, run `pytest`, `ruff`, `mypy`

See [AGENTS.md](../AGENTS.md) for the full development workflow.
