# Merge TradeAgent and FactorPlayground from GitHub

TradeAgent and FactorPlayground are published and versioned as separate repositories:

- `https://github.com/MC-Kangan/TradeAgent`
- `https://github.com/MC-Kangan/FactorPlayground`

Keep their working directories beside one another. FactorPlayground imports the local
TradeAgent package and does not duplicate the analytics implementation.

## Merge order

1. Commit or stash intentional company changes in both repositories.
2. Create an integration branch in each company repository.
3. Fetch and merge TradeAgent first.
4. Run the backend tests, lint, type checks and package build.
5. Fetch and merge FactorPlayground.
6. Install the updated local TradeAgent checkout into the FactorPlayground environment.
7. Run the FactorPlayground tests and the synthetic browser checks.
8. Test one entitled Bloomberg stock and factor pack before accepting the merge.

If the company repository shares history with the published repository, use a normal
upstream remote:

```powershell
git remote add upstream https://github.com/MC-Kangan/TradeAgent.git
git fetch upstream
git switch -c integrate-factor-research-release
git merge upstream/feature/local-research-framework
```

For FactorPlayground, use its repository URL and merge `upstream/main`. If `upstream`
already exists, verify it with `git remote -v`; do not add a duplicate remote.

If the company project does not share Git history, do not use
`--allow-unrelated-histories`. Clone the published repositories into separate review
directories and port the normalized provider contracts, tests and UI changes deliberately.

## Preserve company-owned Bloomberg code

The company integration should continue to own Bloomberg Desktop/BPIPE authentication,
entitlements, approved ticker mappings, proxy settings and secret locations. TradeAgent
should own the typed output boundary: dated levels or returns with source, vendor field,
currency, unit and adjustment basis.

Do not move BLPAPI session objects, credentials or licensed payloads into FactorPlayground.
If the company adapter differs, adapt it to the provider contracts and keep regression,
alignment and presentation code source-independent.

## Validate the paired release

From TradeAgent:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe
.\.venv\Scripts\python.exe -m build
```

From FactorPlayground, using the same environment or an editable install of the sibling
backend:

```powershell
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e ..\TradeAgent
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe app.py --open-browser
```

Verify synthetic single-stock, watchlist and cross-sectional workflows first. Then verify
native Bloomberg prices, aligned model inputs, the last common observation, currency,
adjustment basis and factor transforms against the Terminal.

Record the upstream and company commit IDs for both repositories. Future updates should
follow the same backend-first order. See the FactorPlayground `MERGE_GUIDE.md` for the
detailed file ownership and YAML reconciliation checklist.
