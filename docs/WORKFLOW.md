# Development F — Workflow / State Machine

Status: **PASS** (Phase 4 workflow / state machine foundation).

This document defines the deterministic Development F workflow as a state
machine. Every state, transition, failure path, retry bound, human gate,
rollback path, and owner agent is defined here. It is consistent with
`docs/DEVELOPMENT-F.md` (Phase 1), `docs/SECURITY-ARCHITECTURE.md` (Phase 2),
and `docs/AGENT-CONTRACT.md` (Phase 3).

## STATE MACHINE

```text
PROPOSAL
↓
RESEARCH
↓
COUNCIL_REVIEW
↓
HUMAN_APPROVAL
↓
PLANNING
↓
IMPLEMENTATION
↓
HANDOFF_CHECKPOINT
↓
QUALITY_AUDIT
↓
SECURITY_AUDIT
↓
DEPLOYMENT_CHECK
↓
HUMAN_RELEASE_APPROVAL
↓
DEPLOY
↓
POST_DEPLOY_VERIFY
↓
PRODUCTION
```

Terminal and control states:

```text
ABORTED              — proposal rejected or cancelled by human
ESCALATED            — reached human after bound-exceeded retry or NEEDS_REVIEW
ROLLED_BACK          — release rolled back after deploy/post-deploy failure
```

## STATES AND OWNERS

| State | Owner agent | Entry evidence |
|---|---|---|
| PROPOSAL | AI Council | Proposal object exists. |
| RESEARCH | AI Council | Research material collected; analysis produced. |
| COUNCIL_REVIEW | AI Council | Decision proposal with rationale and evidence. |
| HUMAN_APPROVAL | Human (coordinated by OpenClaw) | Decision proposal presented; approval requested. |
| PLANNING | Project Council | Approved decision; Work Order produced. |
| IMPLEMENTATION | Coding Agents | Approved Work Order with owner, scope, acceptance criteria. |
| HANDOFF_CHECKPOINT | Handoff Agent | Checkpoint (Context, Git State, Changes, Decisions, Next Action). |
| QUALITY_AUDIT | Quality Guardian | Verified checkpoint; audit result. |
| SECURITY_AUDIT | Security Gate | Quality PASS; security decision. |
| DEPLOYMENT_CHECK | Deployment Check | Security PASS; readiness + rollback plan. |
| HUMAN_RELEASE_APPROVAL | Human (coordinated by OpenClaw) | Deployment Check PASS; release approval requested. |
| DEPLOY | Deployment Check | Approved release; deployment executed. |
| POST_DEPLOY_VERIFY | Deployment Check | Post-deploy verification evidence. |
| PRODUCTION | Human / ops | Post-deploy verification PASS. |

## VALID TRANSITIONS

```text
PROPOSAL                 → RESEARCH
RESEARCH                 → COUNCIL_REVIEW
COUNCIL_REVIEW           → RESEARCH                    (REQUIRE_REVIEW → more research)
COUNCIL_REVIEW           → HUMAN_APPROVAL              (GO proposal)
COUNCIL_REVIEW           → ABORTED                     (NO_GO)
HUMAN_APPROVAL           → PLANNING                    (project approved)
HUMAN_APPROVAL           → COUNCIL_REVIEW              (requested revision)
HUMAN_APPROVAL           → ABORTED                     (project rejected)
PLANNING                 → IMPLEMENTATION
IMPLEMENTATION           → HANDOFF_CHECKPOINT
HANDOFF_CHECKPOINT       → QUALITY_AUDIT               (checkpoint accepted)
HANDOFF_CHECKPOINT       → IMPLEMENTATION              (checkpoint rejected → repair)
QUALITY_AUDIT            → SECURITY_AUDIT              (PASS)
QUALITY_AUDIT            → IMPLEMENTATION              (FAIL / ERROR → repair, bounded)
QUALITY_AUDIT            → ESCALATED                   (retry bound exceeded / NEEDS_REVIEW)
SECURITY_AUDIT           → DEPLOYMENT_CHECK            (PASS)
SECURITY_AUDIT           → IMPLEMENTATION              (FAIL → repair, bounded)
SECURITY_AUDIT           → ESCALATED                   (BLOCK / NEEDS_REVIEW / retry bound exceeded)
DEPLOYMENT_CHECK         → HUMAN_RELEASE_APPROVAL      (PASS)
DEPLOYMENT_CHECK         → IMPLEMENTATION              (FAIL → repair, bounded)
DEPLOYMENT_CHECK         → ESCALATED                   (retry bound exceeded)
HUMAN_RELEASE_APPROVAL   → DEPLOY                      (release approved)
HUMAN_RELEASE_APPROVAL   → ABORTED                     (release rejected/cancelled)
HUMAN_RELEASE_APPROVAL   → ESCALATED                   (required review)
DEPLOY                   → POST_DEPLOY_VERIFY          (success)
DEPLOY                   → ROLLED_BACK                 (deploy failure)
POST_DEPLOY_VERIFY       → PRODUCTION                  (verification PASS)
POST_DEPLOY_VERIFY       → ROLLED_BACK                 (verification FAIL)
ROLLED_BACK              → IMPLEMENTATION              (after human decision, bounded repair)
ROLLED_BACK              → ESCALATED
```

