"""The policy gate client (L5). The rules themselves are `policy/rego/refund.rego`.

This module decides nothing. It gathers the signals the graph produced into a
`GateInput`, looks up the segment's rung on the autonomy ladder and the kill
switch, asks OPA, and hands back what OPA said. Every routing rule — including
the ones `_route()` held at L2-L4, under the same rule ids — now lives in Rego,
where it can be tested with `opa test` and changed without a deploy.

If OPA cannot be reached or answers with something that is not a `GateOutput`,
the case escalates under `gate_unavailable`. There is no local fallback policy:
a second copy of the rules is a second authority.
"""

from __future__ import annotations

import os

import httpx

from packages.contracts import GateInput, GateOutput

from .tools import _ROUTES

OPA_URL = os.environ.get("OPA_URL", "http://127.0.0.1:8181")
_DECISION = "/v1/data/refund/decision"


def facts(state: dict) -> dict:
    """Commerce tool results from the frozen bundle, keyed by tool name.

    The bundle, not the working list: the gate reads what the proposer read.
    Inferences and classifier signals are left out — they already reach the gate
    as their own booleans.
    """
    bundle = state.get("bundle")
    return {f.source: f.data["result"] for f in (bundle.fragments if bundle else [])
            if f.source in _ROUTES and "derived_from" not in f.data}


def gate_input(state: dict, autonomy_mode: str, kill_switch_active: bool) -> GateInput:
    rec, crit = state.get("recommendation"), state.get("critique")
    case = state.get("case") or {}
    found = facts(state)
    return GateInput(
        recommendation=rec,
        critic_dissents=bool(crit and crit.dissents),
        unsupported_claims=crit.unsupported_claims if crit else [],
        evidence_completeness=state["completeness"],
        degraded_nodes=sorted(set(state.get("degraded_nodes") or [])),
        amount=case.get("amount", "0"),
        reason_code=case.get("reason_code", ""),
        customer_tier=tier(found),
        autonomy_mode=autonomy_mode,
        kill_switch_active=kill_switch_active,
        injection_flagged=state["injection_flagged"],
        safety_flagged=state["safety_flagged"],
        reason_mismatch=state["reason_mismatch"],
        bundle_truncated=bool(state.get("bundle_truncated")),
        citations_verified=state["citations_verified"],
        bundle_frozen=bool(state.get("bundle_hash")),
        refs_outside_bundle=state.get("refs_outside_bundle") or [],
        critic_reviewed=crit is not None,
        critic_independent=bool(state.get("critic_independent")),
        facts=found,
    )


def tier(facts: dict) -> str:
    """From `get_customer`. Unknown is a tier no segment is promoted on."""
    return (facts.get("get_customer") or {}).get("tier") or "unknown"


async def ladder(conn, reason_code: str, amount: str, customer_tier: str) -> tuple[str, bool]:
    """(autonomy_mode, kill_switch_active) for this case's segment.

    A segment with no `autonomy_config` row is in `shadow` — PLANNING.md §10.2
    ships there on day one, and a rung is only ever earned by a promotion row.
    The kill switch is read on every evaluation: `global`, or the segment key
    `reason_code/amount_band/customer_tier`.
    """
    cur = await conn.execute(
        "SELECT mode, amount_band FROM autonomy_config WHERE reason_code = %s"
        " AND customer_tier = %s AND %s::numeric >= split_part(amount_band, '-', 1)::numeric"
        " AND %s::numeric < split_part(amount_band, '-', 2)::numeric LIMIT 1",
        (reason_code, customer_tier, amount, amount),
    )
    row = await cur.fetchone()
    mode, band = (row["mode"], row["amount_band"]) if row else ("shadow", None)
    cur = await conn.execute(
        "SELECT coalesce(bool_or(active), false) AS on FROM kill_switch WHERE scope IN ('global', %s)",
        (f"{reason_code}/{band}/{customer_tier}",),
    )
    return mode, (await cur.fetchone())["on"]


async def evaluate(gi: GateInput) -> GateOutput:
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.post(f"{OPA_URL}{_DECISION}", json={"input": gi.model_dump(mode="json")})
            r.raise_for_status()
            return GateOutput.model_validate(r.json()["result"])
    except Exception as e:  # noqa: BLE001 — any failure to get an answer escalates
        print(f"[gate] unavailable: {type(e).__name__}: {e}", flush=True)
        return GateOutput(result="ESCALATE", rule_id="gate_unavailable", policy_version="none")
