# Governed Refund Agents — Locked Architecture & Build Plan

**Project:** AI-powered e-commerce refund adjudication with governed AI agents
**Type:** Capstone / portfolio project. Showcase-quality, not production.
**Status:** Architecture LOCKED as of 2026-09-19. Implementation starts next iteration.

---

## 1. What this is

An asynchronous, multi-agent system that adjudicates e-commerce return/refund requests.
It auto-approves obviously-legitimate refunds and escalates everything risky, ambiguous,
or expensive to a human reviewer through a fraud-review dashboard.

### The one-line thesis

> The LLM is an evidence engine, not a decision authority.

Agents gather, normalise and reason over evidence and emit a **structured recommendation**.
A separate **deterministic policy engine** decides whether that recommendation is allowed
to auto-execute. That split is the entire meaning of "governed."

### What makes it capstone-worthy (the showcase angles)

1. **Supervised multi-agent graph** — one tool-looping collector, parallel bounded
   specialists, an adversarial critic, and a deterministic policy gate.
2. **Reproducible decisions** — the decision model is a pure function of a frozen,
   hashed evidence bundle. Any historical decision replays byte-for-byte.
3. **Policy-as-code** — OPA/Rego, versioned, the LLM cannot override it.
4. **Autonomy ladder** — shadow → suggest → assist → auto, promoted on measured evidence.
5. **Hash-chained audit log** — tamper-evident, full provenance per decision.
6. **Replay-based eval harness as a CI gate** — change a prompt, replay the corpus, diff outcomes.

---

## 2. Scope

### In scope
- Refund/return intake (web form + batch CLI + webhook)
- Evidence gathering from a mocked commerce backend
- Multi-agent risk reasoning + adversarial critique
- Deterministic policy gate with autonomy ladder + kill switch
- Idempotent refund execution against a **mock** payment provider
- Reviewer dashboard with queue, evidence view, override, SLA timers
- Hash-chained audit log + full decision provenance
- Replay/eval harness + shadow-mode comparison reporting

### Explicitly out of scope
- Real payment provider integration (mock adapter behind an interface)
- Real e-commerce platform integration (mock commerce service)
- Multi-region, HA, autoscaling
- PII tokenisation / vaulting
- VPC isolation, private subnets, NAT (noted as known prod gaps)
- SSO / enterprise IdP (simple seeded JWT auth instead)

---

## 3. Locked decisions

These are settled. Do not re-litigate during implementation.

| Area | Decision | Rationale |
|---|---|---|
| Flow model | **Async** with optimistic `202` ack | Mirrors real refund systems; removes latency pressure; makes Lambda cold starts irrelevant |
| Agent framework | **LangGraph** | Parallel fan-out, state reducers, conditional edges, Postgres checkpointing |
| API framework | **FastAPI** (+ Mangum on Lambda) | Same code local and deployed |
| LLM provider | **Anthropic API direct** | Bedrock has no free tier and adds IAM/region/model-access friction for zero benefit |
| Embeddings | **Local `all-MiniLM-L6-v2`** (sentence-transformers) | Free, deterministic, adequate for a ~100-clause corpus. Upgrade path: Voyage AI |
| Database | **Postgres + pgvector** (Supabase free tier) | Relational domain; pgvector removes need for a separate vector DB; free forever, no 12-month cliff |
| Queue | **AWS SQS** (always-free 1M req/mo) | Native retry + DLQ |
| Compute | **AWS Lambda container images** (always-free 1M req + 400k GB-s) | Async means cold starts don't matter |
| Policy engine | **OPA compiled to WASM**, evaluated in-process | Real OPA semantics, versioned bundle, zero servers |
| Tracing | **Langfuse Cloud** free tier (50k events/mo) | Self-hosting now needs Postgres + ClickHouse |
| Auth | **Seeded JWT** (3 roles) | Cognito/Keycloak is days of work for a capstone; roles are what the governance story needs |
| Object storage | **S3** (evidence photos, bundles, policy bundles) | 5 GB free 12 mo |
| Dashboard | **Next.js on Vercel** free | Trivial, always works for a demo |
| Dev environment | **Docker Compose**, identical images to prod | No code fork between local and cloud |
| Validation | **Pydantic v2** everywhere | Structured outputs, contract enforcement |
| Migrations | **Alembic** | |
| Tests | **pytest** + replay harness | |

