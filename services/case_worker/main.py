"""Consumes `case-events` and runs the decision graph.

The graph is guards → classifier → capped tool-calling collector → three
parallel specialists → completeness check → citation check → frozen bundle →
proposer → cross-family critic (`graph.py`). Then the OPA policy gate (L5,
`gate.py` + `policy/rego/refund.rego`) makes the only routing decision: every
signal above is an input to it, none is an edge to a terminal state. Its
answer, the input it answered, and the rule that bound it are recorded in
`gate_evaluations` for every case, whichever way it went.

Cannot move money: it publishes `refund.approved` and stops. Only `executor`
holds the credentials for `refund_attempts` and `ledger_entries`.
"""

from __future__ import annotations

import asyncio
import json
import os

import psycopg
from aio_pika.abc import AbstractIncomingMessage
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from packages import audit
from packages.bus import connect, declare, publish
from packages.contracts.canonical import sha256_hex
from packages.llm import Gateway, lineage

from . import gate
from .graph import run
from .tools import ToolProvider, provider_from_env

DSN = os.environ["DATABASE_URL"]
PREFETCH = int(os.environ.get("MAX_CONCURRENT_CASES", "3"))
_bus: dict = {}
_gw: Gateway
_tools: ToolProvider


async def decide(case_id: str) -> str | None:
    """Returns the decision id, or None if there is nothing to publish."""
    async with await psycopg.AsyncConnection.connect(DSN, row_factory=dict_row) as conn:
        cur = await conn.execute("SELECT * FROM cases WHERE id = %s", (case_id,))
        case = await cur.fetchone()
        if case is None:
            print(f"[worker] {case_id} no such case — dropping", flush=True)
            return None

        # Idempotent on case_id: a re-delivered or swept message must not
        # produce a second decision.
        cur = await conn.execute("SELECT id FROM decisions WHERE case_id = %s", (case_id,))
        if existing := await cur.fetchone():
            print(f"[worker] {case_id} already decided — skipping", flush=True)
            return str(existing["id"])

        # The DLQ demo hook. Raises before any state change.
        if "BOOM" in (case["description"] or ""):
            raise RuntimeError("forced failure (description contains BOOM)")

        await conn.execute(
            "UPDATE cases SET status = 'EVIDENCE_GATHERING', updated_at = now() WHERE id = %s",
            (case_id,),
        )
        await conn.commit()

        case["amount"] = str(case["amount"])  # Decimal is not JSON-serialisable
        state = await run(case, _gw, _tools)
        tier = gate.tier(gate.facts(state))
        mode, kill = await gate.ladder(conn, case["reason_code"], case["amount"], tier)
        gi = gate.gate_input(state, mode, kill)
        out = await gate.evaluate(gi)
        status = "AUTO_APPROVED" if out.result == "ALLOW_AUTO" else "PENDING_REVIEW"
        reason = out.rule_id
        rec, crit = state.get("recommendation"), state.get("critique")
        print(
            f"[worker] {case_id} {state['completeness']}"
            f" frags={len(state['fragments'])} calls={state.get('tool_calls_made', 0)}"
            f" cited={state['citations_verified']}"
            f" bundle={(state.get('bundle_hash') or '')[:12]}"
            f"{' TRUNCATED' if state.get('bundle_truncated') else ''}"
            f" rec={rec.outcome if rec else None}"
            f" critic={'dissent' if crit and crit.dissents else 'ok' if crit else None}"
            f" degraded={sorted(set(state['degraded_nodes']))} -> {status} ({reason})",
            flush=True,
        )

        # Recorded whichever way the case routed: an escalated case's
        # recommendation is what L7 measures shadow agreement against.
        rec_id = None
        if rec is not None:
            cur = await conn.execute(
                "INSERT INTO recommendations (case_id, bundle_hash, outcome, confidence,"
                " rationale, evidence_refs, model, prompt_version)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (case_id, state["bundle_hash"], rec.outcome, rec.confidence, rec.rationale,
                 rec.evidence_refs, rec.model, rec.prompt_version),
            )
            rec_id = (await cur.fetchone())["id"]
            if crit is not None:
                await conn.execute(
                    "INSERT INTO critiques (recommendation_id, dissents, unsupported_claims,"
                    " notes, model, prompt_version) VALUES (%s, %s, %s, %s, %s, %s)",
                    (rec_id, crit.dissents, crit.unsupported_claims, crit.notes, crit.model,
                     crit.prompt_version),
                )
        # The gate's answer, the exact input it answered, and the rule that
        # bound it — for every case, so an escalation is as explainable as an
        # approval and shadow mode has something to count.
        cur = await conn.execute(
            "INSERT INTO gate_evaluations (case_id, recommendation_id, result, rule_id,"
            " policy_version, autonomy_mode, inputs_json) VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " RETURNING id",
            (case_id, rec_id, out.result, out.rule_id, out.policy_version, mode,
             Jsonb(gi.model_dump(mode="json"))),
        )
        gate_id = (await cur.fetchone())["id"]
        # The chain carries a hash of the exact input, so an edited
        # `gate_evaluations.inputs_json` no longer matches its audit event.
        await audit.append(conn, case_id, "gate_evaluated", {
            "gate_evaluation_id": gate_id, "result": out.result, "rule_id": out.rule_id,
            "policy_version": out.policy_version, "autonomy_mode": mode,
            "bundle_hash": state.get("bundle_hash"),
            "inputs_sha256": sha256_hex(gi.model_dump(mode="json")),
            "recommendation": rec and {"outcome": rec.outcome, "model": rec.model,
                                       "confidence": rec.confidence},
            "critique": crit and {"dissents": crit.dissents, "model": crit.model},
        }, "policy-gate")

        await conn.execute(
            "UPDATE cases SET status = 'SCORED', injection_flagged = %s, safety_flagged = %s,"
            " reason_mismatch = %s, updated_at = now() WHERE id = %s",
            (state["injection_flagged"], state["safety_flagged"], state["reason_mismatch"],
             case_id),
        )

        if status == "PENDING_REVIEW":
            # No decision row: nobody has decided anything yet. A human will.
            await conn.execute(
                "UPDATE cases SET status = 'PENDING_REVIEW',"
                " sla_due_at = COALESCE(sla_due_at, now() + interval '48 hours'),"
                " updated_at = now() WHERE id = %s",
                (case_id,),
            )
            await conn.commit()
            return None

        cur = await conn.execute(
            "INSERT INTO decisions (case_id, outcome, actor_type, actor_id, gate_evaluation_id)"
            " VALUES (%s, 'APPROVE', 'agent', 'policy-gate', %s) RETURNING id",
            (case_id, gate_id),
        )
        decision_id = str((await cur.fetchone())["id"])
        await audit.append(conn, case_id, "decided", {
            "decision_id": decision_id, "outcome": "APPROVE", "actor_type": "agent",
            "gate_evaluation_id": gate_id}, "policy-gate")
        await conn.execute(
            "UPDATE cases SET status = 'AUTO_APPROVED', updated_at = now() WHERE id = %s",
            (case_id,),
        )
        await conn.commit()
        return decision_id


