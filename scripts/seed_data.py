#!/usr/bin/env python3
"""Seed the mocked upstream and the labelled eval corpus (PLANNING.md §6.5).

Two phases:

1. A clean base population, which is what makes the cohort statistics mean
   anything — a serial returner is only detectable against a normal baseline.
2. 50 cases per planted pattern, each with a dedicated customer so its returns
   history cannot contaminate another pattern's signal. Every planted case gets
   a `seed_labels` row: the ground truth for the eval harness and the
   shadow-agreement metric.

Runs on the host against the published Postgres port. Deterministic — same seed,
same corpus, so eval numbers are comparable across runs.
"""

from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone

import psycopg
from faker import Faker

DSN = os.environ.get(
    "DATABASE_URL",
    f"postgresql://refund:refund@127.0.0.1:{os.environ.get('PG_PORT') or 5432}/refund",
)
SEED = 20260920
N_BASE_CUSTOMERS = 250
N_BASE_ORDERS = 2000
PER_PATTERN = 50
NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)

fake = Faker()
Faker.seed(SEED)
rng = random.Random(SEED)

# (category, event_adjacent) — wardrobing concentrates in event-adjacent goods.
CATEGORIES = [
    ("apparel-formal", True),
    ("footwear-formal", True),
    ("apparel-casual", False),
    ("electronics", False),
    ("home", False),
    ("beauty", False),
]
SKUS = [
    (f"SKU-{i:04d}", *rng.choice(CATEGORIES), rng.randint(150, 4000))
    for i in range(1, 81)
]  # id, category, event_adjacent, weight_grams

customers: list[tuple] = []
orders: list[tuple] = []
items: list[tuple] = []
shipments: list[tuple] = []
payments: list[tuple] = []
returns: list[tuple] = []
defects: list[tuple] = []
labels: list[tuple] = []

_seq = {"cust": 0, "order": 0, "ship": 0, "pay": 0, "ret": 0}


def _next(kind: str) -> str:
    _seq[kind] += 1
    return f"{kind}-{_seq[kind]:05d}"


def add_customer(
    *,
    tier: str | None = None,
    tenure_days: int | None = None,
    address_changed_at: datetime | None = None,
    payment_changed_at: datetime | None = None,
) -> tuple[str, str]:
    cid = _next("cust")
    tier = tier or rng.choices(["standard", "plus", "vip"], weights=[70, 22, 8])[0]
    created = NOW - timedelta(days=tenure_days if tenure_days is not None else rng.randint(30, 1500))
    customers.append(
        (cid, fake.email(), fake.name(), tier, created, address_changed_at, payment_changed_at)
    )
    return cid, tier


def add_order(
    customer_id: str,
    *,
    placed_at: datetime,
    sku: tuple | None = None,
    amount: int | None = None,
    status: str = "delivered",
) -> tuple[str, tuple, int]:
    oid = _next("order")
    sku = sku or rng.choice(SKUS)
    amount = amount if amount is not None else rng.randrange(150_000, 4_000_000, 10_000)
    orders.append((oid, customer_id, placed_at, amount, "IDR", status))
    items.append((oid, sku[0], sku[1], f"{sku[1].replace('-', ' ').title()} item", 1, amount, sku[3]))
    return oid, sku, amount


def add_shipment(
    order_id: str,
    *,
    delivered_at: datetime | None,
    shipped_weight: int,
    delivery_confirmed: bool = True,
    signature_captured: bool = False,
    returned_weight: int | None = None,
) -> None:
    shipped_at = (delivered_at - timedelta(days=rng.randint(1, 5))) if delivered_at else None
    shipments.append(
        (
            _next("ship"),
            order_id,
            rng.choice(["JNE", "SiCepat", "AnterAja", "GoSend"]),
            fake.bothify("TRK-########"),
            shipped_at,
            delivered_at,
            delivery_confirmed,
            signature_captured,
            shipped_weight,
            returned_weight,
        )
    )


def add_payment(order_id: str, amount: int, *, captured_at: datetime, status: str = "captured") -> None:
    payments.append(
        (
            _next("pay"),
            order_id,
            rng.choice(["card", "wallet", "va"]),
            status,
            captured_at,
            amount,
            fake.bothify("psp_????????"),
        )
    )


