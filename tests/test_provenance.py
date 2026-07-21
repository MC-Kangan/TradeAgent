from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from trade_research.domain import InstrumentId, Observation
from trade_research.domain.provenance import sanitize_provenance
from trade_research.providers import PricePoint

OPAQUE_REFERENCE = "sha256:" + "a" * 64


def test_closed_provenance_schema_retains_only_typed_values() -> None:
    provenance = {
        "provider_kind": "bloomberg",
        "metric": "revenue",
        "vendor_field": "REVENUE",
        "timestamp": "2026-01-02T00:00:00+00:00",
        "period_end": "2025-12-31",
        "valuation_as_of": "2026-01-02T00:00:00+00:00",
        "currency": "USD",
        "period_type": "annual",
        "period_role": "current",
        "reference": OPAQUE_REFERENCE,
        "period_ref": OPAQUE_REFERENCE,
        "prior_period_ref": OPAQUE_REFERENCE,
        "snapshot_ref": OPAQUE_REFERENCE,
    }

    assert sanitize_provenance(provenance) == provenance


@pytest.mark.parametrize(
    "key",
    (
        "provider_kind",
        "metric",
        "vendor_field",
        "timestamp",
        "observed_at",
        "period_end",
        "valuation_as_of",
        "currency",
        "period_type",
        "period_role",
        "reference",
        "period_ref",
        "prior_period_ref",
        "snapshot_ref",
        "source_id",
        "dataset",
        "filing_id",
        "field",
    ),
)
@pytest.mark.parametrize(
    "sensitive_value",
    (
        "secret-token",
        "broker-account-123",
        "203.0.113.9",
        "ACME:1000-shares",
    ),
)
def test_every_provenance_field_rejects_or_omits_sensitive_text(
    key: str, sensitive_value: str
) -> None:
    assert sanitize_provenance({key: sensitive_value}) == {}


@pytest.mark.parametrize(
    "sensitive_value",
    ("secret-token", "broker-account-123", "203.0.113.9", "ACME:1000-shares"),
)
@pytest.mark.parametrize(
    "structured_provenance",
    (
        {"inputs": "SENSITIVE"},
        {"inputs": [{"value": "SENSITIVE"}]},
        {"inputs": [{"provider_reference": {"reference": "SENSITIVE"}}]},
        {"inputs": [{"value": {"close": "SENSITIVE"}}]},
    ),
)
def test_nested_provenance_keys_cannot_carry_sensitive_text(
    structured_provenance: dict[str, object], sensitive_value: str
) -> None:
    payload = json.dumps(structured_provenance).replace("SENSITIVE", sensitive_value)

    assert sensitive_value not in json.dumps(sanitize_provenance(json.loads(payload)))


@pytest.mark.parametrize(
    "sensitive_source",
    (
        "secret-token",
        "broker-account-123",
        "203.0.113.9",
        "ACME:1000-shares",
        "/Users/alice/private",
    ),
)
def test_observation_rejects_free_form_source(sensitive_source: str) -> None:
    with pytest.raises(ValidationError):
        Observation(
            instrument=InstrumentId(symbol="ACME", market="US"),
            metric="close",
            value=100.0,
            source=sensitive_source,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_observation_normalizes_known_provider_kind() -> None:
    observation = Observation(
        instrument=InstrumentId(symbol="ACME", market="US"),
        metric="close",
        value=100.0,
        source="LOCAL_CSV",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert observation.source == "local_csv"


@pytest.mark.parametrize(
    "sensitive_source",
    ("secret-token", "broker-account-123", "203.0.113.9", "ACME:1000-shares"),
)
def test_price_point_rejects_free_form_custom_provider_source(sensitive_source: str) -> None:
    with pytest.raises(ValueError, match="known provider kind"):
        PricePoint(
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            close=100.0,
            source=sensitive_source,
        )


def test_custom_price_provider_uses_internal_kind_and_hashed_reference() -> None:
    point = PricePoint(
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        close=100.0,
        source="internal",
        provenance={
            "provider_kind": "internal",
            "vendor_field": "CLOSE",
            "reference": OPAQUE_REFERENCE,
        },
    )

    assert point.source == "internal"
    assert point.provenance == {
        "provider_kind": "internal",
        "vendor_field": "CLOSE",
        "reference": OPAQUE_REFERENCE,
    }
