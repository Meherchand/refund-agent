"""Intake, status, the customer-facing page, and the reviewer dashboard (L6).

Intake is deliberately outbox-free (PLANNING-LOCAL.md §3.1): insert, commit,
publish, return 202. The dual-write gap that opens is contained by the sweeper
below, which re-publishes any case still sitting in RECEIVED.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import boto3
import psycopg
from botocore.config import Config
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from psycopg.rows import dict_row
from pydantic import BaseModel, model_validator

from packages import audit
from packages.bus import connect, declare, publish
from packages.contracts import CaseIntake, EvidenceBundle

DSN = os.environ["DATABASE_URL"]
SLA_HOURS = 48
SWEEP_SECONDS = 60
MAX_SWEEPS = 3
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="refund-api", version="0.1.0")
_bus: dict = {}


async def db() -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(DSN, row_factory=dict_row)


async def sweeper() -> None:
    """The outbox's replacement, and the SLA timer, in one loop.

    A case still RECEIVED a minute after intake means the publish was lost
    between commit and broker. Re-publishing is safe: case-worker is idempotent
    on case_id, so the worst case is a duplicate message that no-ops.

    Bounded by `sweep_count`. A case that always fails never leaves RECEIVED,
    so an unbounded sweeper would re-publish it every 60s forever, each round
    costing another x-delivery-limit worth of attempts and another dead-letter.
    After MAX_SWEEPS the case is marked FAILED and left alone; the messages stay
    in case-events.dlq for an operator to inspect.
    """
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            async with await db() as conn:
                cur = await conn.execute(
                    "UPDATE cases SET sweep_count = sweep_count + 1, updated_at = now()"
                    " WHERE status = 'RECEIVED' AND sweep_count < %s"
                    " AND created_at < now() - interval '60 seconds' RETURNING id",
                    (MAX_SWEEPS,),
                )
                rows = await cur.fetchall()
                await conn.execute(
                    "UPDATE cases SET status = 'FAILED', updated_at = now()"
                    " WHERE status = 'RECEIVED' AND sweep_count >= %s",
                    (MAX_SWEEPS,),
                )
                await conn.execute(
                    "UPDATE cases SET status = 'EXPIRED', updated_at = now()"
                    " WHERE status = 'PENDING_REVIEW' AND sla_due_at < now()"
                )
                await conn.commit()
                for row in rows:
                    await publish(_bus["channel"], "case.submitted", {"case_id": str(row["id"])})
        except Exception as e:  # a background loop must not die
            print(f"[sweeper] {type(e).__name__}: {e}", flush=True)


@app.on_event("startup")
async def startup() -> None:
    conn = await connect()
    channel = await conn.channel()
    await declare(channel)
    _bus["conn"], _bus["channel"] = conn, channel
    _bus["task"] = asyncio.create_task(sweeper())


@app.on_event("shutdown")
async def shutdown() -> None:
    _bus["task"].cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _bus["task"]
    await _bus["conn"].close()


@app.get("/health")
async def health() -> dict:
    async with await db() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/v1/returns", status_code=202)
async def submit(intake: CaseIntake) -> dict:
    sla = datetime.now(timezone.utc) + timedelta(hours=SLA_HOURS)
    async with await db() as conn:
        cur = await conn.execute(
            "INSERT INTO cases (order_id, customer_id, reason_code, description, amount,"
            " currency, source, sla_due_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (intake.order_id, intake.customer_id, intake.reason_code, intake.description,
             intake.amount, intake.currency, intake.source, sla),
        )
        case_id = (await cur.fetchone())["id"]
        await audit.append(conn, case_id, "case_received",
                           {"order_id": intake.order_id, "reason_code": intake.reason_code,
                            "amount": intake.amount, "source": intake.source}, "customer")
        await conn.commit()  # commit first — see the module docstring

    try:
        await publish(_bus["channel"], "case.submitted", {"case_id": str(case_id)})
    except Exception as e:
        # The row exists and the sweeper will pick it up, but tell the caller
        # rather than implying the case is moving.
        raise HTTPException(502, f"case {case_id} accepted but not queued: {e}") from e

    return {"case_id": str(case_id), "status": "RECEIVED"}


@app.get("/v1/cases/{case_id}")
async def get_case(case_id: UUID) -> dict:
    """The customer's own view of their case, so an allow-list, not `SELECT *`.

    The row also carries the guard outcomes — `injection_flagged` among them —
    and returning those to the person who wrote the description is an oracle:
    iterate on an injection and read off whether each attempt was caught. Who
    decided is left out for the same reason; the status page reads `status`.
    """
    async with await db() as conn:
        cur = await conn.execute(
            "SELECT id, order_id, reason_code, amount, currency, status, created_at,"
            " updated_at, sla_due_at FROM cases WHERE id = %s",
            (case_id,),
        )
        case = await cur.fetchone()
        if case is None:
            raise HTTPException(404, "no such case")
        cur = await conn.execute(
            "SELECT outcome, created_at FROM decisions"
            " WHERE case_id = %s ORDER BY created_at LIMIT 1",
            (case_id,),
        )
        case["decision"] = await cur.fetchone()
        cur = await conn.execute(
            "SELECT a.status FROM refund_attempts a JOIN decisions d ON d.id = a.decision_id"
            " WHERE d.case_id = %s",
            (case_id,),
        )
        case["refund"] = await cur.fetchone()
    return case


@app.get("/v1/samples")
async def samples() -> list[dict]:
    """One labelled case per planted pattern, to prefill the form."""
    async with await db() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT ON (pattern) pattern, order_id, customer_id, reason_code,"
            " description, amount FROM seed_labels ORDER BY pattern, id"
        )
        return await cur.fetchall()


@app.get("/", response_class=HTMLResponse)
@app.get("/c/{case_id}", response_class=HTMLResponse)
async def page(case_id: str = "") -> str:
    return (STATIC / "index.html").read_text()


# --- Reviewer dashboard (L6) -------------------------------------------------
# Everything below needs a signed-in reviewer, and is a separate surface from the
# customer's `/v1/cases/{id}` above on purpose: that one is an allow-list, and a
# reviewer needs exactly the fields it leaves out.
#
# HTTP Basic against `reviewers`, checked by Postgres (`crypt`, see
# infra/local/reviewers.sql). The browser asks once at /review and resends the
# credentials on every fetch, so there is no login page and no token to issue.

_basic = HTTPBasic()
APPROVE_REASONS = {"EVIDENCE_SUFFICIENT", "POLICY_EXCEPTION", "GOODWILL"}
REJECT_REASONS = {"POLICY_NOT_MET", "FRAUD_SUSPECTED", "INSUFFICIENT_EVIDENCE"}


async def reviewer(creds: Annotated[HTTPBasicCredentials, Depends(_basic)]) -> dict:
    async with await db() as conn:
        cur = await conn.execute(
            "SELECT email, role FROM reviewers WHERE email = %s"
            " AND password_hash = crypt(%s, password_hash)",
            (creds.username, creds.password),
        )
        who = await cur.fetchone()
    if who is None:
        raise HTTPException(401, "unknown reviewer", headers={"WWW-Authenticate": "Basic"})
    return who


Reviewer = Annotated[dict, Depends(reviewer)]


async def admin(who: Reviewer) -> dict:
    if who["role"] != "admin":
        raise HTTPException(403, "admin only")
    return who


Admin = Annotated[dict, Depends(admin)]


class HumanDecision(BaseModel):
    """A reviewer's decision. The reason code is required and must fit the
    outcome — it is what L7 reads to learn why people overrode the gate."""

    outcome: Literal["APPROVE", "REJECT"]
    reason_code: str

    @model_validator(mode="after")
    def _reason_fits(self) -> HumanDecision:
        allowed = APPROVE_REASONS if self.outcome == "APPROVE" else REJECT_REASONS
        if self.reason_code not in allowed:
            raise ValueError(f"{self.outcome} needs one of {sorted(allowed)}")
        return self


class KillSwitch(BaseModel):
    scope: str = "global"
    active: bool
    reason: str


def _bundle(key: str) -> EvidenceBundle:
    s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT_URL"],
                      config=Config(s3={"addressing_style": "path"}))
    body = s3.get_object(Bucket=os.environ.get("S3_BUCKET", "refund-evidence"), Key=key)["Body"]
    # Constructing it re-checks the hash: an edited bundle cannot be shown.
    return EvidenceBundle.model_validate_json(body.read())


@app.get("/review", response_class=HTMLResponse)
async def review_page(_: Reviewer) -> str:
    return (STATIC / "review.html").read_text()


@app.get("/v1/review/me")
async def me(who: Reviewer) -> dict:
    return who


@app.get("/v1/review/queue")
async def queue(_: Reviewer) -> list[dict]:
    """Oldest deadline first."""
    async with await db() as conn:
        cur = await conn.execute(
            "SELECT c.id, c.reason_code, c.amount, c.currency, c.sla_due_at, c.created_at,"
            " c.safety_flagged, g.rule_id, g.autonomy_mode, r.outcome AS recommendation,"
            " k.dissents AS critic_dissents FROM cases c"
            " LEFT JOIN gate_evaluations g ON g.case_id = c.id"
            " LEFT JOIN recommendations r ON r.id = g.recommendation_id"
            " LEFT JOIN critiques k ON k.recommendation_id = r.id"
            " WHERE c.status = 'PENDING_REVIEW' ORDER BY c.sla_due_at NULLS LAST LIMIT 200"
        )
        return await cur.fetchall()


@app.get("/v1/review/cases/{case_id}")
async def evidence_view(case_id: UUID, _: Reviewer) -> dict:
    """Everything the decision rested on: the frozen bundle (hash re-checked),
    the gate's exact input and binding rule, the agent's recommendation and the
    critic's objections, and the case's audit trail."""
    async with await db() as conn:
        async def one(sql: str) -> dict | None:
            return await (await conn.execute(sql, (case_id,))).fetchone()

        async def many(sql: str) -> list[dict]:
            return await (await conn.execute(sql, (case_id,))).fetchall()

        view = {"case": await one("SELECT * FROM cases WHERE id = %s")}
        if view["case"] is None:
            raise HTTPException(404, "no such case")
        view["gate"] = await one("SELECT * FROM gate_evaluations WHERE case_id = %s")
        view["recommendation"] = await one(
            "SELECT * FROM recommendations WHERE case_id = %s ORDER BY created_at DESC LIMIT 1")
        view["critique"] = view["recommendation"] and await one(
            "SELECT k.* FROM critiques k JOIN recommendations r ON r.id = k.recommendation_id"
            " WHERE r.case_id = %s ORDER BY k.created_at DESC LIMIT 1")
        view["decisions"] = await many("SELECT * FROM decisions WHERE case_id = %s")
        view["audit"] = await many("SELECT seq, event_type, actor, at, payload_json"
                                   " FROM audit_log WHERE case_id = %s ORDER BY seq")
        row = await one("SELECT s3_key FROM evidence_bundles WHERE case_id = %s"
                        " ORDER BY created_at DESC LIMIT 1")
    view["bundle"] = None
    if row:
        try:
            b = await asyncio.to_thread(_bundle, row["s3_key"])
            view["bundle"] = {"hash": b.bundle_hash, "hash_verified": True,
                              "fragments": [f.model_dump(mode="json") | {"ref": f.ref}
                                            for f in b.fragments]}
        except Exception as e:  # shown, not raised: the rest of the view still helps
            view["bundle"] = {"error": f"{type(e).__name__}: {e}"}
    return view


@app.post("/v1/review/cases/{case_id}/decision", status_code=201)
async def decide(case_id: UUID, d: HumanDecision, who: Reviewer) -> dict:
    """The only way a case leaves PENDING_REVIEW for a person's reason. The
    status flip is the guard: two reviewers deciding at once, one wins."""
    status = "EXECUTING" if d.outcome == "APPROVE" else "REJECTED"
    async with await db() as conn:
        cur = await conn.execute(
            "UPDATE cases SET status = %s, updated_at = now()"
            " WHERE id = %s AND status = 'PENDING_REVIEW' RETURNING id",
            (status, case_id),
        )
        if await cur.fetchone() is None:
            raise HTTPException(409, "case is not awaiting review")
        cur = await conn.execute("SELECT id FROM gate_evaluations WHERE case_id = %s", (case_id,))
        gate = await cur.fetchone()
        cur = await conn.execute(
            "INSERT INTO decisions (case_id, outcome, actor_type, actor_id, gate_evaluation_id,"
            " override_reason_code) VALUES (%s, %s, 'human', %s, %s, %s) RETURNING id",
            (case_id, d.outcome, who["email"], gate and gate["id"], d.reason_code),
        )
        decision_id = (await cur.fetchone())["id"]
        await audit.append(conn, case_id, "decided", {
            "decision_id": decision_id, "outcome": d.outcome, "actor_type": "human",
            "override_reason_code": d.reason_code,
            "gate_evaluation_id": gate and gate["id"]}, who["email"])
        await conn.commit()
    if d.outcome == "APPROVE":
        await publish(_bus["channel"], "refund.approved",
                      {"case_id": str(case_id), "decision_id": str(decision_id)})
    return {"decision_id": str(decision_id), "status": status}


@app.get("/v1/admin/kill-switch")
async def kill_switch_state(_: Reviewer) -> list[dict]:
    async with await db() as conn:
        return await (await conn.execute("SELECT * FROM kill_switch ORDER BY scope")).fetchall()


@app.post("/v1/admin/kill-switch")
async def set_kill_switch(k: KillSwitch, who: Admin) -> dict:
    """Admin only. The gate reads this row on every evaluation (L5)."""
    async with await db() as conn:
        await conn.execute(
            "INSERT INTO kill_switch (scope, active, set_by, reason, set_at)"
            " VALUES (%s, %s, %s, %s, now()) ON CONFLICT (scope) DO UPDATE SET"
            " active = EXCLUDED.active, set_by = EXCLUDED.set_by, reason = EXCLUDED.reason,"
            " set_at = EXCLUDED.set_at",
            (k.scope, k.active, who["email"], k.reason),
        )
        await audit.append(conn, None, "kill_switch_set", k.model_dump(), who["email"])
        await conn.commit()
    return k.model_dump() | {"set_by": who["email"]}