def add_return(
    customer_id: str, order_id: str, reason: str, requested_at: datetime, amount: int
) -> None:
    returns.append(
        (
            _next("ret"),
            order_id,
            customer_id,
            reason,
            requested_at,
            rng.choices(["refunded", "rejected"], weights=[85, 15])[0],
            amount,
        )
    )


def add_label(
    pattern: str,
    expected: str,
    order_id: str,
    customer_id: str,
    reason: str,
    description: str,
    amount: int,
) -> None:
    labels.append((pattern, expected, order_id, customer_id, reason, description, amount))


def delivered_order(customer_id: str, *, days_ago: int, **kw) -> tuple[str, tuple, int, datetime]:
    """An ordinary completed purchase: order + shipment + captured payment."""
    placed = NOW - timedelta(days=days_ago)
    oid, sku, amount = add_order(customer_id, placed_at=placed, **kw)
    delivered = placed + timedelta(days=rng.randint(2, 6))
    add_shipment(oid, delivered_at=delivered, shipped_weight=sku[3])
    add_payment(oid, amount, captured_at=placed)
    return oid, sku, amount, delivered


# ---------------------------------------------------------------------------
# Phase 1 — clean base population
# ---------------------------------------------------------------------------
def build_base() -> None:
    base = [add_customer()[0] for _ in range(N_BASE_CUSTOMERS)]
    for _ in range(N_BASE_ORDERS):
        cid = rng.choice(base)
        oid, _sku, amount, delivered = delivered_order(cid, days_ago=rng.randint(10, 700))
        # A modest, realistic background return rate. Without this the cohort
        # stddev is zero and every planted returner looks infinitely anomalous.
        if rng.random() < 0.06:
            add_return(
                cid,
                oid,
                rng.choice(["DIDNT_FIT", "CHANGED_MIND", "DAMAGED", "NOT_AS_DESCRIBED"]),
                delivered + timedelta(days=rng.randint(1, 20)),
                amount,
            )


# ---------------------------------------------------------------------------
# Phase 2 — the eight planted patterns
# ---------------------------------------------------------------------------
_CLEAN_DAMAGE = {
    "apparel-formal": "The parcel arrived torn open and the jacket inside has a long rip down the sleeve.",
    "footwear-formal": "The box was crushed in transit and one of the shoes has a split along the sole.",
    "apparel-casual": "The bag arrived soaked through and the shirt inside is stained and ruined.",
    "electronics": "The box arrived dented on one corner and the device's screen is cracked.",
    "home": "The carton arrived crushed and the item inside is broken into pieces.",
    "beauty": "The package arrived wet and the bottle inside had leaked everywhere.",
}


def p_clean() -> None:
    """Long tenure, first return, low value, reason consistent with evidence."""
    for _ in range(PER_PATTERN):
        cid, _ = add_customer(tier=rng.choice(["plus", "vip"]), tenure_days=rng.randint(700, 1500))
        for _ in range(rng.randint(4, 9)):  # purchase history, no returns
            delivered_order(cid, days_ago=rng.randint(60, 900))
        oid, sku, amount, _d = delivered_order(
            cid, days_ago=rng.randint(3, 10), amount=rng.randrange(150_000, 500_000, 10_000)
        )
        add_label(
            "clean_legitimate",
            "AUTO_APPROVE",
            oid,
            cid,
            "DAMAGED",
            # ⚠️ Fixed at L4. This was one fixed string — "the mug inside is
            # cracked. Photos attached." — on whatever SKU the order drew, so a
            # "clean" case claimed a mug on a footwear order and photos that did
            # not exist. The proposer escalated it for exactly that, correctly,
            # and the label called it wrong. Derived from the SKU, so no draw
            # from `rng` and the rest of the corpus is byte-identical.
            _CLEAN_DAMAGE[sku[1]],
            amount,
        )


