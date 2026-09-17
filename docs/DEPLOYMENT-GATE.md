# Deployment Gate — Development F Phase 10

## Purpose

The Deployment Gate is the **release / preflight boundary** of the Development F
pipeline. It is **NOT a deployment executor**: it decides, with real evidence,
whether an artifact is allowed to enter deployment, and it is the last automated
gate before a **human** performs the controlled deployment step.

Pipeline placement:

```
Coding -> Handoff -> Quality -> Security -> DEPLOYMENT_GATE -> HUMAN_RELEASE_APPROVAL
                                                              -> DEPLOY -> POST_DEPLOY_VERIFY
                                                                                  -> PRODUCTION | ROLLED_BACK
```

Implementation: `openclaw/deployment_gate.py` (module), tests
`tests/openclaw/test_deployment_gate.py` (24 tests).

## Boundary

- The gate **verifies with evidence**, it does not grant approvals.
- Human release approval is recorded through OpenClaw
  (`HUMAN_RELEASE_APPROVAL`); a **rejection routes to ABORTED** and deploy after
  rejection is impossible (`deploy_without_approval` raises).
- The gate **cannot grant release approval** (`grant_release_approval` raises)
  and **cannot change quality or security verdicts**
  (`change_security_or_quality_verdict` raises).
- The gate is not a deployment executor: the actual deployment step runs only
  after approval; `deploy_not_executed` documents this separation.

## Mandatory preflight items (with evidence, not assumptions)

| Check ID | Mandatory item |
| --- | --- |
| `preflight.repository_clean` | Repository clean (no uncommitted changes) |
| `preflight.correct_branch` | Correct branch |
| `preflight.correct_remote` | Expected remote |
| `preflight.artifact_identity` | Artifact identity (recorded commit SHA) |
| `preflight.tests_pass` | Tests PASS |
| `preflight.quality_pass` | Quality Guardian PASS |
| `preflight.security_pass` | Security Gate PASS |
| `preflight.build_pass` | Build (compileall) PASS |
| `preflight.env_config` | Required environment configuration available |
| `preflight.secrets_configured` | Required secrets configured (none embedded) |
| `preflight.rollback_readiness` | Rollback readiness (artifact recorded) |

Every check is derived from real repository / tool / gate evidence. An
unavailable required check is `NOT_SCANNED` — **never** routed or recorded as
PASS. Decision precedence (shared with the Security Gate):

```
FAIL  >  ERROR  >  NOT_SCANNED  >  NEEDS_REVIEW  >  PASS
```

`DeploymentGateResult.validate()` rejects a PASS decision that carries a
FAIL / ERROR / NOT_SCANNED / NEEDS_REVIEW check.

## Routing (through OpenClaw, owner `deployment_check`)

| Decision | Route | Outcome |
| --- | --- | --- |
| PASS | `PASS` | `HUMAN_RELEASE_APPROVAL` |
| FAIL | `FAIL` | repair (`IMPLEMENTATION`), `MAX_REPAIR_ITERATIONS` then escalation |
| ERROR | `ERROR` | repair (`IMPLEMENTATION`) |
| NOT_SCANNED | `NEEDS_REVIEW` (OpenClaw has no NOT_SCANNED verdict here) | `ESCALATED` — **never** forwarded |
| NEEDS_REVIEW | `NEEDS_REVIEW` | `ESCALATED` |

Wrong-state routing raises `RoutingError`.

## Human release approval

1. `DeploymentGate.request_release_approval(oc)` — only OpenClaw requests.
2. `DeploymentGate.record_release_approval(oc, approved, approver)` — only the
   human role may be the approver; an AI agent recording an approval raises
   `PermissionDenied`.
3. `DeploymentGate.continue_after_release_approval(oc)` — with no granted
   approval raises `HumanApprovalRequired`; granted → `DEPLOY`; rejected →
   `ABORTED`.

## Rollback

`RollbackPlan` is defined (artifact-before, trigger, procedure, verification,
post-rollback verification) and readiness is verified before any release
(`verify_rollback_readiness`). The gate never claims rollback availability
without a recorded, verifiable plan.

## Audit trail

Gate evidence is recorded to the OpenClaw `AuditLog` as `EVIDENCE` events
(`DeploymentGateClient.record_evidence`), alongside the routing transitions.

## Phase 10 checkpoint — STATUS: PASS

- 24 new tests in `tests/openclaw/test_deployment_gate.py` (clean/ dirty repo,
  branch/remote/commit identity, build error detection, missing-gate
  NOT_SCANNED, rollback definition & verification, full routing table, approval
  approve/reject, agent-cannot-grant, deploy-executor separation, evidence
  audit).
- Full regression suite: **160 passed** (`python3 -m pytest -q`).
- Real repository preflight (evidence run against committed state, before the
  Phase 10 doc commit; expected remote
  `https://github.com/Fadly-bot/quality-guardian.git`):

```
DECISION: FAIL (working tree had the Phase 10 doc/.gitignore uncommitted —
         the gate correctly blocked a non-clean tree; see finding)
preflight.repository_clean   FAIL   3 uncommitted change(s)
preflight.correct_branch     PASS   branch='main' expected='main'
preflight.correct_remote     PASS   remote='https://github.com/Fadly-bot/quality-guardian.git' expected=...
preflight.artifact_identity  PASS   artifact commit SHA recorded: 4792c29...
preflight.tests_pass         PASS   full Development F suite green
preflight.quality_pass       PASS   quality decision='PASS'
preflight.security_pass      PASS   security decision='PASS'
preflight.build_pass         PASS   compileall succeeded for ['openclaw']
preflight.env_config         NOT_APPLICABLE
preflight.secrets_configured NOT_APPLICABLE
preflight.rollback_readiness PASS   rollback artifact recorded: 4792c29...
```

### Finding / repair

- Finding: the gate correctly **failed** the release because user-owned planning
  files (`1-8.md`, `12.md`, `hasil.md`) were untracked in the working tree.
- Repair: the workspace-planning files are excluded from Development F commits
  by design, so they are now declared in `.gitignore`; the clean-tree check is
  then evaluated on Development F project state only. The document/commit state
  in the E2E gate run (Phase 11) verifies the release on a clean tree.

## Push status (11)

Per user decision, commits are done locally and the verified push to `origin`
happens once, after **Phase 12** (single `main` push). Until then this phase is
flagged **UNVERIFIED** for remote visibility.