### Deliberately rejected

| Rejected | Why |
|---|---|
| AWS Bedrock | No free tier for inference; extra friction, no learning benefit |
| AWS RDS | 750h/12-month cliff; Supabase is free forever |
| AWS Step Functions for the agent graph | 4,000 free state transitions/month — one case burns ~10 |
| Keycloak | JVM service to babysit; overkill |
| ContextForge / Bifrost | MCP + LLM gateways. Solve multi-team fan-out and provider failover. We have one team, one provider |
| Self-hosted Langfuse | Needs Postgres + ClickHouse |
| 10 agents | Lecture-count artifact. Every LLM hop is latency, cost, and a failure mode |
| LangGraph `interrupt()` for human review | Human SLA is hours-to-days; parks a live checkpoint and couples the dashboard to graph internals |
| LLM supervisor that can skip specialists | An LLM deciding what evidence *not* to look at is unbounded authority in a fraud system |
| Auto-deny | Denying a legitimate customer is worse than escalating a fraudulent one. **Only humans deny.** |

---

## 4. LLM strategy

### Model assignment per node

| Node | Model | Why |
|---|---|---|
| `intake_normalizer` | — none — | Deterministic parsing + Pydantic validation |
| `evidence_collector` | `claude-haiku-4-5-20251001` | Tool loop, many turns, needs to be cheap and fast |
| `image_analyst` | `claude-haiku-4-5-20251001` | Multimodal; emits observations, not verdicts |
| `behavior_analyst` | `claude-haiku-4-5-20251001` | Interprets precomputed numeric features |
| `policy_retriever` | `claude-haiku-4-5-20251001` | Clause selection over pgvector hits |
| `freeze_bundle` | — none — | Deterministic join + SHA-256 |
| `decision_proposer` | `claude-sonnet-5` | This is the judgement call. Worth the stronger model |
| `critic` | `claude-sonnet-5` | **Must be >= proposer.** A weaker critic cannot catch a stronger proposer |
| `policy_gate` | — none — | OPA/Rego |

> **Design rule:** the critic is never weaker than the proposer. Otherwise the adversarial
> review is theatre.

### Cost control
- **Dev/iteration mode:** Haiku everywhere including proposer + critic → ~$0.02/case
- **Showcase/eval mode:** Sonnet for proposer + critic → ~$0.12/case
- **Prompt caching** on the static prefix (system prompt, policy corpus, few-shots) — the
  cached portion is a large fraction of input tokens on proposer + critic
- Model tier is config, not code: `MODEL_PROFILE=dev|showcase`

### Budget
| Item | Estimate |
|---|---|
| Development iteration (~800 runs, Haiku) | ~$16 |
| Final eval corpus (200 cases, Sonnet) | ~$24 |
| Contingency | ~$10 |
| **Total** | **~$25–40** |

Everything else in the stack is free tier.

---

## 5. End-to-end flow with tech at every touchpoint

