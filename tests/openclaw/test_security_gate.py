import pytest

from openclaw.orchestrator import OpenClaw, RoutingError
from openclaw.security_gate import (
    ScannerRun,
    SecurityCheck,
    SecurityGate,
    SecurityGateClient,
    SecurityGateError,
    SecurityGateResult,
    SecurityGateSeparationError,
    ValidationError,
    _decide,
    find_secrets,
)
from openclaw.workflow import (
    DEPLOYMENT_CHECK,
    ESCALATED,
    HANDOFF_CHECKPOINT,
    IMPLEMENTATION,
    QUALITY_AUDIT,
    SECURITY_AUDIT,
)


def _back_to_security(oc: OpenClaw) -> None:
    """Re-route the pipeline from IMPLEMENTATION back to SECURITY_AUDIT."""
    oc.advance(HANDOFF_CHECKPOINT, actor="coding_agent")
    oc.route("ACCEPTED", actor="handoff_agent")
    oc.route("PASS", actor="quality_guardian")


def make_result(*statuses, decision="PASS"):
    checks = [
        SecurityCheck(check_id=f"c{i}", category="repository", description="d", status=s)
        for i, s in enumerate(statuses)
    ]
    return SecurityGateResult(project="p", checks=checks, decision=decision)


def test_decision_precedence_fail_wins():
    assert _decide(make_result("PASS", "FAIL", "NOT_SCANNED").checks) == "FAIL"
    assert _decide(make_result("ERROR", "NOT_SCANNED").checks) == "ERROR"
    assert _decide(make_result("NOT_SCANNED", "NEEDS_REVIEW").checks) == "NOT_SCANNED"
    assert _decide(make_result("NEEDS_REVIEW", "PASS").checks) == "NEEDS_REVIEW"
    assert _decide(make_result("PASS", "NOT_APPLICABLE").checks) == "PASS"


def test_decision_pass_rejected_with_blocking_checks():
    with pytest.raises(ValidationError):
        make_result("PASS", "NOT_SCANNED", decision="PASS").validate()
    with pytest.raises(ValidationError):
        make_result("PASS", "ERROR", decision="PASS").validate()
    with pytest.raises(ValidationError):
        make_result("PASS", "NEEDS_REVIEW", decision="PASS").validate()


def test_decision_fail_requires_failure():
    with pytest.raises(ValidationError):
        make_result("PASS", decision="FAIL").validate()
    assert make_result("FAIL", decision="FAIL").validate().decision == "FAIL"


def test_secret_patterns_detected(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "code.py").write_text("token = 'ghp_%s'" % ("A" * 40))
    (tmp_path / ".env").write_text("AWS_SECRET_ACCESS_KEY='%s'" % ("B" * 40))
    (tmp_path / "server.key").write_text("-----BEGIN RSA PRIVATE KEY-----")
    matches = find_secrets([(p.relative_to(tmp_path), p) for p in tmp_path.rglob("*")])
    kinds = {m["kind"] for m in matches}
    assert "github_token" in kinds
    assert "aws_secret_access_key" in kinds
    assert "sensitive_file" in kinds


def test_clean_tmp_project_passes_builtins(tmp_path):
    (tmp_path / "code.py").write_text("def f():\n    return 1\n")
    (tmp_path / ".gitignore").write_text(".env\n.env.*\n*.env\n*.pem\n*.key\nid_rsa\n")
    gate = SecurityGate()
    result = gate.run(tmp_path)
    passed = {c.check_id for c in result.checks if c.status == "PASS"}
    assert "repo.secret_scan" in passed
    assert "app.static_analysis" in passed
    assert "repo.gitignore" in passed
    assert "repo.sensitive_files" in passed


def test_static_analysis_flags_real_call_but_not_literal(tmp_path):
    (tmp_path / "bad.py").write_text("import os\nos.system('ls')\n")
    (tmp_path / "literal.py").write_text('marker = "os.system(" \n')
    gate = SecurityGate()
    result = gate.run(tmp_path)
    app = next(c for c in result.checks if c.check_id == "app.static_analysis")
    assert app.status == "FAIL"
    assert any("bad.py" in e for e in app.evidence)
    assert not any("literal.py" in e for e in app.evidence)


