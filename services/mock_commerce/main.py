"""The mocked upstream commerce platform (PLANNING.md §6.4).

A hard trust boundary. It reads only the `commerce` schema, knows nothing about
cases, evidence or decisions, and swapping it for a real commerce API must
require no change to the agent. Deliberately has no dependency on
`packages.contracts` — the upstream does not share our types.
"""

import os

import psycopg
from fastapi import FastAPI, HTTPException
from psycopg.rows import dict_row

DSN = os.environ["DATABASE_URL"]

app = FastAPI(title="mock-commerce", version="0.1.0")


def q(sql: str, params: tuple, one: bool = False):
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        rows = conn.execute(sql, params).fetchall()
    if one:
        if not rows:
            raise HTTPException(404, "not found")
        return rows[0]
    return rows


@app.get("/health")
def health():
    q("SELECT 1", ())
    return {"status": "ok"}


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    order = q("SELECT * FROM commerce.orders WHERE id = %s", (order_id,), one=True)
    order["items"] = q(
        "SELECT sku, category, title, qty, unit_price, weight_grams"
        " FROM commerce.order_items WHERE order_id = %s ORDER BY id",
        (order_id,),
    )
    return order


@app.get("/orders/{order_id}/shipments")
def get_shipments(order_id: str):
    return q("SELECT * FROM commerce.shipments WHERE order_id = %s ORDER BY id", (order_id,))


@app.get("/customers/{customer_id}")
def get_customer(customer_id: str):
    return q("SELECT * FROM commerce.customers WHERE id = %s", (customer_id,), one=True)


@app.get("/customers/{customer_id}/returns")
def get_returns(customer_id: str):
    return q(
        "SELECT * FROM commerce.returns WHERE customer_id = %s ORDER BY requested_at DESC",
        (customer_id,),
    )


@app.get("/customers/{customer_id}/cohort-stats")
def get_cohort_stats(customer_id: str):
    """Return rate for this customer against its tier cohort.

    Reports both a z-score and a percentile rank, on purpose. Serial returners
    are part of their own cohort, so they inflate the standard deviation they
    are measured against — a z-score caps out near 1/sqrt(fraction of cohort
    that is anomalous) no matter how large the population grows. The percentile
    rank has no such degenerate case, and is the signal to reason over.
    """
    return q(
        """
        WITH per_customer AS (
            SELECT c.id, c.tier,
                   COUNT(DISTINCT o.id)                                  AS order_count,
                   COUNT(DISTINCT r.id)                                  AS return_count,
                   COUNT(DISTINCT r.id)::float
                       / GREATEST(COUNT(DISTINCT o.id), 1)               AS return_rate
            FROM commerce.customers c
            LEFT JOIN commerce.orders  o ON o.customer_id  = c.id
            LEFT JOIN commerce.returns r ON r.customer_id = c.id
            GROUP BY c.id, c.tier
        ),
        me AS (SELECT * FROM per_customer WHERE id = %s),
        cohort AS (
            SELECT AVG(return_rate)                  AS mean,
                   COALESCE(STDDEV_POP(return_rate), 0) AS sd,
                   COUNT(*)                          AS n,
                   -- strictly-less, i.e. PERCENT_RANK semantics: everyone with
                   -- no returns lands at 0.0 rather than at the tie ceiling
                   AVG(CASE WHEN return_rate < (SELECT return_rate FROM me)
                            THEN 1.0 ELSE 0.0 END)   AS pct
            FROM per_customer
            WHERE tier = (SELECT tier FROM me)
        )
        SELECT me.id AS customer_id, me.tier, me.order_count, me.return_count,
               ROUND(me.return_rate::numeric, 4)  AS return_rate,
               ROUND(cohort.mean::numeric, 4)     AS cohort_mean_return_rate,
               ROUND(cohort.sd::numeric, 4)       AS cohort_stddev,
               cohort.n                           AS cohort_size,
               CASE WHEN cohort.sd = 0 THEN 0
                    ELSE ROUND(((me.return_rate - cohort.mean) / cohort.sd)::numeric, 2)
               END                                AS sigma_above_cohort,
               ROUND(cohort.pct::numeric, 4)      AS cohort_percentile
        FROM me, cohort
        """,
        (customer_id,),
        one=True,
    )


@app.get("/payments/{order_id}")
def get_payment_status(order_id: str):
    return q("SELECT * FROM commerce.payments WHERE order_id = %s", (order_id,), one=True)


@app.get("/skus/{sku}/defect-reports")
def get_defect_reports(sku: str):
    """Supports the genuine-defect-batch pattern: same SKU, same defect window."""
    return q(
        "SELECT defect_code, reported_at FROM commerce.defect_reports"
        " WHERE sku = %s ORDER BY reported_at",
        (sku,),
    )
