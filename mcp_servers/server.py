"""One image, three MCP servers — PLANNING-LOCAL.md §4A.

`MCP_DOMAIN` decides which tools this process registers and, more to the point,
which credentials it holds. The point is not interop: it is that
`decision_proposer` cannot reach a commerce tool because nothing in its process
holds a session to one, and that a path-traversal bug in `fetch_attachment`
sits next to no commerce credential at all.

Named `mcp_servers/` and not `mcp/` as §7 planned: a top-level `mcp` package
shadows the MCP SDK of the same name on `sys.path`, and every import here would
resolve to the wrong thing.

Two invariants this file exists to hold:

**Nothing is served that is not logged.** Every tool writes `mcp_access_log`
before it returns, through a role that can INSERT and nothing else — so the
server cannot rewrite its own audit trail. If the log write fails, the tool
fails: an unlogged call is an unauditable one, and the collector turns a failure
into a hole rather than into evidence nobody can trace.

**The case id comes from the transport, not the model.** It arrives as the
`X-Case-Id` header the client sets on the session. As a tool argument it would
put the audit key in the hands of the thing being audited.

One habit of the SDK worth knowing, because it shapes every tool below: a tool
that raises `ToolError` sends its message to the caller, and a tool that raises
anything else has the detail masked. So a *deliberate* refusal — a key outside
the case prefix, an upstream 404 — says so, and an unexpected crash tells the
model nothing it could act on. Every return type is also annotated exactly,
because that is what makes the payload arrive as one structured object instead
of one text block per list element.
"""

from __future__ import annotations

import base64
import os
import uuid

import httpx
import psycopg
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from psycopg.types.json import Jsonb

DOMAIN = os.environ["MCP_DOMAIN"]
PORT = int(os.environ.get("MCP_PORT", "9101"))
# INSERT-only on mcp_access_log. Every domain holds this and nothing more of the
# refund database — see infra/local/mcp-roles.sql.
LOG_DSN = os.environ["MCP_LOG_DSN"]

server = MCPServer(f"mcp-{DOMAIN}")


def _case_id(ctx: Context) -> str | None:
    """The case this session belongs to, or None when the caller is a probe.

    A correlation key, not an identity assertion — the SDK is right to warn that
    a header is client-supplied. It is trusted only to the extent that the
    client is the case worker; what it buys is that the *model* cannot choose
    which case its tool calls are recorded against.
    """
    raw = (ctx.headers or {}).get("x-case-id")
    try:
        return str(uuid.UUID(raw)) if raw else None
    except ValueError:
        return None  # a malformed header logs as an uncorrelated call, not as a crash


