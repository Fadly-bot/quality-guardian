"""Phase 14 — Real Scanner Infrastructure tests (Development F).

Covers the Section 10 matrix: availability, version, execution, evidence
normalization, status rules (NOT_SCANNED/FAIL/ERROR/NEEDS_REVIEW never PASS by
shortcut), provenance, redaction, project isolation, timeouts, unexpected
failures, and the Security Gate adapter.

The fake scanners are REAL executables executed through REAL subprocesses
(only PATH resolution is injected), so the execution contract is exercised
end to end. Pure seam injection is used for the failure branches that cannot
be produced deterministically with a real binary (timeout, exec failure).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openclaw.scanner_infrastructure import (
    ERROR,
    FAIL,
    NOT_SCANNED,
    NEEDS_REVIEW,
    PASS,
    ProjectIsolationError,
    ScannerDefinition,
    ScannerRunner,
    UnsafeTargetError,
    _default_findings,
    make_gate_scanner_runner,
)
from openclaw.security_gate import SecurityGate


# --- fixture helpers ---------------------------------------------------------


def _write_fake_scanner(
    tmp_path: Path,
    *,
    name: str = "fakescan",
    findings_json: str = "[]",
    exit_code: int = 0,
    version: str = "fakescan 2.4.7",
    version_fail: bool = False,
    version_garbage: bool = False,
) -> Path:
    """Write a real executable shell script that emulates a JSON scanner."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    if version_fail:
        version_block = "exit 3"
    elif version_garbage:
        version_block = "printf '%s\\n' 'version: unknown-format'\n  exit 0"
    else:
        version_block = "printf '%s\\n' '" + version + "'\n  exit 0"
    lines = [
        "#!/bin/sh",
        'if [ "$1" = "--version" ]; then',
        "  " + version_block,
        "fi",
        "cat <<'FAKE_EOF'",
        '{"Findings": ' + findings_json + "}",
        "FAKE_EOF",
        "exit " + str(exit_code),
    ]
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    return script


def _runner_for(script: Path, project_root, **overrides) -> ScannerRunner:
    def which(cmd: str):
        return str(script) if cmd == script.name else None

    options = {
        "_which": which,
        "_time": lambda: 1700000000.0,
        "_monotonic": lambda: 42.0,
    }
    options.update(overrides)
    return ScannerRunner(
        project_root,
        definitions={
            script.name: ScannerDefinition(
                name=script.name,
                command=script.name,
                scan_args=("scan", "{target}"),
                findings_from=_default_findings,
            )
        },
        **options,
    )


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "app.py").write_text("print('ok')\n")
    return proj


def _fake_run(version_stdout: str = "fakescan 9.9.9", scan_side_effect=None):
    """A _run seam: git fails quietly, version succeeds, scan side-effects."""

    def run(argv, **kwargs):
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        if "--version" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout=version_stdout, stderr=""
            )
        if scan_side_effect is not None:
            raise scan_side_effect
        raise AssertionError("unexpected scan invocation")

    return run


def _gate_fixture(tmp_path: Path) -> Path:
    """A committed fixture repo that passes every built-in Security Gate check."""
    import json

    repo = tmp_path
    (repo / ".gitignore").write_text(
        ".env\n.env.*\n*.env\n*.pem\n*.key\nid_rsa\nid_dsa\n__pycache__/\n"
    )
    (repo / "app.py").write_text("print('ok')\n")
    (repo / "openclaw").mkdir()
    (repo / "openclaw" / "orchestrator.py").write_text(
        "_assert_actor\napprover != HUMAN_ROLE\n"
    )
    (repo / "openclaw" / "agent_registry.json").write_text(json.dumps({
        "agents": {
            "openclaw": {"forbidden_actions": ["grant_approval"]},
            "deployment_check": {
                "permissions": ["deployment_after_approval"],
                "forbidden_actions": ["deploy_without_approval"],
            },
            "quality_guardian": {"forbidden_actions": ["deploy"]},
        }
    }))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "p14@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "phase14"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repo, check=True)
    return repo


# --- A/B/H: availability ------------------------------------------------------


def test_a_available_scanner_is_detected_and_runs(tmp_path):
    script = _write_fake_scanner(tmp_path)
    runner = _runner_for(script, _project(tmp_path))
    assert runner.is_available("fakescan") is True
    assert runner.resolved_path("fakescan") == str(script)


