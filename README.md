# Trade Research

Typed, local-first investment research infrastructure. This package is for research and
analytics only; it does not place trades or connect to brokers.

## Quick start

On macOS or Linux with Python 3.12:

```sh
python3.12 scripts/bootstrap.py --dev
.venv/bin/trade-research doctor
```

Bootstrap installs exact lock files and the editable local package. It never creates or fills
secret files. See `docs/deployment.md` for native and Docker operation, `docs/hermes.md` for agent
integration, and `docs/adapters.md` for enterprise data adapters.