async def on_message(msg: AbstractIncomingMessage) -> None:
    case_id = json.loads(msg.body)["case_id"]
    try:
        decision_id = await decide(case_id)
    except Exception as e:
        # Requeue. The quorum queue counts deliveries and dead-letters this
        # after x-delivery-limit, so there is no infinite redelivery loop.
        print(f"[worker] {case_id} failed: {type(e).__name__}: {e}", flush=True)
        await msg.nack(requeue=True)
        return

    if decision_id:
        await publish(
            _bus["channel"], "refund.approved", {"case_id": case_id, "decision_id": decision_id}
        )
    await msg.ack()


async def main() -> None:
    global _gw, _tools
    _gw = Gateway()
    # Reaches the three MCP servers, or mock-commerce directly when
    # TOOL_PROVIDER=direct. Fails here rather than mid-case if a server is down.
    _tools = await provider_from_env()
    # The configured half of the critic rule. The served half is checked per
    # case in review.critic, because a fallback can collapse the pair with no
    # config edit — this only catches someone editing models.yaml into it.
    if lineage(_gw.model_for("reasoner-primary")) == lineage(_gw.model_for("reasoner-critic")):
        print("[worker] WARNING: proposer and critic are configured from one lineage", flush=True)

    conn = await connect()
    channel = await conn.channel()
    await channel.set_qos(prefetch_count=PREFETCH)
    queues = await declare(channel)
    _bus["channel"] = channel
    print(f"[worker] consuming case-events, prefetch={PREFETCH}", flush=True)
    await queues["case-events"].consume(on_message)
    await asyncio.Future()  # run until killed


if __name__ == "__main__":
    asyncio.run(main())
