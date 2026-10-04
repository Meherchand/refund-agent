"""Moves the money. The only service with write access to `refund_attempts`
and `ledger_entries` — see PLANNING-LOCAL.md §3.3 for why it is separate.

Idempotency is a UNIQUE constraint, not a check-then-act: the key is
sha256(case_id + decision_id) and the INSERT is ON CONFLICT DO NOTHING. A
duplicate delivery loses the race in the database rather than in application
logic, so two concurrent consumers cannot both pay out.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

import psycopg
from aio_pika.abc import AbstractIncomingMessage
from psycopg.rows import dict_row

from packages import audit
from packages.bus import connect, declare

DSN = os.environ["DATABASE_URL"]


async def execute_refund(case_id: str, decision_id: str) -> str:
    key = hashlib.sha256(f"{case_id}{decision_id}".encode()).hexdigest()
    async with await psycopg.AsyncConnection.connect(DSN, row_factory=dict_row) as conn:
        cur = await conn.execute(
            "INSERT INTO refund_attempts (decision_id, idempotency_key, psp_ref, status)"
            " VALUES (%s, %s, %s, 'succeeded') ON CONFLICT (idempotency_key) DO NOTHING"
            " RETURNING id",
            (decision_id, key, f"psp_{key[:12]}"),
        )
        attempt = await cur.fetchone()
        if attempt is None:
            await conn.rollback()
            return "duplicate"

        cur = await conn.execute("SELECT amount FROM cases WHERE id = %s", (case_id,))
        amount = (await cur.fetchone())["amount"]
        await conn.execute(
            "INSERT INTO ledger_entries (case_id, account, direction, amount, psp_ref)"
            " VALUES (%s,'merchant_revenue','debit',%s,%s), (%s,'customer_payable','credit',%s,%s)",
            (case_id, amount, f"psp_{key[:12]}", case_id, amount, f"psp_{key[:12]}"),
        )
        await conn.execute(
            "UPDATE cases SET status = 'REFUNDED', updated_at = now() WHERE id = %s", (case_id,)
        )
        await audit.append(conn, case_id, "refund_executed", {
            "decision_id": decision_id, "refund_attempt_id": attempt["id"],
            "psp_ref": f"psp_{key[:12]}", "amount": amount}, "executor")
        await conn.commit()
        return "refunded"


async def on_message(msg: AbstractIncomingMessage) -> None:
    body = json.loads(msg.body)
    try:
        result = await execute_refund(body["case_id"], body["decision_id"])
        print(f"[executor] {body['case_id']} {result}", flush=True)
    except Exception as e:
        print(f"[executor] {body['case_id']} failed: {type(e).__name__}: {e}", flush=True)
        await msg.nack(requeue=True)
        return
    await msg.ack()


async def main() -> None:
    conn = await connect()
    channel = await conn.channel()
    await channel.set_qos(prefetch_count=3)
    queues = await declare(channel)
    print("[executor] consuming refund-execute", flush=True)
    await queues["refund-execute"].consume(on_message)
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
