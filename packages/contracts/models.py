"""The shared spine. Everything in the system imports these.

Three invariants live in the type system rather than in documentation:

1. `Outcome` has no DENY. Only a human can deny a refund, so the agent has no
   vocabulary for it and failures can only escalate.
2. An APPROVE must cite evidence. Enforced by a validator, not a prompt.
3. Fragment refs are content-addressed, so a citation cannot point at a
   fragment that was not in the frozen bundle.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .canonical import sha256_hex

FragmentKind = Literal[
    "order",
    "payment_status",
    "shipment",
    "returns_history",
    "cohort_stats",
    "image_observation",
    "behavior_features",
    "policy_clause",
    "classifier_signal",
]
Completeness = Literal["complete", "incomplete"]
Outcome = Literal["APPROVE", "ESCALATE"]  # no DENY — see invariant 1
GateResult = Literal["ALLOW_AUTO", "ESCALATE"]
AutonomyMode = Literal["shadow", "suggest", "assist", "auto"]

CaseStatus = Literal[
    "RECEIVED",
    "EVIDENCE_GATHERING",
    "EVIDENCE_FAILED",
    "SCORED",
    "AUTO_APPROVED",
    "PENDING_REVIEW",
    "EXECUTING",
    "REFUNDED",
    "REJECTED",  # human actor only
    "EXPIRED",
    "FAILED",
]

REASON_CODES = (
    "DAMAGED",
    "DEFECTIVE",
    "NOT_AS_DESCRIBED",
    "WRONG_ITEM",
    "DIDNT_FIT",
    "CHANGED_MIND",
    "NEVER_ARRIVED",
    "MISSING_PARTS",
)
ReasonCode = Literal[
    "DAMAGED",
    "DEFECTIVE",
    "NOT_AS_DESCRIBED",
    "WRONG_ITEM",
    "DIDNT_FIT",
    "CHANGED_MIND",
    "NEVER_ARRIVED",
    "MISSING_PARTS",
]


class EvidenceFragment(BaseModel):
    """One retrieved fact. `ref` is derived, never supplied."""

    model_config = ConfigDict(frozen=True)

    kind: FragmentKind
    source: str  # which tool or node produced it
    data: dict
    degraded: bool = False
    retrieved_at: datetime

    @property
    def ref(self) -> str:
        # Content-addressed: retrieved_at and degraded are excluded so the same
        # fact fetched twice cites identically.
        return sha256_hex({"kind": self.kind, "source": self.source, "data": self.data})[:12]


class EvidenceBundle(BaseModel):
    """A frozen snapshot. The proposer is a pure function of this."""

    model_config = ConfigDict(frozen=True)

    case_id: UUID
    fragments: list[EvidenceFragment]
    completeness: Completeness
    missing_evidence: list[str] = Field(default_factory=list)
    degraded_nodes: list[str] = Field(default_factory=list)
    bundle_truncated: bool = False
    bundle_hash: str

    @staticmethod
    def compute_hash(
        case_id: UUID,
        fragments: list[EvidenceFragment],
        completeness: str,
        missing_evidence: list[str],
        degraded_nodes: list[str],
        bundle_truncated: bool,
    ) -> str:
        return sha256_hex(
            {
                "case_id": case_id,
                "fragments": [f.model_dump(mode="json") for f in fragments],
                "completeness": completeness,
                "missing_evidence": sorted(missing_evidence),
                "degraded_nodes": sorted(degraded_nodes),
                "bundle_truncated": bundle_truncated,
            }
        )

    @classmethod
    def freeze(
        cls,
        *,
        case_id: UUID,
        fragments: list[EvidenceFragment],
        completeness: Completeness,
        missing_evidence: list[str] | None = None,
        degraded_nodes: list[str] | None = None,
        bundle_truncated: bool = False,
    ) -> EvidenceBundle:
        missing = missing_evidence or []
        degraded = degraded_nodes or []
        return cls(
            case_id=case_id,
            fragments=fragments,
            completeness=completeness,
            missing_evidence=missing,
            degraded_nodes=degraded,
            bundle_truncated=bundle_truncated,
            bundle_hash=cls.compute_hash(
                case_id, fragments, completeness, missing, degraded, bundle_truncated
            ),
        )

    @model_validator(mode="after")
    def _hash_must_match(self) -> EvidenceBundle:
        expected = self.compute_hash(
            self.case_id,
            self.fragments,
            self.completeness,
            self.missing_evidence,
            self.degraded_nodes,
            self.bundle_truncated,
        )
        if self.bundle_hash != expected:
            raise ValueError(f"bundle_hash mismatch: expected {expected}")
        return self

    @property
    def refs(self) -> set[str]:
        return {f.ref for f in self.fragments}


class Recommendation(BaseModel):
    """What the proposer emits. A signal, never a decision."""

    outcome: Outcome
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    evidence_refs: list[str] = Field(default_factory=list)
    model: str
    prompt_version: str

    @model_validator(mode="after")
    def _approve_must_cite(self) -> Recommendation:
        if self.outcome == "APPROVE" and not self.evidence_refs:
            raise ValueError("APPROVE requires at least one evidence_ref")
        return self


class Critique(BaseModel):
    """What the cross-family critic emits. Also only a signal."""

    dissents: bool
    unsupported_claims: list[str] = Field(default_factory=list)
    notes: str = ""
    model: str
    prompt_version: str


class GateInput(BaseModel):
    """Every signal the policy gate is allowed to consider.

    `recommendation` is optional because the gate decides every case, including
    one whose proposer failed — that case must still get a binding rule_id
    (`no_recommendation`), not skip the gate. `facts` are the commerce tool
    results from the frozen bundle, keyed by tool name, so policy rules read
    what a tool returned rather than what a model said about it.
    """

    recommendation: Recommendation | None = None
    critic_dissents: bool
    unsupported_claims: list[str] = Field(default_factory=list)
    evidence_completeness: Completeness
    degraded_nodes: list[str] = Field(default_factory=list)
    amount: Decimal
    reason_code: str
    customer_tier: str
    autonomy_mode: AutonomyMode
    kill_switch_active: bool
    injection_flagged: bool = False
    safety_flagged: bool = False
    reason_mismatch: bool = False
    bundle_truncated: bool = False
    citations_verified: bool = False
    bundle_frozen: bool = False
    refs_outside_bundle: list[str] = Field(default_factory=list)
    critic_reviewed: bool = False
    critic_independent: bool = False
    facts: dict = Field(default_factory=dict)


class GateOutput(BaseModel):
    """The only terminal routing decision in the system."""

    result: GateResult
    rule_id: str  # which rule bound the decision
    policy_version: str


class CaseIntake(BaseModel):
    """What every intake surface (form, webhook, fixture CLI) normalises to."""

    order_id: str
    customer_id: str
    reason_code: ReasonCode
    description: str = ""
    amount: Decimal
    currency: str = "IDR"
    attachment_keys: list[str] = Field(default_factory=list)
    source: Literal["form", "webhook", "fixture"] = "form"
