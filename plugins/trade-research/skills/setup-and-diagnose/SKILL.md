---
name: setup-and-diagnose
description: Use when installing, configuring, or diagnosing a local Trade Research checkout on macOS, Linux, native Python, or Docker.
---

# Set up and diagnose

Runtime skills are immutable. Diagnose configuration; never create, request, copy, or print a secret.

1. Resolve the repository through `TRADE_RESEARCH_HOME`, the client project directory, or the current directory.
2. If `.venv/bin/trade-research` is absent, ask the user to run `python3.12 scripts/bootstrap.py --dev` from the repository. Bootstrap never fills secrets.
3. Run `.venv/bin/trade-research doctor`. Report only its booleans and status labels.
4. For Docker diagnostics, run only `docker compose config`; add `-f compose.override.local.yaml` to validation only when a host listener is intended. Ask the user to start services themselves.

Do not probe secret-bearing webhook URLs, send LLM prompts, install unreviewed packages, or invoke a shell through MCP.