def test_planted_secret_causes_fail_with_evidence(tmp_path):
    (tmp_path / "secret.txt").write_text("api_key = 'sk_live_%s'" % ("C" * 20))
    gate = SecurityGate()
    result = gate.run(tmp_path)
    scan = next(c for c in result.checks if c.check_id == "repo.secret_scan")
    assert scan.status == "FAIL"
    assert result.decision == "FAIL"


def test_user_session_transcripts_excluded_from_secret_scan(tmp_path):
    # Regression: user-owned session transcripts quoting the *test fixture*
    # string must not be scanned as Development F evidence; a real planted
    # secret in a project file is still detected.
    (tmp_path / "session-ses_123.md").write_text(
        "transcript quoting fixture: api_key = 'sk_live_%s'" % ("C" * 20)
    )
    (tmp_path / "session-ses_456.md").write_text("ghp_%s" % ("A" * 40))
    (tmp_path / "lanjut.md").write_text("password = '%s'" % ("D" * 20))
    (tmp_path / "code.py").write_text("def f():\n    return 1\n")
    (tmp_path / ".gitignore").write_text(".env\n*.env\n*.pem\n*.key\nid_rsa\n")
    gate = SecurityGate()
    result = gate.run(tmp_path)
    scan = next(c for c in result.checks if c.check_id == "repo.secret_scan")
    assert scan.status == "PASS"
    scanned = [e for e in scan.evidence if e.startswith("scanned_files=")]
    # code.py + .gitignore are in scope; all user artifacts are excluded.
    assert scanned == ["scanned_files=2"]
    # The aggregate decision is governed by unrelated checks (e.g. registry
    # presence); NOT_SCANNED-vs-FAIL decision behavior is pinned by
    # test_unavailable_external_scanners_are_not_scanned.


def test_planted_secret_in_project_file_still_fails_despite_exclusions(tmp_path):
    (tmp_path / "session-ses_123.md").write_text("harmless transcript")
    (tmp_path / "lanjut.md").write_text("harmless continuation prompt")
    (tmp_path / "config.py").write_text("api_key = 'sk_live_%s'" % ("C" * 20))
    (tmp_path / ".gitignore").write_text(".env\n*.env\n*.pem\n*.key\nid_rsa\n")
    gate = SecurityGate()
    result = gate.run(tmp_path)
    scan = next(c for c in result.checks if c.check_id == "repo.secret_scan")
    assert scan.status == "FAIL"
    assert any("config.py" in e for e in scan.evidence)
    assert result.decision == "FAIL"


def test_unavailable_external_scanners_are_not_scanned():
    gate = SecurityGate(tool_available=lambda tool: False)
    result = gate.run(".")
    not_scanned = result.not_scanned()
    ids = {c.check_id for c in not_scanned}
    assert "repo.gitleaks" in ids
    assert "dep.pip-audit" in ids
    assert "dep.osv-scanner" in ids
    assert "app.semgrep" in ids
    assert "repo.branch_protection" in ids
    assert result.decision == "NOT_SCANNED"


def test_real_repo_builtins_pass():
    gate = SecurityGate()
    result = gate.run(".")
    passed = {c.check_id for c in result.checks if c.status == "PASS"}
    assert "app.static_analysis" in passed
    assert "ai.permission_enforcement" in passed
    assert "ai.approval_boundary" in passed
    assert "ai.registry_forbidden_actions" in passed
    assert "gov.deploy_authorization" in passed
    assert "gov.not_scanned_never_pass" in passed
    assert "gov.gate_ownership" in passed
    assert "repo.gitignore" in passed


def test_routing_pass_to_deployment_check():
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    result = make_result("PASS", decision="PASS")
    assert SecurityGateClient().route_result(oc, result) == DEPLOYMENT_CHECK
    assert oc.state == DEPLOYMENT_CHECK


