"""Real scanner infrastructure for Development F (Phase 14).

Implements the Phase 14 execution contract as one consistent pipeline:

    Scanner Definition
        -> Availability Check
        -> Version Check
        -> Execution (real subprocess, bounded)
        -> Raw Result (command, exit code, stdout, stderr, duration, version)
        -> Evidence Normalization (deterministic, redacted, provenance-stamped)
        -> Security/Quality Decision (PASS / FAIL / ERROR / NOT_SCANNED /
           NEEDS_REVIEW)

Status rules (Section 6 — none of these is ever PASS by shortcut):

- tool not found ................. NOT_SCANNED
- version unreadable ............. ERROR   (evidence cannot be trusted)
- execution failure / timeout .... ERROR
- findings present ............... FAIL
- clean run, trusted evidence .... PASS
- ambiguous / incomplete evidence  NEEDS_REVIEW
- exit code 0 alone is NEVER sufficient for PASS: empty or unparseable
  output stays NEEDS_REVIEW.

Evidence provenance (Section 7): every ScannerResult records WHO (scanner +
version), WHAT (argv), WHERE (target), WHEN (started_at + duration), RESULT
(exit code + normalized status), OUTPUT (redacted stdout/stderr + findings),
and SOURCE (project identity: git HEAD + branch, or a deterministic content
hash for repositories without commits). The ``evidence_id`` is a SHA-256
content address over the provenance payload (timing excluded), so identical
inputs normalize to identical evidence.

Project isolation (Section 9): every target must resolve inside the project
root the runner was explicitly constructed with. Evidence from project A can
never be attributed to project B because the project identity is part of the
evidence itself.

Security boundary (Section 8): this infrastructure only OBSERVES. It never
edits source, never fixes findings, never commits, pushes, deploys, bypasses
approvals, or disables a scanner to obtain PASS. It also never installs a
missing tool: an unavailable scanner is NOT_SCANNED, not fabricated.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

# --- status vocabulary (mirrors the gates; shared semantics) ----------------

PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"
NOT_SCANNED = "NOT_SCANNED"
NEEDS_REVIEW = "NEEDS_REVIEW"
BLOCKED = "BLOCKED"

VERSION_TIMEOUT_S = 15.0
SCAN_TIMEOUT_S = 120.0

# Redaction of sensitive material in scanner output (Section 7). Same secret
# classes as the Security Gate's catalog; evidence must never store secrets.
_REDACTIONS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA[REDACTED]"),
    (re.compile(r"(?i)gh[pousr]_[0-9A-Za-z]{30,}"), "gh_[REDACTED]"),
    (re.compile(r"(?i)sk_live_[0-9A-Za-z]{16,}"), "sk_live_[REDACTED]"),
    (re.compile(r"glpat-[0-9A-Za-z_\-]{20,}"), "glpat-[REDACTED]"),
    (re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}"), "xox-[REDACTED]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "AIza[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "[REDACTED PRIVATE KEY]"),
    (
        re.compile(
            r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?token)"
            r"\s*[:=]\s*[\"'][^\"']{12,}[\"']"
        ),
        r"\1=[REDACTED]",
    ),
)


class ScannerInfrastructureError(Exception):
    """Base error for the scanner infrastructure."""


class UnknownScanner(ScannerInfrastructureError):
    """Raised when a scanner has no definition in this runner."""


class ProjectIsolationError(ScannerInfrastructureError):
    """Raised when a target escapes the explicitly-given project root."""


class UnsafeTargetError(ScannerInfrastructureError):
    """Raised when a scan target does not exist or is not scannable."""


def redact(text: str) -> str:
    """Redact credential-shaped material from scanner output."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _default_findings(stdout: str, stderr: str, exit_code: int) -> list[str]:
    """Default normalizer: JSON blob with a findings list, else untrusted.

    Raises ``ValueError`` when output cannot be interpreted — the caller maps
    that to NEEDS_REVIEW instead of inventing a clean result.
    """
    if not stdout.strip():
        if exit_code == 0 and not stderr.strip():
            raise ValueError("empty scanner output")  # ambiguous, not PASS
        raise ValueError("no parseable stdout")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stdout is not JSON: {exc}") from exc
    if isinstance(data, list):
        return [json.dumps(item, sort_keys=True) for item in data]
    if isinstance(data, dict) and isinstance(data.get("Findings"), list):
        return [json.dumps(item, sort_keys=True) for item in data["Findings"]]
    raise ValueError("unrecognized JSON shape (no Findings list)")


# --- scanner definitions ----------------------------------------------------


