"""Memory Layer — Development F verified-state store (Phase 12).

Memory is NOT the primary source of truth. Priority is always:

    CURRENT REPOSITORY > CURRENT EVIDENCE > CURRENT DOCUMENTATION > MEMORY

This module stores only VERIFIED facts (verified checkpoints, decisions, test
results, audits, deployments, human-approved decisions). AI assumptions,
speculations, and unverified claims are rejected as untrusted. Secrets are
never stored: every entry is screened with the Security Gate's secret patterns
before it is accepted.

Security properties:

- Secret rejection: entries containing credential-like values are refused
  (write and update alike) using the same patterns as the Security Gate.
- Authorization: writes require an authorized role for the memory category;
  entries carrying human authority require the human role.
- Project isolation: every entry carries a project_id and can only be read or
  written through a store bound to that project; cross-project access raises.
- Conflict resolution: ``resolve_conflict`` always prefers current repository /
  evidence / documentation state over remembered state.
- Injection resistance: rendering is length-bounded, control characters are
  stripped, and stored content is data — never executed or interpreted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .security_gate import SECRET_PATTERNS

# --- roles / authorization ---------------------------------------------------

HUMAN_ROLE = "human"
OPENCLAW_ROLE = "openclaw"

# Role -> categories it may write. Every role may read its own project's memory.
_WRITE_AUTHORITY: dict[str, frozenset[str]] = {
    "openclaw": frozenset(
        {"task", "checkpoint", "project", "architecture"}
    ),
    "coding_agent": frozenset({"task", "checkpoint"}),
    "quality_guardian": frozenset({"quality"}),
    "security_gate": frozenset({"security"}),
    "deployment_check": frozenset({"deployment", "incident"}),
    "handoff_agent": frozenset({"checkpoint"}),
    "human": frozenset({"decision"}),
}

# Categories recording human authority require the human actor.
_HUMAN_ONLY_CATEGORIES = frozenset({"decision"})

MEMORY_CATEGORIES = frozenset(
    {
        "project",
        "architecture",
        "decision",
        "agent",
        "task",
        "checkpoint",
        "quality",
        "security",
        "deployment",
        "incident",
    }
)

VALID_TRUST = frozenset({"trusted", "untrusted"})

# Content longer than this is rejected outright (injection/abuse bound).
MAX_CONTENT_CHARS = 4000
MAX_KEY_CHARS = 200

# Control characters (except tab/newline) are stripped from stored text.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class MemoryError(Exception):
    """Base error for the memory layer."""


class SecretRejected(MemoryError):
    """Raised when content contains a credential-like value."""


class UnauthorizedWrite(MemoryError):
    """Raised when an actor lacks write authority for a category."""


class CrossProjectAccess(MemoryError):
    """Raised when an operation crosses a project isolation boundary."""


class UntrustedMemoryRejected(MemoryError):
    """Raised when unverified content is submitted as trusted memory."""


# --- data model ---------------------------------------------------------------


@dataclass
class MemoryEntry:
    """One verified (or explicitly untrusted) fact about a project."""

    project_id: str
    category: str
    key: str
    content: str
    trust: str = "untrusted"
    recorded_by: str = ""
    evidence: list[str] = field(default_factory=list)
    supersedes: str | None = None

    def trusted(self) -> bool:
        return self.trust == "trusted"


def _screen_secrets(text: str) -> None:
    """Raise SecretRejected when text matches a known secret pattern."""
    for _name, pattern in SECRET_PATTERNS:
        if re.search(pattern, text):
            raise SecretRejected(
                "Content matches a credential pattern; secrets are never stored."
            )


def _clean_text(text: str) -> str:
    """Strip control characters from stored text (injection hygiene)."""
    return _CONTROL_CHARS.sub("", text)


def _validate_content(content: str) -> str:
    if not isinstance(content, str) or not content.strip():
        raise MemoryError("Memory content must be a non-empty string.")
    if len(content) > MAX_CONTENT_CHARS:
        raise MemoryError(
            f"Memory content exceeds {MAX_CONTENT_CHARS} characters."
        )
    return _clean_text(content)


# --- conflict resolution -------------------------------------------------------

# Source-of-truth priority: CURRENT REPOSITORY > CURRENT EVIDENCE >
# CURRENT DOCUMENTATION > MEMORY > AGENT ASSUMPTION.
_SOURCE_PRIORITY = ("repository", "evidence", "documentation", "memory", "assumption")


def resolve_conflict(source: str, current_value: object, memory_value: object) -> object:
    """Resolve a conflict between a current observation and remembered state.

    Memory never overrides the current repository, evidence, or documentation.
    When the current source outranks memory, the current value wins; memory
    only wins when the caller explicitly passes a lower-priority source
    (e.g. an assumption).
    """
    if source not in _SOURCE_PRIORITY:
        raise MemoryError(f"Unknown conflict source: {source!r}")
    memory_rank = _SOURCE_PRIORITY.index("memory")
    if _SOURCE_PRIORITY.index(source) <= memory_rank:
        return current_value
    return memory_value


# --- store ---------------------------------------------------------------------


class ProjectMemory:
    """Write/read store for one project's verified memory.

    The store is bound to a single ``project_id``. Entries from other projects
    are invisible and attempting to touch them raises ``CrossProjectAccess``.
    """

    def __init__(self, project_id: str) -> None:
        if not project_id or not isinstance(project_id, str):
            raise MemoryError("project_id must be a non-empty string.")
        self.project_id = project_id
        self._entries: dict[str, MemoryEntry] = {}

    # --- internal ----------------------------------------------------------

    def _key(self, category: str, key: str) -> str:
        return f"{category}:{key}"

    def _assert_owned(self, entry: MemoryEntry) -> None:
        if entry.project_id != self.project_id:
            raise CrossProjectAccess(
                f"Entry belongs to project {entry.project_id!r}, "
                f"store is bound to {self.project_id!r}."
            )

    def _assert_write_authority(self, actor: str, category: str) -> None:
        allowed = _WRITE_AUTHORITY.get(actor, frozenset())
        if category not in allowed:
            raise UnauthorizedWrite(
                f"Actor {actor!r} may not write category {category!r}. "
                f"Allowed: {sorted(allowed)}"
            )
        if category in _HUMAN_ONLY_CATEGORIES and actor != HUMAN_ROLE:
            raise UnauthorizedWrite(
                f"Category {category!r} records human authority; "
                f"only the human may write it."
            )

    # --- write ---------------------------------------------------------------

    def write(
        self,
        actor: str,
        category: str,
        key: str,
        content: str,
        *,
        trust: str = "untrusted",
        evidence: Iterable[str] = (),
        supersedes: str | None = None,
    ) -> MemoryEntry:
        """Write an entry after authorization, trust, and secret screening.

        ``trust="trusted"`` requires verifiable evidence and an authorized
        role; unverified content is stored only as ``untrusted`` and is never
        returned by ``trusted_entries``.
        """
        if category not in MEMORY_CATEGORIES:
            raise MemoryError(f"Unknown memory category: {category!r}")
        if trust not in VALID_TRUST:
            raise MemoryError(f"Invalid trust value: {trust!r}")
        if not key or not isinstance(key, str) or len(key) > MAX_KEY_CHARS:
            raise MemoryError("Memory key must be a short non-empty string.")

        self._assert_write_authority(actor, category)
        if trust == "trusted":
            if actor == OPENCLAW_ROLE and category in _HUMAN_ONLY_CATEGORIES:
                raise UntrustedMemoryRejected(
                    "OpenClaw cannot record trusted human decisions."
                )
            if not list(evidence):
                raise UntrustedMemoryRejected(
                    "Trusted memory requires evidence; unverified claims stay "
                    "untrusted."
                )

        clean = _validate_content(content)
        _screen_secrets(clean)
        evidence_list = [_clean_text(str(e)) for e in evidence]
        for item in evidence_list:
            _screen_secrets(item)

        entry = MemoryEntry(
            project_id=self.project_id,
            category=category,
            key=key,
            content=clean,
            trust=trust,
            recorded_by=actor,
            evidence=evidence_list,
            supersedes=supersedes,
        )
        self._entries[self._key(category, key)] = entry
        return entry

    # --- read ----------------------------------------------------------------

    def read(self, category: str, key: str) -> MemoryEntry:
        entry = self._entries.get(self._key(category, key))
        if entry is None:
            raise MemoryError(
                f"No memory entry {category}:{key} for project {self.project_id!r}."
            )
        self._assert_owned(entry)
        return entry

    def update(
        self,
        actor: str,
        category: str,
        key: str,
        content: str,
        *,
        trust: str = "untrusted",
        evidence: Iterable[str] = (),
    ) -> MemoryEntry:
        """Update an existing entry (same authorization and screening as write)."""
        current = self.read(category, key)
        self._assert_owned(current)
        return self.write(
            actor,
            category,
            key,
            content,
            trust=trust,
            evidence=evidence,
        )

    def delete(self, actor: str, category: str, key: str) -> None:
        """Only the human may delete memory (tamper resistance)."""
        if actor != HUMAN_ROLE:
            raise UnauthorizedWrite("Only the human may delete memory entries.")
        self._assert_owned(self.read(category, key))
        del self._entries[self._key(category, key)]

    def trusted_entries(self, category: str | None = None) -> list[MemoryEntry]:
        """All trusted entries of this project (optionally by category)."""
        entries = [
            e for e in self._entries.values()
            if e.trusted() and (category is None or e.category == category)
        ]
        return sorted(entries, key=lambda e: (e.category, e.key))

    def render_for_prompt(self, category: str | None = None) -> str:
        """Render trusted memory as bounded, sanitized context text.

        Output is data only: control characters stripped, per-entry and total
        length bounded. It must never be interpreted as instructions.
        """
        lines: list[str] = []
        total = 0
        for entry in self.trusted_entries(category):
            line = f"[{entry.category}] {entry.key}: {entry.content}"
            line = _clean_text(line)[:500]
            if total + len(line) > MAX_CONTENT_CHARS:
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines)


def open_project_memory(project_id: str) -> ProjectMemory:
    """Open (create if needed) the memory store for a project."""
    return ProjectMemory(project_id)