```
 ┌── SURFACE ─────────────────────────────────────────────────────────────┐
 │ Customer return form   │ Next.js (Vercel)                              │
 │ Batch fixture CLI      │ Typer                                          │
 │ Webhook (Shopify-ish)  │ FastAPI route + HMAC verify                   │
 └────────────────────────┬──────────────────────────────────────────────┘
                          │  POST /v1/returns    (photos → S3 presigned PUT)
                          ▼
 ┌── INTAKE ──────────────────────────────────────────────────────────────┐
 │ API Gateway HTTP API → refund-api  [Lambda · FastAPI + Mangum]        │
 │  1. Pydantic validate                                                  │
 │  2. INSERT case (status=RECEIVED)   ┐ ONE TRANSACTION                  │
 │  3. INSERT outbox (case.created)    ┘ (outbox pattern)                 │
 │  4. audit_append(case_created)                                         │
 │  → 202 { case_id, status_url }                                         │
 └────────────────────────┬──────────────────────────────────────────────┘
                          ▼
 ┌── RELAY ───────────────────────────────────────────────────────────────┐
 │ outbox-relay  [Lambda · EventBridge Scheduler every 60s]              │
 │ polls unpublished outbox rows → SQS, marks published                   │
 └────────────────────────┬──────────────────────────────────────────────┘
                          ▼
              SQS  case-events  (visibility 15m, maxReceive 3 → DLQ)
                          ▼
 ┌── ORCHESTRATION ───────────────────────────────────────────────────────┐
 │ case-orchestrator  [Lambda container · LangGraph]                      │
 │ reserved concurrency = 5 (bounds Postgres connections)                 │
 │ checkpointer: LangGraph Postgres saver                                 │
 │                                                                        │
 │  intake_normalizer      deterministic · Pydantic                       │
 │         ▼                                                              │
 │  route_specialists      deterministic rules table → required set       │
 │         ▼                                                              │
 │  evidence_collector     Haiku ReAct loop · read-only tools             │
 │                         CAPS: max_tool_steps=8, 40k tok, 90s           │
 │         ▼                                                              │
 │  completeness_check     deterministic assertion over REQUIRED kinds    │
 │         ▼                                                              │
 │    ┌────┴─────┬──────────────┐   PARALLEL (LangGraph superstep)        │
 │    ▼          ▼              ▼   each wrapped: returns `degraded`,     │
 │  image     behavior    policy_retriever        never raises            │
 │  analyst   analyst      ⚠ MANDATORY                                    │
 │    └────┬─────┴──────────────┘   reducer: Annotated[list, operator.add]│
 │         ▼                                                              │
 │  ❄ freeze_bundle        deterministic join → SHA-256 → S3              │
 │         ▼                                                              │
 │  decision_proposer      Sonnet · NO TOOLS · pure fn of ❄               │
 │         ▼                                                              │
 │  critic                 Sonnet · emits signals, does NOT route         │
 │         ▼                                                              │
 │  ╔═══════════════════════════════════════════════════════════════╗     │
 │  ║ policy_gate — THE ONLY TERMINAL AUTHORITY                     ║     │
 │  ║ OPA WASM bundle (versioned, loaded from S3)                   ║     │
 │  ║ in: recommendation · critic_dissents · degraded_nodes[]       ║     │
 │  ║     evidence_completeness · autonomy_mode · kill_switch       ║     │
 │  ║ out: ALLOW_AUTO | ESCALATE  + binding rule_id + policy_version║     │
 │  ╚═══════════════════════════════════════════════════════════════╝     │
 └──────────┬──────────────────────────────────┬─────────────────────────┘
            │ ALLOW_AUTO                       │ ESCALATE
            ▼                                  ▼
   SQS refund-execute              case.status = PENDING_REVIEW
            ▼                      EventBridge Scheduler → SLA timer (24h)
 ┌── EXECUTION ───────────────┐             GRAPH TERMINATES
 │ executor [Lambda]          │                   │
 │ idempotency_key =          │                   ▼
 │   sha256(case_id+dec_id)   │    ┌── HUMAN REVIEW ──────────────────────┐
 │ UNIQUE constraint on       │    │ Next.js dashboard (Vercel) + JWT      │
 │   refund_attempts          │    │ queue · evidence · rationale · trace  │
 │ → mock PSP adapter         │    │ approve / deny / request-info         │
 │ → ledger_entries           │    │ KILL SWITCH (admin role only)         │
 │ → audit_append             │    │                                       │
 │ → case.status = REFUNDED   │    │ POST /v1/cases/{id}/decision          │
 └──────────┬─────────────────┘    │  = NEW transaction, not a graph resume│
            ▼                      │  writes decision(actor=human)         │
      SES → customer email         │  → if approved: SQS refund-execute    │
                                   └───────────────────────────────────────┘

 ── CROSS-CUTTING ────────────────────────────────────────────────────────
  Postgres+pgvector (Supabase) · S3 (photos, bundles, policy bundles)
  audit_log (hash-chained) · Langfuse (traces, prompt versions, cost)
  CloudWatch (7-day retention) · EventBridge Scheduler (relay + SLA timers)
```

### Case state machine

