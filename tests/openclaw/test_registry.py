import pytest

from openclaw.registry import (
    AVAILABLE,
    AgentRegistry,
    AvailabilityError,
    CapabilityError,
    ContractVersionError,
    InvocationError,
    PermissionError_Registry,
    REQUIRED_AGENTS,
    UnknownAgent,
)


@pytest.fixture(scope="module")
def registry() -> AgentRegistry:
    return AgentRegistry()


def test_registry_completeness(registry):
    ids = {record.agent_id for record in registry.agents()}
    assert REQUIRED_AGENTS.issubset(ids)


def test_all_agents_have_required_fields(registry):
    required = {
        "agent_id",
        "name",
        "department",
        "role",
        "capabilities",
        "inputs",
        "outputs",
        "permissions",
        "forbidden_actions",
        "contract_version",
        "invocation_method",
        "status",
        "availability",
    }
    for record in registry.agents():
        for field in required:
            value = getattr(record, field)
            assert value not in (None, "", []), f"{record.agent_id}.{field}"
    assert registry.require_complete() == []


def test_availability_values_are_valid(registry):
    for record in registry.agents():
        assert record.availability in {
            "AVAILABLE",
            "UNAVAILABLE",
            "DEGRADED",
            "DISABLED",
        }


def test_agent_lookup(registry):
    record = registry.lookup("quality_guardian")
    assert record.name == "Quality Guardian"
    assert record.department == "Quality"


def test_unknown_agent_rejection(registry):
    with pytest.raises(UnknownAgent):
        registry.lookup("not_an_agent")


def test_unknown_agent_contains_false(registry):
    assert "not_an_agent" not in registry


def test_capability_valid(registry):
    registry.validate_capabilities("quality_guardian", ["scanner_invocation"])


def test_capability_invalid(registry):
    with pytest.raises(CapabilityError):
        registry.validate_capabilities("openclaw", ["deployment_execution"])


def test_permission_valid(registry):
    registry.validate_permissions(
        "security_gate", ["security_verification", "release_blocking"]
    )


def test_permission_denied(registry):
    with pytest.raises(PermissionError_Registry):
        registry.validate_permissions("quality_guardian", ["deploy"])


def test_contract_version_valid(registry):
    registry.validate_contract_version("handoff_agent", "3.0")


def test_contract_version_invalid(registry):
    with pytest.raises(ContractVersionError):
        registry.validate_contract_version("handoff_agent", "9.9")


def test_invocation_valid(registry):
    registry.validate_invocation("quality_guardian", "openclaw:quality_audit")


def test_invocation_invalid(registry):
    with pytest.raises(InvocationError):
        registry.validate_invocation("quality_guardian", "openclaw:security_audit")


def test_availability_valid(registry):
    registry.validate_availability("ai_council")
    assert registry.availability_of("ai_council") == AVAILABLE


def test_unavailable_agent_rejected():
    data = {
        "x_agent": {
            "agent_id": "x_agent",
            "name": "X Agent",
            "department": "Test",
            "role": "role",
            "capabilities": [],
            "inputs": [],
            "outputs": [],
            "permissions": [],
            "forbidden_actions": [],
            "contract_version": "3.0",
            "invocation_method": "test",
            "status": "ACTIVE",
            "availability": "UNAVAILABLE",
        }
    }
    reg = AgentRegistry.from_records(data)
    with pytest.raises(AvailabilityError):
        reg.validate_availability("x_agent")


def test_capability_neq_permission(registry):
    record = registry.lookup("quality_guardian")
    assert "release_decision" in record.capabilities
    assert "release_decision" not in record.permissions


def test_openclaw_forbidden_actions(registry):
    record = registry.lookup("openclaw")
    for action in [
        "grant_approval",
        "change_security_verdict",
        "change_quality_verdict",
        "treat_not_scanned_as_pass",
        "decide_release",
    ]:
        assert action in record.forbidden_actions


def test_handoff_agent_quality_guardian_separation(registry):
    handoff = registry.lookup("handoff_agent")
    quality = registry.lookup("quality_guardian")
    assert handoff.location == "/home/fadly_03/handoff-agent"
    assert quality.location == "/home/fadly_03/quality-guardian"
    assert handoff.department == "Continuity"
    assert quality.department == "Quality"
    assert "quality_approval" in handoff.forbidden_actions
    assert "deploy" in quality.forbidden_actions


def test_registry_does_not_copy_component_source():
    # Registry entries only describe; no source code is embedded.
    reg = AgentRegistry()
    handoff = reg.lookup("handoff_agent")
    assert set(handoff.capabilities) <= {
        "checkpoint_validation",
        "git_state_verification",
        "ownership_verification",
        "freshness_verification",
        "context_preservation",
        "state_continuity",
    }