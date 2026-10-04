"""`freeze_bundle` — the node that turns a working set into a quotable artifact.

Everything before this point accumulates; this is where accumulation stops. The
proposer at L4 is a pure function of the bundle, the critic falsifies against the
same bundle, and `recommendations.bundle_hash` is what ties a recommendation to
the exact evidence it was made from. None of that means anything unless the
bundle is fixed at a point in time and identified by its contents — hence a
SHA-256 over canonical JSON, computed by `EvidenceBundle` itself (it recomputes
on construction, so a tampered bundle cannot be instantiated).

Two things it does that are not just serialisation:

**It bounds the bundle.** A model has a context window, and a case with forty
policy clauses and two long image observations can exceed it. Truncation is
deterministic and ordered — the facts that decide completeness are kept, the
re-retrievable ones go first — and `bundle_truncated` travels to the gate as its
own signal. The gate escalates on it rather than letting the proposer reason
from a bundle it cannot know was trimmed.

**It fails soft.** A bundle that cannot be written is not a reason to
dead-letter the case; it is a reason for a human to look at it. So a storage
failure degrades, and the gate refuses to auto-approve a case with no
frozen bundle (`bundle_not_frozen`) — no artifact, no autonomy.
"""

from __future__ import annotations

import asyncio
import json
import os
from uuid import UUID

import boto3
import psycopg
from botocore.config import Config
from botocore.exceptions import ClientError

from packages.contracts import EvidenceBundle, EvidenceFragment

# What survives a trim, most important first. The five required kinds decide
# `completeness`, so dropping one would silently change the case's verdict;
# policy clauses go first because they are the cheapest to retrieve again and
# the most numerous.
_KEEP_ORDER = (
    "order",
    "shipment",
    "payment_status",
    "returns_history",
    "cohort_stats",
    "classifier_signal",
    "behavior_features",
    "image_observation",
    "policy_clause",
)


def _max_bytes() -> int:
    """Read at call time, not at import: the verifier lowers it to force a trim."""
    return int(os.environ.get("BUNDLE_MAX_BYTES", "120000"))


def _client():
    # addressing_style=path: MinIO does not do virtual-host buckets locally.
    return boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        config=Config(s3={"addressing_style": "path"}),
    )


def _put(bucket: str, key: str, body: bytes) -> None:
    s3 = _client()
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("NoSuchBucket", "404"):
            raise
        s3.create_bucket(Bucket=bucket)
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")


def trim(fragments: list[EvidenceFragment], limit: int) -> tuple[list[EvidenceFragment], bool]:
    """Keep as much as fits, in priority order, and say whether anything was cut.

    Returned in collection order rather than priority order: the bundle is read
    by people as well as by models, and the order things were gathered in is the
    one that makes sense to read.
    """
    order = sorted(
        range(len(fragments)),
        key=lambda i: (_KEEP_ORDER.index(fragments[i].kind)
                       if fragments[i].kind in _KEEP_ORDER else len(_KEEP_ORDER), i),
    )
    kept: set[int] = set()
    size = 2  # the enclosing "[]"
    for i in order:
        cost = len(json.dumps(fragments[i].model_dump(mode="json"), default=str)) + 1
        if size + cost > limit and kept:
            continue
        kept.add(i)
        size += cost
    return [f for i, f in enumerate(fragments) if i in kept], len(kept) != len(fragments)


async def freeze_bundle(state: dict) -> dict:
    """Freeze, store by hash, record. Returns the hash the rest of the system cites."""
    case_id = state["case"].get("id")
    if not case_id:
        return {"degraded_nodes": ["freeze_bundle"],
                "missing_evidence": ["freeze_bundle: no case id to freeze against"]}

    fragments, truncated = trim(state["fragments"], _max_bytes())
    dropped = len(state["fragments"]) - len(fragments)
    bundle = EvidenceBundle.freeze(
        case_id=UUID(str(case_id)),
        fragments=fragments,
        completeness=state["completeness"],
        missing_evidence=state["missing_evidence"],
        degraded_nodes=sorted(set(state["degraded_nodes"])),
        bundle_truncated=truncated,
    )
    key = f"bundles/{case_id}/{bundle.bundle_hash}.json"
    bucket = os.environ.get("S3_BUCKET", "refund-evidence")
    try:
        # boto3 is synchronous and this is the event loop the other cases share.
        await asyncio.to_thread(_put, bucket, key, bundle.model_dump_json(indent=2).encode())
        async with await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"]) as conn:
            await conn.execute(
                "INSERT INTO evidence_bundles (case_id, bundle_hash, s3_key, completeness,"
                " missing_evidence, degraded_nodes, bundle_truncated)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (str(case_id), bundle.bundle_hash, key, bundle.completeness,
                 bundle.missing_evidence, bundle.degraded_nodes, bundle.bundle_truncated),
            )
            await conn.commit()
    except Exception as e:  # noqa: BLE001 — an unstorable bundle escalates, it does not crash
        return {"degraded_nodes": ["freeze_bundle"],
                "missing_evidence": [f"freeze_bundle: {type(e).__name__}: {e}"],
                "bundle_truncated": truncated}

    return {
        # The object itself, not just its name: from L4 the proposer and critic
        # read this and nothing else — never the working state it came from.
        "bundle": bundle,
        "bundle_hash": bundle.bundle_hash,
        "bundle_key": key,
        "bundle_truncated": truncated,
        "bundle_fragments": len(fragments),
        "bundle_dropped": dropped,
    }