def p_wardrobing() -> None:
    """Returned 3-6 days after delivery, event-adjacent category, 'didn't fit'."""
    event_skus = [s for s in SKUS if s[2]]
    for _ in range(PER_PATTERN):
        cid, _ = add_customer(tenure_days=rng.randint(120, 600))
        for _ in range(rng.randint(2, 5)):
            delivered_order(cid, days_ago=rng.randint(60, 500))
        sku = rng.choice(event_skus)
        placed = NOW - timedelta(days=rng.randint(8, 12))
        oid, _s, amount = add_order(cid, placed_at=placed, sku=sku, amount=rng.randrange(900_000, 3_500_000, 50_000))
        delivered = placed + timedelta(days=2)
        add_shipment(oid, delivered_at=delivered, shipped_weight=sku[3], returned_weight=sku[3])
        add_payment(oid, amount, captured_at=placed)
        add_label(
            "wardrobing",
            "ESCALATE",
            oid,
            cid,
            "DIDNT_FIT",
            "Didn't fit right, would like to return it.",
            amount,
        )


def p_serial_returner() -> None:
    """Return rate far above the tier cohort."""
    for _ in range(PER_PATTERN):
        cid, _ = add_customer(tenure_days=rng.randint(200, 500))
        for _ in range(rng.randint(9, 14)):
            oid, _s, amount, delivered = delivered_order(cid, days_ago=rng.randint(20, 400))
            if rng.random() < 0.8:  # ~80% return rate against a ~6% cohort
                add_return(cid, oid, rng.choice(["DIDNT_FIT", "CHANGED_MIND"]),
                           delivered + timedelta(days=rng.randint(2, 10)), amount)
        oid, _s, amount, _d = delivered_order(cid, days_ago=rng.randint(2, 8))
        add_label("serial_returner", "ESCALATE", oid, cid, "NOT_AS_DESCRIBED",
                  "Item is not what was shown on the listing.", amount)


def p_never_arrived() -> None:
    """'Never arrived' contradicted by carrier confirmation plus a signature."""
    for _ in range(PER_PATTERN):
        cid, _ = add_customer(tenure_days=rng.randint(60, 400))
        placed = NOW - timedelta(days=rng.randint(9, 16))
        oid, sku, amount = add_order(cid, placed_at=placed)
        add_shipment(
            oid,
            delivered_at=placed + timedelta(days=3),
            shipped_weight=sku[3],
            delivery_confirmed=True,
            signature_captured=True,   # the contradiction
        )
        add_payment(oid, amount, captured_at=placed)
        add_label("never_arrived_friendly_fraud", "ESCALATE", oid, cid, "NEVER_ARRIVED",
                  "I never received this package. Nothing was delivered.", amount)