async def _log(
    ctx: Context, tool: str, args: dict, ok: bool, result_ids: list[str] | None = None
) -> None:
    """`result_ids` is for tools whose arguments do not determine their result.

    A commerce tool is identified by what it was asked — `get_order` for one
    order id returns that order. A retrieval is not: the same query can return
    different clauses as the corpus changes, so the ids that came back have to
    be recorded or a quote cannot be checked against them.
    """
    async with await psycopg.AsyncConnection.connect(LOG_DSN) as conn:
        await conn.execute(
            "INSERT INTO mcp_access_log (case_id, domain, tool, args_json, ok, result_ids)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (_case_id(ctx), DOMAIN, tool, Jsonb(args), ok, result_ids),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# commerce — read-only upstream facts. Holds no database credential at all:
# it goes through mock-commerce, which is the trust boundary L0 established.
# ---------------------------------------------------------------------------

if DOMAIN == "commerce":
    COMMERCE = os.environ.get("COMMERCE_URL", "http://mock-commerce:8010")

    async def _get(ctx: Context, tool: str, path: str, args: dict):
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{COMMERCE}{path}")
        await _log(ctx, tool, args, r.is_success)
        if not r.is_success:
            # Legible on purpose: an upstream 404 is a fact the collector
            # should be able to record as a hole, not a masked crash.
            raise ToolError(f"{tool}: upstream returned {r.status_code}")
        return r.json()

    @server.tool()
    async def get_order(ctx: Context, order_id: str) -> dict:
        """Order header and line items: total, status, placed_at, SKUs."""
        return await _get(ctx, "get_order", f"/orders/{order_id}", {"order_id": order_id})

    @server.tool()
    async def get_shipments(ctx: Context, order_id: str) -> list[dict]:
        """Carrier events for an order: delivery confirmation, signature capture,
        shipped vs returned weight. The weight pair is how an empty-box claim shows up."""
        return await _get(
            ctx, "get_shipments", f"/orders/{order_id}/shipments", {"order_id": order_id}
        )

    @server.tool()
    async def get_customer(ctx: Context, customer_id: str) -> dict:
        """Customer record: tier, tenure, and when the address and payment method were
        last changed. Recent changes to both are an account-takeover signal."""
        return await _get(
            ctx, "get_customer", f"/customers/{customer_id}", {"customer_id": customer_id}
        )

    @server.tool()
    async def get_returns_history(ctx: Context, customer_id: str) -> list[dict]:
        """This customer's prior returns, with reason codes and dates."""
        return await _get(
            ctx, "get_returns_history", f"/customers/{customer_id}/returns",
            {"customer_id": customer_id},
        )

    @server.tool()
    async def get_cohort_stats(ctx: Context, customer_id: str) -> dict:
        """This customer's return rate against its tier cohort. Read cohort_percentile
        (0-1), not the z-score: serial returners are inside their own cohort and inflate
        the deviation they are measured against."""
        return await _get(
            ctx, "get_cohort_stats", f"/customers/{customer_id}/cohort-stats",
            {"customer_id": customer_id},
        )

    @server.tool()
    async def get_payment_status(ctx: Context, order_id: str) -> dict:
        """Payment state for an order: captured, method, chargeback status."""
        return await _get(
            ctx, "get_payment_status", f"/payments/{order_id}", {"order_id": order_id}
        )

    @server.tool()
    async def get_defect_reports(ctx: Context, sku: str) -> list[dict]:
        """Other customers' defect reports for a SKU. A cluster corroborates a defect
        claim that would otherwise rest on the customer's word alone."""
        return await _get(ctx, "get_defect_reports", f"/skus/{sku}/defect-reports", {"sku": sku})


# ---------------------------------------------------------------------------
# evidence — object store only. The one process holding MinIO credentials.
# ---------------------------------------------------------------------------

elif DOMAIN == "evidence":
    import boto3  # imported here: only this domain has any use for it

    BUCKET = os.environ.get("S3_BUCKET", "refund-evidence")
    _s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        # Prefers the scoped evidence credential when one exists; falls back to
        # the root key, which is the honest state of local MinIO (§14 gap).
        aws_access_key_id=os.environ.get("MINIO_EVIDENCE_KEY") or os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=(
            os.environ.get("MINIO_EVIDENCE_SECRET") or os.environ["AWS_SECRET_ACCESS_KEY"]
        ),
    )

    def _scoped(case_id: str | None, key: str) -> str:
        """Confine every read to the session's own case prefix.

        The attack this blocks is not exotic: a model talked into fetching
        `other-case-id/passport.jpg` is asking politely, and without this the
        credential is wide enough to comply.
        """
        if not case_id:
            raise ToolError("no case id on this session")
        if ".." in key or not key.startswith(f"{case_id}/"):
            raise ToolError(f"key outside {case_id}/ — refused")
        return key

    @server.tool()
    async def list_attachments(ctx: Context) -> list[dict]:
        """Attachments belonging to the case on this session. Takes no arguments on
        purpose — which case this is, is the session's to know, not the caller's to pick."""
        case_id = _case_id(ctx)
        await _log(ctx, "list_attachments", {}, True)
        if not case_id:
            return []
        page = _s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{case_id}/")
        return [{"key": o["Key"], "size": o["Size"]} for o in page.get("Contents", [])]

    @server.tool()
    async def fetch_attachment(ctx: Context, key: str) -> dict:
        """One attachment, base64 encoded. Rejects any key outside the case's prefix."""
        try:
            key = _scoped(_case_id(ctx), key)
        except ToolError:
            await _log(ctx, "fetch_attachment", {"key": key}, False)
            raise
        obj = _s3.get_object(Bucket=BUCKET, Key=key)
        body = obj["Body"].read()
        await _log(ctx, "fetch_attachment", {"key": key}, True)
        return {
            "key": key,
            "content_type": obj.get("ContentType"),
            "size": len(body),
            # L4 decides whether the analyst gets bytes or a presigned URL; a
            # cap here keeps a 50 MB upload from becoming a 67 MB JSON-RPC frame.
            "b64": base64.b64encode(body).decode() if len(body) <= 2_000_000 else None,
        }