@dataclass(frozen=True)
class ScannerDefinition:
    """Declarative definition of one external scanner.

    ``scan_args`` is an argv template; ``{target}`` is substituted with the
    resolved scan target. ``findings_from`` maps raw output to normalized,
    human-auditable finding strings; raising ``ValueError`` signals output the
    infrastructure refuses to trust (NEEDS_REVIEW).
    """

    name: str
    command: str
    scan_args: tuple[str, ...]
    version_args: tuple[str, ...] = ("--version",)
    version_pattern: str = r"(\d+\.\d+(?:\.\d+)?)"
    findings_from: Callable[[str, str, int], list[str]] = _default_findings

    def argv(self, target: str) -> list[str]:
        return [
            part.format(target=str(target)) for part in (self.command, *self.scan_args)
        ]


def _extract(*maps: str):
    """Build a findings extractor over nested JSON shapes per scanner."""
    keys = set(maps)

    def _find(node, out):
        if isinstance(node, dict):
            if keys & node.keys():
                rule = node.get("ruleId") or node.get("check_id") or node.get(
                    "VulnerabilityID"
                ) or node.get("id") or node.get("name") or "finding"
                where = node.get("path") or node.get("File") or node.get(
                    "PkgName"
                ) or ""
                line = node.get("StartLine") or node.get("line") or ""
                out.append(f"{rule}@{where}" + (f":{line}" if line else ""))
            for value in node.values():
                _find(value, out)
        elif isinstance(node, list):
            for item in node:
                _find(item, out)

    def extractor(stdout: str, stderr: str, exit_code: int) -> list[str]:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"stdout is not JSON: {exc}") from exc
        # A trustworthy report is a findings list (or an object that contains
        # one). Any other shape is ambiguous evidence, never a clean claim.
        if isinstance(data, dict) and not any(
            isinstance(value, list) for value in data.values()
        ):
            raise ValueError("report contains no findings collection")
        if not isinstance(data, (list, dict)):
            raise ValueError("unrecognized report shape")
        out: list[str] = []
        _find(data, out)
        return out

    return extractor


# Default definitions for the scanners Section 4 requires to be evaluated.
# The argv templates follow each tool's documented interface; they are real
# commands executed verbatim when the tool is installed on the host.
DEFAULT_SCANNERS: tuple[ScannerDefinition, ...] = (
    ScannerDefinition(
        name="gitleaks",
        command="gitleaks",
        scan_args=("detect", "--source", "{target}", "--report-format", "json",
                   "--report-path", "-"),
        findings_from=_extract("RuleID", "File", "StartLine"),
    ),
    ScannerDefinition(
        name="semgrep",
        command="semgrep",
        scan_args=("scan", "--json", "--quiet", "{target}"),
        findings_from=_extract("check_id", "path", "start"),
    ),
    ScannerDefinition(
        name="trivy",
        command="trivy",
        scan_args=("fs", "--format", "json", "--quiet", "{target}"),
        findings_from=_extract("VulnerabilityID", "PkgName"),
    ),
    ScannerDefinition(
        name="osv-scanner",
        command="osv-scanner",
        scan_args=("--json", "{target}"),
        findings_from=_extract("id", "source"),
    ),
    ScannerDefinition(
        name="pip-audit",
        command="pip-audit",
        scan_args=("--format", "json", "-r", "{target}"),
        findings_from=_extract("id", "name"),
    ),
)


# --- raw + normalized results ----------------------------------------------


@dataclass
class ScannerResult:
    """Normalized, provenance-complete result of one scanner execution."""

    scanner: str
    scanner_version: str
    command: list[str]
    target: str
    project_identity: str
    started_at: float
    duration: float
    exit_code: int
    stdout: str
    stderr: str
    status: str
    findings_count: int
    findings: list[str] = field(default_factory=list)
    evidence_id: str = ""
    error: str = ""
    metadata: dict = field(default_factory=dict)

    def _identity_payload(self) -> dict:
        return {
            "scanner": self.scanner,
            "scanner_version": self.scanner_version,
            "command": self.command,
            "target": self.target,
            "project_identity": self.project_identity,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "status": self.status,
            "findings_count": self.findings_count,
            "findings": self.findings,
            "error": self.error,
            "metadata": self.metadata,
        }

    def compute_evidence_id(self) -> str:
        """Content-addressed evidence identity (SHA-256 over provenance)."""
        payload = json.dumps(self._identity_payload(), sort_keys=True,
                             separators=(",", ":"))
        self.evidence_id = "ev_" + hashlib.sha256(payload.encode()).hexdigest()[:16]
        return self.evidence_id

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "scanner": self.scanner,
            "scanner_version": self.scanner_version,
            "command": self.command,
            "target": self.target,
            "project_identity": self.project_identity,
            "started_at": self.started_at,
            "duration": self.duration,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "status": self.status,
            "findings_count": self.findings_count,
            "findings": self.findings,
            "error": self.error,
            "metadata": self.metadata,
        }


