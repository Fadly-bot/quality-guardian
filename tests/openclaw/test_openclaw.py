import pytest

from openclaw.orchestrator import OpenClaw
from openclaw.workflow import (
    ABORTED,
    COUNCIL_REVIEW,
    DEPLOY,
    DEPLOYMENT_CHECK,
    ESCALATED,
    HANDOFF_CHECKPOINT,
    HUMAN_APPROVAL,
    HUMAN_RELEASE_APPROVAL,
    IMPLEMENTATION,
    PLANNING,
    POST_DEPLOY_VERIFY,
    PRODUCTION,
    PROPOSAL,
    QUALITY_AUDIT,
    RESEARCH,
    ROLLED_BACK,
    SECURITY_AUDIT,
    STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    WorkflowStateError,
    can_transition,
    owner_of,
    valid_transitions,
)


def test_all_required_states_present():
    required = {
        "PROPOSAL",
        "RESEARCH",
        "COUNCIL_REVIEW",
        "HUMAN_APPROVAL",
        "PLANNING",
        "IMPLEMENTATION",
        "HANDOFF_CHECKPOINT",
        "QUALITY_AUDIT",
        "SECURITY_AUDIT",
        "DEPLOYMENT_CHECK",
        "HUMAN_RELEASE_APPROVAL",
        "DEPLOY",
        "POST_DEPLOY_VERIFY",
        "PRODUCTION",
    }
    assert required.issubset(STATES)


def test_full_forward_chain_valid():
    chain = [
        PROPOSAL,
        RESEARCH,
        COUNCIL_REVIEW,
        HUMAN_APPROVAL,
        PLANNING,
        IMPLEMENTATION,
        HANDOFF_CHECKPOINT,
        QUALITY_AUDIT,
        SECURITY_AUDIT,
        DEPLOYMENT_CHECK,
        HUMAN_RELEASE_APPROVAL,
        DEPLOY,
        POST_DEPLOY_VERIFY,
        PRODUCTION,
    ]
    for current, target in zip(chain, chain[1:]):
        assert can_transition(current, target), f"{current} -> {target}"


def test_proposal_to_production_rejected():
    assert not can_transition(PROPOSAL, PRODUCTION)
    assert not can_transition(PROPOSAL, DEPLOY)
    assert not can_transition(IMPLEMENTATION, PRODUCTION)


def test_terminal_states_have_no_successors():
    for state in TERMINAL_STATES:
        assert valid_transitions(state) == frozenset()


def test_no_skip_human_gate():
    assert not can_transition(PLANNING, DEPLOY)
    assert not can_transition(IMPLEMENTATION, PRODUCTION)
    assert not can_transition(DEPLOY, PRODUCTION)
    assert not can_transition(DEPLOYMENT_CHECK, DEPLOY)


def test_every_state_defines_an_owner():
    for state in STATES:
        assert owner_of(state), state


def test_all_transitions_reference_known_states():
    for current, targets in TRANSITIONS.items():
        assert current in STATES
        for target in targets:
            assert target in STATES


# --- orchestrator routing --------------------------------------------------


def test_quality_pass_routes_forward_to_security():
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    oc.route("PASS", actor="quality_guardian")
    assert oc.state == SECURITY_AUDIT


def test_security_pass_routes_to_deployment_check():
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    oc.route("PASS", actor="security_gate")
    assert oc.state == DEPLOYMENT_CHECK


def test_deployment_check_pass_routes_to_human_release_gate():
    oc = OpenClaw(initial_state=DEPLOYMENT_CHECK)
    oc.route("PASS", actor="deployment_check")
    assert oc.state == HUMAN_RELEASE_APPROVAL


def test_quality_fail_routes_to_repair():
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    oc.route("FAIL", actor="quality_guardian")
    assert oc.state == IMPLEMENTATION
    assert oc.repair_count == 1


def test_security_fail_routes_to_repair():
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    oc.route("FAIL", actor="security_gate")
    assert oc.state == IMPLEMENTATION
    assert oc.repair_count == 1


def test_handoff_reject_routes_to_repair():
    oc = OpenClaw(initial_state=HANDOFF_CHECKPOINT)
    oc.route("REJECTED", actor="handoff_agent")
    assert oc.state == IMPLEMENTATION
    assert oc.repair_count == 1


def test_security_block_escalates():
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    oc.route("BLOCK", actor="security_gate")
    assert oc.state == ESCALATED


def test_quality_needs_review_escalates():
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    oc.route("NEEDS_REVIEW", actor="quality_guardian")
    assert oc.state == ESCALATED


def test_not_scanned_is_never_pass():
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    oc.route("NOT_SCANNED", actor="quality_guardian")
    assert oc.state == ESCALATED
    assert oc.state != SECURITY_AUDIT


def test_security_not_scanned_is_never_pass():
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    oc.route("NOT_SCANNED", actor="security_gate")
    assert oc.state == ESCALATED
    assert oc.state != DEPLOYMENT_CHECK


