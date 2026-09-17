import pytest

from openclaw.orchestrator import OpenClaw, RoutingError
from openclaw.security_gate import (
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