```
RECEIVED → EVIDENCE_GATHERING → SCORED ─┬→ AUTO_APPROVED → EXECUTING → REFUNDED
                  │                     │                            └→ FAILED → DLQ
                  │                     └→ PENDING_REVIEW ─┬→ (human approve) → EXECUTING
                  │                            │           ├→ REJECTED   (human only)
                  │                            │           └→ EXPIRED    (SLA breach)
                  └→ EVIDENCE_FAILED → PENDING_REVIEW      (fail-safe: never fail closed)
```

**Invariants:**
1. Every arrow into a terminal state passes through the **policy gate**.
2. No path reaches `REJECTED` without a human actor.
3. Failures escalate. They never deny.

---

## 6. How input arrives

### 6.1 Customer return form (primary demo surface)
Next.js page: order lookup → item selection → reason code dropdown → free-text description
→ optional photo upload (presigned S3 PUT, max 3 images, 5 MB each) → submit.
Returns a status page that polls `GET /v1/cases/{id}` and shows
`under review` / `approved` / `needs more info`.

### 6.2 Batch fixture CLI (how you run evals)
`python -m refund.cli submit --fixtures tests/fixtures/*.json [--mode shadow]`
This is the eval driver, not a toy. Same code path as the web form.

### 6.3 Webhook (realism)
`POST /v1/webhooks/commerce` with HMAC-SHA256 signature verification, shaped like a
Shopify `refunds/create`. Demonstrates the intake adapter pattern.

### 6.4 Mocked upstream systems
A `mock-commerce` FastAPI service, seeded from generated data, exposing:
`GET /orders/{id}`, `/orders/{id}/shipments`, `/customers/{id}`,
`/customers/{id}/returns`, `/customers/{id}/cohort-stats`, `/payments/{id}`.
The agent's tools call this. It is a hard trust boundary — swapping it for a real
commerce API must require no agent changes.

### 6.5 Seed dataset (drives the whole eval story)
Generated with Faker + a scripted fraud planter. ~300 customers, ~2,000 orders,
~400 return requests, with **labelled** planted patterns:

| Pattern | Signature | Expected outcome |
|---|---|---|
| Clean legitimate | Long tenure, first return, low value, reason consistent | AUTO_APPROVE |
| Wardrobing | Returned 3–6 days after delivery, event-adjacent category, "didn't fit" | ESCALATE |
| Serial returner | Return rate >4σ above cohort | ESCALATE |
| INR / friendly fraud | "Never arrived" + carrier delivery confirmation + signature | ESCALATE |
| Empty box | Return weight << shipped weight | ESCALATE |
| Account takeover | Shipping address + payment method changed <7d before request | ESCALATE |
| Reason–evidence mismatch | "Defective" + photos show pristine item | ESCALATE |
| Genuine defect batch | Multiple customers, same SKU, same defect window | AUTO_APPROVE |

Labels are the ground truth for the eval harness and the shadow-mode agreement metric.

---

## 7. Repo layout

```
governed-refund-agents/
├─ docker-compose.yml
├─ PLANNING.md                      ← this file
├─ README.md                        ← demo script, screenshots, metrics
├─ services/
│  ├─ refund_api/                   FastAPI: intake, status, reviewer API
│  ├─ orchestrator/
│  │  ├─ graph.py                   LangGraph assembly
│  │  ├─ nodes/                     one module per node
│  │  ├─ tools/                     read-only tool defs + per-node scoping
│  │  ├─ prompts/                   versioned; prompt_version in every trace
│  │  └─ state.py                   CaseState + reducers
│  ├─ executor/                     idempotent refund execution
│  ├─ mock_commerce/                fake upstream systems
│  └─ outbox_relay/
├─ policy/
│  ├─ rego/                         refund.rego, autonomy.rego, killswitch.rego
│  ├─ tests/                        opa test (Rego unit tests)
│  └─ build.sh                      opa build -t wasm → S3
├─ packages/
│  ├─ contracts/                    Pydantic models — the shared spine
│  ├─ audit/                        hash-chain append + verify
│  └─ llm/                          client wrapper, caching, cost accounting
├─ dashboard/                       Next.js reviewer + customer UI
├─ evals/
│  ├─ corpus/                       frozen EvidenceBundles + expected outcomes
│  ├─ replay.py                     replay harness (the CI gate)
│  └─ report.py                     agreement / precision / escalation-rate report
├─ infra/                           Terraform: Lambda, SQS, S3, EventBridge, IAM
├─ scripts/seed_data.py
└─ tests/
```