def test_unknown_verdict_rejected():
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    with pytest.raises(Exception):
        oc.route("MAYBE", actor="quality_guardian")


# --- human approval --------------------------------------------------------


def test_approval_required_before_continue():
    oc = OpenClaw(initial_state=HUMAN_APPROVAL)
    with pytest.raises(Exception):
        oc.continue_after_human_gate()


def test_approval_request_and_continue():
    oc = OpenClaw(initial_state=HUMAN_APPROVAL)
    request = oc.request_human_approval("HUMAN_APPROVAL")
    assert request.startswith("HUMAN_APPROVAL-")
    oc.record_human_approval("HUMAN_APPROVAL", approved=True, approver="human")
    assert oc.continue_after_human_gate(actor="human") == PLANNING


def test_rejected_approval_aborts():
    oc = OpenClaw(initial_state=HUMAN_APPROVAL)
    oc.request_human_approval("HUMAN_APPROVAL")
    oc.record_human_approval("HUMAN_APPROVAL", approved=False, approver="human")
    assert oc.continue_after_human_gate(actor="human") == ABORTED


def test_openclaw_cannot_approve_for_human():
    oc = OpenClaw(initial_state=HUMAN_APPROVAL)
    oc.request_human_approval("HUMAN_APPROVAL")
    with pytest.raises(Exception):
        oc.record_human_approval("HUMAN_APPROVAL", approved=True, approver="openclaw")


def test_request_approval_outside_gate_rejected():
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    with pytest.raises(Exception):
        oc.request_human_approval("HUMAN_APPROVAL")


def test_duplicate_approval_record_without_request_rejected():
    oc = OpenClaw(initial_state=HUMAN_APPROVAL)
    with pytest.raises(Exception):
        oc.record_human_approval(
            "HUMAN_APPROVAL", approved=True, approver="human"
        )


def test_release_approval_gate_not_skipped():
    oc = OpenClaw(initial_state=HUMAN_RELEASE_APPROVAL)
    with pytest.raises(Exception):
        oc.continue_after_human_gate()
    oc.request_human_approval("HUMAN_RELEASE_APPROVAL")
    oc.record_human_approval(
        "HUMAN_RELEASE_APPROVAL", approved=True, approver="human"
    )
    assert oc.continue_after_human_gate(actor="human") == DEPLOY


# --- retry bounds ----------------------------------------------------------


def test_repair_bound_escalates():
    oc = OpenClaw(initial_state=QUALITY_AUDIT, max_repair_iterations=2)
    oc.route("FAIL", actor="quality_guardian")  # repair 1
    assert oc.state == IMPLEMENTATION
    oc.advance(HANDOFF_CHECKPOINT, actor="coding_agent")
    oc.route("ACCEPTED", actor="handoff_agent")
    oc.route("PASS", actor="quality_guardian")
    oc.route("FAIL", actor="security_gate")  # repair 2
    oc.advance(HANDOFF_CHECKPOINT, actor="coding_agent")
    oc.route("ACCEPTED", actor="handoff_agent")
    oc.route("FAIL", actor="quality_guardian")  # bound reached -> escalate
    assert oc.state == ESCALATED
    assert oc.repair_count == 2


def test_repair_counter_shared_across_gates():
    oc = OpenClaw(initial_state=HANDOFF_CHECKPOINT, max_repair_iterations=3)
    oc.route("REJECTED", actor="handoff_agent")  # repair 1
    oc.advance(HANDOFF_CHECKPOINT, actor="coding_agent")
    oc.route("ACCEPTED", actor="handoff_agent")
    oc.route("FAIL", actor="quality_guardian")  # repair 2
    oc.advance(HANDOFF_CHECKPOINT, actor="coding_agent")
    oc.route("ACCEPTED", actor="handoff_agent")
    oc.route("PASS", actor="quality_guardian")
    oc.route("FAIL", actor="security_gate")  # repair 3 (bound reached)
    assert oc.state == IMPLEMENTATION
    assert oc.repair_count == 3
    oc.advance(HANDOFF_CHECKPOINT, actor="coding_agent")
    oc.route("ACCEPTED", actor="handoff_agent")
    oc.route("PASS", actor="quality_guardian")
    oc.route("FAIL", actor="security_gate")  # bound exceeded -> escalate
    assert oc.state == ESCALATED


def test_repair_is_bounded_by_default():
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    assert oc.max_repair_iterations == 5


# --- permission ------------------------------------------------------------


def test_actor_without_permission_rejected():
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    with pytest.raises(Exception):
        oc.route("PASS", actor="coding_agent")


def test_advance_by_non_owner_rejected():
    oc = OpenClaw(initial_state=PROPOSAL)
    with pytest.raises(Exception):
        oc.advance(RESEARCH, actor="coding_agent")


