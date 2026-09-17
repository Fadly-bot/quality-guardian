# Development F — Architecture

Status: **PASS** (Phase 1 architecture foundation).

Development F is a virtual software company executed by AI agents under
mandatory human authority. This document is the single source of truth for the
Development F identity, organization, responsibilities, authority boundaries,
high-level flow, and separation of concerns.

It is consistent with the existing, complete components:

```text
/home/fadly_03/handoff-agent     → Handoff Agent (continuity / checkpoint)
/home/fadly_03/quality-guardian  → Quality Guardian (quality / audit)
```

Neither component is rebuilt by Development F. Development F integrates them.

## A. Identity

- **Name:** Development F.
- **Nature:** A virtual software company. It is not a single agent. It is an
  orchestrated organization of agents under human authority.
- **Purpose:** Develop, verify, and release software through a supervised
  pipeline from proposal to production, where every acceptance decision is
  grounded in objective evidence and where release-critical decisions stay
  with the human.
- **Scope:** End-to-end software delivery:

```text
PROPOSAL → RESEARCH → COUNCIL_REVIEW → HUMAN_APPROVAL → PLANNING →
IMPLEMENTATION → HANDOFF_CHECKPOINT → QUALITY_AUDIT → SECURITY_AUDIT →
DEPLOYMENT_CHECK → HUMAN_RELEASE_APPROVAL → DEPLOY → POST_DEPLOY_VERIFY →
PRODUCTION
```

- **Working principles:**
  1. Deny by default: an agent may act only within the capabilities, scope,
     and permissions granted by its contract.
  2. Evidence over assumption: every gate decision is grounded in objective
     test/scan/check evidence, never in agent assertion alone.
  3. The human is the final authority for project approval, release approval,
     and production approval.
  4. Separation of concerns: planning, coding, handoff, quality, security,
     deployment, and orchestration are distinct, non-overlapping
     responsibilities.
  5. Least privilege: no role holds permission outside its stated contract.
  6. Audit integrity: every transition and decision is recorded.

## B. Organization

```text
DEVELOPMENT F
│
├── AI Council               → research / analysis / decision proposal
├── Project Council          → planning / decomposition / assignment
├── Coding Agents            → implementation
├── Handoff Agent            → continuity / checkpoint
├── Quality Guardian         → quality audit
├── Security Gate            → security verification / release blocking
├── Deployment Check         → deployment readiness / deployment execution
├── OpenClaw                 → orchestration
│
└── Human                    → final approval authority (above all gates)
```

> Terminology note: the implemented role model in the existing
> `/home/fadly_03/handoff-agent` uses `ai_council`, `planning_council`,
> `coding_agent`, `handoff_agent`, `quality_guardian`, `human`, and
> `deployment_check`. This document uses the organization names above; the
> mapping to component identifiers is defined in `docs/AGENT-CONTRACT.md`
> (Phase 3) and `docs/AGENT-REGISTRY.md` (Phase 6).

## C. Responsibilities

| Department | Agent | Core responsibilities |
|---|---|---|
| Strategy | AI Council | Research and analysis; decision proposal (GO / NO-GO / REVIEW); never deploys. |
| Planning | Project Council | Planning, decomposition, assignment; produces a spec/Work Order after human project approval; never codes the work itself. |
| Engineering | Coding Agents | Implementation within assigned scope; produces a checkpoint for handoff; never approximates its own acceptance. |
| Continuity | Handoff Agent | Continuity / checkpoint / context preservation; verifies Git state, ownership, freshness; accepts or rejects handoffs. |
| Quality | Quality Guardian | Independent quality / audit; read-only; PASS / FAIL / ERROR / NOT_APPLICABLE / NOT_SCANNED; release decision PASS / BLOCK / NEEDS_REVIEW. |
| Security | Security Gate | Security verification / release blocking; independent boundary; not merged with Quality. |
| Operations | Deployment Check | Deployment readiness verification; deployment execution only after required gates and human release approval. |
| Orchestration | OpenClaw | Orchestration, state management, routing, execution coordination; not a decision authority. |
| Approval | Human | Final authority for project, release, and production approvals. |

## D. Human Authority

The human is positioned **above** the approval gates. AI agents and OpenClaw
may request, wait for, record, and continue after an approval; they may never
grant it.

```text
HUMAN PROJECT APPROVAL     → approve a proposal before planning
HUMAN RELEASE APPROVAL     → approve a release (after Quality + Security +
                             Deployment Check all PASS)
HUMAN PRODUCTION APPROVAL  → approve promotion to production
```

There is no silent bypass for any human gate: no agent, automation mode,
retry, recovery, or orchestration path may continue past an unsatisfied human
gate.

## E. High-Level Flow

