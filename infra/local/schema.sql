-- PLANNING.md §8 + PLANNING-LOCAL.md §9 deltas.
-- Re-runnable: every statement is IF NOT EXISTS. Adding a COLUMN later means
-- `make nuke up migrate seed` (seconds) rather than a migration tool.
--
-- Two schemas on purpose:
--   commerce.*  the mocked upstream. A hard trust boundary — the agent reads it
--               only through mock-commerce / mcp-commerce, never directly.
--   public.*    the refund system's own state.

CREATE EXTENSION IF NOT EXISTS vector;   -- gen_random_uuid() is built in on PG13+

-- ===========================================================================
-- commerce: the fake upstream systems (§6.4)
-- ===========================================================================
CREATE SCHEMA IF NOT EXISTS commerce;

CREATE TABLE IF NOT EXISTS commerce.customers (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL,
    name          TEXT NOT NULL,
    tier          TEXT NOT NULL,              -- standard | plus | vip
    created_at    TIMESTAMPTZ NOT NULL,       -- tenure
    address_changed_at    TIMESTAMPTZ,        -- account-takeover signal
    payment_changed_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS commerce.orders (
    id            TEXT PRIMARY KEY,
    customer_id   TEXT NOT NULL REFERENCES commerce.customers(id),
    placed_at     TIMESTAMPTZ NOT NULL,
    total_amount  NUMERIC(14,2) NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'IDR',
    status        TEXT NOT NULL               -- placed | shipped | delivered | refunded
);
CREATE INDEX IF NOT EXISTS orders_customer_idx ON commerce.orders(customer_id);

CREATE TABLE IF NOT EXISTS commerce.order_items (
    id            BIGSERIAL PRIMARY KEY,
    order_id      TEXT NOT NULL REFERENCES commerce.orders(id),
    sku           TEXT NOT NULL,
    category      TEXT NOT NULL,
    title         TEXT NOT NULL,
    qty           INT NOT NULL DEFAULT 1,
    unit_price    NUMERIC(14,2) NOT NULL,
    weight_grams  INT NOT NULL
);
CREATE INDEX IF NOT EXISTS order_items_order_idx ON commerce.order_items(order_id);

CREATE TABLE IF NOT EXISTS commerce.shipments (
    id                    TEXT PRIMARY KEY,
    order_id              TEXT NOT NULL REFERENCES commerce.orders(id),
    carrier               TEXT NOT NULL,
    tracking_no           TEXT NOT NULL,
    shipped_at            TIMESTAMPTZ,
    delivered_at          TIMESTAMPTZ,
    delivery_confirmed    BOOLEAN NOT NULL DEFAULT FALSE,
    signature_captured    BOOLEAN NOT NULL DEFAULT FALSE,
    shipped_weight_grams  INT,
    returned_weight_grams INT              -- empty-box signal
);
CREATE INDEX IF NOT EXISTS shipments_order_idx ON commerce.shipments(order_id);

CREATE TABLE IF NOT EXISTS commerce.payments (
    id            TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL REFERENCES commerce.orders(id),
    method        TEXT NOT NULL,           -- card | wallet | va | cod
    status        TEXT NOT NULL,           -- captured | authorized | failed | refunded
    captured_at   TIMESTAMPTZ,
    amount        NUMERIC(14,2) NOT NULL,
    psp_ref       TEXT
);
CREATE INDEX IF NOT EXISTS payments_order_idx ON commerce.payments(order_id);

-- Prior returns. Distinct from public.cases: this is upstream history the agent
-- reads as evidence, not requests this system is processing.
CREATE TABLE IF NOT EXISTS commerce.returns (
    id            TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL REFERENCES commerce.orders(id),
    customer_id   TEXT NOT NULL REFERENCES commerce.customers(id),
    reason_code   TEXT NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL,
    resolution    TEXT NOT NULL,           -- refunded | rejected | pending
    amount        NUMERIC(14,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS returns_customer_idx ON commerce.returns(customer_id);

CREATE TABLE IF NOT EXISTS commerce.defect_reports (
    id            BIGSERIAL PRIMARY KEY,
    sku           TEXT NOT NULL,
    reported_at   TIMESTAMPTZ NOT NULL,
    defect_code   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS defect_reports_sku_idx ON commerce.defect_reports(sku);

-- ===========================================================================
-- public: the refund system
-- ===========================================================================

CREATE TABLE IF NOT EXISTS cases (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id        TEXT NOT NULL,
    customer_id     TEXT NOT NULL,
    reason_code     TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    amount          NUMERIC(14,2) NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'IDR',
    status          TEXT NOT NULL DEFAULT 'RECEIVED',
    source          TEXT NOT NULL DEFAULT 'form',   -- form | webhook | fixture
    -- PLANNING-LOCAL §9: guard + classifier outcomes, read by the gate
    injection_flagged BOOLEAN NOT NULL DEFAULT FALSE,
    safety_flagged    BOOLEAN NOT NULL DEFAULT FALSE,
    reason_mismatch   BOOLEAN NOT NULL DEFAULT FALSE,
    sla_due_at      TIMESTAMPTZ,
    -- How many times the sweeper has re-published this case. Bounds the
    -- outbox replacement: without it a hard-failing case is re-published
    -- every 60s forever. See PLANNING-LOCAL.md §3.1.
    sweep_count     INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cases_status_idx ON cases(status);
CREATE INDEX IF NOT EXISTS cases_sla_idx ON cases(sla_due_at) WHERE status = 'PENDING_REVIEW';

CREATE TABLE IF NOT EXISTS case_attachments (
    id           BIGSERIAL PRIMARY KEY,
    case_id      UUID NOT NULL REFERENCES cases(id),
    s3_key       TEXT NOT NULL,
    content_type TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS case_attachments_case_idx ON case_attachments(case_id);

CREATE TABLE IF NOT EXISTS evidence_bundles (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id          UUID NOT NULL REFERENCES cases(id),
    bundle_hash      TEXT NOT NULL,
    s3_key           TEXT NOT NULL,
    completeness     TEXT NOT NULL,
    missing_evidence TEXT[] NOT NULL DEFAULT '{}',
    degraded_nodes   TEXT[] NOT NULL DEFAULT '{}',
    bundle_truncated BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evidence_bundles_hash_idx ON evidence_bundles(bundle_hash);

CREATE TABLE IF NOT EXISTS recommendations (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id        UUID NOT NULL REFERENCES cases(id),
    bundle_hash    TEXT NOT NULL,
    outcome        TEXT NOT NULL CHECK (outcome IN ('APPROVE','ESCALATE')),  -- no DENY
    confidence     DOUBLE PRECISION NOT NULL,
    rationale      TEXT NOT NULL,
    evidence_refs  TEXT[] NOT NULL DEFAULT '{}',
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS critiques (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recommendation_id  UUID NOT NULL REFERENCES recommendations(id),
    dissents           BOOLEAN NOT NULL,
    unsupported_claims TEXT[] NOT NULL DEFAULT '{}',
    notes              TEXT NOT NULL DEFAULT '',
    model              TEXT NOT NULL,
    prompt_version     TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gate_evaluations (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recommendation_id UUID NOT NULL REFERENCES recommendations(id),
    result            TEXT NOT NULL CHECK (result IN ('ALLOW_AUTO','ESCALATE')),
    rule_id           TEXT NOT NULL,
    policy_version    TEXT NOT NULL,
    autonomy_mode     TEXT NOT NULL,
    inputs_json       JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS decisions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id              UUID NOT NULL REFERENCES cases(id),
    outcome              TEXT NOT NULL,   -- APPROVE | REJECT (REJECT: humans only)
    actor_type           TEXT NOT NULL CHECK (actor_type IN ('agent','human')),
    actor_id             TEXT NOT NULL,
    gate_evaluation_id   UUID REFERENCES gate_evaluations(id),
    override_reason_code TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Invariant 2 of the state machine, enforced by the database.
    CONSTRAINT reject_requires_human CHECK (outcome <> 'REJECT' OR actor_type = 'human')
);
CREATE INDEX IF NOT EXISTS decisions_case_idx ON decisions(case_id);

CREATE TABLE IF NOT EXISTS refund_attempts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    decision_id     UUID NOT NULL REFERENCES decisions(id),
    idempotency_key TEXT NOT NULL UNIQUE,     -- sha256(case_id + decision_id)
    psp_ref         TEXT,
    status          TEXT NOT NULL,            -- pending | succeeded | failed
    attempt_no      INT NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id         BIGSERIAL PRIMARY KEY,
    case_id    UUID NOT NULL REFERENCES cases(id),
    account    TEXT NOT NULL,
    direction  TEXT NOT NULL CHECK (direction IN ('debit','credit')),
    amount     NUMERIC(14,2) NOT NULL,
    psp_ref    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Hash chain: hash = sha256(prev_hash || seq || case_id || event_type || canonical_json(payload))
CREATE TABLE IF NOT EXISTS audit_log (
    seq        BIGSERIAL PRIMARY KEY,
    case_id    UUID,
    event_type TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL UNIQUE,
    actor      TEXT NOT NULL,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_case_idx ON audit_log(case_id);

CREATE TABLE IF NOT EXISTS autonomy_config (
    id          BIGSERIAL PRIMARY KEY,
    reason_code TEXT NOT NULL,
    amount_band TEXT NOT NULL,            -- e.g. '0-500000'
    customer_tier TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('shadow','suggest','assist','auto')),
    promoted_at TIMESTAMPTZ,
    promoted_by TEXT,
    UNIQUE (reason_code, amount_band, customer_tier)
);

CREATE TABLE IF NOT EXISTS kill_switch (
    scope   TEXT PRIMARY KEY,             -- 'global' or a segment key
    active  BOOLEAN NOT NULL DEFAULT FALSE,
    set_by  TEXT,
    reason  TEXT,
    set_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS policy_clauses (
    id             TEXT PRIMARY KEY,
    text           TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    embedding      vector(1024)           -- gte-large-en-v1.5, PLANNING-LOCAL §4
);

-- No `outbox` table: refund-api publishes to RabbitMQ directly after commit.
-- The dual-write gap and what contains it are in PLANNING-LOCAL.md §3.1.
-- Supports the sweeper that re-publishes cases stuck in RECEIVED.
CREATE INDEX IF NOT EXISTS cases_unpublished_idx ON cases(created_at) WHERE status = 'RECEIVED';

CREATE TABLE IF NOT EXISTS reviewers (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL UNIQUE,
    role          TEXT NOT NULL CHECK (role IN ('reviewer','senior_reviewer','admin')),
    password_hash TEXT NOT NULL
);

-- PLANNING-LOCAL §4A: the independent access log that makes citations_verified
-- a deterministic check rather than a model's self-report.
CREATE TABLE IF NOT EXISTS mcp_access_log (
    id        BIGSERIAL PRIMARY KEY,
    case_id   UUID,
    domain    TEXT NOT NULL,              -- commerce | evidence | policy
    tool      TEXT NOT NULL,
    args_json JSONB NOT NULL,
    ok        BOOLEAN NOT NULL,
    at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mcp_access_log_case_idx ON mcp_access_log(case_id);

-- Eval ground truth (§6.5). Planted patterns with their expected outcome; the
-- fixture CLI submits these as cases and the harness scores against the label.
CREATE TABLE IF NOT EXISTS seed_labels (
    id               BIGSERIAL PRIMARY KEY,
    pattern          TEXT NOT NULL,
    expected_outcome TEXT NOT NULL,       -- AUTO_APPROVE | ESCALATE
    order_id         TEXT NOT NULL REFERENCES commerce.orders(id),
    customer_id      TEXT NOT NULL REFERENCES commerce.customers(id),
    reason_code      TEXT NOT NULL,
    description      TEXT NOT NULL,
    amount           NUMERIC(14,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS seed_labels_pattern_idx ON seed_labels(pattern);

-- ===========================================================================
-- Columns added after a table first shipped
-- ===========================================================================
-- `CREATE TABLE IF NOT EXISTS` skips the whole table on an existing database,
-- so a new column in a definition above is silently NOT applied. Repeat it
-- here as an ALTER and `make migrate` stays correct without a nuke. This is the
-- poor man's migration log — the honest cost of not running Alembic.
ALTER TABLE cases ADD COLUMN IF NOT EXISTS sweep_count INT NOT NULL DEFAULT 0;

-- The policy corpus is not seeded here. `policy/refund-policy-v1.md` is the
-- source, and `make embed-policy` chunks it by clause and embeds it — the text
-- and its vector have to arrive together or retrieval silently answers from a
-- stale corpus. See scripts/embed_policy_corpus.py (L2.6).
--
-- No index on policy_clauses.embedding on purpose: at ~45 clauses a sequential
-- scan beats HNSW, and an index here would cost accuracy for no measurable
-- speed. Add one when the corpus reaches thousands of rows.

-- L2.6: what a tool call returned, not just what it was asked. `search_policy`'s
-- arguments do not determine its result, so without this a node could quote a
-- clause the retrieval never returned and citation_check would still pass.
ALTER TABLE mcp_access_log ADD COLUMN IF NOT EXISTS result_ids TEXT[];

-- L5: the gate decides every case, including one whose proposer failed, so a
-- gate evaluation can exist without a recommendation. It is tied to its case
-- directly; `recommendation_id` is filled when there was one.
ALTER TABLE gate_evaluations ADD COLUMN IF NOT EXISTS case_id UUID REFERENCES cases(id);
ALTER TABLE gate_evaluations ALTER COLUMN recommendation_id DROP NOT NULL;
CREATE INDEX IF NOT EXISTS gate_evaluations_case_idx ON gate_evaluations(case_id);