# ---------------------------------------------------------------------------
# policy — SELECT on policy_clauses and nothing else in the database.
# ---------------------------------------------------------------------------

elif DOMAIN == "policy":
    from packages.llm import Gateway, LLMUnavailable

    POLICY_DSN = os.environ["MCP_POLICY_DSN"]
    # The only credential this domain holds beyond its read-only role. It is what
    # lets the tool contract stay `search_policy(query: str)`: the alternative,
    # a caller passing a vector, would let anything that can reach this server
    # search by a representation of its own choosing.
    _gw = Gateway()

    @server.tool()
    async def search_policy(ctx: Context, query: str, k: int = 5) -> list[dict]:
        """Retrieve refund policy clauses relevant to a question.

        Ranked by meaning, not by wording: the query is embedded and compared
        against the clause vectors, so "the parcel never turned up" finds the
        clause about carrier records with no words in common. Falls back to
        keyword matching if the gateway is unreachable or the corpus has not
        been embedded yet — a degraded retrieval beats no retrieval, and the
        `degraded` flag on the result says which one the caller got.
        """
        vector, degraded = None, True
        try:
            vector = (await _gw.embed("embed", [query]))[0]
            degraded = False
        except (LLMUnavailable, KeyError):
            pass

        async with await psycopg.AsyncConnection.connect(POLICY_DSN) as conn:
            if vector is not None:
                cur = await conn.execute(
                    "SELECT id, text, policy_version, embedding <=> %s::vector AS distance"
                    " FROM policy_clauses WHERE embedding IS NOT NULL"
                    " ORDER BY distance LIMIT %s",
                    (str(vector), k),
                )
                rows = await cur.fetchall()
            else:
                rows = []
            if not rows:
                degraded = True
                terms = [f"%{w}%" for w in query.split() if len(w) > 3] or [f"%{query}%"]
                cur = await conn.execute(
                    "SELECT id, text, policy_version, NULL AS distance FROM policy_clauses"
                    " WHERE text ILIKE ANY(%s) LIMIT %s",
                    (terms, k),
                )
                rows = await cur.fetchall()

        await _log(ctx, "search_policy", {"query": query, "k": k}, True, [r[0] for r in rows])
        return [
            {
                "id": r[0], "text": r[1], "policy_version": r[2],
                "distance": None if r[3] is None else round(float(r[3]), 4),
                "degraded": degraded,
            }
            for r in rows
        ]


else:
    raise SystemExit(f"unknown MCP_DOMAIN: {DOMAIN!r} (commerce | evidence | policy)")


if __name__ == "__main__":
    print(f"[mcp-{DOMAIN}] serving on :{PORT}/mcp", flush=True)
    # stateless_http: there is no cross-call server state worth resuming, so the
    # client opens a session per call and the case header travels with it.
    server.run(transport="streamable-http", host="0.0.0.0", port=PORT, stateless_http=True)