---

## 8. Data model (tables)

| Table | Purpose | Key columns |
|---|---|---|
| `cases` | The refund request + lifecycle | `id`, `order_id`, `customer_id`, `reason_code`, `amount`, `currency`, `status`, `sla_due_at`, `created_at` |
| `case_attachments` | Uploaded photos | `case_id`, `s3_key`, `content_type`, `sha256` |
| `evidence_bundles` | Frozen snapshots | `id`, `case_id`, `bundle_hash`, `s3_key`, `completeness`, `missing_evidence[]`, `degraded_nodes[]` |
| `recommendations` | Proposer output | `id`, `bundle_hash`, `outcome`, `confidence`, `rationale`, `evidence_refs[]`, `model`, `prompt_version` |
| `critiques` | Critic output | `recommendation_id`, `dissents`, `unsupported_claims[]`, `notes` |
| `gate_evaluations` | Gate output | `recommendation_id`, `result`, `rule_id`, `policy_version`, `autonomy_mode`, `inputs_json` |
| `decisions` | Terminal decision | `id`, `case_id`, `outcome`, `actor_type` (agent\|human), `actor_id`, `gate_evaluation_id`, `override_reason_code` |
| `refund_attempts` | Idempotent execution | `decision_id`, **`idempotency_key` UNIQUE**, `psp_ref`, `status`, `attempt_no` |
| `ledger_entries` | Double-entry money record | `case_id`, `account`, `direction`, `amount`, `psp_ref` |
| `audit_log` | Hash-chained | `seq`, `case_id`, `event_type`, `payload_json`, `prev_hash`, `hash`, `actor`, `at` |
| `autonomy_config` | The ladder | `reason_code`, `amount_band`, `customer_tier`, `mode`, `promoted_at`, `promoted_by` |
| `kill_switch` | Global + per-segment | `scope`, `active`, `set_by`, `reason`, `set_at` |
| `policy_clauses` | RAG corpus | `id`, `text`, `policy_version`, `embedding vector(384)` |
| `outbox` | Transactional outbox | `id`, `topic`, `payload_json`, `published_at` |
| `reviewers` | Seeded auth | `id`, `email`, `role` (reviewer\|senior_reviewer\|admin), `password_hash` |

### Hash chain
`hash = sha256(prev_hash || seq || case_id || event_type || canonical_json(payload))`
A `verify_chain()` CLI walks the log and proves no row was altered or removed.
Demo this. It takes 30 seconds and it lands.

---

## 9. Core contracts

These four Pydantic models are the most important interfaces in the system. Define them
first; everything else depends on them.

```python
class EvidenceFragment(BaseModel):
    kind: Literal["order","payment_status","shipment","returns_history",
                  "cohort_stats","image_observation","behavior_features","policy_clause"]
    source: str                 # which tool/node produced it
    data: dict
    degraded: bool = False
    retrieved_at: datetime

class EvidenceBundle(BaseModel):
    case_id: UUID
    fragments: list[EvidenceFragment]
    completeness: Literal["complete","incomplete"]
    missing_evidence: list[str]
    degraded_nodes: list[str]
    bundle_hash: str            # sha256 of canonical JSON of the above

class Recommendation(BaseModel):
    outcome: Literal["APPROVE","ESCALATE"]        # note: no DENY
    confidence: float                              # 0..1
    rationale: str
    evidence_refs: list[str]                       # every claim must cite a fragment
    model: str
    prompt_version: str

class GateInput(BaseModel):
    recommendation: Recommendation
    critic_dissents: bool
    unsupported_claims: list[str]
    evidence_completeness: Literal["complete","incomplete"]
    degraded_nodes: list[str]
    amount: Decimal
    reason_code: str
    customer_tier: str
    autonomy_mode: Literal["shadow","suggest","assist","auto"]
    kill_switch_active: bool

class GateOutput(BaseModel):
    result: Literal["ALLOW_AUTO","ESCALATE"]
    rule_id: str                # which rule bound the decision
    policy_version: str
```

