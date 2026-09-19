"""Memory Layer — Development F verified-state store (Phases 12 & 15).

Memory is NOT the primary source of truth. Priority is always:

    CURRENT REPOSITORY > CURRENT EVIDENCE > CURRENT DOCUMENTATION > MEMORY

This module stores VERIFIED facts (verified checkpoints, decisions, test
results, audits, deployments, human-approved decisions) plus explicitly
untrusted records. AI assumptions, speculations, and unverified claims are
kept only as untrusted data and are never presented as verified. Secrets are
never stored: every record is screened with the Security Gate's secret
patterns before it is accepted.

Phase 15 evolution (persistent memory — see ``openclaw.persistent_memory.py``
for the storage engine):

- Record contract: every record carries ``id``, ``category``,
  ``project_identity``, ``content``, ``created_at``, ``updated_at``,
  ``source``, trust/classification metadata, ``version`` and
  ``schema_version``.
- Immutability/auditability: writes to an existing key create a new VERSION
  (previous versions stay retrievable via ``history``); deletion records a
  tombstone instead of destroying history. Old facts are never silently
  removed.
- Write classification: records are classified VERIFIED / DERIVED / PROPOSED /
  UNVERIFIED / HUMAN_APPROVED / HUMAN_REJECTED (Phase 12 trust mapping:
  ``trusted`` <=> VERIFIED or HUMAN_APPROVED; ``untrusted`` <=> the rest).
  Persistence never promotes agent-derived data to verified fact.
- Read policy: memory is DATA. ``render_for_prompt`` produces bounded,
  sanitized context text only. Nothing in memory acquires authority from being
  stored; instructions found in memory have no effect.
- Provenance: every record records where it came from (repository, evidence,
  documentation, agent, human, checkpoint).
- Retrieval: project-scoped, category-filtered, deterministic ordering,
  strictly bounded (never unbounded scans).
- Persistence: when constructed with a ``storage`` backend every mutation is a
  locked read-modify-write transaction over durable storage; without one the
  Phase 12 in-RAM behavior is preserved unchanged.

Security properties (Phase 12, preserved):

- Secret rejection: records containing credential-like values are refused
  (write and update alike) using the same patterns as the Security Gate.
- Authorization: writes require an authorized role for the memory category;
  records carrying human authority require the human role.
- Project isolation: every record carries a project_id/identity and can only
  be read or written through a store bound to that project; cross-project
  access raises.
- Conflict resolution: ``resolve_conflict`` always prefers current repository /
  evidence / documentation state over remembered state.
- Injection resistance: rendering is length-bounded, control characters are
  stripped, and stored content is data — never executed or interpreted.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
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

# --- Phase 15 contract constants ----------------------------------------------

# Schema version of the memory record/state contract. Bumped when the record
# or on-disk shape changes; see openclaw.persistent_memory for migration.
MEMORY_SCHEMA_VERSION = 2

# Where a record's information came from (provenance).
VALID_SOURCES = frozenset(
    {"repository", "evidence", "documentation", "agent", "human", "checkpoint"}
)

# Phase 15 write classification. Mapping to the Phase 12 trust axis:
#   trusted   <=> VERIFIED | HUMAN_APPROVED
#   untrusted <=> DERIVED | PROPOSED | UNVERIFIED | HUMAN_REJECTED
VALID_CLASSIFICATIONS = frozenset(
    {"VERIFIED", "DERIVED", "PROPOSED", "UNVERIFIED", "HUMAN_APPROVED", "HUMAN_REJECTED"}
)
TRUSTED_CLASSIFICATIONS = frozenset({"VERIFIED", "HUMAN_APPROVED"})
HUMAN_AUTHORITY_CLASSIFICATIONS = frozenset({"HUMAN_APPROVED", "HUMAN_REJECTED"})

# Audit-critical categories: deletion still records a tombstone (nothing is
# silently destroyed), and history is always retained for these.
AUDIT_CATEGORIES = frozenset(
    {"decision", "checkpoint", "security", "deployment", "incident"}
)

# Retrieval bounds: queries are always limited; a query can never pull the
# whole store unbounded.
MAX_RETRIEVAL_LIMIT = 500
DEFAULT_RETRIEVAL_LIMIT = 50

# Content longer than this is rejected outright (injection/abuse bound).
MAX_CONTENT_CHARS = 4000
MAX_KEY_CHARS = 200

# Control characters (except tab/newline) are stripped from stored text.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Timestamp for records whose original time is unknown (deterministic
# migration of legacy state); never guessed from "now".
_EPOCH = "1970-01-01T00:00:00+00:00"


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
    # --- Phase 15 persistent contract ---
    id: str = ""
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    source: str = "agent"
    classification: str = "UNVERIFIED"
    project_identity: str = ""
    schema_version: int = MEMORY_SCHEMA_VERSION

    def trusted(self) -> bool:
        return self.trust == "trusted"


_ENTRY_FIELD_NAMES = frozenset(f.name for f in fields(MemoryEntry))


def _record_id(
    project_id: str, category: str, key: str, version: int, created_at: str
) -> str:
    """Deterministic record id: stable across restarts, unique per version.

    The id is derived from the persisted identity fields only (never from wall
    clock at derivation time), so it is identical after reload/restart and
    distinct across generations and versions of the same key.
    """
    basis = f"{project_id}|{category}|{key}|{version}|{created_at}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _entry_to_dict(entry: MemoryEntry) -> dict:
    return {name: getattr(entry, name) for name in sorted(_ENTRY_FIELD_NAMES)}


def _entry_from_dict(data: dict) -> MemoryEntry:
    """Rebuild a record from persisted state, ignoring unknown fields."""
    known = {k: v for k, v in data.items() if k in _ENTRY_FIELD_NAMES}
    return MemoryEntry(**known)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
    """Write/read store for one project's memory.

    The store is bound to a single ``project_id``. Entries from other projects
    are invisible and attempting to touch them raises ``CrossProjectAccess``.

    With ``storage=None`` the store behaves exactly like the Phase 12 in-RAM
    layer. With a storage backend (see ``openclaw.persistent_memory``) every
    mutation is applied as a locked read-modify-write transaction over the
    durable store, and the store is hydrated from disk on open.
    """

    def __init__(self, project_id: str, *, storage=None, clock=None) -> None:
        if not project_id or not isinstance(project_id, str):
            raise MemoryError("project_id must be a non-empty string.")
        self.project_id = project_id
        self._storage = storage
        self._clock = clock or _utc_now
        self._project_identity = getattr(storage, "identity", "") or project_id
        self._entries: dict[str, MemoryEntry] = {}
        self._history: dict[str, list[MemoryEntry]] = {}
        self._deleted: dict[str, MemoryEntry] = {}
        if self._storage is not None:
            self._refresh_from_storage()

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

    def _mutate(self, work):
        """Apply a mutation; persist atomically when a storage backend is set.

        The backend serializes writers with an exclusive lock; the live view is
        refreshed from disk inside the lock so concurrent writers never lose
        each other's records. If ``work`` raises, nothing is persisted.
        """
        if self._storage is None:
            return work()
        with self._storage.locked():
            self._refresh_from_storage()
            entry = work()
            self._storage.save_locked(self._dump_state())
            return entry

    def _build_record(
        self,
        actor: str,
        category: str,
        key: str,
        content: str,
        *,
        trust: str,
        evidence: Iterable[str],
        supersedes: str | None,
        source: str | None,
        classification: str | None,
        previous: MemoryEntry | None,
    ) -> MemoryEntry:
        """Validate and build the next record version (pure; no state change)."""
        if category not in MEMORY_CATEGORIES:
            raise MemoryError(f"Unknown memory category: {category!r}")
        if trust not in VALID_TRUST:
            raise MemoryError(f"Invalid trust value: {trust!r}")
        if not key or not isinstance(key, str) or len(key) > MAX_KEY_CHARS:
            raise MemoryError("Memory key must be a short non-empty string.")

        self._assert_write_authority(actor, category)
        if classification in HUMAN_AUTHORITY_CLASSIFICATIONS and actor != HUMAN_ROLE:
            raise UnauthorizedWrite(
                f"Only the human may classify memory as {classification!r}."
            )
        if source is None:
            source = "human" if actor == HUMAN_ROLE else "agent"
        if source not in VALID_SOURCES:
            raise MemoryError(f"Unknown memory source: {source!r}")
        if classification is None:
            classification = "VERIFIED" if trust == "trusted" else "UNVERIFIED"
        if classification not in VALID_CLASSIFICATIONS:
            raise MemoryError(f"Invalid classification: {classification!r}")
        if trust == "trusted":
            if not list(evidence):
                raise UntrustedMemoryRejected(
                    "Trusted memory requires evidence; unverified claims stay "
                    "untrusted."
                )
            if classification not in TRUSTED_CLASSIFICATIONS:
                raise MemoryError(
                    "Trusted memory requires a VERIFIED or HUMAN_APPROVED "
                    "classification."
                )
        elif classification in TRUSTED_CLASSIFICATIONS:
            raise MemoryError(
                f"Classification {classification!r} requires trust='trusted'."
            )

        clean = _validate_content(content)
        _screen_secrets(clean)
        evidence_list = [_clean_text(str(e)) for e in evidence]
        for item in evidence_list:
            _screen_secrets(item)

        now = self._clock()
        version = previous.version + 1 if previous is not None else 1
        created_at = (
            previous.created_at
            if previous is not None and previous.created_at
            else now
        )
        return MemoryEntry(
            project_id=self.project_id,
            category=category,
            key=key,
            content=clean,
            trust=trust,
            recorded_by=actor,
            evidence=evidence_list,
            supersedes=previous.id if previous is not None else supersedes,
            id=_record_id(self.project_id, category, key, version, created_at),
            version=version,
            created_at=created_at,
            updated_at=now,
            source=source,
            classification=classification,
            project_identity=self._project_identity,
            schema_version=MEMORY_SCHEMA_VERSION,
        )

    # --- persistence state ---------------------------------------------------

    def _dump_state(self) -> dict:
        """Serialize the full store state (active records, history, tombstones)."""
        records: dict[str, dict] = {}
        for ck, entry in self._entries.items():
            records[ck] = {
                "active": _entry_to_dict(entry),
                "history": [_entry_to_dict(e) for e in self._history.get(ck, [])],
                "tombstone": None,
            }
        for ck, tombstone in self._deleted.items():
            records.setdefault(
                ck, {"active": None, "history": [], "tombstone": None}
            )
            records[ck]["tombstone"] = _entry_to_dict(tombstone)
            records[ck]["history"] = [
                _entry_to_dict(e) for e in self._history.get(ck, [])
            ]
        return {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "project_id": self.project_id,
            "project_identity": self._project_identity,
            "records": records,
        }

    def _load_state(self, state: dict) -> None:
        """Replace the live view with persisted state (isolation-checked)."""
        self._entries = {}
        self._history = {}
        self._deleted = {}
        if not state:
            return
        if state.get("project_id") != self.project_id:
            raise CrossProjectAccess(
                f"Memory state belongs to project {state.get('project_id')!r}, "
                f"store is bound to {self.project_id!r}."
            )
        for ck, record in state.get("records", {}).items():
            history = [_entry_from_dict(h) for h in record.get("history", [])]
            if record.get("active"):
                entry = _entry_from_dict(record["active"])
                self._assert_owned(entry)
                self._entries[ck] = entry
            if record.get("tombstone"):
                tombstone = _entry_from_dict(record["tombstone"])
                self._assert_owned(tombstone)
                self._deleted[ck] = tombstone
            if history:
                self._history[ck] = history

    def _refresh_from_storage(self) -> None:
        if self._storage is None:
            return
        state = self._storage.load()
        self._load_state(state or {})

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
        source: str | None = None,
        classification: str | None = None,
    ) -> MemoryEntry:
        """Write a record after authorization, trust, and secret screening.

        Writing over an existing key creates a NEW VERSION: the previous record
        moves to version history (append/audit semantics — old facts are never
        silently destroyed). ``trust="trusted"`` requires verifiable evidence
        and an authorized role; unverified content is stored only as
        ``untrusted`` and is never returned by ``trusted_entries``.
        """

        def work() -> MemoryEntry:
            ck = self._key(category, key)
            existing = self._entries.get(ck)
            if existing is not None:
                self._assert_owned(existing)
            record = self._build_record(
                actor,
                category,
                key,
                content,
                trust=trust,
                evidence=evidence,
                supersedes=supersedes,
                source=source,
                classification=classification,
                previous=existing,
            )
            if existing is not None:
                self._history.setdefault(ck, []).append(existing)
            self._entries[ck] = record
            return record

        return self._mutate(work)

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
        """Update an existing entry as a new version (same checks as write)."""

        def work() -> MemoryEntry:
            ck = self._key(category, key)
            current = self._entries.get(ck)
            if current is None:
                raise MemoryError(
                    f"No memory entry {category}:{key} "
                    f"for project {self.project_id!r}."
                )
            self._assert_owned(current)
            record = self._build_record(
                actor,
                category,
                key,
                content,
                trust=trust,
                evidence=evidence,
                supersedes=None,
                source=None,
                classification=None,
                previous=current,
            )
            self._history.setdefault(ck, []).append(current)
            self._entries[ck] = record
            return record

        return self._mutate(work)

    def delete(self, actor: str, category: str, key: str) -> MemoryEntry:
        """Remove a record from the active view; only the human may delete.

        The deletion is recorded as a tombstone and the previous versions stay
        in history — memory is never silently destroyed.
        """
        if actor != HUMAN_ROLE:
            raise UnauthorizedWrite("Only the human may delete memory entries.")

        def work() -> MemoryEntry:
            ck = self._key(category, key)
            current = self._entries.get(ck)
            if current is None:
                raise MemoryError(
                    f"No memory entry {category}:{key} "
                    f"for project {self.project_id!r}."
                )
            self._assert_owned(current)
            now = self._clock()
            tombstone = MemoryEntry(
                project_id=self.project_id,
                category=category,
                key=key,
                content=f"[deleted by {actor}]",
                trust="untrusted",
                recorded_by=actor,
                evidence=[],
                supersedes=current.id,
                id=_record_id(
                    self.project_id, category, key, current.version + 1, current.created_at
                ),
                version=current.version + 1,
                created_at=current.created_at,
                updated_at=now,
                source="human",
                classification="UNVERIFIED",
                project_identity=self._project_identity,
                schema_version=MEMORY_SCHEMA_VERSION,
            )
            self._history.setdefault(ck, []).append(current)
            self._deleted[ck] = tombstone
            del self._entries[ck]
            return tombstone

        return self._mutate(work)

    # --- version history / audit ----------------------------------------------

    def history(
        self, category: str, key: str, *, limit: int = DEFAULT_RETRIEVAL_LIMIT
    ) -> list[MemoryEntry]:
        """Version chain for a key, newest first (active record included).

        Bounded by ``limit``; tombstones appear as the newest entry when the
        key was deleted. Nothing is ever hidden: every prior version of a
        corrected fact stays retrievable here.
        """
        limit = _bound_limit(limit)
        ck = self._key(category, key)
        chain: list[MemoryEntry] = []
        active = self._entries.get(ck)
        if active is not None:
            chain.append(active)
        older = list(self._history.get(ck, []))
        tombstone = self._deleted.get(ck)
        if tombstone is not None:
            older.append(tombstone)
        older.sort(key=lambda e: e.version, reverse=True)
        chain.extend(older)
        return chain[:limit]

    # --- retrieval (bounded, deterministic) -------------------------------------

    def get_by_id(self, record_id: str) -> MemoryEntry:
        """Fetch a record (active, historical, or tombstone) by its id."""
        for entry in self._entries.values():
            if entry.id == record_id:
                return entry
        for entries in self._history.values():
            for entry in entries:
                if entry.id == record_id:
                    return entry
        for tombstone in self._deleted.values():
            if tombstone.id == record_id:
                return tombstone
        raise MemoryError(f"No memory record with id {record_id!r}.")

    def search(
        self,
        query: str | None = None,
        *,
        category: str | None = None,
        source: str | None = None,
        trust: str | None = None,
        limit: int = DEFAULT_RETRIEVAL_LIMIT,
    ) -> list[MemoryEntry]:
        """Deterministic, bounded filtered search over active records.

        Ordering is stable by (created_at, category, key); results are capped
        at ``limit`` (hard-capped at ``MAX_RETRIEVAL_LIMIT``) so a query can
        never pull the whole store unbounded.
        """
        limit = _bound_limit(limit)
        needle = (query or "").strip().lower()
        results: list[MemoryEntry] = []
        for entry in self._entries.values():
            if category is not None and entry.category != category:
                continue
            if source is not None and entry.source != source:
                continue
            if trust is not None and entry.trust != trust:
                continue
            if needle and (
                needle not in entry.content.lower()
                and needle not in entry.key.lower()
            ):
                continue
            results.append(entry)
        results.sort(key=lambda e: (e.created_at, e.category, e.key))
        return results[:limit]

    def latest(self, category: str | None = None, *, limit: int = 10) -> list[MemoryEntry]:
        """Most recently updated active records (deterministic, bounded)."""
        limit = _bound_limit(limit)
        entries = [
            e
            for e in self._entries.values()
            if category is None or e.category == category
        ]
        entries.sort(key=lambda e: (e.updated_at, e.category, e.key), reverse=True)
        return entries[:limit]

    def checkpoints(self, *, limit: int = DEFAULT_RETRIEVAL_LIMIT) -> list[MemoryEntry]:
        """Latest checkpoint records for this project (bounded)."""
        return self.latest("checkpoint", limit=limit)

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
        length bounded. It must never be interpreted as instructions — memory
        is contextual data, not authority.
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


def _bound_limit(limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise MemoryError("limit must be a positive integer.")
    return min(limit, MAX_RETRIEVAL_LIMIT)


def open_project_memory(
    project_id: str, *, storage=None, clock=None
) -> ProjectMemory:
    """Open (create if needed) the memory store for a project.

    Without ``storage`` this is the Phase 12 in-RAM store. Pass a
    ``openclaw.persistent_memory.MemoryStorage`` backend for durable,
    project-isolated persistence.
    """
    return ProjectMemory(project_id, storage=storage, clock=clock)
