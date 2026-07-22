---
name: company-research
description: Use when a user wants a combined company research report using one or more immutable Trade Research analysts.
---

# Company research

Runtime skills are immutable. Use bounded MCP tools only: `list_skills`, `start_research`, status/result retrieval, and report compilation.

1. Confirm the supported symbol, market, and desired analyst set; default to `fundamental` and `technical`.
2. Before starting durable research, confirm a separately supervised `trade-research worker` is running. If that cannot be confirmed, ask the user to start it; the plugin must not launch or supervise processes.
3. Start one durable research request. Poll at a bounded cadence with a two-minute deadline; if it remains queued or running, return the request identifier and pending state instead of polling forever.
4. Retrieve the result and compile the requested report format.
5. Separate evidence, analyst findings, reviewer limits, missing data, and provenance. Do not add broker, order, transaction, arbitrary SQL, filesystem, shell, or Python actions.
