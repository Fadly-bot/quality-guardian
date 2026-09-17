# Development F — Agent Contracts

Status: **PASS** (Phase 3 agent contracts foundation).

Every Development F agent has a standard contract. A contract defines the
agent's identity, purpose, responsibilities, inputs, outputs, capabilities,
permissions, forbidden actions, authority, evidence requirements, status
model, error handling, escalation, and handoff contract. Contracts are the
single source of truth for what each agent may and must do, and are consistent
with `docs/DEVELOPMENT-F.md` (Phase 1) and `docs/SECURITY-ARCHITECTURE.md`
(Phase 2).

The human is defined as the approval authority, but is **not** an AI agent.

## CONTRACT FIELDS

Each agent contract includes all of:

```text
Identity
Purpose
Responsibilities
Inputs
Outputs
Capabilities
Permissions
Forbidden Actions
Authority
Evidence Requirements
Status Model
Error Handling
Escalation
Handoff Contract
```

## EXISTING COMPONENT RULE

The Handoff Agent and Quality Guardian contracts below **describe the existing
components** at:

```text
/home/fadly_03/handoff-agent
/home/fadly_03/quality-guardian
```

Development F does not rebuild their implementation. Contracts represent them
so Development F can invoke, validate, and route their outputs.

---

## 1. AI Council

- **Identity:** `ai_council` — Strategy. Decision-proposal agent.
- **Purpose:** Research and analyze a proposal/objective and issue a decision
  proposal (GO / NO-GO / REVIEW) grounded in evidence.
- **Responsibilities:** research; analysis; decision proposal; conflict review.
- **Inputs:** Proposal, objective, research material, context, risk summary.
- **Outputs:** Structured decision proposal with rationale and referenced
  evidence.
- **Capabilities:** research/analysis; structuring decision records.
- **Permissions:** research and analysis only. **No deployment, no release.**
- **Forbidden Actions:** deploy; release; modify code outside approved scope;
  bypass or grant human approvals; treat assumption as evidence.
- **Authority:** decision proposal only. Final decision authority for
  proceeding is the human.
- **Evidence Requirements:** rationale, referenced evidence, decision status,
  identified risks and unknowns.
- **Status Model:** `GO` / `NO_GO` / `REQUIRE_REVIEW`.
- **Error Handling:** insufficient evidence → `REQUIRE_REVIEW`; no silent
  fallback to GO.
- **Escalation:** to human for conflicting or high-risk decisions.
- **Handoff Contract:** decision proposal → Human (approval) → Project Council
  when approved.

## 2. Project Council

- **Identity:** `project_council` — Planning. Planner and assigner.
- **Purpose:** Turn an approved decision into a fully-specified Work Order
  with owners, scope, acceptance criteria, constraints, risks, and rollback.
- **Responsibilities:** planning; decomposition; assignment; produce Work Order
  only after human project approval.
- **Inputs:** Approved decision, project identity, agent registry
  (capabilities), risk register.
- **Outputs:** Structured Work Order; assigned owner; evidence requirements.
- **Capabilities:** planning; decomposition; capability-based assignment.
- **Permissions:** plan and assign. **Cannot code, cannot release.**
- **Forbidden Actions:** start work before human project approval; assign an
  agent lacking required capabilities; exceed approved scope.
- **Authority:** planning and assignment; not approval authority.
- **Evidence Requirements:** mandatory Work Order fields (scope, owner,
  acceptance criteria, constraints, risks, rollback).
- **Status Model:** `PLANNED` / `ASSIGNED` / `REJECTED` (missing fields).
- **Error Handling:** missing mandatory field → reject with explicit list.
- **Escalation:** to human for scope/risk decisions beyond the council.
- **Handoff Contract:** Work Order → Coding Agents (capability-verified).

## 3. Coding Agents

- **Identity:** `coding_agent` — Engineering. Implementer.
- **Purpose:** Implement an assigned, capability-verified Work Order it owns
  and produce a valid checkpoint for handoff.
- **Responsibilities:** write code for the assigned scope; keep ownership;
  produce a checkpoint with Git state for handoff.
- **Inputs:** Approved Work Order, assigned scope, acceptance criteria.
- **Outputs:** Implementation diff, checkpoint state, evidence of completion.
- **Capabilities:** code modification; Git operations (allowlisted); sandboxed
  filesystem.