# --- the runner --------------------------------------------------------------


class ScannerRunner:
    """Executes defined scanners against one explicitly-given project root.

    Real subprocesses are used for availability, version, and scan execution.
    Seams (``_which``, ``_run``, ``_time``, ``_monotonic``) exist so tests can
    exercise every branch deterministically without mocking the contract away.
    """

    def __init__(
        self,
        project_root: str | Path,
        definitions: Mapping[str, ScannerDefinition] | None = None,
        *,
        scan_timeout: float = SCAN_TIMEOUT_S,
        version_timeout: float = VERSION_TIMEOUT_S,
        _which: Callable[[str], str | None] = shutil.which,
        _run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        _time: Callable[[], float] = time.time,
        _monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root = Path(project_root).resolve()
        if not self._root.is_dir():
            raise UnsafeTargetError(f"project root is not a directory: {self._root}")
        self._definitions = dict(definitions) if definitions else {
            d.name: d for d in DEFAULT_SCANNERS
        }
        self._scan_timeout = scan_timeout
        self._version_timeout = version_timeout
        self._which = _which
        self._run = _run
        self._time = _time
        self._monotonic = _monotonic

    # --- project identity / isolation ---------------------------------------

    @property
    def project_root(self) -> Path:
        return self._root

    def project_identity(self) -> str:
        """Repository identity for provenance: git HEAD, else content hash."""
        head = self._git("rev-parse", "HEAD")
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD")
        if head:
            return f"git:{branch or '?'}@{head}"
        digest = hashlib.sha256()
        for path in sorted(self._root.rglob("*")):
            if ".git" in path.parts:
                continue
            if path.is_file():
                rel = f"{path.relative_to(self._root)}:{path.stat().st_size};"
                digest.update(rel.encode())
            else:
                digest.update(f"{path.relative_to(self._root)}/;".encode())
        return f"content:{digest.hexdigest()[:24]}"

    def _git(self, *args: str) -> str:
        try:
            result = self._run(
                ["git", "-C", str(self._root), *args],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    def _resolve_target(self, target: str | Path | None) -> Path:
        resolved = Path(target).resolve() if target else self._root
        if not resolved.exists():
            raise UnsafeTargetError(f"scan target does not exist: {resolved}")
        if resolved != self._root and self._root not in resolved.parents:
            raise ProjectIsolationError(
                f"scan target {resolved} is outside the project root {self._root}"
            )
        return resolved

    # --- availability / version ----------------------------------------------

    def definition(self, name: str) -> ScannerDefinition:
        defn = self._definitions.get(name)
        if defn is None:
            raise UnknownScanner(f"no scanner definition for {name!r}")
        return defn

    def resolved_path(self, name: str) -> str | None:
        return self._which(self.definition(name).command)

    def is_available(self, name: str) -> bool:
        """True only when the scanner binary exists on this host."""
        return self.resolved_path(name) is not None

    def has_scanner(self, name: str) -> bool:
        """True when a definition exists for ``name`` in this runner."""
        return name in self._definitions

    def read_version(self, name: str) -> str:
        """Read and parse the scanner version; raises on any failure."""
        defn = self.definition(name)
        resolved = self.resolved_path(name)
        if resolved is None:
            raise ScannerInfrastructureError(f"{name} is not installed")
        try:
            result = self._run(
                [resolved, *defn.version_args],
                capture_output=True, text=True,
                timeout=self._version_timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ScannerInfrastructureError(
                f"{name} version check timed out: {exc}"
            ) from exc
        except OSError as exc:
            raise ScannerInfrastructureError(
                f"{name} version check failed: {exc}"
            ) from exc
        except Exception as exc:  # unexpected runner failure -> ERROR, never PASS
            raise ScannerInfrastructureError(
                f"{name} version check failed unexpectedly: {exc}"
            ) from exc
        if result.returncode != 0:
            raise ScannerInfrastructureError(
                f"{name} version check exited {result.returncode}"
            )
        match = re.search(defn.version_pattern, result.stdout + result.stderr)
        if not match:
            raise ScannerInfrastructureError(
                f"{name} version not parseable from: {redact(result.stdout)!r}"
            )
        return match.group(1)

    # --- execution -------------------------------------------------------------

    def run_scanner(self, name: str, target: str | Path | None = None) -> ScannerResult:
        """Run one scanner and normalize its result (full contract chain)."""
        defn = self.definition(name)
        resolved_target = self._resolve_target(target)
        identity = self.project_identity()
        started = self._time()
        start_mono = self._monotonic()

        def _result(status: str, *, version: str = "", exit_code: int = -1,
                    stdout: str = "", stderr: str = "", error: str = "",
                    findings: list[str] | None = None) -> ScannerResult:
            result = ScannerResult(
                scanner=name,
                scanner_version=version,
                command=defn.argv(str(resolved_target)),
                target=str(resolved_target),
                project_identity=identity,
                started_at=started,
                duration=round(self._monotonic() - start_mono, 6),
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                status=status,
                findings_count=len(findings or []),
                findings=list(findings or []),
                error=error,
                metadata={"project_root": str(self._root)},
            )
            result.compute_evidence_id()
            return result

        # 1. availability -> NOT_SCANNED (never PASS, never fabricated).
        resolved = self.resolved_path(name)
        if resolved is None:
            return _result(NOT_SCANNED, error=f"{defn.command} not found on PATH")

        # 2. version -> unreadable version means untrustworthy evidence.
        try:
            version = self.read_version(name)
        except ScannerInfrastructureError as exc:
            return _result(ERROR, error=str(exc))

        # 3. execution (real subprocess, bounded). The defined command is
        #    resolved to its host path; argv[0] runs the installed binary.
        argv = [resolved, *[part.format(target=str(resolved_target))
                            for part in defn.scan_args]]
        try:
            completed = self._run(
                argv, capture_output=True, text=True,
                timeout=self._scan_timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            return _result(
                ERROR, version=version,
                error=f"{name} scan timed out after {self._scan_timeout:g}s",
            )
        except Exception as exc:  # OSError, exec failure, broken runner -> ERROR
            return _result(ERROR, version=version, error=f"{name} failed to run: {exc}")
        if completed.returncode < 0:
            return _result(
                ERROR, version=version, exit_code=completed.returncode,
                stdout=redact(completed.stdout), stderr=redact(completed.stderr),
                error=f"{name} terminated by signal {-completed.returncode}",
            )

        stdout = redact(completed.stdout)
        stderr = redact(completed.stderr)

        # 4. normalization -> findings, or NEEDS_REVIEW when untrusted.
        try:
            findings = [redact(f) for f in defn.findings_from(stdout, stderr,
                                                              completed.returncode)]
        except ValueError as exc:
            return _result(
                NEEDS_REVIEW, version=version, exit_code=completed.returncode,
                stdout=stdout, stderr=stderr,
                error=f"ambiguous output ({exc}); refusing to decide PASS/FAIL",
            )

        if findings:
            return _result(
                FAIL, version=version, exit_code=completed.returncode,
                stdout=stdout, stderr=stderr, findings=findings,
            )

        # 5. decision: exit code 0 alone is not PASS. Evidence must exist and
        #    the run must have succeeded for a clean verdict to be trusted.
        trusted = completed.returncode == 0 and bool(stdout.strip() or stderr.strip())
        if not trusted:
            return _result(
                NEEDS_REVIEW, version=version, exit_code=completed.returncode,
                stdout=stdout, stderr=stderr,
                error="incomplete evidence for a clean claim "
                      f"(exit={completed.returncode}, empty output)",
            )
        return _result(
            PASS, version=version, exit_code=completed.returncode,
            stdout=stdout, stderr=stderr,
        )

    def run_all(self, target: str | Path | None = None) -> dict[str, ScannerResult]:
        """Run every defined scanner; each yields independent evidence."""
        return {
            name: self.run_scanner(name, target) for name in sorted(self._definitions)
        }


# --- Security Gate adapter ----------------------------------------------------


def make_gate_scanner_runner(
    runner: ScannerRunner,
    default_scanner: str = "gitleaks",
) -> Callable[[str, Path], "object"]:
    """Adapt ScannerRunner evidence to the Security Gate's ``scanner_runner``
    seam (``openclaw.security_gate.ScannerRun``).

    The gate's own availability seam stays authoritative for NOT_SCANNED:
    wire ``tool_available=lambda tool: runner.is_available(tool)`` alongside
    this runner. When the tool IS available, the real scan result maps:

        PASS        -> found=0   (gate check PASS with evidence)
        FAIL        -> found=len(findings) (gate check FAIL)
        ERROR       -> found=-1, error set (gate check ERROR)
        NEEDS_REVIEW-> found=-1, error set (gate check ERROR — never PASS)
    """
    from .security_gate import ScannerRun  # local import: adapter, not a cycle

    def _run_gate_scanner(tool: str, root: Path) -> ScannerRun:
        name = tool if runner.has_scanner(tool) else default_scanner
        result = runner.run_scanner(name, root)
        if result.status == PASS:
            return ScannerRun(tool=tool, target=str(root), found=0,
                              findings=[], error="")
        if result.status == FAIL:
            return ScannerRun(tool=tool, target=str(root),
                              found=result.findings_count,
                              findings=result.findings, error="")
        return ScannerRun(tool=tool, target=str(root), found=-1,
                          findings=[], error=result.error or result.status)

    return _run_gate_scanner
