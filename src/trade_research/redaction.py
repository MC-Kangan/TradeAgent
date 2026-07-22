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
_IP_ADDRESS = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6_CANDIDATE = re.compile(r"(?<![0-9A-Za-z:])(?=[0-9A-Fa-f:]*:)[0-9A-Fa-f:]{2,}(?![0-9A-Za-z:])")
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_ACCOUNT_PHRASE = re.compile(
    r"(?i)\b[a-z0-9_]*(?:account|acct)"
    r"(?:[_\s-]?(?:id|number|no|identifier))?\s+(?:is\s+)?"
    r"[A-Za-z0-9][A-Za-z0-9._-]{2,}"
)
_POSITION_NARRATIVE = re.compile(
    r"(?i)(\b(position|positions|portfolio|holding|holdings|exposure)\b|"
    r"\b\d+(?:\.\d+)?\s+(?:shares?|units?|contracts?)\b|"
    r"\b(?:long|short)\s+(?:\d+(?:\.\d+)?\s+[A-Z0-9._/-]+|"
    r"[A-Z0-9._/-]+\s+\d+(?:\.\d+)?)(?:\s+(?:@|at)\s+\d+(?:\.\d+)?)?|"
    r"\b(?:qty|quantity)\s*[:=]?\s*\d+(?:\.\d+)?\s+[A-Z0-9._/-]+"
    r"(?:\s+(?:avg|average)\s+cost\s*[:=]?\s*\d+(?:\.\d+)?)?)"
)
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

    if _POSITION_NARRATIVE.search(value):
        return "[REDACTED SENSITIVE CONTENT]"
    redacted = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    redacted = _BEARER_TOKEN.sub("Bearer [REDACTED]", redacted)
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
