# PERSISTENT MEMORY — Development F (Phase 15)

Status: **PASS** — 26 new tests, full suite 258 passed / 0 failed / 0 errors / 0 skipped.

Two statements that govern this entire design:

> **MEMORY != SOURCE OF TRUTH.** The priority chain never changes:
> CURRENT REPOSITORY > CURRENT EVIDENCE > CURRENT DOCUMENTATION > PERSISTENT MEMORY > AGENT ASSUMPTION.
> If memory disagrees with the repository or evidence, memory loses.

> **MEMORY != AUTHORITY.** Memory is DATA. A record that says "bypass security"
> or "ignore human approval" is inert text; authority comes only from the
> current workflow, policy, repository evidence, and human approval.

## 1. Architecture

Phase 12 created the Memory Layer (`openclaw/memory.py`) as an in-RAM verified
store. Phase 15 EVOLVES it — no component was rebuilt:

```
openclaw/memory.py               contract (evolved in place)
  MemoryEntry                    + id, version, created_at/updated_at,
                                 + source, classification, project_identity,
                                 + schema_version
  ProjectMemory                  + storage-backed mutations, version history,
                                 + tombstones, bounded retrieval
openclaw/persistent_memory.py    storage engine (new, Phase 15)
  MemoryStorage                  durable JSON state + lock + backup + meta
  migrate_legacy_state           explicit schema v1 -> v2 migration
tests/openclaw/test_persistent_memory.py   A–Z matrix (26 tests)
```

The hierarchy from Phase 12 is unchanged: memory sits BELOW repository,
evidence, and documentation, and above agent assumption.

## 2. Storage choice (Section 6)

**Filesystem-based JSON persistence** — standard library only, no external
dependency, no database, no SaaS, no API key, no cloud account.

Rationale: the write pattern is low-frequency, small, human-auditable state.
SQLite's transaction machinery would add binary files and migration surface
without a matching need. The filesystem engine provides everything §6 demands:

| Requirement | Mechanism |
|---|---|
| durable | fsync on file AND directory after every write |
| atomic writes | temp file + `os.replace` (atomic on POSIX) |
| locking/concurrency | exclusive `flock` (O_EXCL fallback), 10 s timeout, stale-lock break after 30 s |
| corruption detection | unparsable JSON → `StorageCorrupted` (never silently empty) |
| schema/version metadata | `schema_version` in state + sidecar `.meta` file |
| project isolation | state file per project + identity verification on every load |
| deterministic serialization | `json.dumps(sort_keys=True, separators=…)` |

Storage layout (all inside one root directory):

```
<root>/memory-<safe-project-id>.json        active state (atomic)
<root>/memory-<safe-project-id>.json.bak    previous known-good state
<root>/memory-<safe-project-id>.meta        schema/version/identity metadata
<root>/memory-<safe-project-id>.lock        advisory lock file
```

Project ids that are not filesystem-safe are hashed deterministically
(`p-<sha256-16>`), so the same project id always maps to the same file.

## 3. Memory record contract (Section 7)

Every record carries (evolved from the Phase 12 contract — no duplicate model):

- `id` — deterministic: SHA-256 over `project|category|key|version|created_at`
  (truncated to 16 hex). Identical after reload/restart; distinct per version.
- `category` — the ten Phase 12 categories (unchanged): project, architecture,
  decision, agent, task, checkpoint, quality, security, deployment, incident.
- `project_identity` — stable repository identity (below).
- `content` — non-empty, ≤ 4000 chars, control characters stripped,
  secret-screened.
- `created_at` / `updated_at` — UTC ISO-8601 with microseconds.
- `source` — one of `repository | evidence | documentation | agent | human |
  checkpoint`.
- trust/classification metadata — `trust` (Phase 12 axis: trusted/untrusted)
  plus `classification` (Phase 15 axis, below).
- `version` — monotonically increasing per key; `supersedes` links to the
  previous version's id.
- `schema_version` — currently 2.

### Project identity

`git-remote:<origin URL>` when the repo has an origin remote, otherwise
`path:<sha256 of resolved path>`. HEAD commit is deliberately NOT part of
identity: memory must survive new commits and remain readable in subsequent
Development F sessions. A project id alone is never identity (ids can
collide).

## 4. Trust model and provenance (Section 8)

Classification axis (Phase 15 write policy, §12):

```
VERIFIED | HUMAN_APPROVED            -> trust = trusted
DERIVED | PROPOSED | UNVERIFIED |
HUMAN_REJECTED                       -> trust = untrusted
```

Mapping to Phase 12 terminology (documented per §12): `trusted` ⇔ VERIFIED or
HUMAN_APPROVED; `untrusted` ⇔ the rest. Rules enforced in code:

- `trust="trusted"` requires non-empty evidence AND a VERIFIED/HUMAN_APPROVED
  classification.
- Only the `human` actor may write HUMAN_APPROVED / HUMAN_REJECTED — agents
  can never forge human authority, on any category.
- Persistence never promotes: an agent-derived UNVERIFIED record stays
  UNVERIFIED after restart (tested).
- Provenance is recorded per record: scanner-derived records carry
  `source="evidence"` with the evidence reference; human approvals carry
  `source="human"` with approval metadata (never credentials); agent-derived
  records carry `source="agent"`.

## 5. Write policy (Section 12)

