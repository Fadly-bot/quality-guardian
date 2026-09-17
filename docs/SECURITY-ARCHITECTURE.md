# Development F — Security Architecture

Status: **PASS** (Phase 2 security architecture foundation).

Security in Development F is an **independent boundary**: the Security Gate.
It is deliberately not merged with the Quality Guardian. The Quality Guardian
audits quality; the Security Gate verifies security and holds release-blocking
authority. This document defines the security layers, the Security Gate, the
scanner evidence model, agent security, and the human approval boundary.

It is consistent with `docs/DEVELOPMENT-F.md` (Phase 1) and with the existing,
complete components:

```text
/home/fadly_03/handoff-agent    → Handoff Agent (continuity / checkpoint)
/home/fadly_03/quality-guardian → Quality Guardian (quality / audit)
```

## SECURITY LAYERS

### 1. Repository Security

- **Secret scanning:** scan source, tests, fixtures, docs, logs, config,
  staged files, and Git diff/history for API keys, tokens, passwords, private
  keys, bearer credentials, and hardcoded credentials.
- **Git history scanning:** audit commit history for leaked secrets.
- **Sensitive files:** `.env`, keystores, and secret-like content are excluded
  and never enter agent context, prompts, checkpoints, reports, or telemetry.
- **Git discipline:** repositories are scanned read-only during audits;
  mutation is restricted to allowlisted commands; push and destructive resets
  are forbidden from restricted surfaces.

Reference (existing implementation): the Quality Guardian scanner stack
(`docs/SECURITY.md` — Semgrep, Gitleaks, Trivy, OSV-Scanner, ecosystem audit)
and the Handoff Agent security/sandbox hardening
(`src/handoff_agent/security.py`, `sandbox.py`).

### 2. Dependency Security

- **Dependency audit:** dependencies are audited for known vulnerabilities
  before release.
- **Lockfile:** release readiness requires a pinned manifest/lockfile where the
  ecosystem supports it.
- **Known vulnerabilities:** a confirmed critical/high dependency
  vulnerability blocks release unless justified with evidence and human
  review.
- **Evidence requirement:** dependency findings require objective evidence
  (lockfile integrity, scanner CVE/GHSA output, registry facts, ecosystem
  audit). AI intuition about a package name or version is never evidence.

### 3. Application Security

- **Authentication / authorization:** deny-by-default; every action is
  capability-checked; least-privilege wins.
- **Input validation:** structured inputs are validated; invalid input is
  rejected, never silently tolerated.
- **Output handling:** outputs are bounded and secret-scanned before they enter
  prompts, reports, or checkpoints.
- **Injection:** no dynamic `eval`/`exec`; shell execution is forbidden on
  restricted surfaces; subprocesses run only allowlisted commands with
  arguments, never a shell string.
- **Path traversal:** filesystem access resolves under the sandbox root;
  traversal outside the root is rejected.
- **SSRF / network:** outbound network access is restricted to allowlisted
  endpoints; private/local/internal hosts are blocked unless explicitly listed;
  TLS verification is enforced.
- **Session/token security:** approval and checkpoint tokens bind to the exact
  request; replay/forgery is rejected.

### 4. Infrastructure Security

- **Environment variables:** only allowlisted variables are exposed to
  processes on restricted surfaces.
- **Secrets in storage:** secrets are never logged, echoed, or persisted.
- **Transport:** HTTPS with TLS verification is required for outbound
  traffic; insecure verification modes are rejected.
- **Deployment configuration:** a release requires Quality PASS, Security Gate
  PASS, Deployment Check PASS, and a satisfied human release approval; the
  dry-run is zero-write and zero-network.

### 5. Agent Security

Agent security covers every agent in Development F, including OpenClaw:

- **Shell permissions:** restricted surfaces run no shell; only allowlisted
  commands may be invoked.
- **File permissions:** filesystem access is restricted to the audited project
  root and sandboxed roots; no unrestricted filesystem access.
- **Network permissions:** allowlisted hosts only; exit to a non-allowlisted
  destination is denied.
- **Secret exposure:** secret-like content is filtered from agent context,
  prompts, and outputs.
- **Prompt injection:** agent context is built from verified project state;
  tool/scanner output is bounded and validated; agent output is never blindly
  trusted.
- **Agent trust:** every handoff between agents is verified (checkpoint
  validity, Git state, ownership, freshness) before acceptance; an agent does
  not implicitly trust another agent's claim.
- **Destructive commands:** destructive actions require explicit human
  approval; approval binds to the exact request.
- **Approval boundaries:** deployment, release, and production approvals are
  human-only; no agent, automation mode, retry, recovery, or orchestration
  path may bypass them.
- **Agent-to-agent trust:** capability and permission of every agent are
  verified separately and validated per invocation.
- **Least privilege:** no agent holds a permission outside its stated contract
  (`docs/AGENT-CONTRACT.md`, Phase 3; enforced by `docs/AGENT-REGISTRY.md`,
  Phase 6).

