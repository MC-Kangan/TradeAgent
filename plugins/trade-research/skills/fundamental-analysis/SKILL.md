---
name: fundamental-analysis
description: Use when a user wants bounded fundamental analysis of a supported US, UK, EU, ETF, or crypto instrument with numeric factors, safe citations, and provenance.
---

# Fundamental analysis

Runtime skills are immutable. Use only the `run_skill` or `start_research` bounded MCP tools (or the equivalent `trade-research run-skill fundamental` CLI).

1. Confirm symbol and market; reject Asian exchanges and execution requests.
2. Select the `fundamental` analyst.
3. Preserve missing-data and partial-result labels.
4. Return numeric factors, missing metrics, limitations, provider kinds, opaque citation/reference hashes, and the report identifier. Raw provider evidence is intentionally omitted. Never turn the result into an order or expose configuration values.
