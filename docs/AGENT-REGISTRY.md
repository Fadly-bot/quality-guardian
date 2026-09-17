# Development F — Agent Registry

Status: **PASS** (Phase 6 agent registry foundation).

The agent registry is the official catalog of every Development F agent. It is
implemented read-only-data in `openclaw/agent_registry.json` and consumed by
`openclaw/registry.py`.

## REGISTRY ENTRY

Every agent entry contains:

```text
agent_id
name
department
role
capabilities
inputs
outputs
permissions
forbidden_actions
contract_version
invocation_method
status
availability
```

## AGENTS

```text
ai_council        — AI Council       (Strategy)
project_council   — Project Council  (Planning)
coding_agent      — Coding Agents    (Engineering)
handoff_agent     — Handoff Agent    (Continuity)
quality_guardian  — Quality Guardian (Quality)
security_gate     — Security Gate    (Security)
deployment_check  — Deployment Check (Operations)
openclaw          — OpenClaw         (Orchestration)
```

Human is the approval authority and is defined in `docs/AGENT-CONTRACT.md`; it
is not an AI agent and is not a registry entry.

## CAPABILITY ≠ PERMISSION

```text
Capability  = what the agent is capable of doing
Permission  = what the agent is allowed to do
```

Both are verified separately (validated independently per invocation).

## AVAILABILITY

```text
AVAILABLE
UNAVAILABLE
DEGRADED
DISABLED
```

## EXISTING COMPONENTS

The registry points to the existing components; it never copies their source:

```text
Handoff Agent     → /home/fadly_03/handoff-agent
Quality Guardian  → /home/fadly_03/quality-guardian
```

---

# PHASE 6 CHECKPOINT — PASS

- Registry complete: all eight agents present with every required field.
- Agent lookup implemented.
- Capability validation implemented separately from permission validation.
- Contract version validation implemented.
- Availability validation implemented (AVAILABLE / UNAVAILABLE / DEGRADED /
  DISABLED).
- Invocation validation implemented.
- Unknown-agent rejection implemented.
- Existing components preserved: registry describes and routes, never copies.

---

# PHASE 6 TEST — 20 PASS

```text
registry completeness       → PASS (all 8 agents + all required fields)
agent lookup                → PASS
capability validation       → PASS (valid + invalid)
permission validation       → PASS (valid + denied)
contract version validation → PASS (valid + mismatch)
availability validation     → PASS (AVAILABLE; UNAVAILABLE rejected)
invocation validation       → PASS (valid + mismatch)
unknown-agent rejection     → PASS
capability ≠ permission     → PASS
component separation        → PASS (Continuity vs Quality; forbidden actions)
component integrity         → PASS (locations point to existing repos)
```

```text
python3 -m pytest tests/openclaw/test_registry.py -> 20 passed
```

---

# PHASE 6 FINAL CHECKPOINT

```text
PHASE: 6
STATUS: PASS

CHECKS:
- Registry Completeness: PASS
- Agent Lookup: PASS
- Capability: PASS
- Permission: PASS
- Contract: PASS
- Availability: PASS
- Invocation: PASS
- Unknown Agent Handling: PASS
- Existing Component Integrity: PASS
- Git Integrity: PASS

FINDINGS:
None.

REPAIRS:
None required.

RETEST:
All 20 registry tests PASS; full suite PASS (regression clean).

CONCLUSION:
Official Development F agent registry established with complete entries,
separate capability/permission validation, availability and invocation
validation, unknown-agent rejection, and read-only pointers to the existing
Handoff Agent and Quality Guardian components.

NEXT:
PHASE 7
```