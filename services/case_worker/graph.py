"""The runner: stages in sequence, nodes within a stage at the same time.

Nodes have LangGraph's contract — take the state, return only the keys you
changed — and this file supplies the other half: reducing those partial updates
into one state, appending to list-valued keys the way `Annotated[list,
operator.add]` channels do. Nodes are written against that assumption, and each
returns only its own additions.

**Why this and not a `StateGraph`.** L2 said L3 would swap one in. Building L3
made the case weaker rather than stronger: fanning three independent nodes out
and joining them is `asyncio.gather`, and the graph here has no cycles, no
conditional edges and no resumable checkpoint to persist. What LangGraph would
add today is a dependency, a second vocabulary for the same thing, and a
framework between the code and the concurrency it is trying to demonstrate.
What would earn it: interrupting for a human mid-graph and resuming from a
checkpoint (L6's review queue), or a topology branchy enough that reading the
edges beats reading the calls. Recorded in §9 as a deferral, not a rejection —
the node signature is unchanged, so it stays a change to this file alone.

**Every node has a wall-clock budget**, which is the fix for the L2.6 incident:
a model needing 140 s to cold-start stalled a node past its own timeout, nothing
was logged until the node returned, and three such cases filled `prefetch` and
stopped the worker consuming altogether. A node that overruns is now degraded
and the case escalates, which is what the rest of the design already promises.
The budget is per node and generous — it bounds a *stall*, not a slow call, and
the collector gets its own because several model calls in a loop is its job.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from packages.llm import Gateway

from . import nodes, review
from .bundle import freeze_bundle
from .tools import ToolProvider

LIST_KEYS = {"fragments", "degraded_nodes", "missing_evidence", "unverified_citations"}

NODE_BUDGET_S = float(os.environ.get("NODE_BUDGET_S", "240"))
# The collector is a loop of model calls by design (6 calls over 2 turns warm),
# so one ceiling for both would either strangle it or fail to bound anything else.
# The proposer is one call, but the largest model in the fleet with a reprompt
# behind it.
BUDGETS = {
    "evidence_collector": float(os.environ.get("COLLECTOR_BUDGET_S", "600")),
    "decision_proposer": float(os.environ.get("PROPOSER_BUDGET_S", "420")),
}

Node = Callable[[dict], Awaitable[dict]]


def new_state(case: dict) -> dict[str, Any]:
    return {
        "case": case,
        "fragments": [],
        "degraded_nodes": [],
        "missing_evidence": [],
        "unverified_citations": [],
        "specialists": [],
        "completeness": "incomplete",
        "injection_flagged": False,
        "safety_flagged": False,
        "reason_mismatch": False,
        "citations_verified": False,
        "bundle": None,
        "bundle_hash": None,
        "bundle_truncated": False,
        "recommendation": None,
        "refs_outside_bundle": [],
        "critique": None,
        "critic_independent": False,
        "node_timings": {},
        # (start, end) seconds from the start of the graph. Kept because
        # "these ran in parallel" is a claim that should be measurable rather
        # than asserted — verify_l3.py reads it, and it is what a trace view
        # would render.
        "node_spans": {},
    }


def _merge(state: dict, update: dict) -> None:
    for k, v in update.items():
        if k in LIST_KEYS:
            state[k].extend(v)
        else:
            state[k] = v


async def _timed(state: dict, name: str, fn: Node, origin: float) -> tuple[str, dict, float, float]:
    budget = BUDGETS.get(name, NODE_BUDGET_S)
    start = time.time()
    try:
        update = await asyncio.wait_for(fn(state), budget)
    except TimeoutError:
        # Deliberately not a retry: whatever is slow is still slow, and the
        # queue is not a suitable place to discover that.
        update = {
            "degraded_nodes": [name],
            "missing_evidence": [f"{name}: exceeded its {budget:.0f}s budget"],
        }
    return name, update, round(start - origin, 3), round(time.time() - origin, 3)


async def _stage(state: dict, origin: float, steps: list[tuple[str, Node]]) -> None:
    """Run these nodes concurrently and reduce their updates in list order.

    Order matters for reproducibility, not for correctness: nodes in a stage only
    read the state, so the result is the same whichever finishes first — but the
    bundle hash would not be, and a hash that depends on a race is not a hash.
    """
    done = await asyncio.gather(*(_timed(state, n, f, origin) for n, f in steps))
    for name, update, start, end in done:
        _merge(state, update)
        state["node_timings"][name] = round(end - start, 2)
        state["node_spans"][name] = [start, end]


async def run(case: dict, gw: Gateway, tools: ToolProvider) -> dict:
    """Run the graph and return the accumulated state."""
    state = new_state(case)
    origin = time.time()

    # Guards first, and together: they are independent of each other, and the
    # collector is the node that holds tools, so what its prompt will contain is
    # worth knowing before it runs.
    await _stage(state, origin, [
        ("injection_guard", nodes.injection_guard),
        ("safety_guard", lambda s: nodes.safety_guard(s, gw)),
        ("reason_classifier", lambda s: nodes.reason_classifier(s, gw)),
        ("route_specialists", nodes.route_specialists),
    ])

    await _stage(state, origin, [
        ("evidence_collector", lambda s: nodes.evidence_collector(s, gw, tools)),
    ])

    # The fan-out. Each reaches a different domain — vision over the case's own
    # attachments, a reading of numbers already collected, and policy retrieval —
    # so none waits on another, and the stage costs the slowest rather than the sum.
    specialists: dict[str, Node] = {
        "image_analyst": lambda s: nodes.image_analyst(s, gw, tools),
        "behavior_analyst": lambda s: nodes.behavior_analyst(s, gw),
        "policy_retriever": lambda s: nodes.policy_retriever(s, tools),
    }
    await _stage(state, origin, [(n, specialists[n]) for n in state["specialists"]])

    await _stage(state, origin, [("completeness_check", nodes.completeness_check)])
    # Audits what the others produced, so it runs after all of them...
    await _stage(state, origin, [("citation_check", lambda s: nodes.citation_check(s, tools))])
    # ...and the freeze follows, because nothing may be added after it.
    await _stage(state, origin, [("freeze_bundle", freeze_bundle)])

    # From here nothing reads the working state's evidence — only the bundle.
    # Sequential by necessity: the critic reviews what the proposer wrote.
    await _stage(state, origin, [("decision_proposer", lambda s: review.decision_proposer(s, gw))])
    await _stage(state, origin, [("critic", lambda s: review.critic(s, gw))])
    return state
