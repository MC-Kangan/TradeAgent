"""Small, deterministic redaction boundary for human-facing text."""

from __future__ import annotations

import re
from ipaddress import ip_address

from pydantic import JsonValue

_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|bearer|client[_-]?ip|ip[_-]?address|"
    r"[a-z0-9_]*(?:account|acct)(?:[_-]?(?:id|number|no|identifier))?|"
    r"secret|token|password)\s*[:=]\s*[^\s,;]+"
)
_CREDENTIAL_PHRASE = re.compile(
    r"(?i)\b(api[\s_-]?key|access[\s_-]?token|client[\s_-]?secret|"
    r"private[\s_-]?key|password|passphrase|credential)"
    r"(?:\s+(?:is|equals)\s+|\s*[:=]\s*)([^\s,;]{4,})"
)
_CREDENTIAL_WHITESPACE = re.compile(
    r"(?i)\b(api[\s_-]?key|access[\s_-]?token|client[\s_-]?secret|"
    r"private[\s_-]?key|password|passphrase|credential)\s+([^\s,;]{4,})"
)
_IP_ADDRESS = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6_CANDIDATE = re.compile(r"(?<![0-9A-Za-z:])(?=[0-9A-Fa-f:]*:)[0-9A-Fa-f:]{2,}(?![0-9A-Za-z:])")
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_ACCOUNT_PHRASE = re.compile(
    r"(?i)\b(?:"
    r"[a-z0-9_]*(?:account|acct)(?:[_\s-]?(?:id|number|no|identifier))"
    r"\s+(?:is\s+)?[A-Za-z0-9][A-Za-z0-9._-]{2,}|"
    r"[a-z0-9_]*(?:account|acct)\s+(?:is\s+)?"
    r"[A-Za-z0-9][A-Za-z0-9._-]{2,})"
)
_POSITION_NARRATIVE = re.compile(
    r"(?i)(\b(position|positions|portfolio|holding|holdings|exposure)\b|"
    r"\b\d+(?:\.\d+)?\s+(?:shares?|units?|contracts?)\b|"
    r"\b(?:qty|quantity)\s*[:=]?\s*\d+(?:\.\d+)?\s+[A-Z0-9._/-]+"
    r"(?:\s+(?:avg|average)\s+cost\s*[:=]?\s*\d+(?:\.\d+)?)?)"
)
_DIRECTIONAL_POSITION = re.compile(
    r"\b(?i:long|short)\s+(?:\d+(?:\.\d+)?\s+[A-Z][A-Z0-9._/-]{0,31}|"
    r"[A-Z][A-Z0-9._/-]{0,31}\s+\d+(?:\.\d+)?)"
    r"(?:\s+(?:@|at)\s+\d+(?:\.\d+)?)?\b"
)
_POSITION_SHORTHAND = re.compile(
    r"\b[A-Z][A-Z0-9._/-]{0,31}\s+\d+(?:\.\d+)?\s+(?:@|at)\s+\d+(?:\.\d+)?\b"
)
_OWNED_POSITION = re.compile(
    r"(?i)\b(?:own|owned|owns)\s+\d+(?:\.\d+)?\s+[A-Z0-9._/-]+\s+"
    r"(?:at\s+)?(?:avg|average)\s+cost\s+\d+(?:\.\d+)?\b"
)
_INSTRUMENT_LABEL = re.compile(r"[A-Z0-9^][A-Z0-9._:/^-]{0,31}")
_NUMERIC_TEXT = re.compile(r"[+-]?\d+(?:\.\d+)?")
_SAFE_OUTPUT_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_SAFE_OUTPUT_KEYS = frozenset(
    {
        "close",
        "currency",
        "date",
        "high",
        "label",
        "labels",
        "low",
        "metric",
        "metrics",
        "nested",
        "note",
        "open",
        "payload",
        "period",
        "safe",
        "safemetric",
        "score",
        "signal",
        "status",
        "timestamp",
        "trend",
        "unit",
        "value",
        "values",
        "volume",
    }
)


def redact_text(value: str) -> str:
    """Project free text into the report-safe textual policy."""

    if (
        _POSITION_NARRATIVE.search(value)
        or _DIRECTIONAL_POSITION.search(value)
        or _POSITION_SHORTHAND.search(value)
        or _OWNED_POSITION.search(value)
    ):
        return "[REDACTED SENSITIVE CONTENT]"
    redacted = _BEARER_TOKEN.sub("Bearer [REDACTED]", value)
    redacted = _CREDENTIAL_PHRASE.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    redacted = _CREDENTIAL_WHITESPACE.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    redacted = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    redacted = _ACCOUNT_PHRASE.sub("account id=[REDACTED]", redacted)
    redacted = _IP_ADDRESS.sub("[REDACTED]", redacted)
    return _IPV6_CANDIDATE.sub(_replace_ipv6, redacted)


def _replace_ipv6(match: re.Match[str]) -> str:
    candidate = match.group(0)
    try:
        parsed = ip_address(candidate)
    except ValueError:
        return candidate
    return "[REDACTED]" if parsed.version == 6 else candidate


def redact_json(value: JsonValue) -> JsonValue:
    """Project a JSON value into closed report-safe structured content."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, dict):
        if _looks_like_position_object(value):
            return {}
        redacted: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not _SAFE_OUTPUT_KEY.fullmatch(key):
                continue
            normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", key)
            collapsed = re.sub(r"[^a-z0-9]", "", normalized.lower())
            if collapsed not in _SAFE_OUTPUT_KEYS:
                continue
            redacted[key] = redact_json(item)
        return redacted
    return value


def _looks_like_position_object(value: dict[str, JsonValue]) -> bool:
    normalized = {
        re.sub(r"[^a-z0-9]", "", re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()): item
        for key, item in value.items()
    }
    label = next(
        (
            normalized[key]
            for key in ("label", "instrument", "symbol", "ticker")
            if key in normalized
        ),
        None,
    )
    quantity = next(
        (
            normalized[key]
            for key in ("quantity", "qty", "value", "amount", "shares", "units")
            if key in normalized
        ),
        None,
    )
    if not isinstance(label, str) or not _INSTRUMENT_LABEL.fullmatch(label.upper()):
        return False
    if not _is_numeric_quantity(quantity):
        return False
    unit = normalized.get("unit")
    unit_context = isinstance(unit, str) and unit.lower() in {
        "share",
        "shares",
        "unit",
        "units",
        "contract",
        "contracts",
    }
    cost_context = any(
        key in normalized
        for key in ("averagecost", "avgcost", "cost", "costbasis", "price", "weight")
    )
    explicit_quantity = any(key in normalized for key in ("quantity", "qty", "shares", "units"))
    return unit_context or cost_context or explicit_quantity


def _is_numeric_quantity(value: JsonValue | None) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return True
    return isinstance(value, str) and _NUMERIC_TEXT.fullmatch(value.strip()) is not None
