# MEMORY ARCHITECTURE — Development F (Phase 12)

Status: **PASS**

Implementation: `openclaw/memory.py`
Tests: `tests/openclaw/test_memory.py` (17 tests, PASS)
Full suite at time of writing: `198 passed, 0 failed` (`python3 -m pytest -q`).

## Source-of-truth priority (invariant)

Memory is NOT the primary source of truth. Priority order:

```text
CURRENT REPOSITORY
> CURRENT EVIDENCE
> CURRENT DOCUMENTATION
> MEMORY
> AGENT ASSUMPTION
```

`resolve_conflict(source, current_value, memory_value)` enforces this: any
current observation from repository, evidence, or documentation always wins
over memory; memory only wins over a lower-priority source (assumption).
Stale memory can never override the current repository state.

## Categories

```text
project | architecture | decision | agent | task | checkpoint |
quality | security | deployment | incident
```

(`incident` implements the Failure/Incident category; `agent` memory is a
valid category whose write authority is reserved for future agent-lifecycle
management.)

## Trust model

Only verified data may enter trusted memory:

```text
trusted   : requires (a) an authorized role and (b) verifiable evidence
            (verified checkpoint, decision, test, audit, deployment, or a
            human-approved decision)
untrusted : default bucket for anything unverified; never rendered into
            prompts and never returned by trusted_entries()
```

Rejected as trusted without evidence: AI assumptions, unverified claims,
speculation, temporary guesses. They are stored only as `untrusted`.

Human authority: the `decision` category is human-only (`human` is the only
role with write authority), and OpenClaw can never record a trusted decision.

## Security

### Secret rejection (never stored)

Every write/update screens content AND evidence with the Security Gate's own
`SECRET_PATTERNS` (`openclaw.security_gate`). Any credential-like value
(AWS/GitHub/GitLab/Slack/Stripe/Google tokens, private keys, generic
`password/secret/api_key/access_token` assignments) raises `SecretRejected`
and nothing is stored. A failed write/update leaves the previous entry
untouched.

### Authorization (least privilege)

```text
openclaw          -> project, architecture, task, checkpoint
coding_agent      -> task, checkpoint
handoff_agent     -> checkpoint
quality_guardian  -> quality
security_gate     -> security
deployment_check  -> deployment, incident
human             -> decision (+ exclusive delete authority)
```

Unauthorized writes raise `UnauthorizedWrite`. Only the human may delete
memory entries (tamper resistance).

### Project isolation (mandatory)

Every `ProjectMemory` store is bound to one `project_id`. Entries carry their
own `project_id`; a foreign entry cannot be read, updated, or rendered through
another project's store — any such attempt raises `CrossProjectAccess`.
Cross-project contamination is impossible by construction.

### Prompt-injection resistance

`render_for_prompt()` renders trusted memory only, with:

- control characters (NUL, ESC, C0/C1) stripped at write time and again at
  render time,
- per-entry cap (500 chars) and total cap (4000 chars),
- stored content treated strictly as data (never executed or interpreted).

Untrusted entries are never rendered into prompts.

## API surface

```text
open_project_memory(project_id) -> ProjectMemory
  .write(actor, category, key, content, trust=, evidence=) -> MemoryEntry
  .read(category, key)              -> MemoryEntry
  .update(actor, category, key, ...) -> MemoryEntry
  .delete(actor, category, key)     -> None   (human only)
  .trusted_entries(category=None)   -> list[MemoryEntry]
  .render_for_prompt(category=None) -> str
resolve_conflict(source, current_value, memory_value) -> object
```

Errors: `MemoryError`, `SecretRejected`, `UnauthorizedWrite`,
`CrossProjectAccess`, `UntrustedMemoryRejected`.

## Test coverage (mandatory minimum)

```text
memory write                    test_memory_write_and_read_roundtrip
memory read                     test_memory_read_missing_entry_raises (negative)
memory update                   test_memory_update_replaces_content_and_requires_authorization
memory isolation                test_memory_project_isolation_write_and_read
trusted/untrusted memory        test_trusted_memory_requires_evidence,
                                test_verified_facts_are_trusted_and_listed,
                                test_untrusted_memory_never_renders_into_prompts
conflict resolution             test_memory_never_overrides_current_repository_or_evidence
secret rejection                test_memory_rejects_secrets_in_content,
                                test_memory_rejects_secrets_in_evidence_and_updates
authorization                   test_memory_write_authorization_by_role,
                                test_human_decision_category_is_human_only,
                                test_only_human_can_delete_memory
project isolation (cross)       test_memory_project_isolation_write_and_read
prompt-injection resistance     test_rendered_memory_is_sanitized_and_bounded
```

## Scope note

The in-repository `openclaw/` package is the Development F orchestration
layer. Memory is integrated at the same layer and does not replace or rebuild
the existing Handoff Agent (`/home/fadly_03/handoff-agent`) or Quality
Guardian (`/home/fadly_03/quality-guardian`) components.