`Recommendation` has no `DENY` variant. The type system enforces the policy.

---

## 10. Governance mechanisms

### 10.1 The single-authority invariant
> Exactly one node produces the terminal routing decision: the **policy gate**.
> Everything upstream contributes *signals*, never edges to a terminal state.

Critic dissent, node degradation, and incomplete evidence are all **gate inputs**, not
bypass edges. This keeps the kill switch universal, gives every decision one uniform
audit shape with a binding `rule_id`, and makes thresholds tunable in Rego without a deploy.

### 10.2 Autonomy ladder
Per `(reason_code, amount_band, customer_tier)` segment:

| Mode | Agent runs | Executes | Human sees |
|---|---|---|---|
| `shadow` | ✅ | ❌ never | nothing — logged and compared against actual human decisions |
| `suggest` | ✅ | ❌ never | recommendation shown; human decides from scratch |
| `assist` | ✅ | ❌ never | dashboard pre-fills; human confirms in one click |
| `auto` | ✅ | ✅ if gate allows | sampled QA only |

**Promotion rule:** a segment earns the next rung only on measured evidence
(e.g. ≥95% shadow agreement over ≥200 cases). Promotions are written to
`autonomy_config` with `promoted_by` and audited.

Ship at `shadow` on day one. The ladder *is* the rollout plan, and the promotion
report is a great README artifact.

### 10.3 Kill switch
A single row read by the gate on **every** evaluation. Flipping it forces all segments to
`suggest` — the pipeline keeps running and keeps learning, nothing auto-executes.
Checked in the gate, not the agent, so no prompt change can route around it.
Admin role only. Flipping it live is part of the demo.

### 10.4 Least privilege per node

| Node | Tool scope |
|---|---|
| `evidence_collector` | read: orders, shipments, returns history, cohort stats. **No payment instruments** |
| `image_analyst` | read: S3 attachments for this case only |
| `behavior_analyst` | read: aggregate customer features only |
| `policy_retriever` | read: `policy_clauses` only |
| `decision_proposer` | **none** |
| `critic` | **none** |
| `executor` | the only component that can move money |

### 10.5 Reproducibility — two tiers, both covered

| Tier | Contract | Deterministic? | Guarded by |
|---|---|---|---|
| 1 | bundle → decision | ✅ | Replay harness (CI gate) |
| 0 | case → bundle | ❌ (ReAct loop) | `completeness_check` + loop caps |

Tier 0 is the weaker one, and that is exactly why `completeness_check` exists: if the
collector stops early, the decision model would otherwise reason confidently over a hole.

---

## 11. Environments

### Local (primary build target)
`docker compose up` brings up: `refund_api`, `orchestrator`, `executor`, `mock_commerce`,
`postgres` (pgvector image), `localstack` (SQS + S3), `opa` (dev sidecar for fast Rego
iteration), `dashboard`. Langfuse points at cloud.

### Cloud (showcase deploy, week 6)

| Layer | Service | Tier |
|---|---|---|
| Dashboard + customer form | Vercel | Free |
| API / orchestrator / executor / relay | AWS Lambda (container images) | Always-free 1M req + 400k GB-s |
| HTTP front door | API Gateway HTTP API | 1M calls/mo, 12 mo |
| Queues | SQS + DLQ | Always-free 1M req |
| Database | Supabase Postgres + pgvector | Free forever, 500 MB |
| Object storage | S3 | 5 GB, 12 mo |
| Schedules / SLA timers | EventBridge Scheduler | 14M invocations free |
| Email | SES (sandbox) | 3k/mo |
| Tracing | Langfuse Cloud | 50k events/mo |
| Logs | CloudWatch, **7-day retention** | 5 GB free |

**Escape hatch:** if Lambda container packaging eats more than a day, deploy the same
Compose stack to a single EC2 `t3.small` and move on. The showcase is the governed-agent
design, not the deployment topology.

### Gotchas to handle on day one
- Lambda reserved concurrency = **5** on the orchestrator (bounds Postgres connections;
  avoids needing non-free RDS Proxy)