## INVALID TRANSITIONS (REJECTED)

The following transitions are invalid and MUST be rejected:

```text
PROPOSAL         → PRODUCTION
PROPOSAL         → DEPLOY
RESEARCH         → PRODUCTION
COUNCIL_REVIEW   → IMPLEMENTATION
HUMAN_APPROVAL   → RELEASE/DEPLOY/PRODUCTION
PLANNING         → DEPLOY/PRODUCTION
IMPLEMENTATION   → SECURITY_AUDIT / DEPLOY / PRODUCTION
QUALITY_AUDIT    → DEPLOY / PRODUCTION
SECURITY_AUDIT   → PRODUCTION
DEPLOYMENT_CHECK → DEPLOY (without HUMAN_RELEASE_APPROVAL)
DEPLOY           → PRODUCTION (without POST_DEPLOY_VERIFY)
```

OpenClaw is the executor of these validations and rejects any invalid
transition (`WorkflowStateError`).

## FAILURE LOOP

```text
HANDOFF_CHECKPOINT reject → IMPLEMENTATION
QUALITY_AUDIT FAIL         → IMPLEMENTATION
SECURITY_AUDIT FAIL        → IMPLEMENTATION
DEPLOYMENT_CHECK FAIL      → IMPLEMENTATION
POST_DEPLOY_VERIFY FAIL    → ROLLED_BACK → IMPLEMENTATION (after human decision)
```

## RETRY MODEL (BOUNDED)

```text
MAX_REPAIR_ITERATIONS = 5
```

- Each return to IMPLEMENTATION for repair increments one shared repair counter
  for the lifetime of the work item.
- When the counter reaches `MAX_REPAIR_ITERATIONS` and a gate still fails:

```text
ESCALATE TO HUMAN
```

- The human decides: continue repair, change scope, or abort.
- No silent infinite repair loop is allowed; no automatic pass at bound.

## HUMAN GATES

```text
HUMAN_APPROVAL           → before PLANNING
HUMAN_RELEASE_APPROVAL   → before DEPLOY
HUMAN PRODUCTION GATE    → before reaching PRODUCTION is guaranteed through
                           POST_DEPLOY_VERIFY + human decision where required
```

Rules:

- There is no silent bypass of any human gate.
- OpenClaw can request, wait for, record, and continue after a valid approval;
  it cannot grant approval or skip the gate.
- `PROPOSAL → PRODUCTION` and any path that skips a human gate is rejected.

## ROLLBACK PATH

- DEPLOY failure or POST_DEPLOY_VERIFY failure → `ROLLED_BACK`.
- A deployment is prepared with a rollback plan by Deployment Check before
  `HUMAN_RELEASE_APPROVAL`.
- From `ROLLED_BACK`, the human decides the next step (repair →
  IMPLEMENTATION, or escalate).

## ROUTING SUMMARY (OpenClaw)

