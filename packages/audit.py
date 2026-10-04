"""The hash-chained audit log (L6). One function to append, one to verify.

Each row's hash covers the previous row's hash, so editing any row — or removing
one from the middle — breaks every hash after it, and `verify()` names the first
row that no longer checks out. What a chain cannot show on its own is the *tail*
being cut off: delete the newest rows and what remains is still a valid chain.
`verify()` returns the head hash and row count for that reason — compare them
with a copy taken earlier (`make verify-chain` prints both).

Appends happen inside the caller's transaction, next to the change they record,
so an event is committed exactly when its state change is. A transaction-scoped
advisory lock serialises appenders across services: without it two writers read
the same previous hash and the chain forks.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from packages.contracts.canonical import canonical_json, sha256_hex

GENESIS = "0" * 64
_LOCK = 0x6175646974  # "audit" — any constant every appender agrees on


def row_hash(prev_hash: str, seq: int, case_id: Any, event_type: str, actor: str,
             at: datetime, payload: dict) -> str:
    # PLANNING.md §8 hashes prev, seq, case, type and payload. `actor` and `at`
    # are covered too: otherwise who did it, and when, could be edited freely.
    return sha256_hex({"prev_hash": prev_hash, "seq": seq, "case_id": case_id,
                       "event_type": event_type, "actor": actor, "at": at,
                       "payload": payload})


async def append(conn, case_id: Any, event_type: str, payload: dict, actor: str) -> str:
    """Add one event to the chain inside `conn`'s open transaction. Returns its hash."""
    cur = conn.cursor(row_factory=tuple_row)
    await cur.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK,))
    await cur.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1")
    last = await cur.fetchone()
    await cur.execute("SELECT nextval(pg_get_serial_sequence('audit_log', 'seq'))")
    seq = (await cur.fetchone())[0]
    # Normalised through canonical JSON first, so what JSONB stores and hands
    # back is exactly what was hashed (Decimals and UUIDs become strings).
    payload = json.loads(canonical_json(payload))
    at = datetime.now(UTC)
    prev = last[0] if last else GENESIS
    h = row_hash(prev, seq, case_id, event_type, actor, at, payload)
    await cur.execute(
        "INSERT INTO audit_log (seq, case_id, event_type, payload_json, prev_hash, hash,"
        " actor, at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (seq, case_id, event_type, Jsonb(payload), prev, h, actor, at),
    )
    return h


def verify(rows) -> tuple[int, str, int | None]:
    """Walk rows in `seq` order. Returns (rows checked, head hash, first bad seq or None).

    A row is bad if its stored hash is not the hash of its contents, or if it
    does not point at the row before it. Gaps in `seq` are fine — a rolled-back
    append consumes a sequence value — so linkage is by hash, not by number.
    """
    prev, n = GENESIS, 0
    for seq, case_id, event_type, payload, prev_hash, h, actor, at in rows:
        if prev_hash != prev or h != row_hash(prev_hash, seq, case_id, event_type, actor, at,
                                               payload):
            return n, prev, seq
        prev, n = h, n + 1
    return n, prev, None


CHAIN_SQL = ("SELECT seq, case_id, event_type, payload_json, prev_hash, hash, actor, at"
             " FROM audit_log ORDER BY seq")