def p_empty_box() -> None:
    """Return weight far below shipped weight."""
    for _ in range(PER_PATTERN):
        cid, _ = add_customer(tenure_days=rng.randint(60, 600))
        sku = rng.choice([s for s in SKUS if s[3] > 800])
        placed = NOW - timedelta(days=rng.randint(10, 20))
        oid, _s, amount = add_order(cid, placed_at=placed, sku=sku)
        add_shipment(
            oid,
            delivered_at=placed + timedelta(days=3),
            shipped_weight=sku[3],
            returned_weight=max(30, sku[3] // 20),   # packaging only
        )
        add_payment(oid, amount, captured_at=placed)
        add_label("empty_box", "ESCALATE", oid, cid, "MISSING_PARTS",
                  "Opened the parcel and the main unit was missing.", amount)


def p_account_takeover() -> None:
    """Shipping address and payment method both changed <7d before the request."""
    for _ in range(PER_PATTERN):
        changed = NOW - timedelta(days=rng.randint(1, 6))
        cid, _ = add_customer(
            tenure_days=rng.randint(400, 1200),
            address_changed_at=changed,
            payment_changed_at=changed - timedelta(hours=rng.randint(1, 20)),
        )
        for _ in range(rng.randint(3, 7)):
            delivered_order(cid, days_ago=rng.randint(90, 1000))
        oid, _s, amount, _d = delivered_order(
            cid, days_ago=rng.randint(2, 5), amount=rng.randrange(2_000_000, 6_000_000, 50_000)
        )
        add_label("account_takeover", "ESCALATE", oid, cid, "NEVER_ARRIVED",
                  "Order never showed up, please refund to my new payment method.", amount)


def p_reason_mismatch() -> None:
    """'Defective' where the evidence shows a pristine item. Needs the vision path."""
    for _ in range(PER_PATTERN):
        cid, _ = add_customer(tenure_days=rng.randint(100, 800))
        oid, _s, amount, _d = delivered_order(cid, days_ago=rng.randint(4, 12))
        add_label("reason_evidence_mismatch", "ESCALATE", oid, cid, "DEFECTIVE",
                  "Completely broken on arrival, unusable.", amount)


def p_defect_batch() -> None:
    """Many customers, one SKU, one defect window — genuinely the seller's fault."""
    batch_skus = rng.sample(SKUS, 3)
    window_start = NOW - timedelta(days=25)
    for sku in batch_skus:
        for _ in range(12):
            defects.append((sku[0], window_start + timedelta(days=rng.randint(0, 18)), "QC-SEAL-FAIL"))
    for n in range(PER_PATTERN):
        sku = batch_skus[n % len(batch_skus)]
        cid, _ = add_customer(tenure_days=rng.randint(200, 1200))
        for _ in range(rng.randint(2, 6)):
            delivered_order(cid, days_ago=rng.randint(100, 800))
        oid, _s, amount, _d = delivered_order(cid, days_ago=rng.randint(3, 20), sku=sku)
        add_label("genuine_defect_batch", "AUTO_APPROVE", oid, cid, "DEFECTIVE",
                  "Seal failed and the contents leaked, same as the reviews describe.", amount)


PATTERNS = [
    p_clean,
    p_wardrobing,
    p_serial_returner,
    p_never_arrived,
    p_empty_box,
    p_account_takeover,
    p_reason_mismatch,
    p_defect_batch,
]


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------
def write() -> None:
    with psycopg.connect(DSN, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE commerce.customers CASCADE")
        cur.execute("TRUNCATE commerce.defect_reports")
        cur.executemany(
            "INSERT INTO commerce.customers"
            " (id,email,name,tier,created_at,address_changed_at,payment_changed_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            customers,
        )
        cur.executemany(
            "INSERT INTO commerce.orders (id,customer_id,placed_at,total_amount,currency,status)"
            " VALUES (%s,%s,%s,%s,%s,%s)",
            orders,
        )
        cur.executemany(
            "INSERT INTO commerce.order_items"
            " (order_id,sku,category,title,qty,unit_price,weight_grams)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            items,
        )
        cur.executemany(
            "INSERT INTO commerce.shipments (id,order_id,carrier,tracking_no,shipped_at,"
            "delivered_at,delivery_confirmed,signature_captured,shipped_weight_grams,"
            "returned_weight_grams) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            shipments,
        )
        cur.executemany(
            "INSERT INTO commerce.payments (id,order_id,method,status,captured_at,amount,psp_ref)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            payments,
        )
        cur.executemany(
            "INSERT INTO commerce.returns"
            " (id,order_id,customer_id,reason_code,requested_at,resolution,amount)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            returns,
        )
        cur.executemany(
            "INSERT INTO commerce.defect_reports (sku,reported_at,defect_code) VALUES (%s,%s,%s)",
            defects,
        )
        cur.executemany(
            "INSERT INTO seed_labels"
            " (pattern,expected_outcome,order_id,customer_id,reason_code,description,amount)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            labels,
        )
        conn.commit()


def main() -> None:
    build_base()
    for p in PATTERNS:
        p()
    write()
    by_pattern: dict[str, int] = {}
    for row in labels:
        by_pattern[row[0]] = by_pattern.get(row[0], 0) + 1
    print(
        f"customers={len(customers)} orders={len(orders)} shipments={len(shipments)} "
        f"payments={len(payments)} prior_returns={len(returns)} "
        f"defect_reports={len(defects)} labels={len(labels)}"
    )
    for k in sorted(by_pattern):
        print(f"  {k:<28} {by_pattern[k]}")


if __name__ == "__main__":
    main()
