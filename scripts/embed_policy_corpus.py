#!/usr/bin/env python3
"""Chunk the refund policy and embed it into `policy_clauses` (L2.6).

Chunks on `###` headings, one clause per chunk, id = the clause number. Not fixed
token windows: a decision cites a clause, and a chunk spanning two clauses cannot
be cited cleanly — `evidence_refs` are meant to point at something a reviewer can
open and read.

Re-runnable. Upserts every clause in the file and deletes anything left in the
table for this policy version that the file no longer contains, so removing a
clause from the document removes it from the corpus. That is also what clears the
ten inline clauses `schema.sql` carried before this script existed.

Runs on the host, like `seed_data.py` — it needs the gateway, and the gateway
needs a key that no container but the worker and mcp-policy has any business
holding.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from packages.llm import Gateway  # noqa: E402

DOC = Path(os.environ.get("POLICY_DOC", "policy/refund-policy-v1.md"))
DSN = os.environ.get(
    "DATABASE_URL",
    f"postgresql://refund:refund@127.0.0.1:{os.environ.get('PG_PORT') or 5432}/refund",
)
BATCH = 32  # the gateway takes a list; this keeps one request well under any body limit
_HEADING = re.compile(r"^(#{2,3})\s+([\d.]+)\s+(.+)$")


def chunks(markdown: str) -> list[tuple[str, str]]:
    """[(clause id, text)] — the `##` group heading is prefixed onto each clause.

    The group heading carries real retrieval signal ("Problems with delivery"
    above "Goods that never arrived") and costs a dozen tokens per chunk.
    """
    group, current, out = "", None, []
    for line in markdown.splitlines():
        if m := _HEADING.match(line):
            level, number, title = m.group(1), m.group(2), m.group(3).strip()
            if level == "##":
                group, current = title, None
            else:
                current = (number, f"{group} — {title}:", [])
                out.append(current)
        elif current is not None and line.strip() and not line.startswith("#"):
            current[2].append(line.strip())
    return [(n, f"{head} {' '.join(body)}") for n, head, body in out if body]


def version() -> str:
    """From the filename, so the document and the corpus cannot disagree."""
    m = re.search(r"-(v\d+)\.md$", DOC.name)
    if not m:
        raise SystemExit(f"{DOC.name}: expected a name ending -v<N>.md")
    return m.group(1)


async def main() -> int:
    if not DOC.exists():
        raise SystemExit(f"{DOC} not found")
    clauses = chunks(DOC.read_text())
    if not clauses:
        raise SystemExit(f"{DOC}: no ### clauses found")
    ver = version()
    words = sum(len(t.split()) for _, t in clauses)
    print(f"{DOC}: {len(clauses)} clauses, {words} words, version {ver}")

    gw = Gateway()
    vectors: list[list[float]] = []
    try:
        for i in range(0, len(clauses), BATCH):
            batch = clauses[i : i + BATCH]
            vectors += await gw.embed("embed", [t for _, t in batch])
            print(f"  embedded {len(vectors)}/{len(clauses)}")
    finally:
        await gw.aclose()

    want = gw.aliases["embed"]["dim"]
    got = len(vectors[0])
    if got != want:
        # The column is vector(1024). A model change that silently altered the
        # width would otherwise fail one row at a time, deep inside the insert.
        raise SystemExit(f"embedding width {got}, expected {want} — schema mismatch")

    with psycopg.connect(DSN) as conn:
        for (cid, text), vec in zip(clauses, vectors, strict=True):
            conn.execute(
                "INSERT INTO policy_clauses (id, text, policy_version, embedding)"
                " VALUES (%s, %s, %s, %s::vector)"
                " ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text,"
                " policy_version = EXCLUDED.policy_version, embedding = EXCLUDED.embedding",
                (cid, text, ver, str(vec)),
            )
        stale = conn.execute(
            "DELETE FROM policy_clauses WHERE policy_version = %s AND id <> ALL(%s)"
            " RETURNING id",
            (ver, [c for c, _ in clauses]),
        ).fetchall()
        conn.commit()
    if stale:
        print(f"  removed {len(stale)} clause(s) no longer in the document: "
              f"{', '.join(r[0] for r in stale)}")
    print(f"{len(clauses)} clauses embedded at {got} dimensions.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
