# Hermes integration

Hermes receives the same bounded research surface as Codex and Claude. It cannot run shell, Python, arbitrary SQL, filesystem operations, broker actions, or mutate analyst skills.

## Local CLI and stdio MCP

Set `TRADE_RESEARCH_HOME` to the checkout and invoke the portable launcher from the Hermes MCP configuration:

```sh
TRADE_RESEARCH_HOME=/path/to/TradeAgent \
  plugins/trade-research/scripts/launch-mcp
```

The launcher resolves, in order, `TRADE_RESEARCH_HOME`, `CLAUDE_PROJECT_DIR`, `CODEX_PROJECT_DIR`, current-directory ancestors, then its own checkout ancestor. It changes to that resolved root before starting `.venv/bin/trade-research mcp`, so default state stays under the checkout rather than the caller or installed plugin directory. If bootstrap is missing it exits with an exact bootstrap command. Stdout is reserved for MCP protocol traffic.

Hermes can also invoke bounded native CLI commands such as `trade-research list-skills`, `research`, and `report`. It must not interpolate untrusted data into shell commands; prefer typed MCP calls.

## HTTP over LAN or VPN

Prefer stdio on one machine. For another trusted device, bind only the server's private LAN/VPN address and require a high-entropy bearer token supplied out of band. Send `Authorization: Bearer <token>` over a trusted encrypted VPN or TLS reverse proxy. Never put the token in a URL, prompt, log, command history, plugin manifest, or repository.

The API has no public docs endpoint and Uvicorn access logs are disabled to avoid client-IP retention. Rotate tokens after suspected exposure. Do not expose the service on a public interface.

## Compose profile

The disabled-by-default `hermes` profile requires an explicit `HERMES_IMAGE` in `name@sha256:<digest>` form. It uses Compose service DNS at `http://research-api:8000`, receives the bearer token through the same read-only secret file, publishes no port, and does not mount research storage. The preflight wrapper fails closed when the image variable or digest is missing; the local sentinel is not runnable as Hermes. The approved image must be shell-compatible and retain a default command. Validate with `HERMES_IMAGE=... docker compose --profile hermes config --quiet` before startup.
