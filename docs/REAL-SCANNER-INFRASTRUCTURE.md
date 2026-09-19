# Real Scanner Infrastructure — Development F (Phase 14)

Status: **PASS**

Test module: `tests/openclaw/test_scanner_infrastructure.py` (22 tests, all PASS).
Full suite at the time of this document: `232 passed, 0 failed, 0 errors, 0 skipped`
(210 Phase 1–13 regression + 22 new).

## Architecture

```
openclaw/scanner_infrastructure.py

ScannerDefinition (name, command, argv template, version args/pattern, normalizer)
        ↓
ScannerRunner (one explicitly-given project root)
        ↓  availability check  → shutil.which of the defined command
        ↓  version check       → real subprocess, parsed + bounded
        ↓  execution           → real subprocess, bounded, redacted
        ↓  raw result          → command, exit code, stdout, stderr, duration, version
        ↓  evidence normalization → deterministic, finding extraction or NEEDS_REVIEW
        ↓
ScannerResult (provenance-complete) ── evidence_id (SHA-256 content address)
        ↓
Security/Quality decision (PASS / FAIL / ERROR / NOT_SCANNED / NEEDS_REVIEW)
        ↓
make_gate_scanner_runner() → openclaw.security_gate.ScannerRun seam
```

The Security Gate keeps its existing contract (`ScannerRun`, availability seam).
Phase 14 does not rebuild the gate; it supplies real, provenance-stamped evidence
through the established seam (`JANGAN rebuild` honored).

## Scanner contract (§5)

`ScannerResult` fields: `scanner`, `scanner_version`, `command` (argv),
`target`, `project_identity`, `started_at`, `duration`, `exit_code`, `stdout`,
`stderr`, `status`, `findings_count`, `findings`, `evidence_id`, `error`,
`metadata`.

Default definitions (`DEFAULT_SCANNERS`): `gitleaks`, `semgrep`, `trivy`,
`osv-scanner`, `pip-audit` — evaluated per §4. Custom scanners are plain
`ScannerDefinition` entries; each carries its own findings normalizer.

## Availability model (§4)

A scanner is AVAILABLE only when its binary resolves on the host
(`shutil.which`). Nothing is ever installed implicitly. On this host the probe
shows none of the five external scanners installed, so every external check is
truthfully **NOT_SCANNED** — never PASS, never fabricated.

## Execution model

- Real `subprocess.run` with `capture_output=True, text=True`.
- Version check bounded by `VERSION_TIMEOUT_S` (15 s); scan by
  `SCAN_TIMEOUT_S` (120 s).
- Negative exit codes (terminated by signal) and every unexpected process
  failure (`OSError`, broken runner) map to **ERROR**.
- `argv[0]` is the resolved host path of the defined command; the target is
  substituted into the definition's argv template.

## Status mapping (§6)

| Condition | Status |
|---|---|
| binary not found | `NOT_SCANNED` |
| version unreadable / unparseable | `ERROR` |
| execution timeout / exec failure / signal / unexpected failure | `ERROR` |
| findings extracted from trusted output | `FAIL` |
| clean run with existing, parseable evidence and exit 0 | `PASS` |
| malformed/ambiguous output; empty output; exit 0 without evidence | `NEEDS_REVIEW` |

Exit code 0 alone is never sufficient for PASS: a scanner that exits 0 but
prints nothing, or prints JSON with no findings collection, is NEEDS_REVIEW.
`NOT_SCANNED != PASS`, `FAIL != PASS`, `ERROR != PASS`, `BLOCKED != PASS` are
preserved everywhere; the mapping is pinned by tests A–Q of §10.

## Evidence model & provenance (§7)

Every result records WHO (scanner + version), WHAT (argv), WHERE (target),
WHEN (`started_at` + `duration`), RESULT (exit code + normalized status),
OUTPUT (stdout/stderr + findings), SOURCE (project identity). `evidence_id`
is `ev_` + 16 hex chars of SHA-256 over the provenance payload with timing
excluded, so identical inputs produce identical evidence (deterministic
normalization, test M) and different scans never collide (test N).

## Project isolation (§9)

`ScannerRunner` is bound to one explicitly-given project root. Targets must
resolve inside it (`ProjectIsolationError` otherwise) and must exist
(`UnsafeTargetError`). Evidence carries `project_identity`
(`git:<branch>@<HEAD>`, or a deterministic content hash for repositories
without commits), so evidence from project A can never be attributed to
project B (test L/L2).

## Security boundary (§8)

The infrastructure only observes. It never edits source, never fixes
findings, never commits, pushes, deploys, bypasses approvals, disables a
scanner to obtain PASS, or installs tooling silently. All scanner output is
passed through `redact()` before it enters evidence (AWS/GitHub/Stripe/
GitLab/Slack/Google token shapes, private-key blocks, generic credential
assignments) — pinned by test K. The same redaction is applied to findings.

## Timeout / error handling

Timeouts raise `subprocess.TimeoutExpired` internally and surface as
`ERROR` results with the bound in the error text (test O). Unexpected process
failures are caught and mapped to `ERROR` — a failing scanner can never crash
the gate and never yields PASS (test P).

## Examples

```python
from openclaw.scanner_infrastructure import ScannerRunner, make_gate_scanner_runner
from openclaw.security_gate import SecurityGate

runner = ScannerRunner("/path/to/project")          # explicit target
result = runner.run_scanner("gitleaks")             # NOT_SCANNED if absent
gate = SecurityGate(
    tool_available=runner.is_available,
    scanner_runner=make_gate_scanner_runner(runner),
)
verdict = gate.run("/path/to/project")              # real evidence or NOT_SCANNED
```

## Verified vs not verified (no overclaiming)

- **Real (verified by tests):** subprocess execution contract, availability
  detection, version parsing, timeout/error paths, normalization, redaction,
  provenance, isolation — exercised through real executables and real
  subprocesses in the test suite.
- **Real (verified on this host):** the full-repo gate run reports the five
  external scanners as NOT_SCANNED because their binaries are not installed.
- **Not verified on this host:** actual gitleaks/semgrep/trivy/osv-scanner/
  pip-audit verdicts. None are claimed. Converting those NOT_SCANNED checks to
  PASS requires installing the tools in a controlled environment — a human
  decision (§4: no silent installs).

## Limitations

- The default findings normalizer trusts JSON reports with a findings
  collection; non-JSON scanners need a custom `findings_from` to be usable.
- `repo.branch_protection` remains NOT_SCANNED by design: it requires
  repository-host API access, not local evidence.
- Evidence is content-addressed but not persisted to disk; storage/append-log
  is future work.

## Future extensions

- Persistent evidence store keyed by `evidence_id` with tamper-evident chaining.
- SARIF output support and per-scanner severity weighting.
- Scanner allowlisting per project profile (`.guardian` policy file).
- Controlled-environment installation path for converting NOT_SCANNED checks
  to verified PASS (explicit human authorization required).
