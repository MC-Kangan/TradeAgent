"""Closed observation-value projection for exported research reports."""

from __future__ import annotations

import math
from collections.abc import Mapping
from itertools import islice
from typing import Final, cast

from pydantic import JsonValue

MAX_EXPORT_DEPTH: Final = 4
MAX_EXPORT_ITEMS: Final = 64
MAX_EXPORT_INTEGER: Final = 10**308 - 1

_STRUCTURE_KEYS = frozenset(
    {
        "close",
        "count",
        "high",
        "low",
        "maximum",
        "mean",
        "median",
        "minimum",
        "open",
        "score",
        "standard_deviation",
        "value",
        "values",
        "volume",
    }
)
_CURRENCIES = frozenset(
    {"AUD", "CAD", "CHF", "CNY", "EUR", "GBP", "HKD", "JPY", "NZD", "SGD", "USD"}
)
_STATUSES = frozenset({"complete", "partial", "stale", "unavailable"})
_SIGNALS = frozenset({"bearish", "bullish", "buy", "hold", "neutral", "sell"})
_DROP: Final = object()


def project_observation_value(value: object) -> JsonValue:
    """Return only bounded numeric structures and explicitly closed enum leaves."""

    projected = _project_value(value, depth=0)
    return None if projected is _DROP else cast(JsonValue, projected)


def _project_value(value: object, *, depth: int) -> JsonValue | object:
    if value is None or isinstance(value, bool):
        return cast(JsonValue, value)
    if isinstance(value, int):
        return cast(JsonValue, value) if abs(value) <= MAX_EXPORT_INTEGER else _DROP
    if isinstance(value, float):
        return cast(JsonValue, value) if math.isfinite(value) else _DROP
    if depth >= MAX_EXPORT_DEPTH:
        return _DROP
    if isinstance(value, list | tuple):
        projected_list: list[JsonValue] = []
        for item in value[:MAX_EXPORT_ITEMS]:
            projected = _project_value(item, depth=depth + 1)
            if projected is not _DROP:
                projected_list.append(cast(JsonValue, projected))
        return projected_list
    if isinstance(value, Mapping):
        projected_dict: dict[str, JsonValue] = {}
        for key, item in islice(value.items(), MAX_EXPORT_ITEMS):
            if not isinstance(key, str):
                continue
            if key in _STRUCTURE_KEYS:
                projected = _project_value(item, depth=depth + 1)
            elif key == "currency":
                projected = item if isinstance(item, str) and item in _CURRENCIES else _DROP
            elif key == "status":
                projected = item if isinstance(item, str) and item in _STATUSES else _DROP
            elif key == "signal":
                projected = item if isinstance(item, str) and item in _SIGNALS else _DROP
            else:
                continue
            if projected is not _DROP:
                projected_dict[key] = cast(JsonValue, projected)
        return projected_dict
    return _DROP