def test_b_h_unavailable_scanner_is_not_scanned_never_pass(tmp_path):
    runner = ScannerRunner(_project(tmp_path))  # no scanners installed on host
    assert runner.is_available("gitleaks") is False
    result = runner.run_scanner("gitleaks")
    assert result.status == NOT_SCANNED
    assert result.status != PASS
    assert result.exit_code == -1
    assert "not found" in result.error
    assert result.evidence_id  # even a NOT_SCANNED carries provenance
    assert result.project_identity


# --- C/D: version ------------------------------------------------------------


def test_c_version_is_read_from_real_binary(tmp_path):
    script = _write_fake_scanner(tmp_path, version="fakescan 3.1.4")
    runner = _runner_for(script, _project(tmp_path))
    assert runner.read_version("fakescan") == "3.1.4"
    result = runner.run_scanner("fakescan")
    assert result.scanner_version == "3.1.4"


def test_d_version_failure_is_error_not_pass(tmp_path):
    script = _write_fake_scanner(tmp_path, version_fail=True)
    runner = _runner_for(script, _project(tmp_path))
    result = runner.run_scanner("fakescan")
    assert result.status == ERROR
    assert result.status != PASS
    assert "exited 3" in result.error

    script2 = _write_fake_scanner(tmp_path, name="fake2", version_garbage=True)
    runner2 = _runner_for(script2, _project(tmp_path))
    result2 = runner2.run_scanner("fake2")
    assert result2.status == ERROR
    assert "not parseable" in result2.error


# --- E/F: clean and finding ----------------------------------------------------


def test_e_clean_scan_with_valid_evidence_is_pass(tmp_path):
    script = _write_fake_scanner(tmp_path, findings_json="[]")
    runner = _runner_for(script, _project(tmp_path))
    result = runner.run_scanner("fakescan")
    assert result.status == PASS
    assert result.findings_count == 0
    assert result.exit_code == 0
    assert result.stdout.strip()  # evidence exists, not just exit code 0
    assert result.evidence_id.startswith("ev_")


def test_f_finding_is_fail_with_normalized_findings(tmp_path):
    script = _write_fake_scanner(
        tmp_path,
        findings_json='[{"VulnerabilityID": "CVE-2026-0001", "PkgName": "left-pad"}]',
    )
    runner = _runner_for(script, _project(tmp_path))
    result = runner.run_scanner("fakescan")
    assert result.status == FAIL
    assert result.findings_count == 1
    assert "CVE-2026-0001" in result.findings[0]


# --- G/O/P: execution failures -------------------------------------------------


def test_g_execution_error_is_error(tmp_path):
    runner = ScannerRunner(
        _project(tmp_path),
        definitions={"fakescan": ScannerDefinition(
            name="fakescan", command="fakescan", scan_args=("scan", "{target}"),
            findings_from=_default_findings,
        )},
        _which=lambda cmd: "/usr/bin/fakescan" if cmd == "fakescan" else None,
        _run=_fake_run(scan_side_effect=OSError("cannot execute binary")),
    )
    result = runner.run_scanner("fakescan")
    assert result.status == ERROR
    assert "failed to run" in result.error


def test_o_timeout_is_handled_as_error(tmp_path):
    runner = ScannerRunner(
        _project(tmp_path),
        definitions={"fakescan": ScannerDefinition(
            name="fakescan", command="fakescan", scan_args=("scan", "{target}"),
            findings_from=_default_findings,
        )},
        _which=lambda cmd: "/usr/bin/fakescan" if cmd == "fakescan" else None,
        _run=_fake_run(scan_side_effect=subprocess.TimeoutExpired(cmd="fakescan", timeout=120)),
    )
    result = runner.run_scanner("fakescan")
    assert result.status == ERROR
    assert "timed out" in result.error


def test_p_unexpected_process_failure_is_error_not_crash(tmp_path):
    runner = ScannerRunner(
        _project(tmp_path),
        definitions={"fakescan": ScannerDefinition(
            name="fakescan", command="fakescan", scan_args=("scan", "{target}"),
            findings_from=_default_findings,
        )},
        _which=lambda cmd: "/usr/bin/fakescan" if cmd == "fakescan" else None,
        _run=_fake_run(scan_side_effect=RuntimeError("exec machinery exploded")),
    )
    result = runner.run_scanner("fakescan")
    assert result.status == ERROR
    assert "failed to run" in result.error


# --- I/J/Q: untrusted evidence is never PASS ------------------------------------


def test_i_malformed_output_is_needs_review(tmp_path):
    script = _write_fake_scanner(tmp_path, findings_json='"just a string"')
    runner = _runner_for(script, _project(tmp_path))
    result = runner.run_scanner("fakescan")
    assert result.status == NEEDS_REVIEW
    assert result.status != PASS
    assert "ambiguous" in result.error


