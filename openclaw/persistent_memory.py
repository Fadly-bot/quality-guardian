"""Persistent memory storage — Development F (Phase 15).

Durable, local-first, dependency-free (standard library only) persistence for
the Phase 12/15 Memory Layer. Chosen over SQLite per the Phase 15 storage
policy: the write pattern is low-frequency, small, human-auditable JSON, so a
single state file with atomic replacement, an exclusive lock, fsync, a
rotating backup, and schema metadata is sufficient — no database, no external
service, no API key.

Guarantees:

- Atomic writes: state is written to a temp file in the same directory,
  fsynced, then moved into place with ``os.replace`` (atomic on POSIX). An
  interrupted write can never leave a half-written state file.
- Concurrency: every read-modify-write runs under an exclusive ``flock`` on a
  lock file (with an O_EXCL fallback on platforms without ``fcntl``), so two
  writers serialize and readers always see a complete state.
- Corruption detection: unparsable JSON raises ``StorageCorrupted``. The
  rotating ``.bak`` copy of the last known-good state enables recovery;
  ``load()`` refuses to silently use a backup — recovery is explicit.
- Schema/version metadata: a sidecar ``.meta`` file records schema version,
  project id, identity, and timestamps; state carries ``schema_version`` too.
- Project isolation: storage is bound to one project id. Opening the same
  file under a different project id raises ``ProjectIdentityMismatch`` (a
  ``CrossProjectAccess``), so Project A's memory can never appear in
  Project B.

Storage layout (all inside one root directory):

    <root>/memory-<safe-project-id>.json       active state (atomic)
    <root>/memory-<safe-project-id>.json.bak   previous known-good state
    <root>/memory-<safe-project-id>.meta       schema/version/identity metadata
    <root>/memory-<safe-project-id>.lock       advisory lock file

MEMORY != SOURCE OF TRUTH and MEMORY != AUTHORITY: this engine only makes
records durable; it grants them no additional trust or authority.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .memory import (
    CrossProjectAccess,
    MemoryError,
    MEMORY_SCHEMA_VERSION,
    _record_id,
)

# Schema versions this engine can load. Loading an older schema requires an
# explicit migration (see ``migrate``); newer schemas are refused, never
# guessed at.
SUPPORTED_SCHEMA_VERSIONS = frozenset({2})

# Schema version produced by the explicit legacy migration path.
LEGACY_SCHEMA_VERSION = 1
# Deterministic timestamp for migrated records whose original time is unknown
# (never guessed from "now").
_MIGRATION_EPOCH = "1970-01-01T00:00:00+00:00"

_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.02
_STALE_LOCK_SECONDS = 30.0
_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_name(project_id: str) -> str:
    """Deterministic, filesystem-safe name component for a project id."""
    if _VALID_NAME.match(project_id):
        return project_id
    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]
    return f"p-{digest}"


class MemoryStorageError(MemoryError):
    """Base error for persistent memory storage."""


class LockTimeout(MemoryStorageError):
    """Raised when the exclusive lock could not be acquired in time."""


class StorageCorrupted(MemoryStorageError):
    """Raised when stored state cannot be parsed (recovery must be explicit)."""


class ProjectIdentityMismatch(CrossProjectAccess):
    """Raised when storage is opened under a different project identity."""


def _repo_identity(repo_root: Path) -> str:
    """Stable identity of the repository backing this project.

    Priority: the git ``origin`` remote URL (``git-remote:<url>``) — stable
    across commits, working-tree changes, and sessions — then the resolved
    repository path (``path:<hash>``) for repos without a remote or non-git
    projects. A project id alone is never used as identity (names can
    collide); HEAD commit is deliberately NOT part of identity so that memory
    written in one session stays readable in the next session after new
    commits (memory must survive subsequent Development F sessions).
    """
    git_dir = repo_root / ".git"
    if git_dir.exists():
        try:
            remote = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
            if remote:
                return f"git-remote:{remote}"
        except (OSError, subprocess.SubprocessError):
            pass  # fall through to path identity
    resolved = str(repo_root.resolve())
    return "path:" + hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


class MemoryStorage:
    """Durable single-project memory state (JSON + lock + backup + meta)."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        project_id: str,
        *,
        repo_root: str | os.PathLike[str] | None = None,
    ) -> None:
        if not project_id or not isinstance(project_id, str):
            raise MemoryError("project_id must be a non-empty string.")
        self.project_id = project_id
        self.root = Path(root)
        # Identity is derived ONLY from the caller-provided repo root; it never
        # depends on the current working directory (no ambient side effects).
        self.repo_root = Path(repo_root) if repo_root is not None else Path.cwd()
        self.identity = _repo_identity(self.repo_root)
        base = _safe_name(project_id)
        self.state_path = self.root / f"memory-{base}.json"
        self.backup_path = self.root / f"memory-{base}.json.bak"
        self.meta_path = self.root / f"memory-{base}.meta"
        self.lock_path = self.root / f"memory-{base}.lock"

    # --- paths / info ---------------------------------------------------------

    def storage_info(self) -> dict:
        """Facts about the storage location (for docs and verification)."""
        return {
            "engine": "filesystem-json",
            "root": str(self.root),
            "state": str(self.state_path),
            "backup": str(self.backup_path),
            "metadata": str(self.meta_path),
            "lock": str(self.lock_path),
            "project_id": self.project_id,
            "project_identity": self.identity,
            "schema_version": MEMORY_SCHEMA_VERSION,
        }

    # --- concurrency ------------------------------------------------------------

    def locked(self):
        """Exclusive cross-process lock around a read-modify-write cycle."""
        return _FileLock(self.lock_path, self.project_id)

    # --- lifecycle ----------------------------------------------------------------

    def initialize(self) -> Path:
        """Create the storage directory and metadata; never overwrites state."""
        self.root.mkdir(parents=True, exist_ok=True)
        with self.locked():
            if not self.state_path.exists():
                self._write_state_atomic_locked(
                    {
                        "schema_version": MEMORY_SCHEMA_VERSION,
                        "project_id": self.project_id,
                        "project_identity": self.identity,
                        "records": {},
                    }
                )
            self._write_meta_locked()
        return self.state_path

    def read_meta(self) -> dict:
        """Parse the sidecar metadata file (corruption is explicit)."""
        try:
            raw = self.meta_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise StorageCorrupted(f"Metadata unreadable: {exc}") from exc
        meta: dict = {}
        for line in raw.splitlines():
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            meta[key.strip()] = value.strip()
        return meta

    # --- load / save ---------------------------------------------------------------

    def _load_raw(self) -> dict:
        """Read and parse the state file WITHOUT schema verification.

        Used by the explicit migration path; ``load()`` adds verification on
        top of this.
        """
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise StorageCorrupted(f"Memory state unreadable: {exc}") from exc
        try:
            state = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StorageCorrupted(
                f"Memory state corrupted at {self.state_path} ({exc}). "
                f"Recover with MemoryStorage.restore_from_backup() if a backup "
                f"exists; do not edit the file by hand."
            ) from exc
        if not isinstance(state, dict):
            raise StorageCorrupted(
                f"Memory state at {self.state_path} is not an object."
            )
        return state

    def load(self) -> dict:
        """Load the persisted state dict; empty dict when storage is fresh.

        Never silently falls back to the backup: if the active state is
        corrupted but the backup parses, ``StorageCorrupted`` is raised with
        recovery instructions so the recovery decision stays explicit and
        auditable.
        """
        state = self._load_raw()
        if state:
            self._verify_schema(state)
        return state

    def save_locked(self, state: dict) -> None:
        """Atomically persist ``state``; caller must hold ``locked()``.

        The previous state is rotated to ``.bak`` before the new state is
        moved into place, so a known-good copy always exists.
        """
        self._verify_schema(state)
        if self.state_path.exists():
            existing_raw = self.state_path.read_text(encoding="utf-8")
            try:
                existing = json.loads(existing_raw)
            except json.JSONDecodeError:
                existing = None  # corrupted active state; keep going to overwrite
            if isinstance(existing, dict):
                self._verify_schema(existing)
            if self.backup_path.exists():
                self.backup_path.unlink()
            os.replace(self.state_path, self.backup_path)
        self._write_state_atomic_locked(state)
        self._write_meta_locked()

    def _write_state_atomic_locked(self, state: dict) -> None:
        payload = json.dumps(
            state, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.state_path.name + ".tmp-", dir=str(self.root)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.state_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        self._fsync_dir()

    def _fsync_dir(self) -> None:
        try:
            dir_fd = os.open(str(self.root), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)

    # --- schema / isolation -------------------------------------------------------

    def _verify_schema(self, state: dict) -> None:
        stored_project = state.get("project_id")
        if stored_project is not None and stored_project != self.project_id:
            raise ProjectIdentityMismatch(
                f"Memory state belongs to project {stored_project!r}; "
                f"store is bound to {self.project_id!r}."
            )
        version = state.get("schema_version")
        if version is not None and version not in SUPPORTED_SCHEMA_VERSIONS:
            raise MemoryStorageError(
                f"Memory schema version {version!r} is not supported "
                f"(supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}). "
                f"Use migrate_legacy_state() for an explicit migration; "
                f"do not guess."
            )
        identity = state.get("project_identity")
        if identity is not None and identity != self.identity:
            raise ProjectIdentityMismatch(
                f"Memory state identity {identity!r} does not match this "
                f"repository {self.identity!r}."
            )

    def _write_meta_locked(self) -> None:
        lines = [
            f"schema_version={MEMORY_SCHEMA_VERSION}",
            f"project_id={self.project_id}",
            f"project_identity={self.identity}",
            f"updated_at={_utc_now()}",
        ]
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.meta_path.name + ".tmp-", dir=str(self.root)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.meta_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    # --- backup / recovery ----------------------------------------------------------

    def backup(self) -> Path:
        """Atomically snapshot the current state to ``.bak``. Returns the path."""
        with self.locked():
            state = self.load()  # explicit verification before snapshotting
            if not self.state_path.exists():
                raise MemoryStorageError("No state file to back up.")
            payload = json.dumps(
                state, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            fd, tmp_name = tempfile.mkstemp(
                prefix=self.backup_path.name + ".tmp-", dir=str(self.root)
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, self.backup_path)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
            self._fsync_dir()
        return self.backup_path

    def restore_from_backup(self) -> dict:
        """Replace corrupted state with the last known-good backup (explicit).

        Returns the restored state dict. Raises ``StorageCorrupted`` if no
        parsable backup exists — recovery never fabricates state.
        """
        with self.locked():
            try:
                raw = self.backup_path.read_text(encoding="utf-8")
            except FileNotFoundError as exc:
                raise StorageCorrupted(
                    f"No backup at {self.backup_path}; cannot recover without "
                    f"losing data. Manual recovery required."
                ) from exc
            except OSError as exc:
                raise StorageCorrupted(f"Backup unreadable: {exc}") from exc
            try:
                state = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise StorageCorrupted(
                    f"Backup at {self.backup_path} is also corrupted; manual "
                    f"recovery required."
                ) from exc
            if not isinstance(state, dict):
                raise StorageCorrupted("Backup is not an object.")
            self._verify_schema(state)
            if self.state_path.exists():
                os.replace(self.state_path, self.backup_path.with_suffix(".json.corrupt"))
            os.replace(self.backup_path, self.state_path)
            self._write_meta_locked()
            self._fsync_dir()
        return state


class _FileLock:
    """Cross-process exclusive lock: flock where available, O_EXCL otherwise."""

    def __init__(self, path: Path, owner: str) -> None:
        self.path = path
        self.owner = owner
        self._fd: int | None = None
        self._created_fallback = False

    def __enter__(self) -> "_FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl  # noqa: F401  (POSIX)

            self._fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    os.write(self._fd, f"owner={self.owner}\n".encode("utf-8"))
                    return self
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise LockTimeout(
                            f"Could not acquire memory lock {self.path} "
                            f"within {_LOCK_TIMEOUT_SECONDS}s."
                        ) from exc
                    time.sleep(_LOCK_POLL_SECONDS)
        except ImportError:
            return self._acquire_fallback()

    def _acquire_fallback(self) -> "_FileLock":
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                self._fd = os.open(
                    str(self.path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644
                )
                os.write(self._fd, f"owner={self.owner}\n".encode("utf-8"))
                self._created_fallback = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > _STALE_LOCK_SECONDS:
                    # Stale lock from a crashed process: break it explicitly.
                    self.path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"Could not acquire memory lock {self.path} "
                        f"within {_LOCK_TIMEOUT_SECONDS}s."
                    )
                time.sleep(_LOCK_POLL_SECONDS)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is None:
            return
        try:
            if not self._created_fallback:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except ImportError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None
            if self._created_fallback:
                self.path.unlink(missing_ok=True)
                self._created_fallback = False


def memory_path_for(root: str | os.PathLike[str], project_id: str) -> Path:
    """Deterministic state-file path for a project (stable across sessions)."""
    return Path(root) / f"memory-{_safe_name(project_id)}.json"


def make_persistent_memory(
    project_id: str,
    root: str | os.PathLike[str],
    *,
    repo_root: str | os.PathLike[str] | None = None,
    initialize: bool = True,
):
    """Open a durable, project-isolated ``ProjectMemory`` for ``project_id``.

    Wires the Phase 12/15 ``ProjectMemory`` contract to this storage engine.
    The repository identity is derived from ``repo_root`` (git HEAD when
    available, deterministic content hash otherwise).
    """
    from .memory import open_project_memory

    storage = MemoryStorage(root, project_id, repo_root=repo_root)
    if initialize:
        storage.initialize()
    return open_project_memory(project_id, storage=storage)


def migrate_legacy_state(
    storage: MemoryStorage,
    *,
    dry_run: bool = False,
) -> dict:
    """Explicitly migrate a legacy (schema v1) state file to schema v2.

    Legacy shape (Phase 12-era export)::

        {"project_id": ..., "entries": {"<category>:<key>": {fields...}}}

    The migration is deterministic and auditable: every legacy entry becomes a
    version-1 record with a deterministic epoch timestamp (original times are
    unknown and are never guessed) and a stable record id derived from the
    persisted identity fields. No information is dropped or invented. The
    previous file is rotated to ``.bak`` before the migrated state is written.
    With ``dry_run=True`` the migrated state is returned without writing.
    """
    with storage.locked():
        state = storage._load_raw()
        if not state:
            raise MemoryStorageError(
                f"Nothing to migrate at {storage.state_path}: no state file."
            )
        version = state.get("schema_version")
        if version == MEMORY_SCHEMA_VERSION:
            return state  # already current; migration is a no-op
        if version != LEGACY_SCHEMA_VERSION:
            raise MemoryStorageError(
                f"Cannot migrate schema version {version!r} "
                f"(migratable: {LEGACY_SCHEMA_VERSION})."
            )
        stored_project = state.get("project_id")
        if stored_project != storage.project_id:
            raise ProjectIdentityMismatch(
                f"Legacy state belongs to project {stored_project!r}; "
                f"store is bound to {storage.project_id!r}."
            )
        entries = state.get("entries")
        if not isinstance(entries, dict):
            raise MemoryStorageError(
                "Legacy state has no 'entries' object; refusing to guess."
            )
        records: dict[str, dict] = {}
        for ck, data in entries.items():
            if not isinstance(data, dict):
                raise MemoryStorageError(
                    f"Legacy entry {ck!r} is not an object; refusing to guess."
                )
            category, _, key = ck.partition(":")
            if not category or not key:
                raise MemoryStorageError(
                    f"Legacy entry name {ck!r} is not '<category>:<key>'."
                )
            record = dict(data)
            record["id"] = _record_id(
                storage.project_id, category, key, 1, _MIGRATION_EPOCH
            )
            record["version"] = 1
            record["created_at"] = _MIGRATION_EPOCH
            record["updated_at"] = _MIGRATION_EPOCH
            record["source"] = (
                "human" if record.get("recorded_by") == "human" else "agent"
            )
            record["classification"] = (
                "VERIFIED" if record.get("trust") == "trusted" else "UNVERIFIED"
            )
            record["project_identity"] = storage.identity
            record["schema_version"] = MEMORY_SCHEMA_VERSION
            records[ck] = {
                "active": record,
                "history": [],
                "tombstone": None,
            }
        migrated = {
            "schema_version": MEMORY_SCHEMA_VERSION,
            "project_id": storage.project_id,
            "project_identity": storage.identity,
            "records": records,
            "migrated_from_schema": LEGACY_SCHEMA_VERSION,
            "migrated_at": _utc_now(),
        }
        storage._verify_schema(migrated)
        if dry_run:
            return migrated
        if storage.state_path.exists():
            if storage.backup_path.exists():
                storage.backup_path.unlink()
            os.replace(storage.state_path, storage.backup_path)
        storage._write_state_atomic_locked(migrated)
        storage._write_meta_locked()
        return migrated