def test_routing_fail_to_repair():
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    result = make_result("FAIL", decision="FAIL")
    assert SecurityGateClient().route_result(oc, result) == IMPLEMENTATION
    assert oc.repair_count == 1


def test_routing_not_scanned_escalates():
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    result = make_result("NOT_SCANNED", decision="NOT_SCANNED")
    assert SecurityGateClient().route_result(oc, result) == ESCALATED
    assert oc.last_verdict == "NOT_SCANNED"


def test_routing_needs_review_escalates():
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    result = make_result("NEEDS_REVIEW", decision="NEEDS_REVIEW")
    assert SecurityGateClient().route_result(oc, result) == ESCALATED
    assert oc.last_verdict == "NEEDS_REVIEW"


def test_routing_error_to_repair():
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    result = make_result("ERROR", decision="ERROR")
    assert SecurityGateClient().route_result(oc, result) == IMPLEMENTATION


def test_route_from_wrong_state_rejected():
    oc = OpenClaw(initial_state=DEPLOYMENT_CHECK)
    result = make_result("PASS", decision="PASS")
    with pytest.raises(RoutingError):
        SecurityGateClient().route_result(oc, result)


def test_repair_bound_escalates():
    oc = OpenClaw(initial_state=SECURITY_AUDIT, max_repair_iterations=2)
    client = SecurityGateClient()
    result = make_result("FAIL", decision="FAIL")
    assert client.route_result(oc, result) == IMPLEMENTATION
    _back_to_security(oc)
    assert client.route_result(oc, result) == IMPLEMENTATION
    _back_to_security(oc)
    assert oc.repair_count == 2
    assert client.route_result(oc, result) == ESCALATED
    assert oc.state == ESCALATED


def test_separation_no_quality_override():
    client = SecurityGateClient()
    with pytest.raises(SecurityGateSeparationError):
        client.override_quality_verdict(oc=None, result=None)


def test_separation_no_deploy():
    with pytest.raises(SecurityGateSeparationError):
        SecurityGateClient().deploy()


def test_separation_no_approval_grant():
    with pytest.raises(SecurityGateSeparationError):
        SecurityGateClient().grant_approval()


def test_need_review_result():
    result = SecurityGate().need_review("critical finding requires human decision")
    assert result.decision == "NEEDS_REVIEW"
    assert result.validate().decision == "NEEDS_REVIEW"


def test_evidence_recorded_to_audit():
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    client = SecurityGateClient()
    checks = [
        SecurityCheck(check_id="repo.secret_scan", category="repository",
                      description="d", status="PASS",
                      evidence=["scanned_files=10"])
    ]
    client.record_evidence(oc, SecurityGateResult(project="p", checks=checks, decision="PASS"))
    events = [e.event for e in oc.audit.entries]
    assert "EVIDENCE" in events


def test_gate_boundary_quality_disjoint():
    from openclaw.security_gate import SECURITY_GATE_ROLE
    from openclaw.workflow import owner_of

    assert owner_of(SECURITY_AUDIT) == SECURITY_GATE_ROLE
    assert owner_of(__import__("openclaw.workflow", fromlist=["QUALITY_AUDIT"]).QUALITY_AUDIT) == "quality_guardian"


def test_categories_complete():
    gate = SecurityGate()
    result = gate.run(".")
    categories = {c.category for c in result.checks}
    assert categories >= {
        "repository", "dependency", "application", "infrastructure",
        "ai_agent", "governance",
    }


# --- controlled environment scanner runner wiring ---------------------------


def _clean_run(tool: str, root) -> ScannerRun:
    return ScannerRun(tool=tool, target=str(root), found=0)


def _finding_run(tool: str, root) -> ScannerRun:
    return ScannerRun(tool=tool, target=str(root), found=2,
                      findings=["tool/a.py:3 leak", "tool/b.py:9 leak"])


def _error_run(tool: str, root) -> ScannerRun:
    return ScannerRun(tool=tool, target=str(root), found=-1,
                      error="scan crashed")


