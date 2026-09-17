# Development F — OpenClaw Orchestrator

Status: **PASS** (Phase 5 orchestration foundation).

OpenClaw is the orchestration layer of Development F. It is a coordinator, not
a decision authority.

```text
OpenClaw
=
Orchestrator
+ State Manager
+ Router
+ Execution Coordinator
```

Implementation: `openclaw/` (Python, stdlib-only).

## RESPONSIBILITIES

- Workflow routing
- State management (deterministic, per `docs/WORKFLOW.md`)
- Agent invocation (route to the agent owning the current state)
- Input/output routing
- Checkpoint coordination
- Failure routing
- Retry control (bounded by `MAX_REPAIR_ITERATIONS = 5`)
- Human approval routing (request / wait / record / continue after valid
  approval)
- Execution coordination
- Audit/event coordination (immutable audit log)

## MUST NOT

- Replace Human Approval
- Change a security verdict
- Change a Quality Guardian verdict
- Bypass the Handoff checkpoint
- Bypass the Security Gate
- Perform silent approval
- Treat `NOT_SCANNED` as PASS
- Take a release decision itself
- Allow invalid transitions (e.g. `PROPOSAL → PRODUCTION`)

## STATE MANAGEMENT

OpenClaw enforces the Phase 4 state machine. Invalid transitions raise
`WorkflowStateError` and are rejected. `NOT_SCANNED` at a quality or security
gate escalates (never forwards).

## HUMAN GATES

OpenClaw can only:

```text
request approval
wait for approval
record approval
continue after valid approval
```

It cannot grant approval on behalf of the human. `record_human_approval`
rejects any approver other than `human`.

## ERROR HANDLING

- Failure detection: routing on a gate verdict
- State preservation: rejected actions leave the current state intact
- Bounded retry: `MAX_REPAIR_ITERATIONS = 5`, then escalation to human
- Escalation: to the human at bound-exceeded retries and at every human gate
- Recovery: from `ROLLED_BACK` is a human-only decision

## MODULES

```text
openclaw/__init__.py    — package exports
openclaw/workflow.py    — state machine (states, transitions, owners)
openclaw/events.py      — immutable audit/event log
openclaw/orchestrator.py — OpenClaw orchestrator
```

## INTEGRATION RULE

OpenClaw does not modify the Handoff Agent
(`/home/fadly_03/handoff-agent`) or the Quality Guardian
(`/home/fadly_03/quality-guardian`). It adapts to their contracts and routes on
their outputs.

---

# PHASE 5 CHECKPOINT — PASS

- Orchestration implemented and tested.
- State management deterministic per Phase 4.
- Routing per gate verdict implemented.
- Invalid transitions rejected.
- Human approvals requested/recorded/continued only by valid human approval;
  OpenClaw cannot approve.
- Retry bounded by MAX_REPAIR_ITERATIONS with escalation.
- Error handling: failure detection, state preservation, escalation, recovery.
- Permission model enforced per state owner.
- Audit/event coordination recorded.
- Regression: all existing Quality Guardian tests still pass.

---

# PHASE 5 TEST — 44 PASS

```text
state transition tests        → PASS (full chain, terminal states)
invalid transition tests      → PASS (PROPOSAL → PRODUCTION rejected)
routing tests                 → PASS (PASS/FAIL/ERROR/BLOCK/REJECTED/SUCCESS)
human approval tests          → PASS (request/record/continue; no self-approve)
retry tests                   → PASS (bounded, shared counter, escalation)
failure recovery tests        → PASS (state preserved, ROLLED_BACK recovery)
permission tests              → PASS (actor + human-only rollback recovery)
audit/event tests             → PASS (independent audit trail)
full pipeline test            → PASS (PROPOSAL → PRODUCTION)
```

```text
python3 -m pytest tests/openclaw  -> 44 passed
python3 -m pytest                  -> 51 passed (regression includes
                                      Quality Guardian detection tests)
```

---

# PHASE 5 FINAL CHECKPOINT

```text
PHASE: 5
STATUS: PASS

CHECKS:
- Orchestration: PASS
- State Management: PASS
- Routing: PASS
- Invalid Transition: PASS
- Human Approval: PASS
- Retry: PASS
- Error Handling: PASS
- Permission: PASS
- Audit: PASS
- Regression: PASS
- Git Integrity: PASS

FINDINGS:
None.

REPAIRS:
None required during implementation; initial test authoring issues corrected
(added public `advance` for non-decision transitions) and retested.

RETEST:
All 44 OpenClaw tests PASS; full suite 51 PASS.

CONCLUSION:
OpenClaw orchestrator implemented as orchestrator + state manager + router +
execution coordinator, deterministic per Phase 4, with bounded retry, human
gate coordination, no decision authority, and a clean audit trail, without
modifying the existing Handoff Agent or Quality Guardian components.

NEXT:
PHASE 6
```