```text
agent output        → state result      → next state
AI Council          → COUNCIL_REVIEW    → HUMAN_APPROVAL | RESEARCH | ABORTED
Human               → approval          → PLANNING | DEPLOY | ABORTED
Project Council     → PLANNING          → IMPLEMENTATION
Coding Agents       → checkpoints       → HANDOFF_CHECKPOINT
Handoff Agent       → verified handoff  → QUALITY_AUDIT | IMPLEMENTATION
Quality Guardian    → audit result      → SECURITY_AUDIT | IMPLEMENTATION | ESCALATED
Security Gate       → security decision → DEPLOYMENT_CHECK | IMPLEMENTATION | ESCALATED
Deployment Check    → readiness verdict → HUMAN_RELEASE_APPROVAL | IMPLEMENTATION | ESCALATED
Deployment Check    → deploy result     → POST_DEPLOY_VERIFY | ROLLED_BACK
Deployment Check    → post-deploy       → PRODUCTION | ROLLED_BACK
```

---

# PHASE 4 CHECKPOINT — PASS

- All required states are present.
- Transitions are explicit with owners and entry evidence.
- Failure path returns work to the repair path (IMPLEMENTATION).
- Retry is bounded (MAX_REPAIR_ITERATIONS = 5) with escalation to human.
- Human gates (HUMAN_APPROVAL, HUMAN_RELEASE_APPROVAL) preserved with no silent
  bypass.
- Rollback path defined (ROLLED_BACK) with a mandated rollback plan.
- Owner agent is defined for every state.
- Invalid transitions (e.g. PROPOSAL → PRODUCTION) are rejected.

---

# PHASE 4 TEST

### State Coverage Test — PASS
All required states exist: PROPOSAL, RESEARCH, COUNCIL_REVIEW, HUMAN_APPROVAL,
PLANNING, IMPLEMENTATION, HANDOFF_CHECKPOINT, QUALITY_AUDIT, SECURITY_AUDIT,
DEPLOYMENT_CHECK, HUMAN_RELEASE_APPROVAL, DEPLOY, POST_DEPLOY_VERIFY,
PRODUCTION; plus ABORTED, ESCALATED, ROLLED_BACK.

### Transition Test — PASS
Every listed transition is from a valid state to a reachable state; no
transition listed is self-contradictory. Invalid transitions (PROPOSAL →
PRODUCTION and similar) are explicitly rejected.

### Failure Test — PASS
Quality FAIL, Security FAIL, Deployment Check FAIL, checkpoint rejection, and
post-deploy failure all route to the repair path (IMPLEMENTATION) or rollback.
Escalation to human occurs at bound-exceeded retry.

### Human Gate Test — PASS
HUMAN_APPROVAL precedes PLANNING; HUMAN_RELEASE_APPROVAL precedes DEPLOY. No
silent bypass path exists; OpenClaw cannot grant approval.

### Retry Test — PASS
MAX_REPAIR_ITERATIONS = 5; exceeding it forces ESCALATE TO HUMAN; no silent
automatic pass.

### Consistency Test — PASS
State machine, owners, and routing are consistent with Phase 1
(architecture flow), Phase 2 (security boundary), and Phase 3 (contracts and
handoff chain).

### Git Test — PASS
`git diff --check` clean; `git status` shows only the intended change set for
this phase.

---

# PHASE 4 FINAL CHECKPOINT

```text
PHASE: 4
STATUS: PASS

DOCUMENT:
docs/WORKFLOW.md

CHECKS:
- State Coverage: PASS
- Transition: PASS
- Failure: PASS
- Retry: PASS
- Human Gate: PASS
- Rollback: PASS
- Owner Mapping: PASS
- Consistency: PASS
- Git Integrity: PASS

FINDINGS:
None.

REPAIRS:
None required.

RETEST:
State coverage, transition, failure, retry, human gate, rollback, owner
mapping, consistency, and git tests all PASS.

CONCLUSION:
Deterministic Development F workflow defined as a state machine with all
required states, valid transitions, bounded repair retries, human gates,
rollback, owner mapping, and no silent bypass.

NEXT:
PHASE 5
```