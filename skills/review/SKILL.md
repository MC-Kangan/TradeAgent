# Review Skill (built-in)

**Name:** (internal — not user-selectable)
**Implementation:** `src/trade_research/skills/core.py:ResearchReviewer`

## Purpose

Post-processes all analyst results after concurrent execution. The reviewer is NOT a
user-selectable skill — it runs automatically as part of `sanitize_report()` and
`ResearchEngine`.

## Behavior

1. **Detects empty results:** If an analyst produces zero observations but their summary
   doesn't start with `"partial data"`, the reviewer rewrites it to
   `"partial data: no derived observations"`.

2. **Preserves all other summaries:** Original summaries from skills pass through
   unchanged. The reviewer does NOT modify observations, metrics, or provenance.

3. **Runs after all skills complete:** `ResearchEngine.analyze()` calls
   `self._reviewer.review(results)` after `asyncio.gather` returns.

## When it matters

The reviewer catches edge cases where a skill reports `COMPLETE` status but produces an
empty observation tuple. This can happen if a future skill implementation has a bug, or
if all inputs are successfully fetched but no factors can be derived due to an unexpected
data shape.

## Example

```python
# A skill returns zero observations but claims completeness
result = AnalystResult(
    analyst="fundamental",
    instrument=instrument,
    summary="complete data: all required inputs available",
    status=ReportStatus.COMPLETE,
    observations=(),  # empty!
)

reviewed = ResearchReviewer().review((result,))
assert reviewed[0].summary == "partial data: no derived observations"
```

## Integration

The reviewer is instantiated once in `ResearchEngine.__init__()` and used in `analyze()`:

```python
return sanitize_report(
    ResearchReport(
        ...
        results=self._reviewer.review(results),  # <-- reviewer runs here
        ...
    )
)
```

Users cannot disable the reviewer — it's part of the engine's output contract.
