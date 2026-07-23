# Review Examples

## Normal pass-through

All skills produce valid results with observations. The reviewer returns them unchanged:

```python
results = (
    AnalystResult(
        analyst="fundamental",
        instrument=InstrumentId(symbol="AAPL", market="NASDAQ"),
        summary="complete data: all required inputs available",
        status=ReportStatus.COMPLETE,
        observations=(...),  # 11 derived factors
    ),
    AnalystResult(
        analyst="technical",
        instrument=InstrumentId(symbol="AAPL", market="NASDAQ"),
        summary="partial data: missing average_true_range",
        status=ReportStatus.PARTIAL,
        observations=(...),  # 12 derived factors
    ),
)

reviewed = ResearchReviewer().review(results)
assert reviewed == results  # unchanged
```

## Empty result correction

A skill with a bug returns no observations but claims completeness:

```python
results = (
    AnalystResult(
        analyst="fundamental",
        instrument=InstrumentId(symbol="NODATA", market="NASDAQ"),
        summary="complete data: all required inputs available",
        status=ReportStatus.COMPLETE,
        observations=(),  # bug: should be empty + partial
    ),
)

reviewed = ResearchReviewer().review(results)
assert reviewed[0].summary == "partial data: no derived observations"
# Note: status is NOT changed — that's handled by _project_result in reporting.py
```

## Text sanitization

The reviewer also applies `_sanitize_text()` to all summaries, stripping control
characters and Unicode bidi overrides. This is defense-in-depth — summaries are generated
from fixed templates, but sanitization ensures no injected characters survive.