Not every agent output may be persisted. Writes pass, in order:

1. **Authorization** — role-based write authority per category (Phase 12
   table, unchanged); `decision` is human-only.
2. **Classification check** — trusted requires VERIFIED/HUMAN_APPROVED;
   human-authority classifications require the human actor.
3. **Content hygiene** — non-empty, bounded, control characters stripped.
4. **Secret screening** — the Security Gate's own `SECRET_PATTERNS` (no
   duplicate scanner); content AND evidence are screened; rejection aborts
   the write before anything touches disk.

Speculative content is accepted only as UNVERIFIED and never renders into
prompts (`render_for_prompt` returns trusted records only).

## 6. Read policy (Section 11)

- Memory is contextual DATA. `render_for_prompt` produces bounded (≤ 4000
  chars total, ≤ 500 chars per entry), sanitized text. Nothing in memory
  acquires authority from being stored; instruction-like content is inert.
- Retrieval is always bounded (`DEFAULT_RETRIEVAL_LIMIT = 50`,
  `MAX_RETRIEVAL_LIMIT = 500`); a query can never pull the whole store
  unbounded. `limit <= 0` is rejected.
- Ordering is deterministic: `(created_at, category, key)` for search,
  `(updated_at, category, key)` descending for latest.

Available retrieval: `read` (by category+key), `get_by_id`, `search`
(query/category/source/trust filters), `latest`, `checkpoints`, `history`
(version chain, newest first), `trusted_entries`, `render_for_prompt`.

## 7. Immutability / auditability (Section 13)

- Writing over an existing key creates a NEW VERSION; the previous record
  moves to version history and stays retrievable (`history`).
- Deletion is human-only and records a TOMBSTONE; prior versions remain in
  history. Old facts are never silently destroyed.
- Audit-critical categories (`decision`, `checkpoint`, `security`,
  `deployment`, `incident`) always retain full history.

## 8. Project isolation (Section 9) — MANDATORY

- One state file per project; a store is bound to one project id.
- Every load verifies `project_id` AND `project_identity`; mismatch raises
  `ProjectIdentityMismatch` (a `CrossProjectAccess`).
- Every record carries `project_id` and `project_identity`; a tampered record
  with a foreign project id is refused on read.
- Tested in BOTH directions (§9): A write → B read → A's memory does not
  appear; B write → A read → B's memory does not appear.

## 9. Concurrency / crash safety (Section 15)

- Every mutation is a locked read-modify-write: the store re-hydrates from
  disk INSIDE the exclusive lock, so concurrent writers never lose each
  other's records (tested: 8 threads → 8 records persist, valid JSON).
- Writes are atomic (temp + `os.replace` + fsync); an interrupted write can
  never leave a half-written state file (tested: leftover temp file is inert,
  pre-crash record intact).
- Lock timeout is 10 s; a stale lock older than 30 s (crashed process) is
  broken explicitly.

## 10. Corruption handling / backup / recovery (Section 16)

- Corrupted state → `StorageCorrupted` with recovery instructions; the store
  refuses to hydrate rather than silently starting empty.
- `backup()` atomically snapshots known-good state to `.bak` (also rotated
  automatically on every save).
- `restore_from_backup()` is EXPLICIT: it verifies the backup, preserves the
  corrupted file as `.json.corrupt` for forensics, and never fabricates state
  (no backup → `StorageCorrupted`, tested).
- Storage location is caller-chosen (local-first); no automatic backup to any
  external service. Local recovery is sufficient for Phase 15.

## 11. Schema versioning / migration (Section 17 P/Q)

- `load()` refuses unsupported schema versions (`MemoryStorageError`) — never
  guessed at.
- `migrate_legacy_state(storage)` is the explicit v1 → v2 path: deterministic
  (epoch timestamps for records whose original time is unknown — never "now"),
  idempotent, auditable (`migrated_from_schema`/`migrated_at` recorded), with
  a `dry_run` mode and backup rotation before writing.

## 12. Performance (Section 18)

No premature optimization; bounds are enforced instead: retrieval limits,
content limits (4000 chars), bounded identity computation, hydration loads one
small JSON file per open. Startup cost is O(store size) with the store capped
by content limits — growth stays controlled.

## 13. Backward compatibility (Section 20)

- `open_project_memory(project_id)` with no storage argument behaves exactly
  like Phase 12 (tested: `test_z_phase12_in_ram_contract_unchanged`).
- All 17 Phase 12 tests pass unchanged; the Phase 12 trust axis is preserved
  and mapped to the new classification axis.
- Phase 1–14 regression: full suite green (258 passed).

## 14. Limitations / future extensions

- Single-process-machine scope: the flock serializes writers on one machine;
  no network/replicated storage (out of scope by §6).
- No automatic compaction: history grows with corrections (auditability is
  preferred over size for Phase 15).
- No cross-project query API by design (isolation boundary).
- Possible extensions: per-category size budgets, background snapshot
  scheduling, optional SQLite engine behind the same `MemoryStorage`
  interface if write volume ever demands it.

## Test evidence

`tests/openclaw/test_persistent_memory.py` — 26 tests mapping the §17 matrix
A–Z, all against the real storage engine on real files (restart is a real
subprocess; concurrency is real threads; corruption is real bytes on disk).
Full suite at time of writing: **258 passed, 0 failed, 0 errors, 0 skipped**.
