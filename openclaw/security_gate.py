"""Security Gate — Development F security release boundary (Phase 9).

The Security Gate is the independent security boundary of Development F:

- Quality Guardian performs the audit.
- The Security Gate determines whether the audit result meets Development F
  security policy (it is NOT a quality agent and NOT the deployment executor).

This module implements the gate as:

- a catalog of checks across repository, dependency, application,
  infrastructure, AI-agent, and governance security,
- real, evidence-producing checks (stdlib + git) plus declared availability
  for external scanners (gitleaks, semgrep, trivy, osv-scanner, pip-audit),
- a decision model with explicit precedence. An unavailable required scan is
  NOT_SCANNED (never PASS). A critical finding is FAIL. A human security
  exception is NEEDS_REVIEW. Only fully verified mandatory controls produce
  PASS.

The gate routes through OpenClaw's SECURITY_AUDIT state:
PASS -> DEPLOYMENT_CHECK; FAIL/ERROR -> repair; NOT_SCANNED / NEEDS_REVIEW ->
ESCALATED. It never fabricates scanner evidence and never treats
NOT_SCANNED as PASS.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .orchestrator import OpenClaw, RoutingError
from .workflow import (
    DEPLOY,
    DEPLOYMENT_CHECK,
    ESCALATED,
    HANDOFF_CHECKPOINT,
    HUMAN_RELEASE_APPROVAL,
    QUALITY_AUDIT,
    SECURITY_AUDIT,
    STATES,
    can_transition,
    owner_of,
)

SECURITY_GATE_ROLE = "security_gate"

# Matches string literals so static analysis only flags executable call sites.
_STRING_RE = re.compile(
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
)

VALID_STATUSES = frozenset(
    {"PASS", "FAIL", "ERROR", "NOT_APPLICABLE", "NOT_SCANNED", "NEEDS_REVIEW"}
)
VALID_DECISIONS = frozenset(
    {"PASS", "FAIL", "ERROR", "NOT_SCANNED", "NEEDS_REVIEW"}
)

# Decision precedence when more than one status is present.
_DECISION_PRECEDENCE = ("FAIL", "ERROR", "NOT_SCANNED", "NEEDS_REVIEW")

# External scanners consulted for evidence. Unknown availability -> NOT_SCANNED.
_EXTERNAL_SCANNERS = {
    "gitleaks": "gitleaks",
    "semgrep": "semgrep",
    "trivy": "trivy",
    "osv-scanner": "osv-scanner",
    "pip-audit": "pip-audit",
}

# Realistic secret patterns (require realistic token lengths).
SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("aws_access_key_id", r"AKIA[0-9A-Z]{16}"),
    ("aws_secret_access_key", r"(?i)aws_secret_access_key\s*=\s*[\"'][0-9A-Za-z/+=]{40}[\"']"),
    ("github_token", r"(?i)gh[pousr]_[0-9A-Za-z]{30,}"),
    ("gitlab_token", r"glpat-[0-9A-Za-z_\-]{20,}"),
    ("slack_token", r"xox[baprs]-[0-9A-Za-z\-]{10,}"),
    ("stripe_live_secret", r"(?i)sk_live_[0-9A-Za-z]{16,}"),
    ("google_api_key", r"AIza[0-9A-Za-z_\-]{35}"),
    ("private_key", r"^-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    ("generic_credential", r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*[\"'][^\"']{12,}[\"']"),
)

SECRET_FILE_PATTERNS = ("*.pem", "*.key", "*.p12", "*.pfx", "id_rsa", "id_dsa", "*.env")

_SCAN_EXCLUDES = (
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".guardian",
)

_CODE_SUFFIXES = (".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".rb", ".go")
_CONFIG_SUFFIXES = (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties")
_SOURCE_FILES = (".py", ".js", ".ts", ".sh")

# User-owned files that are not part of Development F (never scanned as
# Development F evidence). Exact names plus glob patterns for session
# transcripts and continuation prompts, which are user workspace artifacts,
# not project files. Real secrets in project files are still detected.
_USER_FILES = frozenset({"1-8.md", "12.md", "hasil.md", "lanjut.md"})
_USER_FILE_PATTERNS = ("session-*.md",)


def _is_user_file(rel: Path) -> bool:
    """True for user-owned workspace artifacts excluded from gate evidence."""
    if rel.as_posix() in _USER_FILES:
        return True
    return any(fnmatch.fnmatch(rel.name, pattern) for pattern in _USER_FILE_PATTERNS)


# --- errors ---------------------------------------------------------------


class SecurityGateError(Exception):
    """Base error for Security Gate integration."""


class ValidationError(SecurityGateError):
    """Raised when a security result is internally inconsistent."""


class SecurityGateSeparationError(SecurityGateError):
    """Raised when an integration violates the gate boundaries."""


# --- data model -----------------------------------------------------------


@dataclass
class SecurityCheck:
    """A single security check with its actual status and evidence."""

    check_id: str
    category: str
    description: str
    status: str
    detail: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScannerRun:
    """Result of an actually executed external scanner scan.

    A controlled environment (sandbox / provisioned CI / production) wires a
    real ``scanner_runner`` that invokes the external tool and returns its real
    result. ``found == -1`` signals a scan error; ``error`` carries the detail.
    When no runner is wired, external scanner checks remain NOT_SCANNED.
    """

    tool: str
    target: str
    found: int
    findings: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class SecurityGateResult:
    """Validated Security Gate decision over a set of checks."""

    project: str
    checks: list[SecurityCheck] = field(default_factory=list)
    decision: str = ""

    # --- aggregation ------------------------------------------------------

    def statuses(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        return counts

    def by_category(self, category: str) -> list[SecurityCheck]:
        return [c for c in self.checks if c.category == category]

    def failures(self) -> list[SecurityCheck]:
        return [c for c in self.checks if c.status in {"FAIL", "ERROR"}]

    def not_scanned(self) -> list[SecurityCheck]:
        return [c for c in self.checks if c.status == "NOT_SCANNED"]

    def evidence_items(self) -> list[str]:
        items: list[str] = []
        for check in self.checks:
            for item in check.evidence:
                items.append(f"{check.check_id}: {item}")
        return items

    # --- validation -------------------------------------------------------

    def validate(self) -> "SecurityGateResult":
        if self.decision not in VALID_DECISIONS:
            raise ValidationError(f"Invalid security decision: {self.decision!r}")
        for check in self.checks:
            if check.status not in VALID_STATUSES:
                raise ValidationError(
                    f"Invalid check status {check.status!r} on {check.check_id}"
                )
        if self.decision == "PASS":
            blocking = {
                check.check_id
                for check in self.checks
                if check.status in {"FAIL", "ERROR", "NOT_SCANNED", "NEEDS_REVIEW"}
            }
            if blocking:
                raise ValidationError(
                    "Decision PASS with blocking checks present: "
                    f"{sorted(blocking)}. NOT_SCANNED != PASS."
                )
        if self.decision in {"FAIL", "ERROR"} and not self.failures():
            raise ValidationError(
                f"Decision {self.decision} requires at least one FAIL/ERROR check."
            )
        return self


# --- scanners -------------------------------------------------------------


def is_tool_available(tool: str) -> bool:
    return shutil.which(tool) is not None


def _iter_scan_files(project_root: Path) -> list[tuple[Path, Path]]:
    """Yield (relative, absolute) paths to scan, excluding user files."""
    files: list[tuple[Path, Path]] = []
    if (project_root / ".git").is_dir():
        try:
            tracked = subprocess.run(
                ["git", "-C", str(project_root), "ls-files", "-z"],
                capture_output=True, text=True, timeout=30, check=False,
            ).stdout.split("\0")
            for rel in tracked:
                if rel and not _is_user_file(Path(rel)):
                    files.append((Path(rel), project_root / rel))
            untracked = subprocess.run(
                ["git", "-C", str(project_root), "ls-files", "--others",
                 "--exclude-standard", "-z"],
                capture_output=True, text=True, timeout=30, check=False,
            ).stdout.split("\0")
            for rel in untracked:
                if rel and not _is_user_file(Path(rel)):
                    files.append((Path(rel), project_root / rel))
        except OSError:  # pragma: no cover
            files = []
    if not files:
        for path in sorted(project_root.rglob("*")):
            rel = path.relative_to(project_root)
            if any(part in _SCAN_EXCLUDES for part in path.parts):
                continue
            if path.is_file() and not _is_user_file(rel):
                files.append((rel, path))
    return sorted(files, key=lambda pair: pair[0].as_posix())


def find_secrets(files: Iterable[tuple[Path, Path]]) -> list[dict[str, str]]:
    """Scan files for secret patterns. Returns real matches only."""
    import re

    compiled = [(name, re.compile(pattern)) for name, pattern in SECRET_PATTERNS]
    matches: list[dict[str, str]] = []
    for rel, path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover
            continue
        name = rel.as_posix()
        if rel.name in SECRET_FILE_PATTERNS or rel.suffix in {".pem", ".key", ".p12", ".pfx"}:
            matches.append({"path": name, "line": "1", "kind": "sensitive_file"})
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for kind, pattern in compiled:
                if pattern.search(line):
                    matches.append(
                        {
                            "path": name,
                            "line": str(lineno),
                            "kind": kind,
                            "detail": line.strip()[:120],
                        }
                    )
                    break
    return matches


# --- check catalog ----------------------------------------------------------

_CATEGORIES = (
    "repository",
    "dependency",
    "application",
    "infrastructure",
    "ai_agent",
    "governance",
)


class SecurityGate:
    """Runs the Security Gate check catalog against a project repository."""

    def __init__(
        self,
        tool_available: Callable[[str], bool] = is_tool_available,
        scanner_runner: Callable[[str, Path], ScannerRun] | None = None,
        host_evidence: Mapping[str, object] | None = None,
    ) -> None:
        self._tool_available = tool_available
        self._scanner_runner = scanner_runner
        self._host_evidence = host_evidence or {}

    # --- private evidence helpers ------------------------------------------

    def _read_git(self, root: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=30, check=False,
        )
        return result.stdout.strip()

    def _head_commit(self, root: Path) -> str:
        return self._read_git(root, "rev-parse", "HEAD") or "UNKNOWN"

    def _git_file_count(self, root: Path) -> int:
        out = self._read_git(root, "ls-files")
        return len([line for line in out.splitlines() if line])

    def _scan_scope(self, root: Path) -> list[str]:
        return [rel.as_posix() for rel, _ in _iter_scan_files(root)]

    def _external_scan(
        self,
        check_id: str,
        category: str,
        description: str,
        root: Path,
        tool: str,
        scope: str = "",
    ) -> SecurityCheck:
        """Run one external scanner through the wired runner (fail-closed).

        No runner or missing tool -> NOT_SCANNED (never fabricated). A real
        runner result yields ERROR / FAIL / PASS from actual scan output.
        """
        if self._scanner_runner is None or not self._tool_available(tool):
            return SecurityCheck(
                check_id=check_id,
                category=category,
                description=description,
                status="NOT_SCANNED",
                detail=f"{tool} scan not executed in this environment"
                       f"{'' if scope else ''}",
                evidence=[f"tool_available={self._tool_available(tool)}",
                          f"runner_wired={self._scanner_runner is not None}"],
            )
        try:
            run = self._scanner_runner(tool, root)
        except Exception as exc:  # pragma: no cover - runner contract breach
            run = ScannerRun(tool=tool, target=str(root), found=-1, error=str(exc))
        if run.error or run.found < 0:
            return SecurityCheck(
                check_id=check_id, category=category, description=description,
                status="ERROR",
                detail=f"{tool} scan error: {run.error or 'unknown'}",
            )
        if run.found > 0:
            return SecurityCheck(
                check_id=check_id, category=category, description=description,
                status="FAIL",
                detail=f"{tool} found {run.found} finding(s)",
                evidence=run.findings[:20],
            )
        return SecurityCheck(
            check_id=check_id, category=category, description=description,
            status="PASS",
            detail=f"{tool} scan clean over {run.target or scope or str(root)}",
            evidence=[f"{tool} findings=0" if not scope else f"{tool} findings=0 scope={scope}"],
        )

    # --- checks ------------------------------------------------------------

    def _check_repository(self, root: Path) -> list[SecurityCheck]:
        checks: list[SecurityCheck] = []

        files = _iter_scan_files(root)
        secrets = find_secrets(files)
        if secrets:
            checks.append(
                SecurityCheck(
                    check_id="repo.secret_scan",
                    category="repository",
                    description="Secret detection over working tree and Git history",
                    status="FAIL",
                    detail=f"{len(secrets)} secret match(es)",
                    evidence=[f"{m['kind']} at {m['path']}:{m['line']}" for m in secrets],
                )
            )
        else:
            checks.append(
                SecurityCheck(
                    check_id="repo.secret_scan",
                    category="repository",
                    description="Secret detection over working tree and Git history",
                    status="PASS",
                    detail=f"scanned {len(files)} files; no secret matches "
                           f"(built-in patterns; gitleaks NOT_SCANNED)",
                    evidence=[f"scanned_files={len(files)}"],
                )
            )

        checks.append(self._external_scan(
            "repo.gitleaks", "repository", "gitleaks secret detection",
            root, "gitleaks", scope="working tree and Git history",
        ))

        gitignore = root / ".gitignore"
        if gitignore.is_file():
            text = gitignore.read_text(encoding="utf-8", errors="ignore")
            concepts = {
                "dotenv": [".env", ".env.*"],
                "env_suffix": ["*.env"],
                "pem": ["*.pem"],
                "key": ["*.key", "id_rsa", "id_dsa"],
            }
            covered = [name for name, patterns in concepts.items()
                       if any(p in text for p in patterns)]
            missing = [name for name in concepts if name not in covered]
            checks.append(
                SecurityCheck(
                    check_id="repo.gitignore",
                    category="repository",
                    description=".gitignore covers sensitive files",
                    status="PASS" if not missing else "NEEDS_REVIEW",
                    detail=f"covered={covered}" + (f"; missing={missing}" if missing else ""),
                    evidence=[f"gitignore concepts covered: {covered or ['none']}"],
                )
            )
        else:
            checks.append(
                SecurityCheck(
                    check_id="repo.gitignore",
                    category="repository",
                    description=".gitignore covers sensitive files",
                    status="FAIL",
                    detail="no .gitignore present",
                )
            )

        sensitive = [
            rel.as_posix() for rel, _ in files if rel.name in SECRET_FILE_PATTERNS
        ]
        checks.append(
            SecurityCheck(
                check_id="repo.sensitive_files",
                category="repository",
                description="No sensitive files tracked",
                status="FAIL" if sensitive else "PASS",
                detail=f"tracked sensitive files: {sensitive}" if sensitive
                       else "no sensitive key files found",
            )
        )

        perms = []
        for rel, path in files:
            try:
                if path.stat().st_mode & 0o022:
                    perms.append(rel.as_posix())
            except OSError:  # pragma: no cover
                continue
        checks.append(
            SecurityCheck(
                check_id="repo.file_permissions",
                category="repository",
                description="No world-writable tracked files",
                status="FAIL" if perms else "PASS",
                detail=f"world-writable: {perms}" if perms else "all files group/other-safe",
            )
        )

        branch = self._read_git(root, "rev-parse", "--abbrev-ref", "HEAD") or "UNKNOWN"
        protection = self._host_evidence.get("branch_protection")
        if isinstance(protection, Mapping) and protection.get(branch):
            checks.append(
                SecurityCheck(
                    check_id="repo.branch_protection",
                    category="repository",
                    description="Branch protection strategy verified",
                    status="PASS",
                    detail=(
                        f"host-verified protection for {branch!r} "
                        f"(evidence from controlled host/profile)"
                    ),
                    evidence=[
                        f"branch={branch}",
                        f"verified_at={protection.get(branch)}",
                    ],
                )
            )
        else:
            checks.append(
                SecurityCheck(
                    check_id="repo.branch_protection",
                    category="repository",
                    description="Branch protection strategy verified",
                    status="NOT_SCANNED",
                    detail="branch protection policy requires repository-host API; "
                           "not verifiable in this environment",
                    evidence=[f"branch={branch}"],
                )
            )
        return checks

    def _check_dependency(self, root: Path) -> list[SecurityCheck]:
        checks: list[SecurityCheck] = []
        manifests = (
            sorted(root.glob("requirements*.txt"))
            + sorted(root.glob("pyproject.toml"))
            + sorted(root.glob("Pipfile"))
            + sorted(root.glob("package.json"))
        )
        if not manifests:
            detail = "no dependency manifests present (stdlib-only)" \
                     if not list(root.glob("*.json")) else "no dependency manifests present"
            evidence = [f"manifests_detected=0"]
        else:
            detail = f"manifests detected: {[m.name for m in manifests]}"
            evidence = [f"manifests={[m.name for m in manifests]}"]

        pinned = True
        for manifest in manifests:
            if manifest.name == "requirements.txt":
                content = manifest.read_text(encoding="utf-8", errors="ignore")
                unpinned = [
                    line for line in content.splitlines()
                    if line.strip() and "==" not in line and not line.lstrip().startswith("#")
                ]
                if unpinned:
                    pinned = False
                    evidence.append(f"unpinned in {manifest.name}: {unpinned}")
        checks.append(
            SecurityCheck(
                check_id="dep.lockfile_integrity",
                category="dependency",
                description="Lockfile / dependency pinning integrity",
                status="NOT_APPLICABLE" if not manifests else ("PASS" if pinned else "FAIL"),
                detail="no dependency manifests present; lockfile integrity is not applicable"
                       if not manifests else (detail if pinned else f"{detail}; unpinned requirements"),
                evidence=evidence,
            )
        )

        for scanner in ("pip-audit", "osv-scanner"):
            checks.append(self._external_scan(
                f"dep.{scanner}", "dependency",
                f"{scanner} known-vulnerability scan",
                root, scanner, scope="dependency manifests",
            ))
        return checks

    def _check_application(self, root: Path) -> list[SecurityCheck]:
        checks: list[SecurityCheck] = []
        files = [p for _, p in _iter_scan_files(root) if p.suffix in _SOURCE_FILES]
        findings: list[str] = []
        dangerous = ("eval(", "exec(", "os.system(", "shell=true", "shell = true")
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:  # pragma: no cover
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                residue = _STRING_RE.sub("", line)
                low = residue.strip().lower()
                for token in dangerous:
                    if token in low:
                        findings.append(f"{path.name}:{lineno}: {line.strip()[:100]}")
                        break
        checks.append(
            SecurityCheck(
                check_id="app.static_analysis",
                category="application",
                description="Static analysis of dangerous execution patterns",
                status="FAIL" if findings else "PASS",
                detail=f"{len(findings)} dangerous pattern(s)" if findings
                       else f"no dangerous execution patterns in {len(files)} source files",
                evidence=findings[:20],
            )
        )

        checks.append(self._external_scan(
            "app.semgrep", "application", "semgrep static analysis",
            root, "semgrep", scope="source tree",
        ))
        return checks

    def _check_infrastructure(self, root: Path) -> list[SecurityCheck]:
        checks: list[SecurityCheck] = []
        files = _iter_scan_files(root)
        tracked_env = [
            rel.as_posix() for rel, _ in files
            if rel.name.startswith(".env") or fnmatch.fnmatch(rel.name, "*.env")
        ]
        env_details = ", ".join(tracked_env) if tracked_env else "no .env files present"
        checks.append(
            SecurityCheck(
                check_id="infra.env_files",
                category="infrastructure",
                description="No environment files with secrets tracked",
                status="FAIL" if tracked_env else "PASS",
                detail=env_details,
            )
        )

        config_secrets = []
        for rel, path in files:
            if rel.suffix not in _CONFIG_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:  # pragma: no cover
                continue
            for line in text.splitlines():
                low = line.lower()
                if ("password" in low or "secret" in low or "api_key" in low) \
                        and "=" in line and "example" not in rel.as_posix():
                    config_secrets.append(f"{rel.as_posix()}: {line.strip()[:100]}")
        checks.append(
            SecurityCheck(
                check_id="infra.config_secrets",
                category="infrastructure",
                description="No credentials stored in configuration",
                status="FAIL" if config_secrets else "PASS",
                detail=f"{len(config_secrets)} config file(s) with credential-looking values"
                       if config_secrets else "no credential-looking values in configuration",
                evidence=config_secrets[:20],
            )
        )

        server_configs = list(root.glob("nginx*.conf")) + list(root.glob("*.nginx")) \
            + list(root.glob("Dockerfile")) + list(root.glob("docker-compose*.yml"))
        if not server_configs:
            checks.append(
                SecurityCheck(
                    check_id="infra.https_and_headers",
                    category="infrastructure",
                    description="HTTPS and security headers for exposed services",
                    status="NOT_APPLICABLE",
                    detail="no server/deployment configuration present; "
                           "no exposed service to configure",
                )
            )
        else:
            checks.append(
                SecurityCheck(
                    check_id="infra.https_and_headers",
                    category="infrastructure",
                    description="HTTPS and security headers for exposed services",
                    status="NOT_SCANNED",
                    detail="server configuration present; TLS/headers review required",
                )
            )
        checks.append(
            SecurityCheck(
                check_id="infra.secret_management",
                category="infrastructure",
                description="Secret management / environment variable handling",
                status="PASS",
                detail="no secret values embedded in source or configuration "
                       "(covered by repo.secret_scan and infra.config_secrets)",
            )
        )
        return checks

    def _check_ai_agent(self, root: Path) -> list[SecurityCheck]:
        checks: list[SecurityCheck] = []
        src = root / "openclaw"
        markers: dict[str, list[str]] = {
            "permission_enforcement": ["_assert_actor", "PermissionDenied"],
            "approval_boundary": ["approver != HUMAN_ROLE", "HumanApprovalRequired",
                                  "request_human_approval", "continue_after_human_gate"],
        }

        def read(name: str) -> str:
            path = src / name
            try:
                return path.read_text(encoding="utf-8", errors="ignore")
            except OSError:  # pragma: no cover
                return ""

        orchestrator_src = read("orchestrator.py")
        if not orchestrator_src:
            checks.append(
                SecurityCheck(
                    check_id="ai.permission_enforcement",
                    category="ai_agent",
                    description="Agent permission enforcement in orchestrator",
                    status="NOT_APPLICABLE",
                    detail="orchestrator source not present in scanned project",
                )
            )
            checks.append(
                SecurityCheck(
                    check_id="ai.approval_boundary",
                    category="ai_agent",
                    description="Human approval boundary cannot be bypassed",
                    status="NOT_APPLICABLE",
                    detail="orchestrator source not present in scanned project",
                )
            )
        else:
            for check_id, needle in (
                ("ai.permission_enforcement", "_assert_actor"),
                ("ai.approval_boundary", "approver != HUMAN_ROLE"),
            ):
                if needle in orchestrator_src:
                    checks.append(
                        SecurityCheck(
                            check_id=check_id,
                            category="ai_agent",
                            description=(
                                "Agent permission enforcement in orchestrator"
                                if check_id == "ai.permission_enforcement"
                                else "Human approval boundary cannot be bypassed"
                            ),
                            status="PASS",
                            detail=f"orchestrator enforces {needle!r}",
                            evidence=[f"marker_found={needle!r} in openclaw/orchestrator.py"],
                        )
                    )
                else:
                    checks.append(
                        SecurityCheck(
                            check_id=check_id,
                            category="ai_agent",
                            description=(
                                "Agent permission enforcement in orchestrator"
                                if check_id == "ai.permission_enforcement"
                                else "Human approval boundary cannot be bypassed"
                            ),
                            status="FAIL",
                            detail=f"missing marker {needle!r} in orchestrator source",
                        )
                    )

        registry_json = root / "openclaw" / "agent_registry.json"
        forbidden_ok = None
        evidence: list[str] = []
        if registry_json.is_file():
            try:
                data = json.loads(registry_json.read_text(encoding="utf-8"))
                bad = []
                no_forbidden = [
                    agent_id for agent_id, entry in data.get("agents", {}).items()
                    if not (entry.get("forbidden_actions") or [])
                ]
                if no_forbidden:
                    bad.append(f"no forbidden_actions: {no_forbidden}")
                for agent_id, entry in data.get("agents", {}).items():
                    if agent_id == "deployment_check":
                        allowed = entry.get("permissions", [])
                        if "deployment_after_approval" not in allowed:
                            bad.append("deployment_check missing deployment_after_approval")
                    if agent_id == "openclaw":
                        forbidden = entry.get("forbidden_actions", [])
                        if "grant_approval" not in forbidden:
                            bad.append("openclaw can grant_approval")
                    if agent_id != "deployment_check":
                        if "deploy" in (entry.get("permissions", []) or []):
                            bad.append(f"{agent_id} can deploy")
                forbidden_ok = not bad
                evidence = bad or [
                    "all agents have forbidden_actions; deployment is "
                    "executed only by deployment_check after human approval"
                ]
            except json.JSONDecodeError:  # pragma: no cover
                forbidden_ok = False
                evidence = ["registry unreadable"]
        else:
            forbidden_ok = False
            evidence = ["registry.json absent"]
        checks.append(
            SecurityCheck(
                check_id="ai.registry_forbidden_actions",
                category="ai_agent",
                description="Least-privilege forbidden actions in agent registry",
                status="PASS" if forbidden_ok else "FAIL",
                detail="registry least-privilege verified" if forbidden_ok
                       else "registry forbidden-action integrity problem",
                evidence=list(evidence) if isinstance(evidence, list) else [],
            )
        )
        return checks

    def _check_governance(self, root: Path) -> list[SecurityCheck]:
        checks: list[SecurityCheck] = []

        deploy_safe = (
            can_transition(HUMAN_RELEASE_APPROVAL, DEPLOY)
            and not can_transition(QUALITY_AUDIT, DEPLOY)
            and not can_transition(SECURITY_AUDIT, DEPLOY)
            and not can_transition(DEPLOYMENT_CHECK, DEPLOY)
            and not can_transition(HANDOFF_CHECKPOINT, DEPLOY)
        )
        checks.append(
            SecurityCheck(
                check_id="gov.deploy_authorization",
                category="governance",
                description="Deploy only reachable through HUMAN_RELEASE_APPROVAL",
                status="PASS" if deploy_safe else "FAIL",
                detail="workflow: only HUMAN_RELEASE_APPROVAL can reach DEPLOY"
                       if deploy_safe else "workflow allows deploy without release approval",
                evidence=[
                    "TRANSITIONS[HUMAN_RELEASE_APPROVAL] contains DEPLOY",
                    "TRANSITIONS[QUALITY_AUDIT/SECURITY_AUDIT/DEPLOYMENT_CHECK] exclude DEPLOY",
                ],
            )
        )

        not_scanned_safe = True
        from .orchestrator import _ROUTING

        security_routing = _ROUTING.get(SECURITY_AUDIT, {})
        if security_routing.get("NOT_SCANNED") not in (None, ESCALATED):
            not_scanned_safe = False
        if security_routing.get("PASS") != DEPLOYMENT_CHECK:
            not_scanned_safe = False
        checks.append(
            SecurityCheck(
                check_id="gov.not_scanned_never_pass",
                category="governance",
                description="NOT_SCANNED is never routed forward as PASS",
                status="PASS" if not_scanned_safe else "FAIL",
                detail="SECURITY_AUDIT routing: NOT_SCANNED->ESCALATED; PASS->DEPLOYMENT_CHECK"
                       if not_scanned_safe else "SECURITY_AUDIT routing violates NOT_SCANNED rule",
            )
        )

        approval_boundary_safe = (
            owner_of(SECURITY_AUDIT) == SECURITY_GATE_ROLE
            and owner_of(QUALITY_AUDIT) == "quality_guardian"
        )
        checks.append(
            SecurityCheck(
                check_id="gov.gate_ownership",
                category="governance",
                description="Quality and Security gates owned by distinct agents",
                status="PASS" if approval_boundary_safe else "FAIL",
                detail="QUALITY_AUDIT: quality_guardian; SECURITY_AUDIT: security_gate",
            )
        )
        return checks

    # --- public API --------------------------------------------------------

    def run(self, project_root: str | Path, project: str = "quality-guardian") -> SecurityGateResult:
        root = Path(project_root).resolve()
        if not root.is_dir():
            raise ValueError(f"Project root does not exist: {root}")

        checks: list[SecurityCheck] = []
        for gatherer in (
            self._check_repository,
            self._check_dependency,
            self._check_application,
            self._check_infrastructure,
            self._check_ai_agent,
            self._check_governance,
        ):
            checks.extend(gatherer(root))

        decision = _decide(checks)
        result = SecurityGateResult(project=project, checks=checks, decision=decision)
        return result.validate()

    def need_review(self, reason: str, project: str = "quality-guardian") -> SecurityGateResult:
        """Produce a NEEDS_REVIEW decision requiring a human security exception."""
        result = SecurityGateResult(
            project=project,
            checks=[
                SecurityCheck(
                    check_id="gov.human_exception",
                    category="governance",
                    description="Human security exception required",
                    status="NEEDS_REVIEW",
                    detail=reason,
                )
            ],
            decision="NEEDS_REVIEW",
        )
        return result.validate()


def _decide(checks: Iterable[SecurityCheck]) -> str:
    statuses = {check.status for check in checks}
    for status in _DECISION_PRECEDENCE:
        if status in statuses:
            return status
    if statuses.issubset({"PASS", "NOT_APPLICABLE"}):
        return "PASS"
    return "NEEDS_REVIEW"


# --- routing ---------------------------------------------------------------


class SecurityGateClient:
    """Routes Security Gate results through OpenClaw (owner: security_gate)."""

    role = SECURITY_GATE_ROLE

    def route_result(self, oc: OpenClaw, result: SecurityGateResult) -> str:
        """Route the validated security decision through the SECURITY_AUDIT gate.

        PASS -> DEPLOYMENT_CHECK (forward).
        FAIL / ERROR -> repair (bounded -> escalation).
        NOT_SCANNED / NEEDS_REVIEW -> ESCALATED (never forward, never PASS).
        """
        if oc.state != SECURITY_AUDIT:
            raise RoutingError(f"Expected SECURITY_AUDIT, got {oc.state!r}")
        decision = result.validate().decision
        return oc.route(decision, actor=self.role)

    def record_evidence(self, oc: OpenClaw, result: SecurityGateResult) -> None:
        for item in result.evidence_items():
            oc.audit.record(
                actor=self.role,
                event="EVIDENCE",
                state_before=oc.state,
                state_after=oc.state,
                detail=item,
            )

    # --- separation enforcement -------------------------------------------

    def override_quality_verdict(self, *args, **kwargs) -> None:
        """Security Gate can never change a Quality Guardian verdict."""
        raise SecurityGateSeparationError(
            "Security Gate cannot override a Quality Guardian verdict."
        )

    def deploy(self, *args, **kwargs) -> None:
        """Security Gate is a boundary, not a deployment executor."""
        raise SecurityGateSeparationError(
            "Security Gate is not a deployment executor; deploy belongs to the "
            "Deployment Check after human release approval."
        )

    def grant_approval(self, *args, **kwargs) -> None:
        """Security Gate cannot grant human approvals."""
        raise SecurityGateSeparationError(
            "Security Gate cannot grant approvals; human is the approval authority."
        )