def test_rollback_recovery_is_human_only():
    oc = OpenClaw(initial_state=ROLLED_BACK)
    with pytest.raises(Exception):
        oc.route("REPAIR", actor="deployment_check")


def test_rollback_recovery_by_human():
    oc = OpenClaw(initial_state=ROLLED_BACK)
    assert oc.route("REPAIR", actor="human") == IMPLEMENTATION


# --- deploy / post-deploy --------------------------------------------------


def test_deploy_success_to_verify():
    oc = OpenClaw(initial_state=DEPLOY)
    oc.route("SUCCESS", actor="deployment_check")
    assert oc.state == POST_DEPLOY_VERIFY


def test_deploy_failure_rolls_back():
    oc = OpenClaw(initial_state=DEPLOY)
    oc.route("FAILURE", actor="deployment_check")
    assert oc.state == ROLLED_BACK


def test_post_deploy_pass_reaches_production():
    oc = OpenClaw(initial_state=POST_DEPLOY_VERIFY)
    oc.route("PASS", actor="deployment_check")
    assert oc.state == PRODUCTION


def test_post_deploy_fail_rolls_back():
    oc = OpenClaw(initial_state=POST_DEPLOY_VERIFY)
    oc.route("FAIL", actor="deployment_check")
    assert oc.state == ROLLED_BACK


# --- invalid transition ----------------------------------------------------


def test_invalid_orchestrator_transition_rejected():
    oc = OpenClaw(initial_state=PROPOSAL)
    with pytest.raises(WorkflowStateError):
        oc.advance(PRODUCTION, actor="ai_council")


def test_escaped_terminal_is_final():
    assert valid_transitions(ESCALATED) == frozenset()


# --- audit / events --------------------------------------------------------


def test_transitions_recorded_in_audit_log():
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    oc.route("PASS", actor="quality_guardian")
    assert len(oc.audit.entries) == 1
    entry = oc.audit.entries[0]
    assert entry.event == "ROUTED"
    assert entry.state_before == QUALITY_AUDIT
    assert entry.state_after == SECURITY_AUDIT
    assert entry.actor == "quality_guardian"


def test_approval_events_recorded():
    oc = OpenClaw(initial_state=HUMAN_APPROVAL)
    oc.request_human_approval("HUMAN_APPROVAL")
    oc.record_human_approval("HUMAN_APPROVAL", approved=True, approver="human")
    oc.continue_after_human_gate(actor="human")
    events = [entry.event for entry in oc.audit.entries]
    assert "APPROVAL_REQUESTED" in events
    assert "APPROVAL_RECORDED" in events
    assert "TRANSITION" in events


def test_full_pipeline_produces_audit_trail():
    oc = OpenClaw(initial_state=PROPOSAL)
    oc.advance(RESEARCH, actor="ai_council")
    oc.advance(COUNCIL_REVIEW, actor="ai_council")
    oc.advance(HUMAN_APPROVAL, actor="ai_council")
    oc.request_human_approval("HUMAN_APPROVAL")
    oc.record_human_approval("HUMAN_APPROVAL", approved=True, approver="human")
    oc.continue_after_human_gate(actor="human")
    oc.advance(IMPLEMENTATION, actor="project_council")
    oc.advance(HANDOFF_CHECKPOINT, actor="coding_agent")
    oc.route("ACCEPTED", actor="handoff_agent")
    oc.route("PASS", actor="quality_guardian")
    oc.route("PASS", actor="security_gate")
    oc.route("PASS", actor="deployment_check")
    oc.request_human_approval("HUMAN_RELEASE_APPROVAL")
    oc.record_human_approval(
        "HUMAN_RELEASE_APPROVAL", approved=True, approver="human"
    )
    oc.continue_after_human_gate(actor="human")
    oc.route("SUCCESS", actor="deployment_check")
    oc.route("PASS", actor="deployment_check")
    assert oc.state == PRODUCTION
    events = [entry.event for entry in oc.audit.entries]
    assert "APPROVAL_REQUESTED" in events
    assert "APPROVAL_RECORDED" in events
    assert "TRANSITION" in events
    assert "ROUTED" in events


def test_openclaw_does_not_change_verdict():
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    oc.route("BLOCK", actor="security_gate")
    assert oc.last_verdict == "BLOCK"
    assert oc.state == ESCALATED
    assert not hasattr(oc, "set_verdict")


def test_failure_detection_and_state_preservation():
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    with pytest.raises(Exception):
        oc.route("BLOCK", actor="coding_agent")
    assert oc.state == SECURITY_AUDIT


def test_council_review_go_route():
    assert can_transition(COUNCIL_REVIEW, HUMAN_APPROVAL)
    oc = OpenClaw(initial_state=COUNCIL_REVIEW)
    oc.advance(HUMAN_APPROVAL, actor="ai_council")
    assert oc.state == HUMAN_APPROVAL