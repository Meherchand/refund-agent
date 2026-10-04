#!/usr/bin/env python3
"""The eval harness (L7): replay the labelled corpus through the real graph and gate.

    make replay            # runs whatever is left, then reports; safe to re-run
    make replay-report     # reports on what is recorded so far, runs nothing

**The corpus** is the first `REPLAY_PER_PATTERN` (25) seeded cases of each of the
eight patterns, by order_id — 200 cases, the same 200 every time.

**What is measured.** Each case runs through the whole graph in-process (as
`sample_patterns.py` does) and is then put to the live OPA gate **as if its
segment were at `auto`** with the switch off. Every segment really sits at
`shadow`, so this is the question promotion has to answer: *if we let it act
alone, what would it have done?* That answer is compared with the label:

- **false approve** — the gate would have paid out a case labelled ESCALATE.
  This is the CI gate: one is a failure.
- **false escalation** — a case labelled AUTO_APPROVE sent to a person. A cost,
  not a harm; reported.
- **shadow agreement** — the agent's own recommendation against the label, and
  against the decisions reviewers actually made (PLANNING.md §10.2).
- **latency** — wall clock per case and per node.

**An outage is not a measurement.** When the gateway fails, the graph escalates
by design, which would read as a correct escalation and flatter the numbers. So
a case whose evidence says the fleet failed (a model chain exhausted, a node over
its budget) is retried after a backoff, and if it still fails it is recorded as
*unmeasurable*: excluded from the rates, counted in the report, and retried on
the next run. Three unmeasurable cases in a row stop the run — resume later.

**Resumable.** Results are appended to `infra/local/replay/results.jsonl` one line
per case as each finishes; a re-run skips every case already measured. Delete the
file (or set `REPLAY_OUT`) to start over — for example after a prompt or Rego
change, which is exactly when a replay is worth running.

Exit codes: 0 = complete and no false approve; 1 = a false approve; 3 = not
complete yet (outage or interrupted) — run it again.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PG = os.environ.get("PG_PORT") or "5432"
os.environ["DATABASE_URL"] = f"postgresql://refund:refund@127.0.0.1:{PG}/refund"
os.environ.setdefault("MODELS_CONFIG", str(ROOT / "config" / "models.yaml"))
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

DSN = os.environ["DATABASE_URL"]
OUT = Path(os.environ.get("REPLAY_OUT") or ROOT / "infra" / "local" / "replay" / "results.jsonl")
PER_PATTERN = int(os.environ.get("REPLAY_PER_PATTERN", "25"))
# Pacing. The gateway is shared and rate-limited, and one case is ~10-20 model
# calls, so a handful at once is the ceiling; the worker's prefetch is 3 too.
CONCURRENCY = int(os.environ.get("REPLAY_CONCURRENCY", "2"))
ATTEMPTS = 3
BACKOFF_S = float(os.environ.get("REPLAY_BACKOFF_S", "60"))
STOP_AFTER = 3  # consecutive unmeasurable cases
PROMOTE_MIN_CASES, PROMOTE_MIN_AGREEMENT = 200, 0.95  # PLANNING.md §10.2


def corpus() -> list[dict]:
    with psycopg.connect(DSN) as c:
        cur = c.execute(
            "SELECT l.id, l.pattern, l.expected_outcome, l.order_id, l.customer_id,"
            " l.reason_code, l.description, l.amount, cu.tier FROM ("
            "  SELECT *, row_number() OVER (PARTITION BY pattern ORDER BY order_id) AS rn"
            "  FROM seed_labels) l JOIN commerce.customers cu ON cu.id = l.customer_id"
            " WHERE l.rn <= %s ORDER BY l.pattern, l.order_id", (PER_PATTERN,))
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def recorded(path: Path = OUT) -> dict[int, dict]:
    """Latest line per label id. A later line replaces an earlier one, which is
    how an unmeasurable case that succeeds on a re-run takes its place."""
    rows: dict[int, dict] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                rows[r["label_id"]] = r
    return rows


def todo(labels: list[dict], rows: dict[int, dict]) -> list[dict]:
    return [lb for lb in labels if lb["id"] not in rows or rows[lb["id"]]["unmeasurable"]]


def unmeasurable(state: dict) -> str | None:
    """Why this run says nothing about the agent, or None if it does."""
    for m in state["missing_evidence"]:
        if "exhausted its chain" in m or "exceeded its" in m:
            return m
    return None


def live_case(lb: dict) -> dict:
    """A real `cases` row, because `evidence_bundles` has a foreign key to one."""
    with psycopg.connect(DSN) as c:
        cid, created = c.execute(
            "INSERT INTO cases (order_id, customer_id, reason_code, description, amount,"
            " currency, source, status) VALUES (%s,%s,%s,%s,%s,'IDR','fixture','SCORED')"
            " RETURNING id, created_at",
            (lb["order_id"], lb["customer_id"], lb["reason_code"], lb["description"],
             lb["amount"])).fetchone()
        c.commit()
    return {"id": str(cid), "created_at": created, "order_id": lb["order_id"],
            "customer_id": lb["customer_id"], "reason_code": lb["reason_code"],
            "description": lb["description"], "amount": str(lb["amount"]), "currency": "IDR"}


async def replay_one(lb: dict, gw: Gateway, provider) -> dict:
    for attempt in range(1, ATTEMPTS + 1):
        t0 = time.time()
        state = await run(live_case(lb), gw, provider)
        wall = round(time.time() - t0, 1)
        why = unmeasurable(state)
        if why is None or attempt == ATTEMPTS:
            break
        print(f"  … {lb['pattern']} {lb['order_id']} unmeasurable ({why[:80]}),"
              f" retry {attempt} in {BACKOFF_S * attempt:.0f}s", flush=True)
        await asyncio.sleep(BACKOFF_S * attempt)
    out = await gate.evaluate(gate.gate_input(state, "auto", False))
    rec, crit = state.get("recommendation"), state.get("critique")
    return {
        "label_id": lb["id"], "pattern": lb["pattern"], "expected": lb["expected_outcome"],
        "order_id": lb["order_id"], "reason_code": lb["reason_code"], "tier": lb["tier"],
        "amount": str(lb["amount"]), "case_id": state["case"]["id"],
        "gate": out.result, "rule_id": out.rule_id, "policy_version": out.policy_version,
        "recommendation": rec and rec.outcome, "proposer": rec and rec.model,
        "prompt_version": rec and rec.prompt_version,
        "critic_dissents": crit and bool(crit.dissents or crit.unsupported_claims),
        "critic": crit and crit.model,
        "unmeasurable": why, "attempts": attempt, "wall_s": wall,
        "node_timings": state["node_timings"],
        "degraded_nodes": sorted(set(state["degraded_nodes"])),
        "at": datetime.now(UTC).isoformat(),
    }


async def replay() -> int:
    labels, rows = corpus(), recorded()
    left = todo(labels, rows)
    print(f"corpus {len(labels)} cases; {len(labels) - len(left)} already measured;"
          f" {len(left)} to run, {CONCURRENCY} at a time → {OUT}", flush=True)
    if not left:
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    provider = MCPToolProvider()
    await provider.load()
    gw = Gateway()
    sem, streak, stop = asyncio.Semaphore(CONCURRENCY), [0], asyncio.Event()

    async def worker(lb: dict) -> None:
        async with sem:
            if stop.is_set():
                return
            try:
                r = await replay_one(lb, gw, provider)
            except Exception as e:  # noqa: BLE001 — a crashed case is not recorded; the next run retries it
                print(f"  ! {lb['pattern']} {lb['order_id']} crashed: {type(e).__name__}: {e}",
                      flush=True)
                return
            with OUT.open("a") as f:
                f.write(json.dumps(r) + "\n")
            streak[0] = streak[0] + 1 if r["unmeasurable"] else 0
            if streak[0] >= STOP_AFTER:
                stop.set()
            print(f"  {r['pattern']:30} {r['order_id']} {r['expected']:12} gate={r['gate']:10}"
                  f" rule={r['rule_id']:24} {r['wall_s']:>6}s"
                  f"{'  UNMEASURABLE' if r['unmeasurable'] else ''}", flush=True)

    await asyncio.gather(*(worker(lb) for lb in left))
    await gw.aclose()
    if stop.is_set():
        print(f"stopped: {STOP_AFTER} unmeasurable cases in a row — the fleet looks down."
              " Run again later; measured cases are kept.", flush=True)
    return 0


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[round(q * (len(xs) - 1))] if xs else 0.0


def rate(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "—"


def human_agreement() -> tuple[int, int]:
    """(agreed, total) — the agent's recommendation against what a person decided."""
    with psycopg.connect(DSN) as c:
        return c.execute(
            "SELECT count(*) FILTER (WHERE (r.outcome = 'APPROVE') = (d.outcome = 'APPROVE')),"
            " count(*) FROM decisions d JOIN LATERAL (SELECT outcome FROM recommendations"
            " WHERE case_id = d.case_id ORDER BY created_at DESC LIMIT 1) r ON true"
            " WHERE d.actor_type = 'human'").fetchone()


