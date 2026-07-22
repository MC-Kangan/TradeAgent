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

## Docker Compose

Create `secrets/api-token` locally (the ignored `secrets/` directory is never committed), then validate without executing anything:

```sh
docker compose config --quiet
```

`compose.yaml` has `research-api` and `research-worker`, one named data volume, a read-only root filesystem, and no host ports. Start only after reviewing the rendered configuration:

```sh
docker compose up --build
```

For workstation access, add the explicit override:

```sh
docker compose -f compose.yaml -f compose.override.local.yaml up --build
```

It binds `127.0.0.1:8000` by default. For a private WireGuard/Tailscale/other VPN, set `TRADE_RESEARCH_BIND_ADDRESS` to the host's private VPN address before rendering and review the result. Do not use `0.0.0.0` or expose this service to the public internet. Bearer authentication remains mandatory.

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