```text
AI Council
↓
Human Approval
↓
Project Council
↓
Coding Agents
↓
Handoff Agent
↓
Quality Guardian
↓
Security Gate
↓
Deployment Check
↓
Human Release Approval
↓
Deployment
↓
Post-Deploy Verification
↓
Production
```

OpenClaw coordinates the routing of every step above. It does not replace any
decision in it.

The formal deterministic state machine for this flow (transitions, failure
paths, bounded retry, human gates, rollback) is defined in
`docs/WORKFLOW.md` (Phase 4).

## F. Separation of Concerns

```text
AI Council    → research / analysis / decision proposal
Project Council → planning / decomposition / assignment
Coding Agents → implementation
Handoff Agent → continuity / checkpoint
Quality Guardian → quality audit
Security Gate → security verification / release blocking
Deployment Check → deployment readiness
OpenClaw      → orchestration
Human         → approval authority
```

- **AI Council** proposes; it does not implement.
- **Project Council** plans and assigns; it does not code the work itself.
- **Coding Agents** implement; they cannot approve their own work and cannot
  release.
- **Handoff Agent** preserves continuity and verifies checkpoints; it is not a
  quality gate and not a security gate.
- **Quality Guardian** audits quality; it is read-only and is **not** the
  Security Gate.
- **Security Gate** is an independent, release-blocking security boundary; it
  is merged with neither Quality Guardian nor the Handoff Agent.
- **Deployment Check** verifies readiness and executes deployment **only**
  after all prior gates plus human release approval.
- **OpenClaw** orchestrates; it is **not** a decision authority and cannot
  grant or override approvals.
- **Human** holds final approval authority and is the only party that may
  release to production.

This separation guarantees that no single agent controls both the work and its
own acceptance, and that no release is possible without a human authority step
at the release-critical gates.

## G. Existing Components

Development F does not rebuild either existing component:

```text
Quality Guardian  (/home/fadly_03/quality-guardian)
  → EXISTING, COMPLETE
  → integrated by Development F (invoke, validate evidence, route)

Handoff Agent     (/home/fadly_03/handoff-agent)
  → EXISTING, COMPLETE
  → integrated by Development F (invoke, validate checkpoint, route)
```

Strict separation:

```text
Quality Guardian ≠ Handoff Agent
Quality Guardian → Quality / Audit
Handoff Agent    → Continuity / Checkpoint
```

- Quality Guardian must not be moved into Handoff Agent.
- Handoff Agent must not be moved into Quality Guardian.
- Git histories of the two repositories must not be merged.

---

# PHASE 1 CHECKPOINT — PASS

- Identity clear: Development F is a virtual software company, not a single
  agent.
- Organization complete: AI Council, Project Council, Coding Agents, Handoff
  Agent, Quality Guardian, Security Gate, Deployment Check, OpenClaw, Human.
- Responsibilities clear and non-overlapping (see F).
- Human authority clear and placed above the approval gates.
- Flow traceable from proposal to production with human gates.
- Separation of concerns explicit.
- Existing components (Quality Guardian, Handoff Agent) recognized as EXISTING
  and COMPLETE; Development F only integrates them.

---

# PHASE 1 TEST

### Structural Test — PASS
All required architecture sections are present: AI Council, Project Council,
Coding Agents, Handoff Agent, Quality Guardian, Security Gate, Deployment
Check, OpenClaw, and Human above the gates.

### Responsibility Test — PASS
No role overlap: methodology/analysis vs planning vs implementation vs
continuity vs quality vs security vs deployment vs orchestration are disjoint.

### Authority Test — PASS
Human is the final approval authority for project, release, and production; no
agent or OpenClaw can grant approval.

### Flow Test — PASS
The flow is traceable from proposal (AI Council) to production (Deployment)
including Post-Deploy Verification, with Human Approval and Human Release
Approval as mandatory gates.

### Existing-System Consistency Test — PASS
Consistent with the existing Quality Guardian (read-only quality/audit,
PASS/BLOCK/NEEDS_REVIEW) and Handoff Agent (continuity/checkpoint). Neither is
rebuilt or relocated.

### Git Test — PASS
`git diff --check` clean; `git status` shows only the intended change set for
this phase.

---

# PHASE 1 FINAL CHECKPOINT

```text
PHASE: 1
STATUS: PASS

DOCUMENT:
docs/DEVELOPMENT-F.md

CHECKS:
- Structure: PASS
- Responsibilities: PASS
- Authority: PASS
- Separation of Concerns: PASS
- Flow: PASS
- Existing-System Consistency: PASS
- Git Integrity: PASS

FINDINGS:
None.

REPAIRS:
None required.

RETEST:
Structural / responsibility / authority / flow / consistency / git tests all
PASS.

CONCLUSION:
Development F architecture foundation established: identity, organization,
responsibilities, human authority, flow, and separation of concerns defined
and consistent with the existing Quality Guardian and Handoff Agent
components.

NEXT:
PHASE 2
```