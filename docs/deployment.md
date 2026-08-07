# Deployment

## Native

Run `python3.12 scripts/bootstrap.py --dev`, configure values outside the repository, and run `.venv/bin/trade-research doctor`. The supported native platforms are macOS and Linux. Production dependencies are fixed in `requirements.lock`; development-only packages are fixed separately.

Bootstrap reuses only a virtual environment whose actual interpreter reports the target prefix and Python version. It builds a recognized stale environment in a temporary sibling, validates it, then atomically swaps and removes the backup. An unknown non-empty `.venv` (including missing or malformed metadata) is refused and never cleared; move or inspect it manually. `--dry-run` prints the same create/reuse/swap/refusal decision without changing files.

The HTTP service requires `TRADE_RESEARCH_API_TOKEN` or a path in `TRADE_RESEARCH_API_TOKEN_FILE`:

```sh
TRADE_RESEARCH_API_TOKEN_FILE=/secure/path/api-token \
  .venv/bin/trade-research serve --host 127.0.0.1
```

Run a separately supervised worker with `.venv/bin/trade-research worker`. Both processes use `TRADE_RESEARCH_DATA_DIR` and the same SQLite/report schema.

Configure both built-in analysts with allowlisted settings. This deterministic local example uses
normalized CSV files; `.parquet` and fixed read-only SQLite `prices`/`fundamentals` tables are also
supported:

```sh
TRADE_RESEARCH_PRICE_PROVIDER=local_csv \
TRADE_RESEARCH_PRICE_PATH=/authorized/prices.csv \
TRADE_RESEARCH_FUNDAMENTAL_PROVIDER=local_csv \
TRADE_RESEARCH_FUNDAMENTAL_PATH=/authorized/fundamentals.csv \
  .venv/bin/trade-research doctor
```

Yahoo or Stooq may replace the local price provider. CCXT additionally requires
`TRADE_RESEARCH_CCXT_EXCHANGE`. If a selected analyst's capability is absent, submission fails with
a typed configuration error and no empty successful report is created.

## Docker Compose

Create `secrets/api-token` locally (the ignored `secrets/` directory is never committed). Put
authorized normalized inputs and a closed `config.json` in a separate host directory. Paths in
that JSON are container paths, not host paths. For example:

```json
{
  "price_provider": "local_csv",
  "price_path": "/var/lib/trade-research-sources/prices.csv",
  "fundamental_provider": "local_csv",
  "fundamental_path": "/var/lib/trade-research-sources/fundamentals.csv"
}
```

Select the directory and validate without executing anything:

```sh
TRADE_RESEARCH_SOURCE_DIR=/authorized/trade-research-sources docker compose config --quiet
```

Both the API and worker mount `TRADE_RESEARCH_SOURCE_DIR` read-only at the stable
`/var/lib/trade-research-sources` path and read
`/var/lib/trade-research-sources/config.json` by default. Reports and the queue use the distinct
writable `research-data` volume at `/var/lib/trade-research`; licensed source files never enter
that volume. `compose.yaml` also uses a read-only root filesystem and publishes no host ports.
Start only after reviewing the rendered configuration:

```sh
TRADE_RESEARCH_SOURCE_DIR=/authorized/trade-research-sources docker compose up --build
```

For workstation access, add the explicit override:

```sh
docker compose -f compose.yaml -f compose.override.local.yaml up --build
```

It always binds `127.0.0.1:8000`; the address is intentionally not environment-variable
controlled. For a private WireGuard/Tailscale/other VPN, render a validated override and
review it before use:

```sh
.venv/bin/python scripts/compose_private_override.py 100.64.0.9 > /tmp/trade-research-private.yaml
docker compose -f compose.yaml -f /tmp/trade-research-private.yaml config --quiet
docker compose -f compose.yaml -f /tmp/trade-research-private.yaml up --build
```

The renderer rejects wildcard, multicast, hostname, and public IPv4/IPv6 targets. Bearer
authentication remains mandatory.

## Hermes profile

The optional `hermes` profile is disabled by default. Its local sentinel image is the already-built `trade-research:local` image and is intentionally not a Hermes substitute. Profile startup exits with status 78 unless `HERMES_IMAGE` explicitly names a trusted digest-pinned image (`registry/name@sha256:<64 hex characters>`). The configured image must be `/bin/sh` compatible and provide a default command because the read-only preflight wrapper preserves that command after checking the image setting.

```sh
HERMES_IMAGE='approved.registry/hermes@sha256:<approved-digest>' \
  docker compose --profile hermes config --quiet
HERMES_IMAGE='approved.registry/hermes@sha256:<approved-digest>' \
  docker compose --profile hermes up
```

Hermes receives `http://research-api:8000` through Compose service DNS and the same bearer-token secret as a read-only file. It publishes no host port and shares only the default private Compose network, not the research data volume. Never bake credentials into the image.

## Base image release gate

The development default `PYTHON_BASE_IMAGE=python:3.12-slim` is intentionally readable but mutable; no unverifiable digest is invented. Release CI or deployment must supply an approved digest-pinned value before building:

```sh
docker build \
  --build-arg PYTHON_BASE_IMAGE='python:3.12-slim@sha256:<approved-digest>' \
  -t trade-research:release .
```

The release pipeline must resolve the tag in an approved registry, record and review its `RepoDigest`, build with that digest, scan the result, and update the approved digest through code review. Compose accepts the same `PYTHON_BASE_IMAGE` environment variable for local builds.

## PAMASTER / NAS paired deployment

When TradeAgent is deployed behind PAMASTER, the browser and iPhone should still talk only to
PAMASTER. PAMASTER calls TradeAgent over Docker Compose service DNS:

```text
PAMASTER backend-api -> http://research-api:8000 -> TradeAgent
```

The PAMASTER NAS compose file owns this paired topology and starts TradeAgent as a `research-api`
service. Build and import the TradeAgent image on the NAS, then set these values in the PAMASTER
Docker Project environment:

```text
TRADE_RESEARCH_IMAGE=trade-research:<tag>
PA_TRADE_RESEARCH_ENABLED=true
PA_TRADE_RESEARCH_BASE_URL=http://research-api:8000
PA_TRADE_RESEARCH_API_TOKEN=<shared internal bearer token>
TRADE_RESEARCH_PRICE_PROVIDER=yahoo
```

The same token value is injected into TradeAgent as `TRADE_RESEARCH_API_TOKEN` by the PAMASTER
compose file. Do not publish the TradeAgent port to the LAN for the iPhone app path; expose only
PAMASTER's analytics port and keep the TradeAgent service internal to the Compose project.
