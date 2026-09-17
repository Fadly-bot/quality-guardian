"""Audit / event coordination for OpenClaw.

Every workflow transition, approval request, approval record, routing decision,
retry, and escalation is appended to an immutable audit log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class AuditEntry:
    """A single immutable audit/event record."""

    sequence: int
    actor: str
    event: str
    state_before: str
    state_after: str
    detail: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AuditLog:
    """Append-only audit log."""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def record(
        self,
        actor: str,
        event: str,
        state_before: str,
        state_after: str,
        detail: str = "",
    ) -> AuditEntry:
        entry = AuditEntry(
            sequence=len(self._entries),
            actor=actor,
            event=event,
            state_before=state_before,
            state_after=state_after,
            detail=detail,
        )
        self._entries.append(entry)
        return entry

    @property
    def entries(self) -> list[AuditEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def last(self) -> AuditEntry | None:
        if not self._entries:
            return None
        return self._entries[-1]