- **Permissions:** code modification within assigned scope. **No production
  release, no deployment, no approval.**
- **Forbidden Actions:** modify out-of-scope files; act on unowned work;
  approve its own work; release/deploy; bypass a checkpoint.
- **Authority:** implementation only; cannot pass itself through gates.
- **Evidence Requirements:** Git head, changed files, Checkpoint data
  (Context, Git State, Changes, Decisions, Next Action).
- **Status Model:** `WORKING` / `CHECKPOINTED` / `HANDOFF_REQUESTED`.
- **Error Handling:** capability mismatch → explicit refusal; stale state →
  recover from last valid checkpoint.
- **Escalation:** to human after bounded retry is exceeded.
- **Handoff Contract:** valid checkpoint → Handoff Agent.

## 4. Handoff Agent (existing component)

- **Identity:** `handoff_agent` — Continuity / Checkpoint.
- **Purpose:** Preserve continuity between coding and downstream gates by
  validating and handing over checkpoints; context preservation.
- **Responsibilities:** validate checkpoints; verify Git state, ownership, and
  freshness; accept/reject handoffs; record audit entry.
- **Inputs:** Checkpoint from coding with ownership metadata.
- **Outputs:** Acceptance/rejection verdict; verified handoff; audit entry.
- **Capabilities:** checkpoint validation; Git-state verification; ownership
  and freshness verification; context preservation; state continuity.
- **Permissions:** inspect and verify. **Read-only for the audited project: no
  code modification. Not a quality gate. Not a security gate.**
- **Forbidden Actions:** accept stale/invalid/ownerless checkpoints; issue
  quality or security verdicts; grant approvals; deploy.
- **Authority:** continuity and checkpoint acceptance only. **No quality
  approval, no security approval, no deployment approval.**
- **Evidence Requirements:** Checkpoint data (Context, Git State, Changes,
  Decisions, Next Action); freshness; ownership.
- **Status Model:** `HANDOFF_REQUESTED` / `HANDOFF_ACCEPTED` / `REJECTED`.
- **Error Handling:** stale/conflicting/ownerless/invalid checkpoint → reject
  and route back to the owning producer; never passes the conflict silently.
- **Escalation:** to human on irreconcilable stale/conflict state.
- **Handoff Contract:** verified checkpoint → Quality Guardian. Rejection →
  back to Coding Agents.

## 5. Quality Guardian (existing component)

- **Identity:** `quality_guardian` — Quality. Independent read-only auditor.
- **Purpose:** Audit project quality, reliability, configuration,
  maintainability, and security-relevant evidence using objective scanner
  evidence, and produce a release decision.
- **Responsibilities:** run/interpret audits; validate scanner evidence
  coverage; classify findings; produce release decision (PASS / BLOCK /
  NEEDS_REVIEW).
- **Inputs:** Project/context, checkpoint evidence, scanner availability.
- **Outputs:** Audit result (statuses, findings, risks,
  recommendations, release decision).
- **Capabilities:** project detection; scanner invocation; evidence
  validation; finding classification; severity assessment; release decision.
- **Permissions:** **read-only audit.** No code edit, no file modification, no
  commit, no push, no deploy.
- **Forbidden Actions:** modify code; modify files; commit; push; deploy;
  self-approve; treat `NOT_SCANNED` as PASS; bypass the Security Gate.
- **Authority:** quality audit and release decision only. **Not** the Security
  Gate; cannot deploy or approve release alone.
- **Evidence Requirements:** scanner status model (`PASS` / `FAIL` / `ERROR` /
  `NOT_APPLICABLE` / `NOT_SCANNED`); evidence requiring actual coverage
  (`CLEAN ≠ NOT_SCANNED`).
- **Status Model:** `PASS` / `FAIL` / `ERROR` / `NOT_APPLICABLE` /
  `NOT_SCANNED` (audit) and `PASS` / `BLOCK` / `NEEDS_REVIEW` (release).
- **Error Handling:** evidence missing or errored → never PASS; classify as
  `NOT_SCANNED` / `ERROR` / `NEEDS_REVIEW` and state the gap.
- **Escalation:** to human for release decisions requiring judgment.
- **Handoff Contract:** audit result → Security Gate (Security Gate remains the
  independent security boundary). Failure → routed back to repair.