## SECURITY GATE

The Security Gate is a boundary independent of the Quality Guardian.

```text
Quality Guardian
    ↓
Quality Audit
    ↓
Security Gate
    ↓
Security Decision
```

Gate outputs:

```text
PASS
BLOCK
NEEDS_REVIEW
```

- **PASS:** every applicable security evidence channel is present, covered and
  passing.
- **BLOCK:** a confirmed security finding or an unavailable critical evidence
  channel (`ERROR`, `NOT_SCANNED` on a required channel).
- **NEEDS_REVIEW:** evidence insufficient for a definitive PASS or BLOCK.

## EVIDENCE MODEL

Scanner evidence uses exactly these statuses:

```text
PASS
FAIL
ERROR
NOT_APPLICABLE
NOT_SCANNED
```

Rules:

```text
NOT_SCANNED ≠ PASS
```

- `FAIL`, `ERROR`, and `NOT_SCANNED` are never automatically considered safe.
- A missing scan, a failed scan, or a scan whose output does not prove target
  coverage (`COMMAND EXECUTED ≠ TARGET COVERED ≠ SCAN PASSED`) cannot produce
  PASS on that channel.
- `NOT_SCANNED` from the Quality Guardian (e.g. zero-commit history scan) must
  not be reinterpreted by any downstream agent as PASS or as a security
  finding.

## HUMAN APPROVAL

Deployment and release approval remain human-only:

```text
HUMAN PROJECT APPROVAL    → before planning starts
HUMAN RELEASE APPROVAL    → after Quality + Security + Deployment Check PASS
HUMAN PRODUCTION APPROVAL → before production promotion
```

There is no bypass path. OpenClaw may request, wait for, record, and continue
after a valid approval; it never grants one.

## QUALITY ≠ SECURITY

```text
Quality Guardian → quality audit         (read-only)
Security Gate    → security verification (release blocking)
```

- The Quality Guardian may perform security-related checks as part of its
  audit only if its contract allows, but it is **not** the Security Gate.
- The Security Gate remains the release-blocking security boundary.
- Handoff Agent is neither a quality gate nor a security gate.

---

# PHASE 2 CHECKPOINT — PASS

- Security boundary clear: Security Gate is an independent, release-blocking
  boundary distinct from Quality Guardian.
- Scanner evidence model clear: PASS / FAIL / ERROR / NOT_APPLICABLE /
  NOT_SCANNED with `NOT_SCANNED ≠ PASS`.
- Quality ≠ Security: separate agents, separate gates, separate authority.
- Agent security covered: shell, file, network, secrets, prompt injection,
  agent trust, destructive commands, approval boundaries, agent-to-agent
  trust, least privilege.
- Human approval covered: deployment/release approvals are human-only.
- Release-blocking mechanism clear: Quality PASS → Security PASS → Deployment
  Check PASS → Human Release Approval → deploy.

---

# PHASE 2 TEST

### Coverage Test — PASS
All five security layers (Repository, Dependency, Application, Infrastructure,
Agent) are present, plus the Security Gate, evidence model, and human approval.

### Boundary Test — PASS
Quality Guardian = quality audit; Security Gate = security verification and
release blocking. No role overlap. (Cross-referenced with
`docs/DEVELOPMENT-F.md` Section F and `docs/AGENT-CONTRACT.md` Phase 3.)

### Failure Test — PASS
Documented rule: `FAIL`, `ERROR`, and `NOT_SCANNED` are never automatically
treated as safe; only PASS counts, and only with real scanner evidence of
target coverage.

### Human Approval Test — PASS
No bypass path exists: deployment/release/production approvals are human-only;
OpenClaw cannot approve.

### Documentation Consistency Test — PASS
Consistent with `docs/DEVELOPMENT-F.md` (Phase 1) and with the existing
Quality Guardian security model (`docs/SECURITY.md`) and Handoff Agent
hardening.

### Git Test — PASS
`git diff --check` clean; `git status` shows only the intended change set for
this phase.

---

# PHASE 2 FINAL CHECKPOINT

```text
PHASE: 2
STATUS: PASS

DOCUMENT:
docs/SECURITY-ARCHITECTURE.md

CHECKS:
- Security Layers: PASS
- Security Boundary: PASS
- Scanner Evidence Model: PASS
- Agent Security: PASS
- Human Approval: PASS
- Quality/Security Separation: PASS
- Cross-Document Consistency: PASS
- Git Integrity: PASS

FINDINGS:
None.

REPAIRS:
None required.

RETEST:
Coverage, boundary, failure, human-approval, consistency, and git tests all
PASS.

CONCLUSION:
Security architecture established as an independent, release-blocking
boundary, distinct from Quality Guardian, with a strict evidence model and
human-only release approval, consistent with the existing components.

NEXT:
PHASE 3
```