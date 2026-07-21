"""Structural provenance allow-list shared by reports and persistence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from pydantic import JsonValue

_ROOT_SCALAR_FIELDS = frozenset(
    {
        "currency",
        "dataset",
        "field",
        "filing_id",
        "interval",
        "lookback",
        "period",
        "period_end",
        "period_id",
        "period_type",
        "prior_period_id",
        "provider_id",
        "reference_hash",
        "row",
        "snapshot_id",
        "source_id",
        "symbol",
        "table",
        "timeframe",
        "valuation_as_of",
    }
)
_INPUT_SCALAR_FIELDS = frozenset(
    {
        "currency",
        "metric",
        "observed_at",
        "period",
        "period_end",
        "period_id",
        "period_type",
        "prior_period_id",
        "snapshot_id",
        "source",
        "timestamp",
        "valuation_as_of",
    }
)
_REFERENCE_FIELDS = frozenset(
    {
        "dataset",
        "endpoint",
        "field",
        "filing_id",
        "interval",
        "provider_id",
        "reference_hash",
        "row",
        "source_id",
        "symbol",
        "table",
        "timeframe",
    }
)
_OHLCV_FIELDS = frozenset({"close", "high", "low", "open", "volume"})


def sanitize_provenance(provenance: Mapping[str, object]) -> dict[str, JsonValue]:
    """Keep only documented structured metadata fields and value shapes."""

    sanitized: dict[str, JsonValue] = {}
    for key, value in provenance.items():
        if key in _ROOT_SCALAR_FIELDS and _is_scalar(value):
            sanitized[key] = cast(JsonValue, value)
        elif key == "inputs" and isinstance(value, list | tuple):
            sanitized["inputs"] = [
                item
                for item in (_sanitize_input(candidate) for candidate in value)
                if item is not None
            ]
    return sanitized


def sanitize_provider_reference(provenance: Mapping[str, object]) -> dict[str, JsonValue]:
    """Return only bounded provider handles, never filesystem or account metadata."""

    return {
        key: cast(JsonValue, value)
        for key, value in provenance.items()
        if key in _REFERENCE_FIELDS and _is_scalar(value)
    }


def _sanitize_input(value: object) -> dict[str, JsonValue] | None:
    if not isinstance(value, Mapping):
        return None
    sanitized: dict[str, JsonValue] = {}
    for key, candidate in value.items():
        if not isinstance(key, str):
            continue
        if key in _INPUT_SCALAR_FIELDS and _is_scalar(candidate):
            sanitized[key] = cast(JsonValue, candidate)
        elif key == "value":
            sanitized_value = _sanitize_input_value(candidate)
            if sanitized_value is not None:
                sanitized["value"] = sanitized_value
        elif key == "provider_reference" and isinstance(candidate, Mapping):
            sanitized["provider_reference"] = sanitize_provider_reference(
                {str(item_key): item_value for item_key, item_value in candidate.items()}
            )
    return sanitized


def _sanitize_input_value(value: object) -> JsonValue | None:
    if _is_scalar(value):
        return cast(JsonValue, value)
    if isinstance(value, Mapping):
        return {
            str(key): cast(JsonValue, candidate)
            for key, candidate in value.items()
            if isinstance(key, str) and key in _OHLCV_FIELDS and _is_scalar(candidate)
        }
    return None


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, str | int | float | bool)
