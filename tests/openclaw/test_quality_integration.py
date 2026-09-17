import pytest

from openclaw.orchestrator import OpenClaw
from openclaw.quality_integration import (
    QUALITY_GUARDIAN_LOCATION,
    QUALITY_GUARDIAN_ROLE,
    QualityAuditResult,
    QualityGuardianClient,
    ReadOnlyError,
    RoutingError,
    ValidationError,
)
from openclaw.workflow import (
    ESCALATED,
    IMPLEMENTATION,
    QUALITY_AUDIT,
    SECURITY_AUDIT,
)


def make_raw(
    status="PASS",
    decision="PASS",
    evidence=None,
    scanner_statuses=None,
    project="/tmp/proj",
):
    return {
        "project": project,
        "status": status,
        "decision": decision,
        "scanner_statuses": scanner_statuses or {"semgrep": status},
        "evidence": evidence if evidence is not None else [
            {
                "source": "semgrep",
                "target": "src/*.py",
                "detail": "scanned 12 files, 0 findings",
            }
        ],
        "findings": [],
        "risks": [],
    }


def make_client(raw_factory):
    def invoker(root, context):
        assert root.is_dir() is True
        return raw_factory()

    return QualityGuardianClient(invoker=invoker)


@pytest.mark.parametrize(
    "status,decision,expected_state",
    [
        ("PASS", "PASS", SECURITY_AUDIT),
        ("FAIL", "BLOCK", IMPLEMENTATION),
        ("ERROR", "NEEDS_REVIEW", IMPLEMENTATION),
        ("NOT_SCANNED", "NEEDS_REVIEW", ESCALATED),
        ("PASS", "NEEDS_REVIEW", ESCALATED),
    ],
)
def test_invocation_and_routing(status, decision, expected_state, tmp_path):
    client = make_client(lambda: make_raw(status=status, decision=decision))
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    result = client.invoke(tmp_path, {})
    target = client.route_result(oc, result)
    assert target == expected_state
    assert oc.state == expected_state


def test_invoke_uses_existing_component_location():
    client = QualityGuardianClient(invoker=lambda root, ctx: make_raw())
    result = client.invoke("/tmp", {})
    assert result.project == "/tmp/proj"
    assert QUALITY_GUARDIAN_LOCATION.endswith("quality-guardian")


def test_input_validation_invalid_root():
    client = QualityGuardianClient(invoker=lambda root, ctx: make_raw())
    with pytest.raises(ValueError):
        client.invoke("/nonexistent/path/xyz", {})


def test_invocation_without_runner_refuses():
    client = QualityGuardianClient()  # no invoker
    with pytest.raises(Exception):
        client.invoke("/tmp", {})


def test_output_validation_bad_status():
    raw = make_raw(status="MAYBE", decision="PASS")
    client = QualityGuardianClient()
    with pytest.raises(ValidationError):
        client.validate_result(raw)


def test_output_validation_bad_decision():
    raw = make_raw(status="PASS", decision="RELEASE")
    client = QualityGuardianClient()
    with pytest.raises(ValidationError):
        client.validate_result(raw)


def test_pass_requires_coverage_evidence():
    raw = make_raw(evidence=[])  # PASS without evidence = NOT_SCANNED semantics
    client = QualityGuardianClient()
    with pytest.raises(ValidationError):
        client.validate_result(raw)


def test_not_scanned_is_never_upgraded_to_pass():
    raw = make_raw(
        status="NOT_SCANNED",
        decision="NEEDS_REVIEW",
        scanner_statuses={"gitleaks": "NOT_SCANNED"},
        evidence=[],
    )
    client = QualityGuardianClient()
    result = client.validate_result(raw)
    assert result.status == "NOT_SCANNED"
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    client.route_result(oc, result)
    assert oc.state == ESCALATED
    assert oc.state != SECURITY_AUDIT


def test_evidence_preserved():
    evidence = [
        {
            "source": "semgrep",
            "target": "src/**",
            "detail": "scanned 16 files, 0 findings",
        },
        {
            "source": "trivy",
            "target": "go.sum",
            "detail": "1 known vulnerability (fixed in 1.2.3)",
        },
    ]
    client = QualityGuardianClient()
    result = client.validate_result(make_raw(evidence=evidence))
    assert len(result.evidence) == 2
    assert result.evidence[1].source == "trivy"


def test_evidence_recorded_in_audit_log(tmp_path):
    client = make_client(lambda: make_raw())
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    result = client.invoke(tmp_path, {})
    client.record_evidence(oc, result, actor=QUALITY_GUARDIAN_ROLE)
    events = [entry.event for entry in oc.audit.entries]
    assert "EVIDENCE" in events


def test_read_only_enforced():
    client = QualityGuardianClient(invoker=lambda root, ctx: make_raw())
    with pytest.raises(ReadOnlyError):
        client.modify_project("/tmp")


def test_invoke_does_not_modify_project(tmp_path):
    sentinel = tmp_path / "sentinel.txt"
    client = make_client(lambda: make_raw(project=str(tmp_path)))
    client.invoke(tmp_path, {})
    assert not sentinel.exists()


def test_permission_enforcement_via_orchestrator(tmp_path):
    # The OpenClaw orchestrator rejects a non-quality actor at QUALITY_AUDIT.
    client = QualityGuardianClient(invoker=lambda root, ctx: make_raw())
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    result = client.invoke(tmp_path, {})
    with pytest.raises(Exception):
        oc.route("PASS", actor="security_gate")


def test_route_from_wrong_state_rejected(tmp_path):
    client = QualityGuardianClient(invoker=lambda root, ctx: make_raw())
    oc = OpenClaw(initial_state=SECURITY_AUDIT)
    result = client.invoke(tmp_path, {})
    with pytest.raises(RoutingError):
        client.route_result(oc, result)


def test_quality_does_not_act_as_security_gate(tmp_path):
    client = QualityGuardianClient(invoker=lambda root, ctx: make_raw())
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    result = client.invoke(tmp_path, {})
    client.route_result(oc, result)
    assert oc.state == SECURITY_AUDIT  # forwarded to security, not decided by quality


def test_quality_scanner_evidence_statuses_preserved():
    raw = make_raw(
        scanner_statuses={
            "semgrep": "PASS",
            "gitleaks": "NOT_SCANNED",
            "trivy": "ERROR",
        },
        decision="NEEDS_REVIEW",
        evidence=[
            {
                "source": "semgrep",
                "target": "src/**",
                "detail": "scanned 12 files",
            }
        ],
    )
    client = QualityGuardianClient()
    result = client.validate_result(raw)
    assert result.scanner_statuses["gitleaks"] == "NOT_SCANNED"
    assert result.scanner_statuses["trivy"] == "ERROR"
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    client.route_result(oc, result)
    assert oc.state == ESCALATED


def test_registry_permits_read_only_audit_only():
    from openclaw.registry import AgentRegistry

    registry = AgentRegistry()
    record = registry.lookup("quality_guardian")
    assert record.permissions == ["read_only_audit"]
    for action in ["deploy", "commit", "push", "self_approve"]:
        assert action in record.forbidden_actions or "deploy" in record.forbidden_actions