def test_default_no_runner_keeps_external_scanners_not_scanned():
    gate = SecurityGate()
    result = gate.run(".")
    not_scanned = {c.check_id for c in result.not_scanned()}
    assert {"repo.gitleaks", "app.semgrep", "dep.pip-audit",
            "dep.osv-scanner", "repo.branch_protection"} <= not_scanned


def test_runner_clean_scans_pass():
    gate = SecurityGate(
        tool_available=lambda tool: True,
        scanner_runner=_clean_run,
    )
    result = gate.run(".")
    ids = {c.check_id: c.status for c in result.checks}
    assert ids["repo.gitleaks"] == "PASS"
    assert ids["app.semgrep"] == "PASS"
    assert ids["dep.pip-audit"] == "PASS"
    assert ids["dep.osv-scanner"] == "PASS"


def test_runner_findings_fail():
    gate = SecurityGate(
        tool_available=lambda tool: True,
        scanner_runner=_finding_run,
    )
    result = gate.run(".")
    ids = {c.check_id: c.status for c in result.checks}
    assert ids["repo.gitleaks"] == "FAIL"
    assert ids["app.semgrep"] == "FAIL"
    assert result.decision == "FAIL"
    check = next(c for c in result.checks if c.check_id == "repo.gitleaks")
    assert check.evidence == ["tool/a.py:3 leak", "tool/b.py:9 leak"]


def test_runner_error_is_error():
    gate = SecurityGate(
        tool_available=lambda tool: True,
        scanner_runner=_error_run,
    )
    result = gate.run(".")
    ids = {c.check_id: c.status for c in result.checks}
    assert ids["dep.osv-scanner"] == "ERROR"
    assert result.decision == "ERROR"


def test_runner_with_missing_tool_stays_not_scanned():
    gate = SecurityGate(
        tool_available=lambda tool: False,
        scanner_runner=_clean_run,
    )
    result = gate.run(".")
    ids = {c.check_id: c.status for c in result.checks}
    assert ids["repo.gitleaks"] == "NOT_SCANNED"
    assert ids["app.semgrep"] == "NOT_SCANNED"


def test_runner_pass_validation_still_blocks_not_scanned():
    # Even with a runner, a repo.branch_protection without host evidence is
    # NOT_SCANNED, so decision can never become PASS by skipping a check.
    gate = SecurityGate(
        tool_available=lambda tool: True,
        scanner_runner=_clean_run,
    )
    assert gate.run(".").decision in {"NOT_SCANNED", "FAIL", "ERROR"}


def test_branch_protection_host_evidence_verified():
    gate = SecurityGate(
        scanner_runner=_clean_run,
        tool_available=lambda tool: True,
        host_evidence={"branch_protection": {"main": "2026-09-17"}},
    )
    result = gate.run(".")
    bp = next(c for c in result.checks if c.check_id == "repo.branch_protection")
    assert bp.status == "PASS"
    assert "host-verified" in bp.detail


def test_branch_protection_without_evidence_not_scanned():
    gate = SecurityGate(scanner_runner=_clean_run, tool_available=lambda tool: True)
    bp = next(c for c in gate.run(".").checks if c.check_id == "repo.branch_protection")
    assert bp.status == "NOT_SCANNED"


def test_full_provisioned_sandbox_reaches_pass(tmp_path):
    # Controlled environment with all scanners wired, host evidence present,
    # and a fixture repository that passes every built-in check.
    import json
    import subprocess
    from pathlib import Path

    repo = Path(tmp_path)
    (repo / ".gitignore").write_text(".env\n.env.*\n*.env\n*.pem\n*.key\nid_rsa\nid_dsa\n")
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
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=False)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=False)
    subprocess.run(["git", "add", "."], cwd=repo, check=False)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repo, check=False)
    gate = SecurityGate(
        scanner_runner=_clean_run,
        tool_available=lambda tool: True,
        host_evidence={"branch_protection": {"main": "2026-09-17"}},
    )
    result = gate.run(repo)
    block = [c.check_id for c in result.checks
             if c.status in {"FAIL", "ERROR", "NOT_SCANNED", "NEEDS_REVIEW"}]
    assert not block
    assert result.decision == "PASS"