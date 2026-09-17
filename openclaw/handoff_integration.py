"""Handoff Agent integration for Development F.

Development F integrates the EXISTING Handoff Agent component at
``/home/fadly_03/handoff-agent``. This module is a client that:

- requests checkpoints (Context, Git State, Changes, Decisions, Next Action),
- validates checkpoints (ownership, freshness, Git state, required fields,
  valid state),
- verifies the Git truth: an inconsistent checkpoint (CURRENT GIT STATE !=
  HANDOFF CLAIM) is flagged and rejected,
- accepts/rejects handoffs and routes the pipeline through OpenClaw.

It does NOT rebuild the Handoff Agent and it does NOT grant the Handoff Agent
quality, security, or deployment authority.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .orchestrator import OpenClaw
from .workflow import (
    HANDOFF_CHECKPOINT,
    IMPLEMENTATION,
    QUALITY_AUDIT,
    STATES,
)

HANDOFF_AGENT_LOCATION = "/home/fadly_03/handoff-agent"
HANDOFF_AGENT_ROLE = "handoff_agent"
VALID_OWNERS = frozenset({"coding_agent"})

REQUIRED_CHECKPOINT_FIELDS = (
    "owner",
    "state",
    "context",
    "git_state",
    "changes",
    "decisions",
    "next_action",
    "created_at",
)


class HandoffError(Exception):
    """Base error for Handoff Agent integration."""


class CheckpointError(HandoffError):
    """Raised when a checkpoint is invalid or incomplete."""


class OwnershipError(HandoffError):
    """Raised when the checkpoint owner is invalid."""


class StaleCheckpointError(HandoffError):
    """Raised when a checkpoint is older than the freshness bound."""


class GitStateConflict(HandoffError):
    """Raised when the checkpoint Git state disagrees with the repository.

    GIT TRUTH: CURRENT GIT STATE > HANDOFF CLAIM.
    """


class HandoffSeparationError(HandoffError):
    """Raised when a handoff tried to act as quality/security/deployment."""


@dataclass
class Checkpoint:
    """Minimal checkpoint data preserved across handoff."""

    owner: str
    state: str
    context: str
    git_state: dict[str, str] = field(default_factory=dict)
    changes: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    next_action: str = ""
    created_at: float = field(default_factory=time.time)

    def missing_fields(self) -> list[str]:
        missing = []
        for field_name in REQUIRED_CHECKPOINT_FIELDS:
            value = getattr(self, field_name)
            if value in (None, "", []):
                missing.append(field_name)
        return missing


GitStateProvider = Callable[[Path], dict[str, str]]


def default_git_state(project_root: str | Path) -> dict[str, str]:
    """Read Git truth with allowlisted git subcommands (read-only)."""
    root = Path(project_root)
    commands = {
        "head": ["git", "-C", str(root), "rev-parse", "HEAD"],
        "branch": ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
    }
    state: dict[str, str] = {}
    for key, cmd in commands.items():
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=False
        )
        if result.returncode == 0:
            state[key] = result.stdout.strip()
        else:
            state[key] = ""
    return state


class HandoffClient:
    """Development F client for the existing Handoff Agent component."""

    def __init__(self, git_state_provider: GitStateProvider = default_git_state) -> None:
        self._git_state_provider = git_state_provider

    # --- git truth --------------------------------------------------------

    def git_state(self, project_root: str | Path) -> dict[str, str]:
        return self._git_state_provider(Path(project_root))

    # --- checkpoint creation ---------------------------------------------

    def create_checkpoint(
        self,
        project_root: str | Path,
        owner: str,
        context: str,
        changes: list[str],
        decisions: list[str],
        next_action: str,
        state: str = IMPLEMENTATION,
    ) -> Checkpoint:
        return Checkpoint(
            owner=owner,
            state=state,
            context=context,
            git_state=self.git_state(project_root),
            changes=changes,
            decisions=decisions,
            next_action=next_action,
        )

    # --- validation -------------------------------------------------------

    def validate_checkpoint(
        self,
        checkpoint: Checkpoint,
        actual_git_state: Mapping[str, str],
        now: float | None = None,
        max_age_seconds: float = 3600.0,
        expected_owner: str = "coding_agent",
    ) -> None:
        """Validate a checkpoint. Raises on any invalid condition."""
        missing = checkpoint.missing_fields()
        if missing:
            raise CheckpointError(
                f"Checkpoint missing required fields: {sorted(missing)}"
            )

        if checkpoint.owner not in VALID_OWNERS:
            raise OwnershipError(
                f"Invalid checkpoint owner {checkpoint.owner!r}; "
                f"valid owners: {sorted(VALID_OWNERS)}"
            )
        if expected_owner is not None and checkpoint.owner != expected_owner:
            raise OwnershipError(
                f"Checkpoint owner {checkpoint.owner!r} != expected {expected_owner!r}"
            )

        if checkpoint.state not in STATES:
            raise CheckpointError(f"Invalid checkpoint state: {checkpoint.state!r}")

        current = now if now is not None else time.time()
        if current - checkpoint.created_at > max_age_seconds:
            raise StaleCheckpointError(
                f"Checkpoint is stale: age {current - checkpoint.created_at:.0f}s "
                f"> max {max_age_seconds:.0f}s"
            )

        if dict(checkpoint.git_state) != dict(actual_git_state):
            raise GitStateConflict(
                "Git state mismatch: CURRENT GIT STATE > HANDOFF CLAIM "
                f"(claim={checkpoint.git_state!r}, actual={dict(actual_git_state)!r})"
            )

    def is_valid(
        self,
        checkpoint: Checkpoint,
        actual_git_state: Mapping[str, str],
        now: float | None = None,
        max_age_seconds: float = 3600.0,
        expected_owner: str = "coding_agent",
    ) -> bool:
        try:
            self.validate_checkpoint(
                checkpoint,
                actual_git_state,
                now=now,
                max_age_seconds=max_age_seconds,
                expected_owner=expected_owner,
            )
        except HandoffError:
            return False
        return True

    # --- routing ----------------------------------------------------------

    def route_checkpoint(
        self,
        oc: OpenClaw,
        checkpoint: Checkpoint,
        actual_git_state: Mapping[str, str],
        now: float | None = None,
        max_age_seconds: float = 3600.0,
        expected_owner: str = "coding_agent",
    ) -> str:
        """Validate and route the checkpoint through OpenClaw.

        A valid checkpoint is ACCEPTED (→ QUALITY_AUDIT). Any invalid
        checkpoint (missing fields, invalid owner/state, stale, or a Git-state
        conflict) is REJECTED (→ IMPLEMENTATION) with the reason recorded in
        the audit log.
        """
        if oc.state != HANDOFF_CHECKPOINT:
            from .orchestrator import RoutingError

            raise RoutingError(
                f"Expected HANDOFF_CHECKPOINT, got {oc.state!r}"
            )
        try:
            self.validate_checkpoint(
                checkpoint,
                actual_git_state,
                now=now,
                max_age_seconds=max_age_seconds,
                expected_owner=expected_owner,
            )
        except HandoffError as exc:
            oc.audit.record(
                actor=HANDOFF_AGENT_ROLE,
                event="HANDOFF_REJECTED",
                state_before=oc.state,
                state_after=IMPLEMENTATION,
                detail=str(exc),
            )
            return oc.route("REJECTED", actor=HANDOFF_AGENT_ROLE)
        return oc.route("ACCEPTED", actor=HANDOFF_AGENT_ROLE)

    # --- separation enforcement -------------------------------------------

    def quality_approval(self, *args, **kwargs) -> None:
        raise HandoffSeparationError(
            "Handoff Agent cannot issue quality approval."
        )

    def security_approval(self, *args, **kwargs) -> None:
        raise HandoffSeparationError(
            "Handoff Agent cannot issue security approval."
        )

    def deployment_approval(self, *args, **kwargs) -> None:
        raise HandoffSeparationError(
            "Handoff Agent cannot issue deployment approval."
        )