"""Phase 11 — End-to-End Integration Tests (Development F).

Each test drives the REAL Development F pipeline components:

- openclaw.workflow state machine (transitions, owners, validation)
- openclaw.orchestrator.OpenClaw (routing, bounded repair, human gates, audit)
- openclaw.handoff_integration.HandoffClient (checkpoint validation + routing)
- openclaw.quality_integration.QualityGuardianClient (audit validation + routing)
- openclaw.security_gate.SecurityGate / SecurityGateClient (real checks + routing)
- openclaw.deployment_gate.DeploymentGate / DeploymentGateClient (preflight + release)

External agents (Handoff Agent, Quality Guardian) are exercised through their
established client seams with controlled delegates, so scenario evidence is
SIMULATION at the agent-invocation boundary and REAL at the state machine,
validation, routing, and gate-decision boundaries. No rollback execution is
simulated as real: Scenario H verifies routing/plan only.
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
)
from openclaw.orchestrator import (
    HumanApprovalRequired,
    OpenClaw,
    PermissionDenied,
    RoutingError,
)
from openclaw.quality_integration import QualityGuardianClient
from openclaw.security_gate import (
    ScannerRun,
    SecurityCheck,
    SecurityGate,
    SecurityGateClient,
    SecurityGateResult,
    ValidationError,
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
    (repo / ".gitignore").write_text(".env\n.env.*\n*.env\n*.pem\n*.key\nid_rsa\nid_dsa\n__pycache__/\n")
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
    subprocess.run(["git", "config", "user.email", "e2e@example.com"], cwd=repo, check=False)
    subprocess.run(["git", "config", "user.name", "e2e"], cwd=repo, check=False)
    subprocess.run(["git", "add", "."], cwd=repo, check=False)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repo, check=False)
    return repo


def _to_implementation(oc: OpenClaw) -> None:
    """Scenario A front half: PROPOSAL -> ... -> IMPLEMENTATION (with human gate)."""
    oc.advance(RESEARCH, actor="ai_council")
    oc.advance(COUNCIL_REVIEW, actor="ai_council")
    oc.advance(HUMAN_APPROVAL, actor="ai_council")
    oc.request_human_approval(HUMAN_APPROVAL)
    oc.record_human_approval(HUMAN_APPROVAL, approved=True, approver="human")
    assert oc.continue_after_human_gate() == PLANNING  # human gate satisfied
    oc.advance(IMPLEMENTATION, actor="project_council")


def _to_handoff(oc: OpenClaw) -> None:
    oc.advance(HANDOFF_CHECKPOINT, actor="coding_agent")


def _back_to_security_after_repair(oc: OpenClaw) -> None:
    oc.advance(HANDOFF_CHECKPOINT, actor="coding_agent")
    handoff = HandoffClient()
    checkpoint = handoff.create_checkpoint(
        ".", owner="coding_agent", context="repaired", changes=["app.py"],
        decisions=["repair applied and re-verified locally"],
        next_action="re-audit",
    )
    assert handoff.route_checkpoint(oc, checkpoint, handoff.git_state(".")) == QUALITY_AUDIT
    quality = QualityGuardianClient(invoker=lambda root, ctx: _quality_raw("PASS", "PASS"))
    assert quality.route_result(oc, quality.invoke(".", {})) == SECURITY_AUDIT


# --- Scenario A — Full Forward Path ----------------------------------------


def test_scenario_a_full_forward_path(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    oc = OpenClaw()

    _to_implementation(oc)
    assert oc.state == IMPLEMENTATION

    # Handoff: real checkpoint validated against real git state.
    _to_handoff(oc)
    handoff = HandoffClient()
    checkpoint = handoff.create_checkpoint(
        repo, owner="coding_agent", context="phase 11 e2e",
        changes=["app.py"], decisions=["use existing quality guardian"],
        next_action="quality audit",
    )
    assert handoff.route_checkpoint(oc, checkpoint, handoff.git_state(repo)) == QUALITY_AUDIT

    # Quality: PASS -> SECURITY_AUDIT.
    quality = QualityGuardianClient(invoker=lambda root, ctx: _quality_raw("PASS", "PASS"))
    q_result = quality.invoke(repo, {"phase": "e2e"})
    quality.record_evidence(oc, q_result, actor="quality_guardian")
    assert quality.route_result(oc, q_result) == SECURITY_AUDIT

    # Security: real gate over the fixture with scanners wired -> PASS.
    gate = SecurityGate(
        scanner_runner=_clean_run,
        tool_available=lambda tool: True,
        host_evidence={"branch_protection": {"main": "2026-09-17"}},
    )
    s_result = gate.run(repo)
    assert s_result.decision == "PASS"
    SecurityGateClient().record_evidence(oc, s_result)
    assert SecurityGateClient().route_result(oc, s_result) == DEPLOYMENT_CHECK

    # Deployment preflight on the clean committed artifact -> PASS.
    dg = DeploymentGate()
    d_result = dg.preflight(
        repo,
        gates={
            "tests": {"status": "PASS", "detail": "pytest -q -> 171 passed",
                      "evidence": ["pytest -q -> 171 passed"]},
            "quality": {"decision": "PASS", "evidence": [q_result.evidence[0].text]},
            "security": {"decision": "PASS", "evidence": ["security decision=PASS"]},
        },
        expected_branch="main",
        expected_remote="",  # fixture declares no origin; identity must match declaration
        expected_commit=s_result.project and subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        ).stdout.strip(),
    )
    assert d_result.decision == "PASS"
    client = DeploymentGateClient()
    client.record_evidence(oc, d_result)
    assert client.route_result(oc, d_result) == HUMAN_RELEASE_APPROVAL

    # Human release approval: AI cannot approve; human approves -> DEPLOY.
    dg.request_release_approval(oc)
    with pytest.raises(PermissionDenied):
        oc.record_human_approval(HUMAN_RELEASE_APPROVAL, True, approver="openclaw")
    dg.record_release_approval(oc, approved=True, approver="human")
    assert dg.continue_after_release_approval(oc) == DEPLOY

    # Deploy -> post deploy verify -> PRODUCTION.
    assert oc.route("SUCCESS", actor="deployment_check") == POST_DEPLOY_VERIFY
    assert oc.route("PASS", actor="deployment_check") == PRODUCTION

    events = [e.event for e in oc.audit.entries]
    assert "APPROVAL_REQUESTED" in events and "APPROVAL_RECORDED" in events
    assert "EVIDENCE" in events


# --- Scenario B — Quality Failure ------------------------------------------


def test_scenario_b_quality_failure_routes_to_repair_and_recovers():
    oc = OpenClaw(initial_state=IMPLEMENTATION)
    _to_handoff(oc)
    handoff = HandoffClient()
    checkpoint = handoff.create_checkpoint(
        ".", owner="coding_agent", context="e2e B", changes=["app.py"],
        decisions=["submitted for quality audit"], next_action="quality audit",
    )
    assert handoff.route_checkpoint(oc, checkpoint, handoff.git_state(".")) == QUALITY_AUDIT

    quality = QualityGuardianClient(invoker=lambda root, ctx: _quality_raw("FAIL", "BLOCK"))
    bad = quality.invoke(".", {})
    assert quality.route_result(oc, bad) == IMPLEMENTATION
    assert oc.repair_count == 1

    # Repair loop: IMPLEMENTATION -> HANDOFF -> QUALITY (now PASS).
    _back_to_security_after_repair(oc)
    assert oc.state == SECURITY_AUDIT


# --- Scenario C — Security Failure -----------------------------------------


def test_scenario_c_security_failure_routes_to_repair_and_recovers():
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    client = SecurityGateClient()

    failing = _security_result("FAIL", decision="FAIL")
    assert client.route_result(oc, failing) == IMPLEMENTATION
    assert oc.repair_count == 1

    _back_to_security_after_repair(oc)
    passing = _security_result("PASS", decision="PASS")
    assert client.route_result(oc, passing) == DEPLOYMENT_CHECK


# --- Scenario D — Security NOT_SCANNED -------------------------------------


def test_scenario_d_security_not_scanned_escalates_and_never_passes():
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    client = SecurityGateClient()

    not_scanned = _security_result("PASS", "NOT_SCANNED", decision="NOT_SCANNED")
    assert client.route_result(oc, not_scanned) == ESCALATED
    assert oc.last_verdict == "NOT_SCANNED"

    # NOT_SCANNED can never be promoted to a forwarding decision.
    with pytest.raises(ValidationError):
        _security_result("PASS", "NOT_SCANNED", decision="PASS").validate()

    # The routing table itself contains no NOT_SCANNED -> forward path.
    assert can_transition(SECURITY_AUDIT, DEPLOYMENT_CHECK)  # PASS only
    from openclaw.orchestrator import _ROUTING
    assert _ROUTING[SECURITY_AUDIT]["NOT_SCANNED"] == ESCALATED


# --- Scenario E — Deployment Failure ---------------------------------------


def test_scenario_e_deployment_check_fail_routes_to_repair(tmp_path):
    repo = _make_fixture_repo(tmp_path)
    oc = OpenClaw(initial_state=DEPLOYMENT_CHECK)
    client = DeploymentGateClient()

    failing = DeploymentGate().preflight(
        repo, gates={"security": {"decision": "FAIL"},
                     "quality": {"decision": "PASS"}, "tests": {"status": "PASS"}},
    )
    assert failing.decision == "FAIL"
    assert client.route_result(oc, failing) == IMPLEMENTATION
    assert oc.repair_count == 1

    # Missing evidence is escalated, never forwarded.
    oc2 = OpenClaw(initial_state=DEPLOYMENT_CHECK)
    missing = DeploymentGate().preflight(
        repo,
        gates={"security": {"decision": "NOT_SCANNED"},
               "quality": {"decision": "NOT_SCANNED"},
               "tests": {"status": "UNKNOWN"}},
    )
    assert missing.decision == "NOT_SCANNED"
    assert client.route_result(oc2, missing) == ESCALATED


# --- Scenario F — Human Reject ---------------------------------------------


def test_scenario_f_human_reject_stops_release():
    oc = OpenClaw(initial_state=HUMAN_RELEASE_APPROVAL)
    dg = DeploymentGate()

    # No approval yet: continuing is impossible.
    with pytest.raises(HumanApprovalRequired):
        oc.continue_after_human_gate()

    dg.request_release_approval(oc)
    dg.record_release_approval(oc, approved=False, approver="human")
    assert oc.continue_after_human_gate() == ABORTED

    # Separation: the Deployment Gate (an AI agent) can never grant approval.
    with pytest.raises(DeploymentGateSeparationError):
        DeploymentGateClient().grant_release_approval()
    with pytest.raises(DeploymentGateSeparationError):
        DeploymentGateClient().deploy_without_approval()


# --- Scenario G — Invalid Transitions --------------------------------------


def test_scenario_g_invalid_transitions_rejected():
    with pytest.raises(Exception):
        validate_transition(PROPOSAL, PRODUCTION)
    with pytest.raises(Exception):
        validate_transition(QUALITY_AUDIT, DEPLOY)
    with pytest.raises(Exception):
        validate_transition(SECURITY_AUDIT, DEPLOY)
    with pytest.raises(Exception):
        validate_transition(HANDOFF_CHECKPOINT, PRODUCTION)

    # No skip-gate: IMPLEMENTATION cannot jump to DEPLOYMENT_CHECK.
    assert not can_transition(IMPLEMENTATION, DEPLOYMENT_CHECK)

    oc = OpenClaw(initial_state=PROPOSAL)
    with pytest.raises(Exception):
        oc.advance(PRODUCTION, actor="ai_council")

    # Unsupported verdicts cannot be smuggled through routing.
    oc2 = OpenClaw(initial_state=QUALITY_AUDIT)
    with pytest.raises(RoutingError):
        oc2.route("SUCCESS", actor="quality_guardian")

    # Actor permission boundary: a coding agent cannot act in COUNCIL_REVIEW.
    oc3 = OpenClaw(initial_state=COUNCIL_REVIEW)
    with pytest.raises(PermissionDenied):
        oc3.advance(RESEARCH, actor="coding_agent")


# --- Scenario H — Post Deploy Failure --------------------------------------


def test_scenario_h_post_deploy_failure_rollback_path():
    oc = OpenClaw(initial_state=DEPLOY)

    # DEPLOY failure -> ROLLED_BACK (defined rollback path, SIMULATION only).
    assert oc.route("FAILURE", actor="deployment_check") == ROLLED_BACK

    # Recovery from ROLLED_BACK is a HUMAN decision; the deployment agent
    # cannot decide it alone.
    with pytest.raises(PermissionDenied):
        oc.route("REPAIR", actor="deployment_check")
    assert oc.route("REPAIR", actor="human") == IMPLEMENTATION

    # POST_DEPLOY_VERIFY failure also lands in ROLLED_BACK.
    oc2 = OpenClaw(initial_state=POST_DEPLOY_VERIFY)
    assert oc2.route("FAIL", actor="deployment_check") == ROLLED_BACK

    # Rollback plan is defined and verifiable before release (plan only —
    # no rollback is actually executed in this suite).
    plan = DeploymentGate().plan_rollback(".")
    assert plan.artifact_before
    assert plan.trigger and plan.procedure and plan.verification
    assert DeploymentGate().verify_rollback_readiness(".").status == "PASS"


# --- Cross-scenario: handoff git-truth and bounded repair -------------------


def test_e2e_handoff_git_truth_conflict_is_rejected():
    oc = OpenClaw(initial_state=HANDOFF_CHECKPOINT)
    handoff = HandoffClient()
    checkpoint = handoff.create_checkpoint(
        ".", owner="coding_agent", context="e2e", changes=["app.py"],
        decisions=["checkpoint claims a git state that never existed"],
        next_action="quality",
    )
    # Claim a git state that contradicts the repository (GIT TRUTH wins).
    bad = dict(checkpoint.git_state, head="0" * 40)
    with pytest.raises(GitStateConflict):
        handoff.validate_checkpoint(checkpoint, bad)
    assert handoff.route_checkpoint(oc, checkpoint, bad) == IMPLEMENTATION
    assert oc.repair_count == 1


def test_e2e_bounded_repair_escalates_to_human():
    oc = OpenClaw(initial_state=SECURITY_AUDIT, max_repair_iterations=2)
    client = SecurityGateClient()
    failing = _security_result("FAIL", decision="FAIL")
    assert client.route_result(oc, failing) == IMPLEMENTATION
    _back_to_security_after_repair(oc)
    assert client.route_result(oc, failing) == IMPLEMENTATION
    _back_to_security_after_repair(oc)
    assert client.route_result(oc, failing) == ESCALATED
    assert oc.state == ESCALATED
