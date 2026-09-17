import time

import pytest

from openclaw.handoff_integration import (
    HANDOFF_AGENT_LOCATION,
    HANDOFF_AGENT_ROLE,
    Checkpoint,
    CheckpointError,
    GitStateConflict,
    HandoffClient,
    HandoffSeparationError,
    OwnershipError,
    StaleCheckpointError,
)
from openclaw.orchestrator import OpenClaw
from openclaw.workflow import (
    HANDOFF_CHECKPOINT,
    IMPLEMENTATION,
    QUALITY_AUDIT,
)

GIT_STATE = {"head": "abc123", "branch": "main"}


def make_checkpoint(created_at=None, owner="coding_agent", state=IMPLEMENTATION, **kw):
    return Checkpoint(
        owner=owner,
        state=state,
        context=kw.get("context", "Integrated feature X"),
        git_state=kw.get("git_state", dict(GIT_STATE)),
        changes=kw.get("changes", ["src/app.py", "tests/test_app.py"]),
        decisions=kw.get("decisions", ["Chose library Y"]),
        next_action=kw.get("next_action", "Quality audit"),
        created_at=created_at if created_at is not None else time.time(),
    )


def make_client():
    return HandoffClient(git_state_provider=lambda root: dict(GIT_STATE))


def test_client_points_to_existing_component():
    assert HANDOFF_AGENT_LOCATION == "/home/fadly_03/handoff-agent"
    client = make_client()
    assert client.git_state("/tmp") == GIT_STATE


def test_checkpoint_creation_preserves_required_data(tmp_path):
    client = make_client()
    cp = client.create_checkpoint(
        tmp_path,
        owner="coding_agent",
        context="Integrated feature X",
        changes=["src/app.py"],
        decisions=["Chose library Y"],
        next_action="Quality audit",
    )
    assert cp.owner == "coding_agent"
    assert cp.context == "Integrated feature X"
    assert cp.git_state == GIT_STATE
    assert cp.next_action == "Quality audit"
    assert cp.missing_fields() == []


def test_checkpoint_missing_required_fields_rejected():
    cp = make_checkpoint()
    cp.next_action = ""
    client = make_client()
    with pytest.raises(CheckpointError):
        client.validate_checkpoint(cp, GIT_STATE)


def test_checkpoint_invalid_owner_rejected():
    cp = make_checkpoint(owner="quality_guardian")
    client = make_client()
    with pytest.raises(OwnershipError):
        client.validate_checkpoint(cp, GIT_STATE)


def test_checkpoint_wrong_expected_owner_rejected():
    cp = make_checkpoint(owner="coding_agent")
    client = make_client()
    with pytest.raises(OwnershipError):
        client.validate_checkpoint(cp, GIT_STATE, expected_owner="nobody")


def test_checkpoint_invalid_state_rejected():
    cp = make_checkpoint(state="NOT_A_STATE")
    client = make_client()
    with pytest.raises(CheckpointError):
        client.validate_checkpoint(cp, GIT_STATE)


def test_stale_checkpoint_rejected():
    cp = make_checkpoint(created_at=time.time() - 9999)
    client = make_client()
    with pytest.raises(StaleCheckpointError):
        client.validate_checkpoint(cp, GIT_STATE, max_age_seconds=3600)


def test_fresh_checkpoint_accepted():
    cp = make_checkpoint(created_at=time.time() - 5)
    client = make_client()
    client.validate_checkpoint(cp, GIT_STATE, max_age_seconds=3600)


def test_git_state_conflict_flagged():
    cp = make_checkpoint()  # claims GIT_STATE
    actual = {"head": "different", "branch": "main"}
    client = make_client()
    with pytest.raises(GitStateConflict):
        client.validate_checkpoint(cp, actual)
    assert client.is_valid(cp, actual) is False


def test_valid_checkpoint_routes_to_quality():
    oc = OpenClaw(initial_state=HANDOFF_CHECKPOINT)
    client = make_client()
    cp = make_checkpoint()
    target = client.route_checkpoint(oc, cp, GIT_STATE)
    assert target == QUALITY_AUDIT
    assert oc.state == QUALITY_AUDIT


def test_invalid_checkpoint_routes_to_repair():
    oc = OpenClaw(initial_state=HANDOFF_CHECKPOINT)
    client = make_client()
    cp = make_checkpoint(owner="quality_guardian")
    target = client.route_checkpoint(oc, cp, GIT_STATE)
    assert target == IMPLEMENTATION
    assert oc.state == IMPLEMENTATION
    assert oc.repair_count == 1


def test_git_conflict_checkpoint_routes_to_repair(tmp_path):
    oc = OpenClaw(initial_state=HANDOFF_CHECKPOINT)
    client = make_client()
    cp = make_checkpoint()
    actual = {"head": "uncommitted", "branch": "main"}
    target = client.route_checkpoint(oc, cp, actual)
    assert target == IMPLEMENTATION
    events = [entry.event for entry in oc.audit.entries]
    assert "HANDOFF_REJECTED" in events
    assert any("Git state mismatch" in entry.detail for entry in oc.audit.entries)


def test_stale_checkpoint_routes_to_repair():
    oc = OpenClaw(initial_state=HANDOFF_CHECKPOINT)
    client = make_client()
    cp = make_checkpoint(created_at=time.time() - 9999)
    target = client.route_checkpoint(oc, cp, GIT_STATE, max_age_seconds=60)
    assert target == IMPLEMENTATION


def test_context_preserved_across_handoff():
    client = make_client()
    cp = make_checkpoint()
    assert cp.context == "Integrated feature X"
    assert cp.decisions == ["Chose library Y"]


def test_state_continuity_quality_to_handoff():
    # Continuity flows: handoff accepted -> quality audit -> security.
    from openclaw.workflow import SECURITY_AUDIT

    oc = OpenClaw(initial_state=HANDOFF_CHECKPOINT)
    client = make_client()
    cp = make_checkpoint()
    client.route_checkpoint(oc, cp, GIT_STATE)
    oc.route("PASS", actor="quality_guardian")
    assert oc.state == SECURITY_AUDIT
    assert oc.state != QUALITY_AUDIT


def test_handoff_cannot_quality_approve():
    client = make_client()
    with pytest.raises(HandoffSeparationError):
        client.quality_approval()


def test_handoff_cannot_security_approve():
    client = make_client()
    with pytest.raises(HandoffSeparationError):
        client.security_approval()


def test_handoff_cannot_deployment_approve():
    client = make_client()
    with pytest.raises(HandoffSeparationError):
        client.deployment_approval()


def test_handoff_separate_from_quality_guardian():
    from openclaw.registry import AgentRegistry

    registry = AgentRegistry()
    handoff = registry.lookup("handoff_agent")
    assert "quality_approval" in handoff.forbidden_actions
    assert "security_approval" in handoff.forbidden_actions
    assert "deployment_approval" in handoff.forbidden_actions


def test_route_from_wrong_state_rejected():
    oc = OpenClaw(initial_state=QUALITY_AUDIT)
    client = make_client()
    cp = make_checkpoint()
    with pytest.raises(Exception):
        client.route_checkpoint(oc, cp, GIT_STATE)


def test_registry_points_handoff_to_component():
    from openclaw.registry import AgentRegistry

    registry = AgentRegistry()
    record = registry.lookup("handoff_agent")
    assert record.location == "/home/fadly_03/handoff-agent"
    assert record.department == "Continuity"