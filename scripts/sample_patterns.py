#!/usr/bin/env python3
"""Run the whole graph in-process on seeded cases and print what the agent concluded.

A measuring tool, not a verifier: it asserts nothing. It is how the L4 table in
PLANNING-LOCAL.md ("What L4 proves") was produced, and it is how to re-measure
after changing a prompt, a policy clause or a Rego rule. The last column is
what the live OPA gate says, evaluated as if the segment were at `auto`.

    uv run --env-file .env --with httpx --with 'psycopg[binary]' --with 'pydantic>=2.9' \\
      --with pyyaml --with mcp --with boto3 --with aio-pika \\
      scripts/sample_patterns.py clean_legitimate:6 wardrobing:3 empty_box:3

`pattern:N` takes the first N seeded cases of that pattern, in order_id order, so
repeat runs see the same cases. `BRIEF=1` prints one line per case. Each case is
inserted as a real `cases` row, because `evidence_bundles` has a foreign key.

Patterns: clean_legitimate, wardrobing, serial_returner, empty_box,
account_takeover, never_arrived_friendly_fraud, genuine_defect_batch,
reason_evidence_mismatch.
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PG = os.environ.get("PG_PORT") or "5432"
os.environ["DATABASE_URL"] = f"postgresql://refund:refund@127.0.0.1:{PG}/refund"
os.environ.setdefault("MODELS_CONFIG", os.path.join(ROOT, "config", "models.yaml"))
for d, p in (("COMMERCE", "9101"), ("EVIDENCE", "9102"), ("POLICY", "9103")):
    os.environ[f"MCP_{d}_URL"] = f"http://127.0.0.1:{os.environ.get(f'MCP_{d}_PORT', p)}/mcp"
MINIO = os.environ.get("MINIO_PORT") or "9000"
os.environ.setdefault("S3_ENDPOINT_URL", f"http://127.0.0.1:{MINIO}")
os.environ.setdefault("AWS_ACCESS_KEY_ID", os.environ.get("MINIO_ROOT_USER", ""))
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", os.environ.get("MINIO_ROOT_PASSWORD", ""))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("OPA_URL", f"http://127.0.0.1:{os.environ.get('OPA_PORT') or 8181}")

import psycopg  # noqa: E402

from packages.llm import Gateway  # noqa: E402
from services.case_worker import gate  # noqa: E402
from services.case_worker.graph import run  # noqa: E402
from services.case_worker.tools import MCPToolProvider  # noqa: E402


def seeded(pattern: str, offset: int = 0) -> dict:
    with psycopg.connect(os.environ["DATABASE_URL"]) as c:
        r = c.execute(
            "SELECT order_id, customer_id, reason_code, description, amount FROM seed_labels"
            " WHERE pattern = %s ORDER BY order_id LIMIT 1 OFFSET %s", (pattern, offset)).fetchone()
        cid, created = c.execute(
            "INSERT INTO cases (order_id, customer_id, reason_code, description, amount,"
            " currency, source, status) VALUES (%s,%s,%s,%s,%s,'IDR','fixture','SCORED')"
            " RETURNING id, created_at", r).fetchone()
        c.commit()
    return {"id": str(cid), "created_at": created, "order_id": r[0], "customer_id": r[1],
            "reason_code": r[2], "description": r[3], "amount": str(r[4]), "currency": "IDR"}


async def main() -> None:
    provider = MCPToolProvider()
    await provider.load()
    gw = Gateway()
    brief = os.environ.get("BRIEF")
    for arg in sys.argv[1:]:
        pattern, n = (arg.split(":") + ["1"])[:2]
        for i in range(int(n)):
            s = await run(seeded(pattern, i), gw, provider)
            rec, crit = s["recommendation"], s["critique"]
            verdict = ("dissent" if crit.dissents else "agree") if crit else "-"
            # As if the segment were at `auto`: the rule that binds is the
            # substantive one, not the ladder every segment starts on.
            out = await gate.evaluate(gate.gate_input(s, "auto", False))
            print(f"{pattern:30} #{i} rec={str(rec.outcome if rec else None):8} "
                  f"critic={verdict:8} -> {out.result} ({out.rule_id})", flush=True)
            if brief:
                continue
            if rec and s["refs_outside_bundle"]:
                print("   cited outside the bundle:", s["refs_outside_bundle"])
            if rec:
                print(f"   rec {rec.confidence} by {rec.model}: {rec.rationale}")
            if crit:
                print(f"   crit by {crit.model}: {crit.unsupported_claims} | {crit.notes}")
    await gw.aclose()


if __name__ == "__main__":
    asyncio.run(main())
