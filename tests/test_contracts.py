from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from packages.contracts import (
    EvidenceBundle,
    EvidenceFragment,
    GateInput,
    Recommendation,
    canonical_json,
)

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)


def frag(kind="order", source="mcp-commerce.get_order", **data) -> EvidenceFragment:
    return EvidenceFragment(kind=kind, source=source, data=data or {"id": "o-1"}, retrieved_at=NOW)


# --- canonical_json ---------------------------------------------------------


def test_key_order_does_not_change_bytes():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_decimal_serialises_as_string_not_float():
    assert canonical_json({"amount": Decimal("19.99")}) == '{"amount":"19.99"}'


def test_naive_and_aware_utc_datetimes_agree():
    naive = datetime(2026, 9, 20, 10, 0)
    assert canonical_json(naive) == canonical_json(NOW)


def test_offset_datetime_normalises_to_utc():
    jakarta = datetime(2026, 9, 20, 17, 0, tzinfo=timezone(timedelta(hours=7)))
    assert canonical_json(jakarta) == canonical_json(NOW)


# --- fragment refs ----------------------------------------------------------


def test_ref_is_stable_across_retrieval_time():
    a = EvidenceFragment(kind="order", source="s", data={"x": 1}, retrieved_at=NOW)
    b = EvidenceFragment(
        kind="order", source="s", data={"x": 1}, retrieved_at=NOW + timedelta(hours=3)
    )
    assert a.ref == b.ref


def test_ref_changes_with_content():
    assert frag(id="o-1").ref != frag(id="o-2").ref


# --- bundle hash ------------------------------------------------------------


def test_freeze_then_validate_roundtrips():
    b = EvidenceBundle.freeze(case_id=uuid4(), fragments=[frag()], completeness="complete")
    assert EvidenceBundle(**b.model_dump()).bundle_hash == b.bundle_hash


def test_fragment_order_is_part_of_the_hash():
    cid = uuid4()
    f1, f2 = frag(id="o-1"), frag(id="o-2")
    a = EvidenceBundle.freeze(case_id=cid, fragments=[f1, f2], completeness="complete")
    b = EvidenceBundle.freeze(case_id=cid, fragments=[f2, f1], completeness="complete")
    assert a.bundle_hash != b.bundle_hash


def test_tampered_bundle_is_rejected():
    b = EvidenceBundle.freeze(case_id=uuid4(), fragments=[frag()], completeness="complete")
    payload = b.model_dump()
    payload["completeness"] = "incomplete"
    with pytest.raises(ValidationError, match="bundle_hash mismatch"):
        EvidenceBundle(**payload)


# --- recommendation invariants ---------------------------------------------


def test_deny_is_not_expressible():
    with pytest.raises(ValidationError):
        Recommendation(
            outcome="DENY", confidence=0.9, rationale="r", model="m", prompt_version="v1"
        )


def test_approve_without_citations_is_rejected():
    with pytest.raises(ValidationError, match="evidence_ref"):
        Recommendation(
            outcome="APPROVE", confidence=0.9, rationale="r", model="m", prompt_version="v1"
        )


def test_escalate_needs_no_citations():
    r = Recommendation(
        outcome="ESCALATE", confidence=0.4, rationale="thin", model="m", prompt_version="v1"
    )
    assert r.evidence_refs == []


def test_confidence_is_bounded():
    with pytest.raises(ValidationError):
        Recommendation(
            outcome="ESCALATE", confidence=1.4, rationale="r", model="m", prompt_version="v1"
        )


# --- gate input -------------------------------------------------------------


def test_gate_input_defaults_the_new_signals_to_false():
    gi = GateInput(
        recommendation=Recommendation(
            outcome="ESCALATE", confidence=0.5, rationale="r", model="m", prompt_version="v1"
        ),
        critic_dissents=False,
        evidence_completeness="complete",
        amount=Decimal("250000"),
        reason_code="DIDNT_FIT",
        customer_tier="standard",
        autonomy_mode="suggest",
        kill_switch_active=False,
    )
    assert not any(
        [
            gi.injection_flagged,
            gi.safety_flagged,
            gi.reason_mismatch,
            gi.bundle_truncated,
            gi.citations_verified,
        ]
    )
