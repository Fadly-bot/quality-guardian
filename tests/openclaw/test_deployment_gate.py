import subprocess

import pytest

from openclaw.deployment_gate import (
    DeploymentGate,
    DeploymentGateClient,
    DeploymentGateError,
    DeploymentGateResult,
    DeploymentGateSeparationError,
    PreflightCheck,
    ValidationError,
)
from openclaw.orchestrator import HUMAN_ROLE, HumanApprovalRequired, OpenClaw, RoutingError
from openclaw.workflow import (
    ABORTED,
    DEPLOY,
    DEPLOYMENT_CHECK,
    ESCALATED,
    HUMAN_RELEASE_APPROVAL,
    IMPLEMENTATION,
)

REMOTE = "https://example.com/org/repo.git"


def gates_pass():
    return {
        "quality": {"decision": "PASS", "evidence": ["quality audit verified"]},
        "security": {"decision": "PASS", "evidence": ["security gate verified"]},
        "tests": {
            "status": "PASS",
            "evidence": ["pytest -q -> 0 failed"],
            "detail": "full suite green",
        },
    }


def make_git_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=False)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=False)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=False)
    (path / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "."], cwd=path, check=False)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=False)
    subprocess.run(["git", "remote", "add", "origin", REMOTE], cwd=path, check=False)


def make_checks(*statuses, decision="PASS"):
    return [
        PreflightCheck(check_id=f"c{i}", description="d", status=s)
        for i, s in enumerate(statuses)
    ]


def test_happy_path_preflight(tmp_path):
    make_git_repo(tmp_path)
    gate = DeploymentGate()
    result = gate.preflight(
        tmp_path, gates_pass(), expected_branch="master",
        expected_remote=REMOTE, build_targets=(),
    )
    assert result.validate().decision == "PASS"
    check_ids = {c.check_id for c in result.checks if c.status == "PASS"}
    assert "preflight.repository_clean" in check_ids
    assert "preflight.correct_branch" in check_ids
    assert "preflight.correct_remote" in check_ids
    assert "preflight.artifact_identity" in check_ids
    assert "preflight.tests_pass" in check_ids
    assert "preflight.quality_pass" in check_ids
    assert "preflight.security_pass" in check_ids
    assert "preflight.build_pass" in check_ids
    assert "preflight.rollback_readiness" in check_ids


def test_dirty_repo_fails(tmp_path):
    make_git_repo(tmp_path)
    (tmp_path / "untracked.txt").write_text("x")
    result = DeploymentGate().preflight(
        tmp_path, gates_pass(), expected_branch="master",
        expected_remote=REMOTE, build_targets=(),
    )
    assert result.decision == "FAIL"
    assert result.failures()[0].check_id == "preflight.repository_clean"


def test_wrong_branch_fails(tmp_path):
    make_git_repo(tmp_path)
    result = DeploymentGate().preflight(
        tmp_path, gates_pass(), expected_branch="main",
        expected_remote=REMOTE, build_targets=(),
    )
    assert result.decision == "FAIL"
    assert any(c.check_id == "preflight.correct_branch" and c.status == "FAIL"
               for c in result.checks)


def test_wrong_remote_fails(tmp_path):
    make_git_repo(tmp_path)
    result = DeploymentGate().preflight(
        tmp_path, gates_pass(), expected_branch="master",
        expected_remote="https://example.com/other.git", build_targets=(),
    )
    assert result.decision == "FAIL"


def test_missing_quality_gate_not_scanned(tmp_path):
    make_git_repo(tmp_path)
    gates = {
        "quality": {"decision": "NOT_SCANNED"},
        "security": {"decision": "PASS"},
        "tests": {"status": "PASS"},
    }
    result = DeploymentGate().preflight(
        tmp_path, gates, expected_branch="master",
        expected_remote=REMOTE, build_targets=(),
    )
    assert result.decision == "NOT_SCANNED"
    qc = next(c for c in result.checks if c.check_id == "preflight.quality_pass")
    assert qc.status == "NOT_SCANNED"


def test_failed_tests_fail(tmp_path):
    make_git_repo(tmp_path)
    gates = {
        "quality": {"decision": "PASS"},
        "security": {"decision": "PASS"},
        "tests": {"status": "FAIL", "detail": "1 failure"},
    }
    result = DeploymentGate().preflight(
        tmp_path, gates, expected_branch="master",
        expected_remote=REMOTE, build_targets=(),
    )
    assert result.decision == "FAIL"