def test_j_exit_zero_with_invalid_evidence_is_not_auto_pass(tmp_path):
    # JSON but no findings collection -> ambiguous, never PASS.
    script = _write_fake_scanner(tmp_path, findings_json='{"ok": true}')
    runner = _runner_for(script, _project(tmp_path))
    result = runner.run_scanner("fakescan")
    assert result.exit_code == 0
    assert result.status == NEEDS_REVIEW
    assert result.status != PASS


def test_q_empty_output_is_not_automatically_pass(tmp_path):
    script = _write_fake_scanner(
        tmp_path, findings_json="[]", exit_code=0
    )
    # Overwrite the heredoc body so the scanner prints nothing at all.
    body = (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then\n'
        "  printf '%s\\n' 'fakescan 2.4.7'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    runner = _runner_for(script, _project(tmp_path))
    result = runner.run_scanner("fakescan")
    assert result.exit_code == 0
    assert not result.stdout.strip()
    assert result.status == NEEDS_REVIEW
    assert result.status != PASS


# --- K: redaction ---------------------------------------------------------------


def test_k_sensitive_output_is_redacted_in_evidence(tmp_path):
    aws_like = "AKIA" + "B" * 16  # built at runtime: no literal secret in source
    cred_line = "api_key = '" + "x" * 24 + "'"
    leaked = "[{\n  \"RuleID\": \"leak\",\n  \"Secret\": \"" + aws_like + "\",\n" \
             "  \"Raw\": \"" + cred_line + "\"\n}]"
    script = _write_fake_scanner(tmp_path, findings_json=leaked.replace("\n", " "))
    runner = _runner_for(script, _project(tmp_path))
    result = runner.run_scanner("fakescan")

    assert result.status == FAIL  # findings still surface as findings
    blobs = (result.stdout, result.stderr, " ".join(result.findings),
             str(result.to_dict()))
    for blob in blobs:
        # No credential-shaped material anywhere in the evidence.
        assert aws_like not in blob
        assert cred_line not in blob
    # Redaction is visible wherever evidence exists (stderr may be empty).
    non_empty = [blob for blob in blobs if blob]
    assert non_empty
    assert all("[REDACTED]" in blob for blob in non_empty)
    assert "x" * 24 not in result.stdout


# --- L: project identity / isolation ---------------------------------------------


def test_l_project_identity_is_git_head_and_preserved_in_evidence(tmp_path):
    repo = _project(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    script = _write_fake_scanner(tmp_path)
    runner = _runner_for(script, repo)
    assert runner.project_identity() == "git:main@" + head
    result = runner.run_scanner("fakescan")
    assert result.project_identity == "git:main@" + head

    # A different project yields a different identity: no cross-project mixing.
    other = _project(tmp_path / "other_root" / "sub")
    runner2 = _runner_for(script, other)
    assert runner2.project_identity() != runner.project_identity()


def test_l2_identity_falls_back_to_content_hash_without_git(tmp_path):
    script = _write_fake_scanner(tmp_path)
    runner = _runner_for(script, _project(tmp_path))
    identity = runner.project_identity()
    assert identity.startswith("content:")
    assert identity == runner.project_identity()  # deterministic for same tree


def test_isolation_target_outside_project_root_is_refused(tmp_path):
    script = _write_fake_scanner(tmp_path)
    runner = _runner_for(script, _project(tmp_path))
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ProjectIsolationError):
        runner.run_scanner("fakescan", outside)


def test_isolation_missing_target_is_refused(tmp_path):
    script = _write_fake_scanner(tmp_path)
    runner = _runner_for(script, _project(tmp_path))
    with pytest.raises(UnsafeTargetError):
        runner.run_scanner("fakescan", tmp_path / "does-not-exist")


# --- M/N: determinism and separation ----------------------------------------------


def test_m_normalization_is_deterministic(tmp_path):
    script = _write_fake_scanner(tmp_path, findings_json="[]")
    runner = _runner_for(script, _project(tmp_path))
    first = runner.run_scanner("fakescan")
    second = runner.run_scanner("fakescan")
    assert first.evidence_id == second.evidence_id
    assert first.to_dict() == second.to_dict()


def test_n_multiple_scanners_produce_separate_evidence(tmp_path):
    script_a = _write_fake_scanner(tmp_path, name="scana", findings_json="[]")
    script_b = _write_fake_scanner(
        tmp_path, name="scanb",
        findings_json='[{"VulnerabilityID": "CVE-2026-0002", "PkgName": "pkg"}]',
    )

    def which(cmd: str):
        for script in (script_a, script_b):
            if cmd == script.name:
                return str(script)
        return None

    proj = _project(tmp_path)
    runner = ScannerRunner(
        proj,
        definitions={
            "scana": ScannerDefinition(name="scana", command="scana",
                                       scan_args=("scan", "{target}"),
                                       findings_from=_default_findings),
            "scanb": ScannerDefinition(name="scanb", command="scanb",
                                       scan_args=("scan", "{target}"),
                                       findings_from=_default_findings),
        },
        _which=which,
        _time=lambda: 1700000000.0,
        _monotonic=lambda: 42.0,
    )
    results = runner.run_all(proj)
    assert set(results) == {"scana", "scanb"}
    assert results["scana"].status == PASS
    assert results["scanb"].status == FAIL
    assert results["scana"].evidence_id != results["scanb"].evidence_id
    assert results["scana"].scanner != results["scanb"].scanner
    assert all(r.project_identity == runner.project_identity() for r in results.values())


# --- Security Gate adapter ----------------------------------------------------------


def test_adapter_maps_statuses_to_gate_scanner_run(tmp_path):
    clean = _write_fake_scanner(tmp_path, name="fakescan", findings_json="[]")
    runner = _runner_for(clean, _project(tmp_path))
    adapter = make_gate_scanner_runner(runner, default_scanner="fakescan")

    run = adapter("gitleaks", _project(tmp_path))  # unknown tool -> default scanner
    assert run.found == 0 and run.error == ""

    failing = _write_fake_scanner(tmp_path, name="fakescan",
                                  findings_json='[{"VulnerabilityID": "CVE-1"}]')
    runner_f = _runner_for(failing, _project(tmp_path))
    run_f = make_gate_scanner_runner(runner_f, default_scanner="fakescan")(
        "gitleaks", _project(tmp_path)
    )
    assert run_f.found == 1 and "CVE-1" in run_f.findings[0]

    broken = _write_fake_scanner(tmp_path, name="fakescan", version_fail=True)
    runner_e = _runner_for(broken, _project(tmp_path))
    run_e = make_gate_scanner_runner(runner_e, default_scanner="fakescan")(
        "gitleaks", _project(tmp_path)
    )
    assert run_e.found == -1 and run_e.error


def test_security_gate_consumes_real_scanner_evidence(tmp_path):
    repo = _gate_fixture(tmp_path)
    script = _write_fake_scanner(tmp_path, name="fakescan", findings_json="[]")
    runner = _runner_for(script, repo)
    gate = SecurityGate(
        tool_available=lambda tool: True,
        scanner_runner=make_gate_scanner_runner(runner, default_scanner="fakescan"),
        host_evidence={"branch_protection": {"main": "2026-09-19"}},
    )
    result = gate.run(repo)
    external = {
        c.check_id: c for c in result.checks
        if c.check_id.split(".")[0] in {"repo", "dep", "app"}
        and c.check_id.split(".")[-1] in
        {"gitleaks", "semgrep", "trivy", "osv-scanner", "pip-audit"}
    }
    assert external, "expected external scanner checks in gate output"
    assert all(check.status == PASS for check in external.values())
    assert result.decision == PASS

    # Now the scanner finds something: the gate must FAIL, never PASS.
    leaky = _write_fake_scanner(
        tmp_path, name="fakescan",
        findings_json='[{"VulnerabilityID": "CVE-2026-9999", "PkgName": "pkg"}]',
    )
    gate_leaky = SecurityGate(
        tool_available=lambda tool: True,
        scanner_runner=make_gate_scanner_runner(
            _runner_for(leaky, repo), default_scanner="fakescan"
        ),
        host_evidence={"branch_protection": {"main": "2026-09-19"}},
    )
    result_leaky = gate_leaky.run(repo)
    assert result_leaky.decision == FAIL


def test_security_gate_reports_not_scanned_when_scanner_missing(tmp_path):
    repo = _gate_fixture(tmp_path)
    runner = ScannerRunner(repo)  # nothing installed
    gate = SecurityGate(
        tool_available=lambda tool: runner.is_available(tool),
        scanner_runner=make_gate_scanner_runner(runner),
    )
    result = gate.run(repo)
    assert result.decision == NOT_SCANNED  # unavailable is never PASS
    assert all(
        c.status == NOT_SCANNED for c in result.checks
        if c.check_id.split(".")[-1] in
        {"gitleaks", "semgrep", "trivy", "osv-scanner", "pip-audit"}
    )
