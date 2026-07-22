---
name: create-analysis-skill
description: Use when a developer wants to add a new immutable analyst implementation to the Trade Research codebase and its bounded discovery surface.
---

# Create an analysis skill

Runtime skills are immutable: they cannot create or modify analysts while the service is running. This is a coding workflow, not an MCP capability.

1. Write contract and numerical tests first and run them to observe the expected failure.
2. Implement the `ResearchSkill` protocol against provider protocols in `src/trade_research/skills/`; use a frozen dataclass, accept validated observations only, and preserve provenance. Declare `required_capabilities` as a mandatory `tuple[CapabilityName, ...]` using enum members such as `CapabilityName.PRICES`; registration rejects missing declarations, strings, and unknown capabilities.
3. Register a fixed frozen instance in the composition root. Never load caller-supplied code, prompts, import paths, SQL, or filesystem paths.
4. Run focused tests, the full pytest suite, Ruff, mypy, `python -m build`, and `docker compose config` when Docker Compose is installed.
5. Ask the user to restart native processes or rebuild/restart containers so the immutable registry reloads. Confirm `trade-research list-skills` after reload.

Keep the MCP/HTTP tool set bounded. A new analyst name may be selected through existing tools; no self-modification endpoint is added.
