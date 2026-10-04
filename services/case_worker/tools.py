"""Two tool providers behind one interface — PLANNING-LOCAL.md §4A.

`DirectToolProvider` calls mock-commerce over plain HTTP; it was L2's, and it
survives as a debugging path. `MCPToolProvider` speaks JSON-RPC to the three MCP
servers and is the default from L2.5. `TOOL_PROVIDER` picks one.

`schemas_for(node)` is what enforces privilege: the collector is the only node
handed a schema, and under MCP it is also the only node for which a transport
exists, so the proposer and the critic cannot reach a tool even if an injection
talks them into trying.

The difference that matters is not the transport. It is that the MCP servers
write their own access log, which makes `citations_verified` a deterministic
check instead of a model's self-report — so only the MCP path can produce
evidence an auto-approval is allowed to rest on. `enforces_access_log` is how a
provider admits which of the two it is.

Everything here is a GET. There is no write tool in this process by
construction — moving money lives in `executor`, behind different credentials.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any, Protocol

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

COMMERCE = os.environ.get("COMMERCE_URL", "http://mock-commerce:8010")

# OpenAI tool schemas. P1 confirmed branch A — Qwen3-14B emits real `tool_calls`
# against these and consumes the results (infra/local/capability-report.json).
SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "Order header and line items: total, status, placed_at, SKUs.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_shipments",
            "description": (
                "Carrier events for an order: delivery confirmation, signature capture, "
                "shipped vs returned weight. The weight pair is how an empty-box claim shows up."
            ),
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_customer",
            "description": (
                "Customer record: tier, tenure, and when the address and payment method "
                "were last changed. Recent changes to both are an account-takeover signal."
            ),
            "parameters": {
                "type": "object",
                "properties": {"customer_id": {"type": "string"}},
                "required": ["customer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_returns_history",
            "description": "This customer's prior returns, with reason codes and dates.",
            "parameters": {
                "type": "object",
                "properties": {"customer_id": {"type": "string"}},
                "required": ["customer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_cohort_stats",
            "description": (
                "This customer's return rate against its tier cohort. Read "
                "cohort_percentile (0-1), not the z-score: serial returners are inside "
                "their own cohort and inflate the deviation they are measured against."
            ),
            "parameters": {
                "type": "object",
                "properties": {"customer_id": {"type": "string"}},
                "required": ["customer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_payment_status",
            "description": "Payment state for an order: captured, method, chargeback status.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_defect_reports",
            "description": (
                "Other customers' defect reports for a SKU. A cluster corroborates a "
                "defect claim that would otherwise rest on the customer's word alone."
            ),
            "parameters": {
                "type": "object",
                "properties": {"sku": {"type": "string"}},
                "required": ["sku"],
            },
        },
    },
]

# tool name -> (url template, evidence kind)
_ROUTES: dict[str, tuple[str, str]] = {
    "get_order": ("/orders/{order_id}", "order"),
    "get_shipments": ("/orders/{order_id}/shipments", "shipment"),
    "get_customer": ("/customers/{customer_id}", "behavior_features"),
    "get_returns_history": ("/customers/{customer_id}/returns", "returns_history"),
    "get_cohort_stats": ("/customers/{customer_id}/cohort-stats", "cohort_stats"),
    "get_payment_status": ("/payments/{order_id}", "payment_status"),
    "get_defect_reports": ("/skus/{sku}/defect-reports", "order"),
}


class ToolProvider(Protocol):
    enforces_access_log: bool

    def schemas_for(self, node: str) -> list[dict]: ...
    async def call(
        self, name: str, args: dict, case_id: str | None = None
    ) -> tuple[Any, str | None]: ...


class DirectToolProvider:
    """HTTP against mock-commerce. L2's provider, kept as a debugging path.

    No access log: nothing but the agent itself witnesses these calls, so a
    bundle collected this way cannot be citation-checked and the gate will not
    auto-approve it. That is the intended consequence, not a shortcoming —
    see `citation_check` in nodes.py.
    """

    enforces_access_log = False

    def __init__(self, base: str = COMMERCE, timeout: float = 10.0) -> None:
        self.base = base.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    def schemas_for(self, node: str) -> list[dict]:
        return SCHEMAS if node == "evidence_collector" else []

    async def call(self, name: str, args: dict, case_id: str | None = None) -> tuple[Any, str]:
        """Returns (payload, evidence_kind). Raises on anything unexpected —
        the collector catches and records the failure as a hole."""
        if name not in _ROUTES:
            raise KeyError(f"no such tool: {name}")
        path, kind = _ROUTES[name]
        r = await self._client.get(f"{self.base}{path.format(**args)}")
        r.raise_for_status()
        return r.json(), kind


# Which domain a node is allowed to talk to. `decision_proposer` and `critic`
# are absent on purpose: there is no entry to look up, so no URL, so no session.
NODE_DOMAIN: dict[str, str] = {
    "evidence_collector": "commerce",
    "image_analyst": "evidence",      # L4
    "policy_retriever": "policy",     # L4
}

# Evidence kind per tool. The commerce mapping is the same one the direct
# provider uses; the evidence tools are deliberately absent, and `call` returns
# None for them — what an attachment becomes is `image_analyst`'s decision, and
# raw bytes are not a fragment kind. Anything that tried to build a fragment
# straight from that would fail validation at the point of the mistake, which is
# the right place for it.
_MCP_KINDS: dict[str, str] = {name: kind for name, (_, kind) in _ROUTES.items()} | {
    "search_policy": "policy_clause",
}


class MCPToolProvider:
    """The three MCP servers over streamable HTTP.

    A session per call, which the stateless servers are built for: the JSON-RPC
    hop is ~10 ms against a tool that takes ~5 ms and a model call that takes
    seconds, and it keeps this class free of long-lived state that would have to
    be torn down correctly on every failure path.

    The case id travels as a header, never as a tool argument — the server
    writes it to `mcp_access_log`, and an audit key the model can choose is not
    an audit key.
    """

    enforces_access_log = True

    def __init__(self) -> None:
        self.urls = {
            d: os.environ.get(f"MCP_{d.upper()}_URL") for d in ("commerce", "evidence", "policy")
        }
        self._schemas: dict[str, list[dict]] = {}
        self._domain_of: dict[str, str] = {}

    @asynccontextmanager
    async def _session(self, url: str, case_id: str | None = None):
        # `create_mcp_http_client` is the SDK's own factory, which is how the
        # session gets a header at all: the transport takes a prepared client.
        headers = {"X-Case-Id": case_id} if case_id else {}
        async with (
            create_mcp_http_client(headers=headers) as http,
            streamable_http_client(url, http_client=http) as (r, w),
            ClientSession(r, w) as s,
        ):
            await s.initialize()
            yield s

    async def load(self) -> dict[str, int]:
        """Ask each server what it exposes and translate to OpenAI schemas.

        Done once at startup rather than per case: it doubles as the readiness
        check, and a server that cannot be reached is better found here than
        halfway through a case.
        """
        counts = {}
        for domain, url in self.urls.items():
            if not url:
                continue
            async with self._session(url) as s:
                tools = (await s.list_tools()).tools
            self._schemas[domain] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": (t.description or "").strip(),
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
            self._domain_of |= {t.name: domain for t in tools}
            counts[domain] = len(tools)
        return counts

    def url_for(self, node: str) -> str | None:
        return self.urls.get(NODE_DOMAIN.get(node, ""))

    def schemas_for(self, node: str) -> list[dict]:
        return self._schemas.get(NODE_DOMAIN.get(node, ""), [])

    async def call(
        self, name: str, args: dict, case_id: str | None = None
    ) -> tuple[Any, str | None]:
        domain = self._domain_of.get(name)
        if domain is None:
            raise KeyError(f"no such tool: {name}")
        async with self._session(self.urls[domain], case_id) as s:
            res = await s.call_tool(name, args)
        if res.is_error:
            # The server returns tool failures as results, not exceptions. Raise
            # so the collector treats it the way it treats any failed tool.
            raise RuntimeError(f"{name}: {res.content[0].text if res.content else 'failed'}")
        if res.structured_content is None:
            return json.loads(res.content[0].text), _MCP_KINDS.get(name)
        # A tool returning a list arrives as one text block per element, so the
        # unstructured content cannot be read as the payload — but the same
        # result is in `structured_content`, wrapped under "result" for any
        # return type that is not itself an object. Unwrap it and the payload is
        # byte-identical to what the HTTP path returned.
        payload = res.structured_content
        return (payload["result"] if set(payload) == {"result"} else payload), _MCP_KINDS.get(name)

    async def aclose(self) -> None:
        """Nothing to close — sessions do not outlive a call."""


async def provider_from_env() -> ToolProvider:
    """`TOOL_PROVIDER=mcp` (default) | `direct`."""
    if os.environ.get("TOOL_PROVIDER", "mcp") == "direct":
        return DirectToolProvider()
    provider = MCPToolProvider()
    print(f"[tools] mcp servers: {await provider.load()}", flush=True)
    return provider