def report(rows: list[dict], total: int, humans: tuple[int, int] | None = None) -> tuple[str, int]:
    """Markdown report and the false-approve count. Pure, so it can be tested."""
    measured = [r for r in rows if not r["unmeasurable"]]
    fa = [r for r in measured if r["gate"] == "ALLOW_AUTO" and r["expected"] == "ESCALATE"]
    fe = [r for r in measured if r["gate"] == "ESCALATE" and r["expected"] == "AUTO_APPROVE"]
    agree = [r for r in measured if (r["gate"] == "ALLOW_AUTO") == (r["expected"] == "AUTO_APPROVE")]
    shadow = [r for r in measured
              if (r["recommendation"] == "APPROVE") == (r["expected"] == "AUTO_APPROVE")]
    out = [f"# Replay report — {datetime.now(UTC):%Y-%m-%d %H:%M} UTC", "",
           (f"**{len(measured)} of {total}** corpus cases measured"
            f" ({len(rows) - len(measured)} unmeasurable — fleet failures, excluded below)."
            " The gate is evaluated as if every segment were at `auto`."), "",
           f"- **False approves: {len(fa)}**"
           + ("" if not fa else " — " + ", ".join(f"{r['pattern']} {r['order_id']}" for r in fa)),
           (f"- False escalations: {rate(len(fe), sum(r['expected'] == 'AUTO_APPROVE' for r in measured))}"
            " of the cases labelled AUTO_APPROVE"),
           f"- Gate agreement with the label: {rate(len(agree), len(measured))}",
           f"- Agent (shadow) agreement with the label: {rate(len(shadow), len(measured))}"]
    if humans and humans[1]:
        out.append(f"- Agent agreement with reviewers' actual decisions: {rate(*humans)}")
    out += ["", "| Pattern | Label | n | Gate allowed | Agent approved | Critic dissented (of approvals) | Top binding rule |",
            "|---|---|---|---|---|---|---|"]
    by = defaultdict(list)
    for r in measured:
        by[r["pattern"]].append(r)
    for p in sorted(by):
        rs = by[p]
        approved = [r for r in rs if r["recommendation"] == "APPROVE"]
        top = Counter(r["rule_id"] for r in rs).most_common(1)[0]
        out.append(f"| `{p}` | {rs[0]['expected']} | {len(rs)} |"
                   f" {rate(sum(r['gate'] == 'ALLOW_AUTO' for r in rs), len(rs))} |"
                   f" {rate(len(approved), len(rs))} |"
                   f" {rate(sum(bool(r['critic_dissents']) for r in approved), len(approved))} |"
                   f" `{top[0]}` {top[1]} |")
    walls = [r["wall_s"] for r in measured]
    nodes = defaultdict(list)
    for r in measured:
        for n, t in r["node_timings"].items():
            nodes[n].append(t)
    out += ["", "## Latency", "",
            (f"Per case: p50 **{pct(walls, .5):.0f}s**, p95 **{pct(walls, .95):.0f}s**,"
             f" max {max(walls, default=0):.0f}s."), "",
            "| Node | p50 s | p95 s | max s |", "|---|---|---|---|"]
    out += [f"| `{n}` | {pct(ts, .5):.1f} | {pct(ts, .95):.1f} | {max(ts):.1f} |"
            for n, ts in sorted(nodes.items(), key=lambda kv: -pct(kv[1], .95))]
    seg = defaultdict(list)
    for r in measured:
        seg[(r["reason_code"], r["tier"])].append(r)
    out += ["", "## Promotion readiness", "",
            (f"A segment earns `auto` on ≥{PROMOTE_MIN_CASES} measured cases at"
             f" ≥{PROMOTE_MIN_AGREEMENT:.0%} gate agreement and no false approve"
             " (PLANNING.md §10.2)."), "",
            "| Segment | n | Gate agreement | False approves | Verdict |", "|---|---|---|---|---|"]
    for (rc, tier), rs in sorted(seg.items()):
        ok = sum((r["gate"] == "ALLOW_AUTO") == (r["expected"] == "AUTO_APPROVE") for r in rs)
        bad = sum(r["gate"] == "ALLOW_AUTO" and r["expected"] == "ESCALATE" for r in rs)
        verdict = ("false approve — stays in shadow" if bad
                   else f"needs {PROMOTE_MIN_CASES - len(rs)} more cases" if len(rs) < PROMOTE_MIN_CASES
                   else "eligible" if ok / len(rs) >= PROMOTE_MIN_AGREEMENT else "agreement too low")
        out.append(f"| {rc} / {tier} | {len(rs)} | {rate(ok, len(rs))} | {bad} | {verdict} |")
    return "\n".join(out) + "\n", len(fa)


def main() -> int:
    if "--report" not in sys.argv:
        asyncio.run(replay())
    labels, rows = corpus(), recorded()
    ids = {lb["id"] for lb in labels}
    rows = [r for i, r in rows.items() if i in ids]
    md, false_approves = report(rows, len(labels), human_agreement())
    (OUT.parent / "report.md").write_text(md)
    print("\n" + md)
    if false_approves:
        return 1
    return 0 if len([r for r in rows if not r["unmeasurable"]]) == len(labels) else 3


if __name__ == "__main__":
    sys.exit(main())
