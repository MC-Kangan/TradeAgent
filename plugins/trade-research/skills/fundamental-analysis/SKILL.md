---
name: fundamental-analysis
description: Use when a user wants bounded fundamental analysis of a supported US, UK, EU, ETF, or crypto instrument with evidence and provenance.
---

# Fundamental analysis

Runtime skills are immutable. Use only the `run_skill` or `start_research` bounded MCP tools (or the equivalent `trade-research run-skill fundamental` CLI).

1. Confirm symbol and market; reject Asian exchanges and execution requests.
2. Select the `fundamental` analyst.
3. Preserve missing-data and partial-result labels.
4. Return factors, cited evidence, provenance, risks, and the report identifier. Never turn the result into an order or expose configuration values.
