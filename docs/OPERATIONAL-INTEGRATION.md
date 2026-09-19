# Operational Integration — Development F (Phase 13)

Status: **PASS**

Test module: `tests/openclaw/test_operational_integration.py` (12 tests, all PASS).
Full suite at the time of this document: `210 passed, 0 failed, 0 errors, 0 skipped`.

## Evidence type

Scenario evidence is REAL at the state-machine, validation, routing, gate-decision,
and audit-logging boundaries. External agent invocation boundaries (Handoff Agent,
Quality Guardian) are exercised through their established client seams with
controlled delegates, so those points are SIMULATION. Rollback is verified as
defined-plan/readiness only — no rollback is executed and nothing is claimed as a
production verification.

## Scope guarantee

Phase 13 modifies no prior behavior. The only implementation change is the §5
permitted repair (see Baseline below): the Security Gate's user-file exclusion
extends to `hasil*.md` transcripts so user-owned workspace artifacts that quote
test fixtures are not scanned as project evidence. Real secrets in project files
remain detected (`test_planted_secret_causes_fail_with_evidence` passes).

## Baseline (§0–§5)

- Recovery audit: HEAD `24bc81b` == `origin/main`, clean working tree, remote
  `git@github.com:Fadly-bot/quality-guardian.git` (SSH), ahead/behind `0 0`.
- All Phase 1–12 artifacts verified present (modules, tests, docs, registry).
- Initial baseline: `2 failed, 196 passed` — the same two security-gate decision
  tests as the Phase 11 recovery. Evidence: `repo.secret_scan: FAIL` with 13
  `generic_credential` matches, all inside the untracked user-owned transcript
  `hasil1.md`, all quoting the known test-fixture credential literal used by the
  planted-secret detection tests (an `api_key` assignment whose value is a
  `sk_live_` placeholder — never a real credential) in prose. Classification: **E —
  post-Phase-12 drift**, same class as the Phase 11 blocker.
- Permitted repair (§5, sole gate change): `_USER_FILE_PATTERNS` extended with
  `hasil*.md` (one line in `openclaw/security_gate.py`). Full retest: `198 passed`.
  Gate not weakened — planted-secret detection still passes.

## Agent Registry verification (§6–§10)

All 8 agents registered with complete identity, department, role, capabilities,
inputs, outputs, permissions, forbidden_actions, contract_version (3.0),
invocation_method, status (ACTIVE), and availability. The five integration agents
are all ACTIVE/AVAILABLE:

| Agent | Permissions (authority) | Key forbidden actions |
|---|---|---|
| `handoff_agent` | checkpoint_accept/reject, handoff_transport | quality/security/deployment approval, code modification, deploy |
| `quality_guardian` | read_only_audit | code edit, commit, push, deploy, self_approve, bypass_security_gate |
| `security_gate` | security_verification, release_blocking | code implementation, treat_not_scanned_as_pass, bypass_human_approval |
| `deployment_check` | deployment_after_approval, rollback_execution | deploy_without_approval, treat_dry_run_as_deploy, release_without_rollback |
| `openclaw` | orchestration | grant_approval, change_security_verdict, change_quality_verdict, bypass_*, allow_invalid_transition |

Unknown agents are refused (`UnknownAgent`); capabilities and permissions are
validated independently; contract version, invocation method, and availability
are enforced. All pinned by Scenario I against the real
`openclaw/agent_registry.json`.

## Integration contracts (§12)

1. **Handoff → Quality**: only a checkpoint that passes validation (required
   fields, owner = coding_agent, valid state, fresh, and Git truth == claim) is
   ACCEPTED and opens QUALITY_AUDIT. Any invalid checkpoint — including a forged
   Git state — is REJECTED to IMPLEMENTATION with `HANDOFF_REJECTED` recorded.
2. **Quality → Security**: PASS is the only forwarding verdict. FAIL/ERROR repair;
   NOT_SCANNED/NEEDS_REVIEW escalate. The quality agent can never issue a
   deployment or release decision (`SecurityGateError` guard).
3. **Security → Deployment**: decision PASS forwards (real checks over the real
   repository). FAIL/ERROR repair; NEEDS_REVIEW/NOT_SCANNED escalate; BLOCK (a
   verdict the gate can never emit as an aggregate) would escalate by routing
   table. `NOT_SCANNED != PASS` everywhere.
4. **Deployment → Human**: preflight PASS opens HUMAN_RELEASE_APPROVAL and
   nothing else. The Deployment Gate can request approval; only `human` can
   record it; a rejection stops the release (no DEPLOY transition from ABORTED).

Stage order is additionally protected by state guards: each client refuses to
route outside its own stage (`RoutingError`), so a component cannot act as
another stage even out of order.

## Scenario results (§11)

| Scenario | Description | Result |
|---|---|---|
| A | Happy path PROPOSAL → PRODUCTION, zero violations (real gates + both human approvals) | PASS |
| B | False handoff (forged Git state, foreign owner) rejected → repair; corrected checkpoint accepted | PASS |
| C | Quality FAIL → OpenClaw bounded repair; operator correction re-enters through the same gates | PASS |
| D | Security FAIL repairs; NEEDS_REVIEW never forwards; BLOCK verdict routes ESCALATED; recovery via real handoff path | PASS |
| E | Deployment preflight FAIL → repair; missing evidence NOT_SCANNED → escalated, never forwarded | PASS |
| F | Human rejection stops the release permanently (ABORTED); AI agents cannot grant approval | PASS |
| G | Invalid transitions/verdicts/actors rejected (5 illegal transitions, foreign verdict, foreign actor) | PASS |
| H | Post-deploy failure → ROLLED_BACK; recovery is a human decision; rollback plan verified as SIMULATION only | PASS |
| I | Agent Registry semantics: identity, capabilities ≠ permissions, contract version, invocation, availability, forbidden actions | PASS |
| J | Bounded repair (max 1) exceeded → ESCALATED to human; terminal state; full audit trail (no handoff rejection, ESCALATED recorded) | PASS |

Plus two integration-contract tests: Handoff→Quality→Security→Deployment→Human
chaining with separation-of-duties guards, and error-state handling at every
boundary (FAIL, ERROR, NOT_SCANNED, NEEDS_REVIEW, BLOCK, unsupported verdicts).

## Error-state handling (§14)

Every verdict class is handled at every boundary, verified by tests:

- `FAIL` → bounded repair (IMPLEMENTATION), audit `ROUTED verdict=FAIL`.
- `ERROR` → bounded repair (never swallowed, never treated as PASS).
- `NOT_SCANNED` → escalated to human at QUALITY_AUDIT / SECURITY_AUDIT; mapped to
  NEEDS_REVIEW→escalated at DEPLOYMENT_CHECK; never forwarded, never PASS.
- `NEEDS_REVIEW` → escalated at all three gates.
- `BLOCK` (security) → routing table sends ESCALATED (defensive: the gate cannot
  emit it as an aggregate decision; blocking checks aggregate to FAIL).
- `SUCCESS`/`FAILURE` (DEPLOY) and `PASS`/`FAIL` (POST_DEPLOY_VERIFY) are the
  only accepted verdicts; FAILURE/FAIL → ROLLED_BACK.
- Unsupported verdicts raise `RoutingError`; foreign actors raise
  `PermissionDenied`; missing approvals raise `HumanApprovalRequired`.

## Final verification (§22–§24)

- Full suite: 210 passed, 0 failed, 0 errors, 0 skipped.
- `git diff --check`: clean (no whitespace/conflict markers).
- Secret-leak scan over the changed scope: clean.
- No debug code, no stale references, no unused imports introduced.