def test_build_error_detected(tmp_path):
    make_git_repo(tmp_path)
    (tmp_path / "broken.py").write_text("def broken(:\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=False)
    subprocess.run(["git", "commit", "-q", "-m", "broken"], cwd=tmp_path, check=False)
    result = DeploymentGate().preflight(
        tmp_path, gates_pass(), expected_branch="master",
        expected_remote=REMOTE, build_targets=("broken.py",),
    )
    build = next(c for c in result.checks if c.check_id == "preflight.build_pass")
    assert build.status == "ERROR"
    assert result.decision == "ERROR"


def test_artifact_identity_recorded(tmp_path):
    make_git_repo(tmp_path)
    gate = DeploymentGate()
    result = gate.preflight(
        tmp_path, gates_pass(), expected_branch="master",
        expected_remote=REMOTE, build_targets=(),
    )
    assert result.artifact["branch"] == "master"
    assert result.artifact["remote"] == REMOTE
    assert result.artifact["commit"]


def test_rollback_plan_defined_and_verified(tmp_path):
    make_git_repo(tmp_path)
    gate = DeploymentGate()
    plan = gate.plan_rollback(tmp_path)
    assert plan.artifact_before
    assert "DEPLOY failure" in plan.trigger
    assert plan.procedure and "restore" in plan.procedure
    assert plan.verification and "restored state" in plan.verification
    assert plan.post_rollback_verification and "verification" in plan.post_rollback_verification
    check = gate.verify_rollback_readiness(tmp_path)
    assert check.status == "PASS"


def test_no_not_scanned_pass():
    with pytest.raises(ValidationError):
        DeploymentGateResult(
            project="p",
            checks=make_checks("PASS", "NOT_SCANNED"),
            decision="PASS",
        ).validate()
    with pytest.raises(ValidationError):
        DeploymentGateResult(
            project="p",
            checks=make_checks("PASS"),
            decision="FAIL",
        ).validate()


def test_routing_pass_to_human_approval():
    oc = OpenClaw(initial_state=DEPLOYMENT_CHECK)
    result = DeploymentGateResult(project="p", checks=make_checks("PASS"), decision="PASS")
    assert DeploymentGateClient().route_result(oc, result) == HUMAN_RELEASE_APPROVAL
    assert oc.state == HUMAN_RELEASE_APPROVAL


def test_routing_fail_to_repair():
    oc = OpenClaw(initial_state=DEPLOYMENT_CHECK)
    result = DeploymentGateResult(project="p", checks=make_checks("FAIL"), decision="FAIL")
    assert DeploymentGateClient().route_result(oc, result) == IMPLEMENTATION
    assert oc.repair_count == 1


def test_routing_error_to_repair():
    oc = OpenClaw(initial_state=DEPLOYMENT_CHECK)
    result = DeploymentGateResult(project="p", checks=make_checks("ERROR"), decision="ERROR")
    assert DeploymentGateClient().route_result(oc, result) == IMPLEMENTATION


def test_routing_not_scanned_escalates():
    oc = OpenClaw(initial_state=DEPLOYMENT_CHECK)
    result = DeploymentGateResult(project="p", checks=make_checks("NOT_SCANNED"), decision="NOT_SCANNED")
    assert DeploymentGateClient().route_result(oc, result) == ESCALATED


def test_routing_needs_review_escalates():
    oc = OpenClaw(initial_state=DEPLOYMENT_CHECK)
    result = DeploymentGateResult(project="p", checks=make_checks("NEEDS_REVIEW"), decision="NEEDS_REVIEW")
    assert DeploymentGateClient().route_result(oc, result) == ESCALATED


def test_routing_wrong_state_rejected():
    oc = OpenClaw(initial_state=HUMAN_RELEASE_APPROVAL)
    result = DeploymentGateResult(project="p", checks=make_checks("PASS"), decision="PASS")
    with pytest.raises(RoutingError):
        DeploymentGateClient().route_result(oc, result)


def test_release_approval_flow_approve():
    oc = OpenClaw(initial_state=HUMAN_RELEASE_APPROVAL)
    gate = DeploymentGate()
    gate.request_release_approval(oc)
    with pytest.raises(HumanApprovalRequired):
        gate.continue_after_release_approval(oc)
    gate.record_release_approval(oc, True, approver=HUMAN_ROLE)
    assert gate.continue_after_release_approval(oc) == DEPLOY
    assert oc.state == DEPLOY


def test_release_approval_flow_reject_aborts():
    oc = OpenClaw(initial_state=HUMAN_RELEASE_APPROVAL)
    gate = DeploymentGate()
    gate.request_release_approval(oc)
    gate.record_release_approval(oc, False, approver=HUMAN_ROLE)
    assert gate.continue_after_release_approval(oc) == ABORTED


def test_approval_cannot_be_granted_by_agent():
    oc = OpenClaw(initial_state=HUMAN_RELEASE_APPROVAL)
    gate = DeploymentGate()
    gate.request_release_approval(oc)
    with pytest.raises(Exception):
        gate.record_release_approval(oc, True, approver="deployment_check")


def test_deploy_without_approval_impossible():
    with pytest.raises(DeploymentGateSeparationError):
        DeploymentGateClient().deploy_without_approval()


def test_gate_cannot_grant_release_approval():
    with pytest.raises(DeploymentGateSeparationError):
        DeploymentGateClient().grant_release_approval()


def test_gate_cannot_change_verdicts():
    with pytest.raises(DeploymentGateSeparationError):
        DeploymentGateClient().change_security_or_quality_verdict()


def test_deploy_executor_boundary():
    oc = OpenClaw(initial_state=DEPLOYMENT_CHECK)
    gate = DeploymentGate()
    with pytest.raises(DeploymentGateSeparationError):
        gate.deploy_not_executed(oc)


def test_evidence_recorded_to_audit():
    oc = OpenClaw(initial_state=DEPLOYMENT_CHECK)
    result = DeploymentGateResult(
        project="p",
        checks=[PreflightCheck(check_id="preflight.tests_pass", description="d",
                               status="PASS", evidence=["pytest green"])],
        decision="PASS",
    )
    DeploymentGateClient().record_evidence(oc, result)
    assert "EVIDENCE" in [e.event for e in oc.audit.entries]