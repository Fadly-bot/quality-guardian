"""OpenClaw orchestrator — deterministic Development F pipeline coordinator.

OpenClaw routes work along the Phase 4 state machine, coordinates agent
invocation, enforces invalid-transition rejection, controls bounded retry,
coordinates human approval requests (never granting them), and records every
action to the audit log.

Guarantees enforced here:

- OpenClaw cannot grant approvals on behalf of a human.
- OpenClaw cannot skip a human gate.
- OpenClaw cannot change a quality or security verdict; it only routes on it.
- NOT_SCANNED is never treated as PASS and never routed forward.
- Invalid transitions (e.g. PROPOSAL -> PRODUCTION) are rejected.
- Retry is bounded by MAX_REPAIR_ITERATIONS, then escalated to human.
"""

from __future__ import annotations

from typing import Any

from .events import AuditLog
from .workflow import (
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
    ROLLED_BACK,
    SECURITY_AUDIT,
    WorkflowStateError,
    owner_of,
    validate_transition,
)

MAX_REPAIR_ITERATIONS = 5

HUMAN_ROLE = "human"
OPENCLAW_ROLE = "openclaw"

REPAIR = "__REPAIR__"


class OrchestratorError(Exception):
    """Base error raised by the OpenClaw orchestrator."""


class PermissionDenied(OrchestratorError):
    """Raised when an actor performs an action outside its authority."""


class HumanApprovalRequired(OrchestratorError):
    """Raised when a human gate has no satisfied approval yet."""


class RoutingError(OrchestratorError):
    """Raised for an unsupported verdict or a state that cannot be routed."""


# Routing table: state -> {verdict: target}. REPAIR is resolved dynamically.
_ROUTING: dict[str, dict[str, str]] = {
    HANDOFF_CHECKPOINT: {
        "ACCEPTED": QUALITY_AUDIT,
        "REJECTED": REPAIR,
    },
    QUALITY_AUDIT: {
        "PASS": SECURITY_AUDIT,
        # NOT_SCANNED never forwards and never counts as PASS.
        "NOT_SCANNED": ESCALATED,
        "NEEDS_REVIEW": ESCALATED,
        "FAIL": REPAIR,
        "ERROR": REPAIR,
    },
    SECURITY_AUDIT: {
        "PASS": DEPLOYMENT_CHECK,
        # NOT_SCANNED never forwards and never counts as PASS.
        "NOT_SCANNED": ESCALATED,
        "NEEDS_REVIEW": ESCALATED,
        "BLOCK": ESCALATED,
        "FAIL": REPAIR,
        "ERROR": REPAIR,
    },
    DEPLOYMENT_CHECK: {
        "PASS": HUMAN_RELEASE_APPROVAL,
        "NEEDS_REVIEW": ESCALATED,
        "FAIL": REPAIR,
        "ERROR": REPAIR,
    },
    DEPLOY: {
        "SUCCESS": POST_DEPLOY_VERIFY,
        "FAILURE": ROLLED_BACK,
    },
    POST_DEPLOY_VERIFY: {
        "PASS": PRODUCTION,
        "FAIL": ROLLED_BACK,
    },
    ROLLED_BACK: {
        "REPAIR": IMPLEMENTATION,
        "ESCALATE": ESCALATED,
    },
}

_HUMAN_GATES = {HUMAN_APPROVAL, HUMAN_RELEASE_APPROVAL}

_APPROVAL_SUCCESSOR = {
    HUMAN_APPROVAL: PLANNING,
    HUMAN_RELEASE_APPROVAL: DEPLOY,
}