- CloudWatch log retention = **7 days** (unbounded retention will silently eat the free tier)
- LangGraph + sentence-transformers → container image Lambda, not zip (size limit)
- S3 bucket fully private; presigned PUT for uploads; 90-day lifecycle expiry

### Environment variables
```
ANTHROPIC_API_KEY           # TODO: Securely load from env/secrets manager. Do not hardcode.
DATABASE_URL                # TODO: Securely load from env/secrets manager. Do not hardcode.
LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
JWT_SIGNING_KEY             # TODO: Securely load from env/secrets manager. Do not hardcode.
WEBHOOK_HMAC_SECRET         # TODO: Securely load from env/secrets manager. Do not hardcode.
AWS_REGION, SQS_CASE_EVENTS_URL, SQS_REFUND_EXECUTE_URL, S3_BUCKET
MODEL_PROFILE=dev|showcase
POLICY_BUNDLE_S3_KEY
```
Local dev reads from `.env` (gitignored). Deployed reads from Lambda env /
SSM Parameter Store. No secrets in the repo, ever — `.env.example` with placeholders only.

---

## 12. Milestones

| # | Milestone | Done when |
|---|---|---|
| **M0** | Scaffold | Compose up; Alembic schema applied; seed data loaded; `mock_commerce` answers all endpoints |
| **M1** | Async plumbing | `POST /v1/returns` → outbox → SQS → worker → **stub** decision → executor → `REFUNDED`. No LLM yet. End-to-end pipe proven |
| **M2** | Collector + completeness | ReAct loop with caps; `completeness_check` sets `incomplete` and routes to escalate |
| **M3** | Parallel specialists + freeze | 3 specialists concurrent, reducers correct, degraded-not-raised, bundle hashed to S3 |
| **M4** | Proposer + critic | Structured `Recommendation`; critic flags unsupported claims; both traced in Langfuse with prompt versions |
| **M5** | Policy gate | Rego rules + `opa test` passing; WASM bundle loaded from S3; autonomy ladder + kill switch wired |
| **M6** | Audit + dashboard | Hash chain + `verify_chain()`; reviewer queue, evidence view, override with reason code, kill switch UI |
| **M7** | Eval harness | 200-case corpus; `replay.py` as CI gate; shadow-mode agreement report |
| **M8** | Deploy + README | Live URL; demo script; metrics table; architecture diagram |

M1 before any LLM work is deliberate. Prove the pipe, then add intelligence to it.

---

## 13. Demo script (what you actually show)

1. Submit a clean legitimate return on the customer form → watch it auto-approve in ~20s
2. Submit a wardrobing case → lands in the reviewer queue with the critic's objection visible
3. Open the case: frozen evidence bundle, cited policy clauses, proposer rationale,
   critic dissent, the exact Rego `rule_id` that bound the decision
4. **Flip the kill switch** → resubmit case 1 → now escalates, and the audit log shows why
5. Run `verify_chain()` → tamper one row in psql → re-run → chain breaks loudly
6. Run `replay.py` against the 200-case corpus → agreement metrics, escalation rate,
   cost per case
7. Show the shadow-mode promotion report: which segments have earned `auto`

That sequence tells the governance story better than any slide.

---

## 14. Known production gaps (state these openly)

Listing these is a strength, not a weakness — it shows you know the difference between
a capstone and a production system.

- No VPC isolation; Postgres reachable over TLS with IP allowlist rather than private subnet
- No PII tokenisation; customer data stored in plaintext columns
- Mock PSP — a real one needs webhook reconciliation, partial refunds, currency handling
- Single region, no DR
- Seeded JWT instead of enterprise SSO; no MFA on the admin role
- No rate limiting or abuse protection on the public intake endpoint
- Prompt-injection hardening on customer free-text and image content is minimal
  (untrusted-input boundary is identified but not fully defended)
- Eval corpus is synthetic; real drift monitoring is not implemented

---

## 15. Next iteration

Start at **M0**. First code written: `packages/contracts/` — the four Pydantic models in
§9. Everything else depends on them, and locking them first prevents the schema churn
that would otherwise ripple through every node.
