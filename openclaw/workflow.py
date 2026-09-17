"""Deterministic Development F workflow state machine (Phase 4).

Defines the states, valid transitions, owners, and transition validation used
by the OpenClaw orchestrator. Invalid transitions (e.g. PROPOSAL -> PRODUCTION)
are rejected with ``WorkflowStateError``.
"""

from __future__ import annotations

from typing import Dict, FrozenSet

# --- States ----------------------------------------------------------------

PROPOSAL = "PROPOSAL"
RESEARCH = "RESEARCH"
COUNCIL_REVIEW = "COUNCIL_REVIEW"
HUMAN_APPROVAL = "HUMAN_APPROVAL"
PLANNING = "PLANNING"
IMPLEMENTATION = "IMPLEMENTATION"
HANDOFF_CHECKPOINT = "HANDOFF_CHECKPOINT"
QUALITY_AUDIT = "QUALITY_AUDIT"
SECURITY_AUDIT = "SECURITY_AUDIT"
DEPLOYMENT_CHECK = "DEPLOYMENT_CHECK"
HUMAN_RELEASE_APPROVAL = "HUMAN_RELEASE_APPROVAL"
DEPLOY = "DEPLOY"
POST_DEPLOY_VERIFY = "POST_DEPLOY_VERIFY"
PRODUCTION = "PRODUCTION"

ABORTED = "ABORTED"
ESCALATED = "ESCALATED"
ROLLED_BACK = "ROLLED_BACK"

STATES: FrozenSet[str] = frozenset(
    {
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
        ABORTED,
        ESCALATED,
        ROLLED_BACK,
    }
)

TERMINAL_STATES: FrozenSet[str] = frozenset({PRODUCTION, ABORTED, ESCALATED})

# --- Valid transitions -----------------------------------------------------

TRANSITIONS: Dict[str, FrozenSet[str]] = {
    PROPOSAL: frozenset({RESEARCH}),
    RESEARCH: frozenset({COUNCIL_REVIEW}),
    COUNCIL_REVIEW: frozenset({RESEARCH, HUMAN_APPROVAL, ABORTED}),
    HUMAN_APPROVAL: frozenset({PLANNING, COUNCIL_REVIEW, ABORTED}),
    PLANNING: frozenset({IMPLEMENTATION}),
    IMPLEMENTATION: frozenset({HANDOFF_CHECKPOINT}),
    HANDOFF_CHECKPOINT: frozenset({QUALITY_AUDIT, IMPLEMENTATION}),
    QUALITY_AUDIT: frozenset({SECURITY_AUDIT, IMPLEMENTATION, ESCALATED}),
    SECURITY_AUDIT: frozenset({DEPLOYMENT_CHECK, IMPLEMENTATION, ESCALATED}),
    DEPLOYMENT_CHECK: frozenset({HUMAN_RELEASE_APPROVAL, IMPLEMENTATION, ESCALATED}),
    HUMAN_RELEASE_APPROVAL: frozenset({DEPLOY, ABORTED, ESCALATED}),
    DEPLOY: frozenset({POST_DEPLOY_VERIFY, ROLLED_BACK}),
    POST_DEPLOY_VERIFY: frozenset({PRODUCTION, ROLLED_BACK}),
    ROLLED_BACK: frozenset({IMPLEMENTATION, ESCALATED}),
    PRODUCTION: frozenset(),
    ABORTED: frozenset(),
    ESCALATED: frozenset(),
}

# --- Owners ----------------------------------------------------------------

OWNERS: Dict[str, str] = {
    PROPOSAL: "ai_council",
    RESEARCH: "ai_council",
    COUNCIL_REVIEW: "ai_council",
    HUMAN_APPROVAL: "human",
    PLANNING: "project_council",
    IMPLEMENTATION: "coding_agent",
    HANDOFF_CHECKPOINT: "handoff_agent",
    QUALITY_AUDIT: "quality_guardian",
    SECURITY_AUDIT: "security_gate",
    DEPLOYMENT_CHECK: "deployment_check",
    HUMAN_RELEASE_APPROVAL: "human",
    DEPLOY: "deployment_check",
    POST_DEPLOY_VERIFY: "deployment_check",
    PRODUCTION: "human",
    ABORTED: "human",
    ESCALATED: "human",
    ROLLED_BACK: "deployment_check",
}


class WorkflowStateError(Exception):
    """Raised when a transition is invalid for the current state."""


def can_transition(current: str, target: str) -> bool:
    """Return True if `current -> target` is a valid workflow transition."""
    if current not in TRANSITIONS:
        return False
    return target in TRANSITIONS[current]


def valid_transitions(state: str) -> FrozenSet[str]:
    """Return every valid successor state for `state`."""
    return TRANSITIONS.get(state, frozenset())


def owner_of(state: str) -> str:
    """Return the owner agent identifier of `state`."""
    return OWNERS[state]


def validate_transition(current: str, target: str) -> str:
    """Validate and return `target`, raising ``WorkflowStateError`` otherwise."""
    if current not in STATES or target not in STATES:
        raise WorkflowStateError(
            f"Unknown state: current={current!r} target={target!r}"
        )
    if not can_transition(current, target):
        raise WorkflowStateError(
            f"Invalid transition: {current} -> {target}. "
            f"Valid successors: {sorted(valid_transitions(current))}"
        )
    return target