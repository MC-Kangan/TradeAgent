# Hermes integration

Hermes receives the same bounded research surface as Codex and Claude. It cannot run shell, Python, arbitrary SQL, filesystem operations, broker actions, or mutate analyst skills.

## Local CLI and stdio MCP

Set `TRADE_RESEARCH_HOME` to the checkout and invoke the portable launcher from the Hermes MCP configuration:

```sh
TRADE_RESEARCH_HOME=/path/to/TradeAgent \
  plugins/trade-research/scripts/launch-mcp
```

The launcher resolves, in order, `TRADE_RESEARCH_HOME`, `CLAUDE_PROJECT_DIR`, `CODEX_PROJECT_DIR`, current-directory ancestors, then its own checkout ancestor. It starts `.venv/bin/trade-research mcp`; if bootstrap is missing it exits with an exact bootstrap command. Stdout is reserved for MCP protocol traffic.

Hermes can also invoke bounded native CLI commands such as `trade-research list-skills`, `research`, and `report`. It must not interpolate untrusted data into shell commands; prefer typed MCP calls.

## HTTP over LAN or VPN

Prefer stdio on one machine. For another trusted device, bind only the server's private LAN/VPN address and require a high-entropy bearer token supplied out of band. Send `Authorization: Bearer <token>` over a trusted encrypted VPN or TLS reverse proxy. Never put the token in a URL, prompt, log, command history, plugin manifest, or repository.

The API has no public docs endpoint and Uvicorn access logs are disabled to avoid client-IP retention. Rotate tokens after suspected exposure. Do not expose the service on a public interface. A Hermes Compose profile is intentionally absent until an approved, digest-pinned image is configured.
