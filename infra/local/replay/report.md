# Replay report — 2026-09-30 16:36 UTC

**73 of 200** corpus cases measured (4 unmeasurable — fleet failures, excluded below). The gate is evaluated as if every segment were at `auto`.

- **False approves: 0**
- False escalations: 18/25 (72%) of the cases labelled AUTO_APPROVE
- Gate agreement with the label: 55/73 (75%)
- Agent (shadow) agreement with the label: 71/73 (97%)
- Agent agreement with reviewers' actual decisions: 6/9 (67%)

| Pattern | Label | n | Gate allowed | Agent approved | Critic dissented (of approvals) | Top binding rule |
|---|---|---|---|---|---|---|
| `account_takeover` | ESCALATE | 25 | 0/25 (0%) | 0/25 (0%) | — | `agent_recommends_review` 20 |
| `clean_legitimate` | AUTO_APPROVE | 25 | 7/25 (28%) | 23/25 (92%) | 15/23 (65%) | `critic_dissent` 15 |
| `empty_box` | ESCALATE | 23 | 0/23 (0%) | 0/23 (0%) | — | `agent_recommends_review` 23 |

## Latency

Per case: p50 **18s**, p95 **87s**, max 107s.

| Node | p50 s | p95 s | max s |
|---|---|---|---|
| `safety_guard` | 0.2 | 37.1 | 56.0 |
| `evidence_collector` | 8.5 | 14.2 | 47.5 |
| `behavior_analyst` | 3.9 | 11.5 | 54.6 |
| `decision_proposer` | 2.2 | 3.2 | 22.2 |
| `critic` | 0.0 | 2.8 | 3.1 |
| `policy_retriever` | 0.8 | 1.3 | 2.5 |
| `reason_classifier` | 0.4 | 0.6 | 1.4 |
| `image_analyst` | 0.1 | 0.1 | 0.2 |
| `freeze_bundle` | 0.0 | 0.1 | 0.4 |
| `citation_check` | 0.0 | 0.0 | 0.0 |
| `injection_guard` | 0.0 | 0.0 | 0.0 |
| `route_specialists` | 0.0 | 0.0 | 0.0 |
| `completeness_check` | 0.0 | 0.0 | 0.0 |

## Promotion readiness

A segment earns `auto` on ≥200 measured cases at ≥95% gate agreement and no false approve (PLANNING.md §10.2).

| Segment | n | Gate agreement | False approves | Verdict |
|---|---|---|---|---|
| DAMAGED / plus | 12 | 3/12 (25%) | 0 | needs 188 more cases |
| DAMAGED / vip | 13 | 4/13 (31%) | 0 | needs 187 more cases |
| MISSING_PARTS / plus | 3 | 3/3 (100%) | 0 | needs 197 more cases |
| MISSING_PARTS / standard | 16 | 16/16 (100%) | 0 | needs 184 more cases |
| MISSING_PARTS / vip | 4 | 4/4 (100%) | 0 | needs 196 more cases |
| NEVER_ARRIVED / plus | 5 | 5/5 (100%) | 0 | needs 195 more cases |
| NEVER_ARRIVED / standard | 16 | 16/16 (100%) | 0 | needs 184 more cases |
| NEVER_ARRIVED / vip | 4 | 4/4 (100%) | 0 | needs 196 more cases |
