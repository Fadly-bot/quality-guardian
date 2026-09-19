"""Phase 13 — Operational Integration Tests (Development F).

Verifies that the existing Phase 1–12 components operate as ONE controlled,
auditable workflow. Scenarios A–J are driven through the REAL components:

- openclaw.workflow (state machine, owners, transition validation)
- openclaw.orchestrator.OpenClaw (routing, bounded repair, human gates, audit)
- openclaw.registry.AgentRegistry (identity/capability/permission/availability)
- openclaw.handoff_integration.HandoffClient (checkpoint validation + git truth)
- openclaw.quality_integration.QualityGuardianClient (audit validation + routing)
- openclaw.security_gate.SecurityGate / SecurityGateClient (real checks)
- openclaw.deployment_gate.DeploymentGate / DeploymentGateClient (preflight)

Evidence model (identical to Phase 11): state-machine transitions, validation,
routing decisions, gate decisions, and audit logging are REAL; the external
agent invocation boundaries (Handoff Agent, Quality Guardian) are exercised
through their established client seams with controlled delegates, so those
points are SIMULATION. Rollback is verified as routing/plan only — never
executed and never claimed as a production verification.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from openclaw.deployment_gate import (
    DeploymentGate,
    DeploymentGateClient,
    DeploymentGateSeparationError,
)
from openclaw.handoff_integration import (
    GitStateConflict,
    HandoffClient,
    HandoffSeparationError,
)
from openclaw.orchestrator import (
    HumanApprovalRequired,
    OpenClaw,
    PermissionDenied,
    RoutingError,
)
from openclaw.quality_integration import (
    QualityGuardianClient,
    SecurityGateError as QualitySecuritySeparationError,
)
from openclaw.registry import (
    AgentRegistry,
    AvailabilityError,
    CapabilityError,
    ContractVersionError,
    InvocationError,
    PermissionError_Registry,
    UnknownAgent,
)
from openclaw.security_gate import (
    ScannerRun,
    SecurityCheck,
    SecurityGate,
    SecurityGateClient,
    SecurityGateResult,
)
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
    can_transition,
    validate_transition,
)

INTEGRATED_AGENTS = (
    "handoff_agent",
    "quality_guardian",
    "security_gate",
    "deployment_check",
    "openclaw",
)


# --- helpers ---------------------------------------------------------------


def _clean_run(tool: str, root) -> ScannerRun:
    return ScannerRun(tool=tool, target=str(root), found=0)


def _quality_raw(status: str, decision: str) -> dict:
    return {
        "project": "fixture",
        "status": status,
        "decision": decision,
        "scanner_statuses": {"pytest": "PASS"},
        "evidence": [{"source": "pytest", "target": "tests/", "detail": "0 failed"}],
        "findings": [],
        "risks": [],
    }


def _security_result(*statuses: str, decision: str | None = None) -> SecurityGateResult:
    checks = [
        SecurityCheck(check_id=f"c{i}", category="repository", description="d", status=s)
        for i, s in enumerate(statuses)
    ]
    return SecurityGateResult(
        project="fixture",
        checks=checks,
        decision=decision if decision is not None else "PASS",
    )


def _make_fixture_repo(tmp_path: Path) -> Path:
    """A minimal committed repository that passes every built-in gate check."""
    repo = tmp_path
    (repo / ".gitignore").write_text(
        ".env\n.env.*\n*.env\n*.pem\n*.key\nid_rsa\nid_dsa\n__pycache__/\n"
    )
    (repo / "app.py").write_text("print('ok')\n")
    (repo / "openclaw").mkdir()
    (repo / "openclaw" / "orchestrator.py").write_text(
        "_assert_actor\napprover != HUMAN_ROLE\n"
    )
    (repo / "openclaw" / "agent_registry.json").write_text(json.dumps({
        "agents": {
            "openclaw": {"forbidden_actions": ["grant_approval"]},
            "deployment_check": {
                "permissions": ["deployment_after_approval"],
                "forbidden_actions": ["deploy_without_approval"],
            },
            "quality_guardian": {"forbidden_actions": ["deploy"]},
        }
    }))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=False)
    subprocess.run(["git", "config", "user.email", "p13@example.com"], cwd=repo, check=False)
    subprocess.run(["git", "config", "user.name", "phase13"], cwd=repo, check=False)
    subprocess.run(["git", "add", "."], cwd=repo, check=False)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repo, check=False)
    return repo


def _to_implementation(oc: OpenClaw) -> None:
    """Front half of the pipeline: PROPOSAL -> ... -> IMPLEMENTATION."""
    oc.advance(RESEARCH, actor="ai_council")
    oc.advance(COUNCIL_REVIEW, actor="ai_council")
    oc.advance(HUMAN_APPROVAL, actor="ai_council")
    oc.request_human_approval(HUMAN_APPROVAL)
    oc.record_human_approval(HUMAN_APPROVAL, approved=True, approver="human")
    assert oc.continue_after_human_gate() == PLANNING  # human gate satisfied
    oc.advance(IMPLEMENTATION, actor="project_council")


def _submit_handoff(oc: OpenClaw, repo, context: str = "phase 13") -> HandoffClient:
    oc.advance(HANDOFF_CHECKPOINT, actor="coding_agent")
    handoff = HandoffClient()
    checkpoint = handoff.create_checkpoint(
        repo, owner="coding_agent", context=context,
        changes=["app.py"], decisions=["submit verified work for audit"],
        next_action="quality audit",
    )
    assert handoff.route_checkpoint(oc, checkpoint, handoff.git_state(repo)) == QUALITY_AUDIT
    return handoff


def _quality_pass(oc: OpenClaw, repo) -> None:
    quality = QualityGuardianClient(
        invoker=lambda root, ctx: _quality_raw("PASS", "PASS")
    )
    result = quality.invoke(repo, {})
    quality.record_evidence(oc, result, actor="quality_guardian")
    assert quality.route_result(oc, result) == SECURITY_AUDIT


def _security_gate_for(repo) -> SecurityGate:
    return SecurityGate(
        scanner_runner=_clean_run,
        tool_available=lambda tool: True,
        host_evidence={"branch_protection": {"main": "2026-09-19"}},
    )


def _events(oc: OpenClaw) -> list[str]:
    return [e.event for e in oc.audit.entries]


# --- Scenario A — Happy path PROPOSAL -> PRODUCTION, zero violations --------


def test_scenario_a_happy_path_proposal_to_production(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    oc = OpenClaw()

    _to_implementation(oc)
    assert oc.state == IMPLEMENTATION

    _submit_handoff(oc, repo, context="scenario A")
    assert oc.state == QUALITY_AUDIT

    _quality_pass(oc, repo)
    assert oc.state == SECURITY_AUDIT

    s_result = _security_gate_for(repo).run(repo)
    assert s_result.decision == "PASS"
    SecurityGateClient().record_evidence(oc, s_result)
    assert SecurityGateClient().route_result(oc, s_result) == DEPLOYMENT_CHECK

    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    d_result = DeploymentGate().preflight(
        repo,
        gates={
            "tests": {"status": "PASS", "detail": "pytest -q -> 0 failed",
                      "evidence": ["pytest -q -> 0 failed"]},
            "quality": {"decision": "PASS", "evidence": ["pytest: target=tests/"]},
            "security": {"decision": "PASS", "evidence": ["security decision=PASS"]},
        },
        expected_branch="main",
        expected_remote="",  # fixture declares no origin; identity must match
        expected_commit=head,
    )
    assert d_result.decision == "PASS"
    client = DeploymentGateClient()
    client.record_evidence(oc, d_result)
    assert client.route_result(oc, d_result) == HUMAN_RELEASE_APPROVAL

    # Human release gate: AI agents can request but never approve.
    dg = DeploymentGate()
    dg.request_release_approval(oc)
    with pytest.raises(PermissionDenied):
        oc.record_human_approval(HUMAN_RELEASE_APPROVAL, True, approver="openclaw")
    with pytest.raises(PermissionDenied):
        oc.record_human_approval(HUMAN_RELEASE_APPROVAL, True, approver="deployment_check")
    dg.record_release_approval(oc, approved=True, approver="human")
    assert dg.continue_after_release_approval(oc) == DEPLOY

    assert oc.route("SUCCESS", actor="deployment_check") == POST_DEPLOY_VERIFY
    assert oc.route("PASS", actor="deployment_check") == PRODUCTION

    events = _events(oc)
    for expected in (
        "TRANSITION", "ROUTED", "APPROVAL_REQUESTED", "APPROVAL_RECORDED", "EVIDENCE",
    ):
        assert expected in events
    assert oc.repair_count == 0


# --- Scenario B — False handoff is rejected (git truth wins) ----------------


def test_scenario_b_false_handoff_rejected_routes_to_repair(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    oc = OpenClaw(initial_state=HANDOFF_CHECKPOINT)
    handoff = HandoffClient()
    checkpoint = handoff.create_checkpoint(
        repo, owner="coding_agent", context="claims a state that never existed",
        changes=["app.py"], decisions=["checkpoint forged after history rewrite"],
        next_action="quality audit",
    )

    # Git truth contradicts the handoff claim -> conflict, then REJECTED.
    forged = dict(checkpoint.git_state, head="0" * 40)
    with pytest.raises(GitStateConflict):
        handoff.validate_checkpoint(checkpoint, forged)
    assert handoff.route_checkpoint(oc, checkpoint, forged) == IMPLEMENTATION
    assert oc.repair_count == 1
    assert "HANDOFF_REJECTED" in _events(oc)

    # An agent that does not own implementation work cannot hand off either.
    oc2 = OpenClaw(initial_state=HANDOFF_CHECKPOINT)
    intruder = handoff.create_checkpoint(
        repo, owner="quality_guardian", context="not the implementation owner",
        changes=[], decisions=["attempts to hand off foreign work"],
        next_action="quality audit",
    )
    assert handoff.route_checkpoint(oc2, intruder, handoff.git_state(repo)) == IMPLEMENTATION

    # A corrected checkpoint is accepted: the repair loop re-enters the
    # pipeline on a fresh HANDOFF_CHECKPOINT.
    oc3 = OpenClaw(initial_state=HANDOFF_CHECKPOINT)
    assert handoff.route_checkpoint(
        oc3, checkpoint, handoff.git_state(repo)
    ) == QUALITY_AUDIT


# --- Scenario C — Quality FAIL -> operator correction via OpenClaw ----------


def test_scenario_c_quality_fail_operator_correction_via_openclaw(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    oc = OpenClaw(initial_state=IMPLEMENTATION)
    _submit_handoff(oc, repo)

    quality = QualityGuardianClient(
        invoker=lambda root, ctx: _quality_raw("FAIL", "BLOCK")
    )
    bad = quality.invoke(repo, {})
    assert quality.route_result(oc, bad) == IMPLEMENTATION  # OpenClaw repair loop
    assert oc.repair_count == 1

    # Operator correction goes through the SAME gates, never around them.
    _submit_handoff(oc, repo, context="corrected after quality FAIL")
    quality_fixed = QualityGuardianClient(
        invoker=lambda root, ctx: _quality_raw("PASS", "PASS")
    )
    fixed = quality_fixed.invoke(repo, {})
    quality_fixed.record_evidence(oc, fixed, actor="quality_guardian")
    assert quality_fixed.route_result(oc, fixed) == SECURITY_AUDIT

    routed = [e for e in oc.audit.entries if e.event == "ROUTED"]
    assert any("verdict=FAIL" in e.detail for e in routed)


# --- Scenario D — Security FAIL repairs; BLOCK never forwards ---------------


def test_scenario_d_security_fail_repairs_and_never_forwards(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    client = SecurityGateClient()

    # Blocking evidence: a FAIL check aggregates to decision FAIL -> repair.
    failing = _security_result("FAIL", decision="FAIL")
    assert client.route_result(oc, failing) == IMPLEMENTATION
    assert oc.repair_count == 1

    # NEEDS_REVIEW decisions are never forwarded as PASS.
    oc_review = OpenClaw(initial_state=SECURITY_AUDIT)
    review = _security_result("PASS", decision="NEEDS_REVIEW")
    assert client.route_result(oc_review, review) == ESCALATED

    # The routing table itself sends a BLOCK verdict to the human (ESCALATED),
    # though the gate can never emit BLOCK as an aggregate decision — blocking
    # checks aggregate to FAIL, which repairs.
    from openclaw.orchestrator import _ROUTING
    assert _ROUTING[SECURITY_AUDIT]["BLOCK"] == ESCALATED

    # Recovery: the corrected work re-enters through the real handoff path and
    # the same gate re-audits; only then does PASS forward.
    _submit_handoff(oc, repo, context="security repair applied")
    _quality_pass(oc, repo)
    assert oc.state == SECURITY_AUDIT
    passing = _security_result("PASS", decision="PASS")
    assert client.route_result(oc, passing) == DEPLOYMENT_CHECK


# --- Scenario E — Deployment gate failure and missing evidence --------------


def test_scenario_e_deployment_gate_fail_and_not_scanned_never_forward(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    client = DeploymentGateClient()

    oc = OpenClaw(initial_state=DEPLOYMENT_CHECK)
    failing = DeploymentGate().preflight(
        repo,
        gates={"security": {"decision": "FAIL"}, "quality": {"decision": "PASS"},
               "tests": {"status": "PASS"}},
    )
    assert failing.decision == "FAIL"
    assert client.route_result(oc, failing) == IMPLEMENTATION
    assert oc.repair_count == 1

    oc2 = OpenClaw(initial_state=DEPLOYMENT_CHECK)
    missing = DeploymentGate().preflight(
        repo,
        gates={"security": {"decision": "NOT_SCANNED"},
               "quality": {"decision": "NOT_SCANNED"},
               "tests": {"status": "UNKNOWN"}},
    )
    assert missing.decision == "NOT_SCANNED"
    assert client.route_result(oc2, missing) == ESCALATED

    # The deployment gate can never declare PASS over blocking evidence.
    with pytest.raises(Exception):
        missing.validate()
        missing.decision = "PASS"
        missing.validate()


# --- Scenario F — Human rejects release -------------------------------------


def test_scenario_f_human_rejection_stops_release_permanently():
    oc = OpenClaw(initial_state=HUMAN_RELEASE_APPROVAL)
    dg = DeploymentGate()

    with pytest.raises(HumanApprovalRequired):
        oc.continue_after_human_gate()  # cannot proceed without the human

    dg.request_release_approval(oc)
    dg.record_release_approval(oc, approved=False, approver="human")
    assert oc.continue_after_human_gate() == ABORTED

    # Rejection is final: no deploy transition exists from ABORTED.
    assert not can_transition(ABORTED, DEPLOY)
    with pytest.raises(RoutingError):
        oc.route("SUCCESS", actor="deployment_check")

    # Separation of duties at the release boundary.
    with pytest.raises(DeploymentGateSeparationError):
        DeploymentGateClient().grant_release_approval()
    with pytest.raises(DeploymentGateSeparationError):
        DeploymentGateClient().deploy_without_approval()
    with pytest.raises(HandoffSeparationError):
        HandoffClient().deployment_approval()


# --- Scenario G — Invalid transitions/verdicts/actors rejected everywhere ---


def test_scenario_g_invalid_transitions_verdicts_and_actors_rejected():
    for current, target in (
        (PROPOSAL, PRODUCTION),
        (QUALITY_AUDIT, DEPLOY),
        (IMPLEMENTATION, SECURITY_AUDIT),
        (HANDOFF_CHECKPOINT, PRODUCTION),
        (PROPOSAL, SECURITY_AUDIT),
    ):
        with pytest.raises(Exception):
            validate_transition(current, target)

    oc = OpenClaw(initial_state=PROPOSAL)
    with pytest.raises(Exception):
        oc.advance(PRODUCTION, actor="ai_council")

    # Unsupported verdict at a gate state cannot be smuggled through routing.
    oc2 = OpenClaw(initial_state=QUALITY_AUDIT)
    with pytest.raises(RoutingError):
        oc2.route("SUCCESS", actor="quality_guardian")

    # Routing rules do not exist outside gate states.
    oc3 = OpenClaw(initial_state=PROPOSAL)
    with pytest.raises(RoutingError):
        oc3.route("PASS", actor="ai_council")

    # Actor authority: only the state owner, human, or openclaw may act.
    oc4 = OpenClaw(initial_state=COUNCIL_REVIEW)
    with pytest.raises(PermissionDenied):
        oc4.advance(RESEARCH, actor="coding_agent")
    oc5 = OpenClaw(initial_state=SECURITY_AUDIT)
    with pytest.raises(PermissionDenied):
        oc5.route("PASS", actor="handoff_agent")


# --- Scenario H — Post-deploy failure rollback path (SIMULATION only) -------


def test_scenario_h_post_deploy_failure_rollback_path(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    oc = OpenClaw(initial_state=DEPLOY)

    assert oc.route("FAILURE", actor="deployment_check") == ROLLED_BACK

    # Recovery from ROLLED_BACK is a human decision, not an agent's.
    with pytest.raises(PermissionDenied):
        oc.route("REPAIR", actor="deployment_check")
    assert oc.route("REPAIR", actor="human") == IMPLEMENTATION

    oc2 = OpenClaw(initial_state=POST_DEPLOY_VERIFY)
    assert oc2.route("FAIL", actor="deployment_check") == ROLLED_BACK
    assert oc2.route("ESCALATE", actor="human") == ESCALATED

    # Rollback is verified as DEFINED AND READINESS-CHECKED only. No rollback
    # is executed anywhere in this suite; this is SIMULATION at the execution
    # boundary, not a production rollback verification.
    plan = DeploymentGate().plan_rollback(repo)
    assert plan.artifact_before
    assert plan.trigger and plan.procedure and plan.verification
    readiness = DeploymentGate().verify_rollback_readiness(repo)
    assert readiness.status == "PASS"
    assert plan.artifact_before in readiness.detail  # procedure embeds the artifact


# --- Scenario I — Agent Registry semantics across integrated agents ---------


def test_scenario_i_agent_registry_identity_capabilities_permissions_availability():
    reg = AgentRegistry()  # the real openclaw/agent_registry.json

    # Every integrated agent is registered, complete, and AVAILABLE.
    for agent_id in INTEGRATED_AGENTS:
        assert reg.has_agent(agent_id)
        reg.validate_availability(agent_id)
        record = reg.lookup(agent_id)
        assert record.status == "ACTIVE"
        assert record.agent_id == agent_id
        assert record.capabilities and record.permissions and record.forbidden_actions
    assert reg.require_complete() == []

    # Unknown agents are refused.
    with pytest.raises(UnknownAgent):
        reg.lookup("ghost_agent")

    # Capability and permission are validated independently.
    reg.validate_capabilities("quality_guardian", ["scanner_invocation", "release_decision"])
    with pytest.raises(CapabilityError):
        reg.validate_capabilities("handoff_agent", ["deploy"])
    with pytest.raises(PermissionError_Registry):
        reg.validate_permissions("quality_guardian", ["deploy"])
    with pytest.raises(PermissionError_Registry):
        reg.validate_permissions("openclaw", ["grant_approval"])

    # Contract version and invocation method are enforced.
    with pytest.raises(ContractVersionError):
        reg.validate_contract_version("security_gate", "2.0")
    reg.validate_contract_version("security_gate", "3.0")
    with pytest.raises(InvocationError):
        reg.validate_invocation("deployment_check", "openclaw:decision_review")

    # An unavailable agent cannot be invoked.
    degraded = AgentRegistry.from_records({
        "security_gate": {
            "agent_id": "security_gate", "name": "Security Gate",
            "department": "Security", "role": "boundary",
            "capabilities": ["secret_scanning"], "inputs": ["x"], "outputs": ["y"],
            "permissions": ["security_verification"],
            "forbidden_actions": ["treat_not_scanned_as_pass"],
            "contract_version": "3.0",
            "invocation_method": "openclaw:security_audit",
            "status": "ACTIVE", "availability": "DEGRADED",
        }
    })
    with pytest.raises(AvailabilityError):
        degraded.validate_availability("security_gate")

    # Forbidden actions pin the separation of duties in the registry itself.
    assert "grant_approval" in reg.lookup("openclaw").forbidden_actions
    assert "change_security_verdict" in reg.lookup("openclaw").forbidden_actions
    assert "deploy" in reg.lookup("quality_guardian").forbidden_actions
    assert "security_approval" in reg.lookup("handoff_agent").forbidden_actions
    assert "deploy_without_approval" in reg.lookup("deployment_check").forbidden_actions
    assert "treat_not_scanned_as_pass" in reg.lookup("security_gate").forbidden_actions


# --- Scenario J — Bounded repair loop ends in human escalation --------------


def test_scenario_j_bounded_repair_escalates_with_full_audit_trail(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    oc = OpenClaw(initial_state=QUALITY_AUDIT, max_repair_iterations=1)
    quality = QualityGuardianClient(
        invoker=lambda root, ctx: _quality_raw("FAIL", "BLOCK")
    )

    # Iteration 1: FAIL -> repair (IMPLEMENTATION).
    assert quality.route_result(oc, quality.invoke(repo, {})) == IMPLEMENTATION
    assert oc.repair_count == 1

    # Resubmission: IMPLEMENTATION -> HANDOFF -> QUALITY (real handoff path).
    oc.advance(HANDOFF_CHECKPOINT, actor="coding_agent")
    handoff = HandoffClient()
    checkpoint = handoff.create_checkpoint(
        repo, owner="coding_agent", context="attempt 2", changes=["app.py"],
        decisions=["resubmit after first repair"], next_action="quality audit",
    )
    assert handoff.route_checkpoint(oc, checkpoint, handoff.git_state(repo)) == QUALITY_AUDIT

    # Iteration 2 exceeds the bound -> ESCALATED to the human, never looping.
    assert quality.route_result(oc, quality.invoke(repo, {})) == ESCALATED
    assert oc.state == ESCALATED
    assert can_transition(ESCALATED, IMPLEMENTATION) is False  # terminal: human owns it

    events = _events(oc)
    assert "HANDOFF_REJECTED" not in events  # handoffs were valid; failure was quality
    assert "ESCALATED" in events
    routed = [e for e in oc.audit.entries if e.event == "ROUTED"]
    assert [e.state_after for e in routed].count("ESCALATED") == 1


# --- §12 Integration contracts between the integrated agents ---------------


def test_integration_contract_handoff_to_quality_to_security_to_deployment(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    oc = OpenClaw(initial_state=HANDOFF_CHECKPOINT)

    # Handoff -> Quality: only a validated checkpoint opens QUALITY_AUDIT.
    handoff = HandoffClient()
    checkpoint = handoff.create_checkpoint(
        repo, owner="coding_agent", context="contract", changes=["app.py"],
        decisions=["contract verification"], next_action="quality audit",
    )
    assert handoff.route_checkpoint(oc, checkpoint, handoff.git_state(repo)) == QUALITY_AUDIT

    quality = QualityGuardianClient(invoker=lambda root, ctx: _quality_raw("PASS", "PASS"))

    # Quality -> Security: PASS is the only forwarding verdict; the quality
    # agent can never issue a deployment or release decision.
    result = quality.invoke(repo, {})
    assert quality.route_result(oc, result) == SECURITY_AUDIT

    # Order preservation: once the pipeline left QUALITY_AUDIT, the quality
    # client refuses to route again (it cannot act as any other stage).
    with pytest.raises(RoutingError):
        quality.route_result(oc, result)

    # Separation guard: a tampered quality result claiming a release decision
    # is refused — deployment/release authority is never the quality agent's.
    from openclaw.quality_integration import (
        QualityAuditResult,
        route_security_separation,
    )
    tampered = QualityAuditResult(project="fixture", status="PASS", decision="DEPLOY")
    with pytest.raises(QualitySecuritySeparationError):
        route_security_separation(oc, tampered)

    # Security -> Deployment: PASS forwards; evidence recorded in the audit log.
    s_result = _security_gate_for(repo).run(repo)
    assert s_result.decision == "PASS"
    sec_client = SecurityGateClient()
    sec_client.record_evidence(oc, s_result)
    assert sec_client.route_result(oc, s_result) == DEPLOYMENT_CHECK

    # Deployment -> Human: PASS opens the human release gate and nothing else.
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    d_result = DeploymentGate().preflight(
        repo,
        gates={"tests": {"status": "PASS", "detail": "0 failed"},
               "quality": {"decision": "PASS"},
               "security": {"decision": "PASS", "evidence": ["decision=PASS"]}},
        expected_branch="main", expected_remote="", expected_commit=head,
    )
    assert DeploymentGateClient().route_result(oc, d_result) == HUMAN_RELEASE_APPROVAL

    evidence_events = [e.detail for e in oc.audit.entries if e.event == "EVIDENCE"]
    assert evidence_events  # every gate preserved its evidence model


# --- §14 Error-state handling at every boundary -----------------------------


def test_error_states_handled_at_every_boundary(tmp_path):
    # Quality boundary: ERROR repairs, NOT_SCANNED escalates.
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    quality = QualityGuardianClient(
        invoker=lambda root, ctx: _quality_raw("ERROR", "NEEDS_REVIEW")
    )
    assert quality.route_result(oc, quality.invoke(".", {})) == IMPLEMENTATION

    oc2 = OpenClaw(initial_state=QUALITY_AUDIT)
    quality_ns = QualityGuardianClient(
        invoker=lambda root, ctx: _quality_raw("NOT_SCANNED", "NEEDS_REVIEW")
    )
    assert quality_ns.route_result(oc2, quality_ns.invoke(".", {})) == ESCALATED

    # Security boundary: NEEDS_REVIEW and NOT_SCANNED escalate; ERROR repairs.
    sec = SecurityGateClient()
    oc3 = OpenClaw(initial_state=SECURITY_AUDIT)
    assert sec.route_result(
        oc3, _security_result("PASS", decision="NEEDS_REVIEW")
    ) == ESCALATED
    oc4 = OpenClaw(initial_state=SECURITY_AUDIT)
    assert sec.route_result(
        oc4, _security_result("PASS", "NOT_SCANNED", decision="NOT_SCANNED")
    ) == ESCALATED
    oc5 = OpenClaw(initial_state=SECURITY_AUDIT)
    assert sec.route_result(oc5, _security_result("ERROR", decision="ERROR")) == IMPLEMENTATION

    # Deployment boundary: NEEDS_REVIEW escalates (client maps NOT_SCANNED too).
    repo = _make_fixture_repo(tmp_path)
    dg_client = DeploymentGateClient()
    oc6 = OpenClaw(initial_state=DEPLOYMENT_CHECK)
    review = DeploymentGate().preflight(
        repo,
        gates={"security": {"decision": "PASS", "evidence": ["x"]},
               "quality": {"decision": "PASS"},
               "tests": {"status": "PASS"}},
    )
    review.decision = "NEEDS_REVIEW"
    assert dg_client.route_result(oc6, review) == ESCALATED

    # Deploy boundary: SUCCESS/FAILURE are the only verdicts, FAILURE rolls back.
    oc7 = OpenClaw(initial_state=DEPLOY)
    with pytest.raises(RoutingError):
        oc7.route("PASS", actor="deployment_check")
    assert oc7.route("FAILURE", actor="deployment_check") == ROLLED_BACK
