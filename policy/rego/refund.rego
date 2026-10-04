package refund

import rego.v1

# The policy gate (L5): the only terminal routing decision in the system.
# Everything upstream — guards, the agent, the critic — is an input here.
#
# Two properties hold the shape together:
#
# 1. **It can only widen from false.** There is no rule that produces a denial,
#    because denial is not the agent's to make. The worst this file can do is
#    send a case to a person.
# 2. **Absence escalates.** Every rule is written as `not <what must be true to
#    pass>`, so a signal that is missing from the input — a field dropped by a
#    refactor, a fact the collector never fetched — binds a rule rather than
#    slipping past it. `opa test` checks that an empty input escalates.
#
# The first rule that holds binds, and its name is `rule_id`. The order is
# deliberate: the evidence's own integrity, then the agent and its critic, then
# the policy clauses the agent was measured getting wrong at L4, then the
# autonomy ladder. So a rule_id of `kill_switch_active` or `autonomy_not_auto`
# means "everything else passed; this would otherwise have been automatic" —
# which is exactly the count shadow mode is there to produce.
#
# Policy facts are read from the frozen bundle (`input.facts`, keyed by the
# tool that returned them), never from free text or the agent's rationale.

policy_version := "refund-policy-v1+gate-2026-09-27"

decision := {
	"result": result,
	"rule_id": rule_id,
	"policy_version": policy_version,
}

default allow_auto := false

allow_auto if rule_id == "allow_auto"

result := "ALLOW_AUTO" if allow_auto

else := "ESCALATE"

# --- the binding rule, first match wins ------------------------------------

rule_id := "incomplete_evidence" if {
	not input.evidence_completeness == "complete"
} else := "bundle_not_frozen" if {
	not input.bundle_frozen == true
} else := "bundle_truncated" if {
	not input.bundle_truncated == false
} else := "unverifiable_citation" if {
	not input.citations_verified == true
} else := "untrusted_input_flagged" if {
	not input.injection_flagged == false
} else := "abusive_content_flagged" if {
	not input.safety_flagged == false
} else := "reason_mismatch" if {
	not input.reason_mismatch == false
} else := "no_recommendation" if {
	not input.recommendation.outcome
} else := "citation_outside_bundle" if {
	not count(input.refs_outside_bundle) == 0
} else := "agent_recommends_review" if {
	not input.recommendation.outcome == "APPROVE"
} else := "critic_unavailable" if {
	not input.critic_reviewed == true
} else := "critic_not_independent" if {
	not input.critic_independent == true
} else := "critic_dissent" if {
	# Either signal counts: a critic that lists an unsupported claim and says it
	# does not dissent is incoherent, and incoherence resolves to a person.
	not critic_agrees
} else := "missing_contents" if {
	# 6.3 — an empty parcel is always reviewed by a person.
	input.reason_code == "MISSING_PARTS"
} else := "payment_not_captured" if {
	# 7.3 — no money back against a payment that was never taken or was reversed.
	not input.facts.get_payment_status.status == "captured"
} else := "amount_over_limit" if {
	# 9.3 — above 5,000,000 needs a person, however strong the evidence.
	not amount_within_limit
} else := "high_return_rate" if {
	# 9.1 — top tenth of the customer's own tier.
	not return_rate_ordinary
} else := "single_use_goods" if {
	# 9.4 — see `single_use` below for what is and is not measured.
	single_use
} else := "account_changes" if {
	# 10.1 — address and payment instrument both changed near the order.
	account_recently_changed
} else := "kill_switch_active" if {
	not input.kill_switch_active == false
} else := "autonomy_not_auto" if {
	not input.autonomy_mode == "auto"
} else := "allow_auto"

# --- helpers ---------------------------------------------------------------

critic_agrees if {
	input.critic_dissents == false
	count(input.unsupported_claims) == 0
}

amount_within_limit if {
	is_string(input.amount) # pydantic serialises Decimal as a string
	to_number(input.amount) <= 5000000
}

return_rate_ordinary if {
	p := input.facts.get_cohort_stats.cohort_percentile
	is_number(p)
	p < 0.9
}

# 9.4 is about signs of use, which only a person handling the goods (or a
# vision model this fleet does not have — P3) could see. What the bundle does
# carry is the pattern: a fit / change-of-mind claim on event-adjacent goods.
# Timing (a return 3-6 days after delivery) is not used; the category and reason
# already separate it in the seeded corpus, and a rule that can only send a case
# to a person may be broad.
single_use if {
	input.reason_code in {"DIDNT_FIT", "CHANGED_MIND"}
	some item in input.facts.get_order.items
	item.category in {"apparel-formal", "footwear-formal"}
}

account_recently_changed if {
	placed := time.parse_rfc3339_ns(input.facts.get_order.placed_at)
	near(input.facts.get_customer.address_changed_at, placed)
	near(input.facts.get_customer.payment_changed_at, placed)
}

near(ts, ref) if abs(time.parse_rfc3339_ns(ts) - ref) <= ((30 * 24) * 3600) * 1000000000