class OpenClaw:
    """Deterministic orchestration of the Development F pipeline."""

    def __init__(
        self,
        max_repair_iterations: int = MAX_REPAIR_ITERATIONS,
        initial_state: str = PROPOSAL,
    ) -> None:
        self._state = initial_state
        self._max_repair = max_repair_iterations
        self._repairs = 0
        self._approvals: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, str] = {}
        self._request_seq = 0
        self._last_verdict: str | None = None
        self.audit = AuditLog()

    # --- state ------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def repair_count(self) -> int:
        return self._repairs

    @property
    def last_verdict(self) -> str | None:
        return self._last_verdict

    @property
    def max_repair_iterations(self) -> int:
        return self._max_repair

    # --- internal ---------------------------------------------------------

    def _assert_actor(self, actor: str) -> None:
        allowed = {owner_of(self._state), HUMAN_ROLE, OPENCLAW_ROLE}
        if actor not in allowed:
            raise PermissionDenied(
                f"Actor {actor!r} has no permission to act in state {self._state!r}. "
                f"Allowed: {sorted(allowed)}"
            )

    def _transition(self, target: str, actor: str, detail: str = "") -> str:
        self._assert_actor(actor)
        before = self._state
        validate_transition(before, target)
        self._state = target
        self.audit.record(
            actor=actor,
            event="TRANSITION",
            state_before=before,
            state_after=target,
            detail=detail,
        )
        return target

    def _repair_or_escalate(self, actor: str, reason: str) -> str:
        if self._repairs >= self._max_repair:
            self.audit.record(
                actor=actor,
                event="ESCALATED",
                state_before=self._state,
                state_after=ESCALATED,
                detail=f"Repair bound {self._max_repair} reached: {reason}",
            )
            return ESCALATED
        self._repairs += 1
        return IMPLEMENTATION

    # --- routing ----------------------------------------------------------

    def advance(self, target: str, actor: str, detail: str = "") -> str:
        """Advance through a non-decision state (e.g. PROPOSAL -> RESEARCH).

        The transition must be valid for the current state and the actor must
        have permission for the current state.
        """
        return self._transition(target, actor, detail=detail)

    def route(self, verdict: str, actor: str, detail: str = "") -> str:
        """Route the pipeline based on the current gate's verdict.

        OpenClaw never interprets or rewrites the verdict; it only routes on
        the value declared by the owning agent. In particular, NOT_SCANNED is
        never treated as PASS and is escalated instead.
        """
        rules = _ROUTING.get(self._state)
        if rules is None:
            raise RoutingError(f"State {self._state!r} has no routing rules.")
        if verdict not in rules:
            raise RoutingError(
                f"Unsupported verdict {verdict!r} for state {self._state!r}. "
                f"Supported: {sorted(rules)}"
            )

        self._assert_actor(actor)
        self._last_verdict = verdict
        target = rules[verdict]

        if self._state == ROLLED_BACK and actor != HUMAN_ROLE:
            raise PermissionDenied(
                f"Recovery from ROLLED_BACK is a human decision; actor {actor!r} is not allowed."
            )

        if target == REPAIR:
            target = self._repair_or_escalate(actor, f"{self._state} -> {verdict}")

        before = self._state
        validate_transition(before, target)
        self._state = target
        self.audit.record(
            actor=actor,
            event="ROUTED",
            state_before=before,
            state_after=target,
            detail=f"verdict={verdict}" + (f"; {detail}" if detail else ""),
        )
        return target

    # --- human approvals --------------------------------------------------

    def request_human_approval(self, gate: str) -> str:
        """Request approval for a human gate. OpenClaw only requests."""
        if self._state != gate:
            raise OrchestratorError(
                f"Cannot request {gate}: current state is {self._state!r}."
            )
        if gate not in _HUMAN_GATES:
            raise OrchestratorError(f"{gate!r} is not a human approval gate.")
        self._request_seq += 1
        request_id = f"{gate}-{self._request_seq}"
        self._pending[gate] = request_id
        before = self._state
        self.audit.record(
            actor=OPENCLAW_ROLE,
            event="APPROVAL_REQUESTED",
            state_before=before,
            state_after=before,
            detail=f"gate={gate} request_id={request_id}",
        )
        return request_id

    def record_human_approval(self, gate: str, approved: bool, approver: str) -> None:
        """Record an approval granted by a human.

        Only the human role may be the approver. OpenClaw (or any AI agent)
        cannot grant an approval on behalf of the human.
        """
        if approver != HUMAN_ROLE:
            raise PermissionDenied(
                f"Only the human can grant an approval; got approver {approver!r}."
            )
        if gate not in self._pending:
            raise OrchestratorError(f"No pending approval request for {gate}.")
        self._approvals[gate] = {
            "approved": approved,
            "request_id": self._pending[gate],
            "approver": approver,
        }
        del self._pending[gate]
        before = self._state
        self.audit.record(
            actor=approver,
            event="APPROVAL_RECORDED",
            state_before=before,
            state_after=before,
            detail=f"gate={gate} approved={approved}",
        )

    def continue_after_human_gate(self, actor: str = HUMAN_ROLE) -> str:
        """Continue past a human gate only after a recorded, granted approval.

        Raises ``HumanApprovalRequired`` when the current gate has no granted
        approval. A recorded rejection routes the workflow to ABORTED.
        """
        gate = self._state
        if gate not in _HUMAN_GATES:
            raise OrchestratorError(
                f"Current state {gate!r} is not a human approval gate."
            )
        approval = self._approvals.get(gate)
        if approval is None:
            raise HumanApprovalRequired(f"{gate} has not been granted yet.")
        if not approval["approved"]:
            return self._transition(ABORTED, actor, detail=f"{gate} rejected by human")
        successor = _APPROVAL_SUCCESSOR[gate]
        return self._transition(successor, actor, detail=f"{gate} approved by human")

    # --- escalation -------------------------------------------------------

    def escalate(self, actor: str, detail: str = "") -> str:
        """Escalate to the human."""
        self._assert_actor(actor)
        before = self._state
        validate_transition(before, ESCALATED)
        self._state = ESCALATED
        self.audit.record(
            actor=actor,
            event="ESCALATED",
            state_before=before,
            state_after=ESCALATED,
            detail=detail,
        )
        return ESCALATED