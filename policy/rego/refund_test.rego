package refund_test

import rego.v1

import data.refund

# A case every rule passes. Each test below changes one thing about it.
clean := {
	"evidence_completeness": "complete",
	"bundle_frozen": true,
	"bundle_truncated": false,
	"citations_verified": true,
	"injection_flagged": false,
	"safety_flagged": false,
	"reason_mismatch": false,
	"recommendation": {"outcome": "APPROVE"},
	"refs_outside_bundle": [],
	"critic_reviewed": true,
	"critic_independent": true,
	"critic_dissents": false,
	"unsupported_claims": [],
	"reason_code": "DAMAGED",
	"amount": "310000.00",
	"customer_tier": "standard",
	"autonomy_mode": "auto",
	"kill_switch_active": false,
	"facts": {
		"get_order": {
			"placed_at": "2026-09-12T00:00:00+00:00",
			"items": [{"category": "home"}],
		},
		"get_payment_status": {"status": "captured"},
		"get_cohort_stats": {"cohort_percentile": 0.0},
		"get_customer": {"address_changed_at": null, "payment_changed_at": null},
	},
}

binds(rule, patch) if {
	d := refund.decision with input as object.union(clean, patch)
	d.rule_id == rule
	d.result == "ESCALATE"
}

test_clean_case_is_allowed if {
	d := refund.decision with input as clean
	d == {"result": "ALLOW_AUTO", "rule_id": "allow_auto", "policy_version": refund.policy_version}
}

test_empty_input_escalates if {
	d := refund.decision with input as {}
	d.result == "ESCALATE"
}

test_missing_signal_escalates if {
	d := refund.decision with input as object.remove(clean, ["kill_switch_active"])
	d.rule_id == "kill_switch_active"
}

test_missing_fact_escalates if {
	d := refund.decision with input as json.remove(clean, ["facts/get_cohort_stats"])
	d.rule_id == "high_return_rate"
}

test_null_fact_escalates if binds("high_return_rate", {"facts": {"get_cohort_stats": {"cohort_percentile": null}}})

# The evidence's own integrity.
test_incomplete if binds("incomplete_evidence", {"evidence_completeness": "incomplete"})

test_not_frozen if binds("bundle_not_frozen", {"bundle_frozen": false})

test_truncated if binds("bundle_truncated", {"bundle_truncated": true})

test_unverified if binds("unverifiable_citation", {"citations_verified": false})

test_injection if binds("untrusted_input_flagged", {"injection_flagged": true})

test_safety if binds("abusive_content_flagged", {"safety_flagged": true})

test_mismatch if binds("reason_mismatch", {"reason_mismatch": true})

# The agent and its critic.
test_no_recommendation if binds("no_recommendation", {"recommendation": null})

test_outside_bundle if binds("citation_outside_bundle", {"refs_outside_bundle": ["abc"]})

test_agent_escalates if binds("agent_recommends_review", {"recommendation": {"outcome": "ESCALATE"}})

test_critic_unavailable if binds("critic_unavailable", {"critic_reviewed": false})

test_critic_not_independent if binds("critic_not_independent", {"critic_independent": false})

test_critic_dissents if binds("critic_dissent", {"critic_dissents": true})

test_critic_incoherent if binds("critic_dissent", {"unsupported_claims": ["never returned anything"]})

# Policy clauses.
test_6_3 if binds("missing_contents", {"reason_code": "MISSING_PARTS"})

test_7_3 if binds("payment_not_captured", {"facts": {"get_payment_status": {"status": "reversed"}}})

test_9_3 if binds("amount_over_limit", {"amount": "5000000.01"})

test_9_3_boundary_is_allowed if {
	d := refund.decision with input as object.union(clean, {"amount": "5000000.00"})
	d.result == "ALLOW_AUTO"
}

test_9_1 if binds("high_return_rate", {"facts": {"get_cohort_stats": {"cohort_percentile": 0.93}}})

test_9_4 if binds("single_use_goods", {
	"reason_code": "DIDNT_FIT",
	"facts": {"get_order": {"items": [{"category": "footwear-formal"}]}},
})

test_9_4_needs_both if {
	d := refund.decision with input as object.union(clean, {"reason_code": "DIDNT_FIT"})
	d.result == "ALLOW_AUTO"
}

test_10_1 if binds("account_changes", {"facts": {"get_customer": {
	"address_changed_at": "2026-09-01T00:00:00+00:00",
	"payment_changed_at": "2026-09-11T12:00:00+00:00",
}}})

test_10_1_needs_both if {
	d := refund.decision with input as object.union(clean, {"facts": {"get_customer": {
		"address_changed_at": "2026-09-01T00:00:00+00:00",
		"payment_changed_at": null,
	}}})
	d.result == "ALLOW_AUTO"
}

test_10_1_old_changes_are_allowed if {
	d := refund.decision with input as object.union(clean, {"facts": {"get_customer": {
		"address_changed_at": "2026-01-01T00:00:00+00:00",
		"payment_changed_at": "2026-01-02T00:00:00+00:00",
	}}})
	d.result == "ALLOW_AUTO"
}

# The ladder and the switch.
test_kill_switch if binds("kill_switch_active", {"kill_switch_active": true})

test_shadow if binds("autonomy_not_auto", {"autonomy_mode": "shadow"})

test_suggest if binds("autonomy_not_auto", {"autonomy_mode": "suggest"})

test_assist if binds("autonomy_not_auto", {"autonomy_mode": "assist"})

# A substantive rule outranks the switch, so the audit row says why the case
# would have escalated anyway rather than blaming the switch.
test_substance_outranks_switch if binds("critic_dissent", {"critic_dissents": true, "kill_switch_active": true})