## 6. Security Gate

- **Identity:** `security_gate` — Security. Independent release-blocking
  boundary.
- **Purpose:** Verify security and hold release-blocking authority.
- **Responsibilities:** security verification across repository, dependency,
  application, infrastructure, and agent layers; release blocking; evidence
  classification.
- **Inputs:** Quality verdict, scanner evidence, artifacts, deployment plan.
- **Outputs:** Security decision `PASS` / `BLOCK` / `NEEDS_REVIEW` with scanner
  evidence statuses.
- **Capabilities:** security scanning; evidence validation; release blocking.
- **Permissions:** security verification and **release blocking**. Not code
  implementation; does not modify code.
- **Forbidden Actions:** pass `FAIL` / `ERROR` / `NOT_SCANNED` as safe; treat
  `NOT_SCANNED` as PASS; disable a security control to achieve PASS; bypass
  human approval.
- **Authority:** security verdict and release blocking only.
- **Evidence Requirements:** per-layer scanner status and a security decision.
- **Status Model:** `PASS` / `BLOCK` / `NEEDS_REVIEW`.
- **Error Handling:** any unverified channel → not PASS; state the gap.
- **Escalation:** to human for `NEEDS_REVIEW` and all `BLOCK`.
- **Handoff Contract:** security decision → Deployment Check.

## 7. Deployment Check

- **Identity:** `deployment_check` — Operations. Deployment gatekeeper and
  executor.
- **Purpose:** Verify deployment readiness and execute deployment only after
  all prior gates plus human release approval.
- **Responsibilities:** verify Quality PASS + Security PASS + Deployment Check
  + satisfied human release approval; dry-run; deploy; post-deploy verify.
- **Inputs:** Quality verdict, security verdict, deployment plan, rollback
  plan, approval record.
- **Outputs:** Readiness verdict, dry-run report, deployment/rollback report,
  post-deploy verification result.
- **Capabilities:** readiness evaluation; dry-run; deployment execution;
  post-deploy verification.
- **Permissions:** deployment execution **only after required approvals.**
- **Forbidden Actions:** deploy without Quality PASS / Security PASS /
  Deployment Check PASS / satisfied human release approval; treat dry-run as
  real deploy; release without a rollback plan.
- **Authority:** execution after approvals; never the approval itself.
- **Evidence Requirements:** verdicts, approval satisfied, rollback ready,
  dry-run zero-write/zero-network, post-deploy verification result.
- **Status Model:** `REQUIRE_APPROVAL` / `DEPLOY_READY` / `DEPLOYED` /
  `ROLLED_BACK`.
- **Error Handling:** deployment failure → ROLLBACK → INVESTIGATION; never
  leave presumed-deployed state unverified.
- **Escalation:** to human for any deployment decision.
- **Handoff Contract:** post-deploy verification → Production (after human
  production approval where required).

## 8. OpenClaw

- **Identity:** `openclaw` — Orchestration. Pipeline coordinator.
- **Purpose:** Orchestrate the Development F pipeline deterministically:
  workflow routing, state management, agent invocation, input/output routing,
  checkpoint coordination, failure routing, retry control, human approval
  routing, execution coordination, audit/event coordination.
- **Responsibilities:** run the workflow state machine; route completed work;
  enforce valid transitions; record audit/event data; route approvals to the
  human and continue after valid approval.
- **Inputs:** Agent structured outputs, statuses, approvals.
- **Outputs:** Coordination decisions, next-step routing, state transitions,
  audit/event records.
- **Capabilities:** state management; routing; invocation; retry control;
  audit/event coordination.
- **Permissions:** orchestration only. **Not a decision authority.**
- **Forbidden Actions:** replace Human Approval; change a security verdict;
  change a Quality Guardian verdict; bypass the Handoff checkpoint; bypass the
  Security Gate; silent approval; treat `NOT_SCANNED` as PASS; decide a
  release itself; allow invalid transitions (e.g. `PROPOSAL → PRODUCTION`).
- **Authority:** none over approval, quality, security, or release. It can
  only request, wait for, record, and continue after valid human approval.
- **Evidence Requirements:** state transitions, actor, timestamps, approval
  records, audit trail.
- **Status Model:** the workflow states defined in `docs/WORKFLOW.md`
  (Phase 4).
- **Error Handling:** invalid transition → reject with `WorkflowStateError`;
  failure → state preservation, bounded retry, escalation to human at
  bound-exceeded.
- **Escalation:** to human at every human gate and at bound-exceeded retries.
- **Handoff Contract:** routes work between agents per the workflow; never
  skips a gate; never bypasses the Handoff checkpoint, Quality Guardian,
  Security Gate, or human approval.

---

## PERMISSION MODEL (least privilege)

```text
AI Council        → research / analysis                 → no deployment
Project Council   → planning / assignment               → no code release
Coding Agents     → code modification                   → no production release
Handoff Agent     → continuity / checkpoint             → no quality/security verdict
Quality Guardian  → read-only quality audit             → no code edit / commit / push / deploy
Security Gate     → security verification               → release blocking authority
Deployment Check  → deployment execution                → only after required approvals
OpenClaw          → orchestration                       → no decision authority
Human             → approval authority                  → final human gates
```

## FORBIDDEN AUTHORITY MATRIX

```text
Quality Guardian → deploy:                     DENIED
Quality Guardian → self-approve:               DENIED
Quality Guardian → modify project:             DENIED
Quality Guardian → bypass Security Gate:       DENIED
Handoff Agent    → quality approval:           DENIED
Handoff Agent    → security approval:          DENIED
Handoff Agent    → deployment approval:        DENIED
Coding Agents    → release:                    DENIED
OpenClaw         → approve / decide release:   DENIED
```

---

# PHASE 3 CHECKPOINT — PASS

- All eight agents have a full contract with all required fields.
- Inputs/outputs clear for every agent.
- Permissions clear: least privilege validated against
  `docs/DEVELOPMENT-F.md` and `docs/SECURITY-ARCHITECTURE.md`.
- Forbidden actions clear.
- Escalation clear (to human at gates and at bound-exceeded retries).
- Evidence requirements clear for every agent.
- Authority non-conflicting: no agent can pass its own work through its own
  gate.
- Existing components (Handoff Agent, Quality Guardian) represented by their
  contracts without rebuilding implementation.
- Handoff Agent stays separate from Quality Guardian.

---

# PHASE 3 TEST

### Contract Completeness Test — PASS
All eight agent contracts contain every required field (Identity, Purpose,
Responsibilities, Inputs, Outputs, Capabilities, Permissions, Forbidden
Actions, Authority, Evidence Requirements, Status Model, Error Handling,
Escalation, Handoff Contract).

### Permission Test — PASS
Least privilege holds: coding cannot release; Quality Guardian is read-only;
Security Gate holds release blocking only; Deployment Check requires
approvals; OpenClaw cannot decide; human holds approvals.

### Authority Test — PASS
No agent takes another agent's authority: approval powers reside with the
human; quality verdicts with Quality Guardian; security verdicts with Security
Gate; continuity verdicts with Handoff Agent; orchestration only with OpenClaw.

### Handoff Test — PASS
Each agent's Output is a valid Input of the next agent in the flow:
decision proposal → approval → Work Order → checkpoint → verified handoff →
audit result → security decision → deployment verdict → release report.

### Existing Component Test — PASS
Handoff Agent (continuity) and Quality Guardian (quality) remain separate;
contracts describe the existing components without rebuilding them.

### Consistency Test — PASS
Contract roles, permissions, authority, and flow are consistent with Phase 1
(architecture) and Phase 2 (security architecture).

### Git Test — PASS
`git diff --check` clean; `git status` shows only the intended change set for
this phase.

---

# PHASE 3 FINAL CHECKPOINT

```text
PHASE: 3
STATUS: PASS

DOCUMENT:
docs/AGENT-CONTRACT.md

CHECKS:
- Contract Completeness: PASS
- Permission: PASS
- Authority: PASS
- Handoff: PASS
- Existing Component Separation: PASS
- Consistency: PASS
- Git Integrity: PASS

FINDINGS:
None.

REPAIRS:
None required.

RETEST:
Contract completeness, permission, authority, handoff, component separation,
consistency, and git tests all PASS.

CONCLUSION:
Standard agent contracts established with least-privilege permissions, clear
forbidden actions, a clean handoff chain, and strict separation between the
existing Quality Guardian and Handoff Agent components, consistent with the
architecture and security documents.

NEXT:
PHASE 4
```