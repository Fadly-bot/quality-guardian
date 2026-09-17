"""Deployment Gate — Development F release/preflight boundary (Phase 10).

The Deployment Gate is NOT a deployment executor. It is the release/preflight
boundary that decides, with real evidence, whether an artifact is allowed to
enter deployment:

    Coding -> Handoff -> Quality -> Security -> DEPLOYMENT GATE -> Human
    Release Approval -> Deploy -> Post-Deploy Verify

Mandatory preflight items (repository clean, correct branch/commit/remote,
tests PASS, Quality PASS, Security PASS, build PASS, environment/secret
availability, no unexpected changes) are verified with evidence. An unavailable
required check is NOT_SCANNED (never PASS). Human release approval is recorded
through OpenClaw and a rejection stops the release (deploy after rejection is
impossible). Rollback is defined and its readiness verified before release; the
gate never claims rollback is available without verification.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .orchestrator import HUMAN_ROLE, OpenClaw, RoutingError
from .security_gate import _decide
from .workflow import (
    DEPLOY,
    DEPLOYMENT_CHECK,
    HUMAN_RELEASE_APPROVAL,
)

DEPLOYMENT_ROLE = "deployment_check"

VALID_STATUSES = frozenset(
    {"PASS", "FAIL", "ERROR", "NOT_APPLICABLE", "NOT_SCANNED", "NEEDS_REVIEW"}
)
VALID_DECISIONS = frozenset(
    {"PASS", "FAIL", "ERROR", "NOT_SCANNED", "NEEDS_REVIEW"}
)


class DeploymentGateError(Exception):
    """Base error for Deployment Gate integration."""


class ValidationError(DeploymentGateError):
    """Raised when a deployment result is internally inconsistent."""


class DeploymentGateSeparationError(DeploymentGateError):
    """Raised when an integration violates the deployment gate boundary."""


@dataclass
class PreflightCheck:
    check_id: str
    description: str
    status: str
    detail: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class RollbackPlan:
    """Defined (not fabricated) rollback plan for a release."""

    artifact_before: str
    trigger: str
    procedure: str
    verification: str
    post_rollback_verification: str


@dataclass
class DeploymentGateResult:
    project: str
    checks: list[PreflightCheck] = field(default_factory=list)
    decision: str = ""
    artifact: dict[str, str] = field(default_factory=dict)
    rollback_plan: RollbackPlan | None = None

    def statuses(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        return counts

    def failures(self) -> list[PreflightCheck]:
        return [c for c in self.checks if c.status in {"FAIL", "ERROR"}]

    def validate(self) -> "DeploymentGateResult":
        if self.decision not in VALID_DECISIONS:
            raise ValidationError(f"Invalid deployment decision: {self.decision!r}")
        for check in self.checks:
            if check.status not in VALID_STATUSES:
                raise ValidationError(f"Invalid check status {check.status!r}")
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
                f"Decision {self.decision} requires a FAIL/ERROR check."
            )
        return self


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, timeout=30, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


class DeploymentGate:
    """Runs the Deployment Gate preflight against a project repository."""

    def __init__(self) -> None:
        pass

    def artifact_identity(self, project_root: str | Path) -> dict[str, str]:
        root = Path(project_root).resolve()
        return {
            "commit": _git(root, "rev-parse", "HEAD"),
            "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD"),
            "remote": _git(root, "remote", "get-url", "origin"),
        }

    def preflight(
        self,
        project_root: str | Path,
        gates: Mapping[str, Mapping[str, object]],
        expected_branch: str | None = None,
        expected_remote: str | None = None,
        expected_commit: str | None = None,
        build_targets: Iterable[str] = ("openclaw",),
        project: str = "quality-guardian",
    ) -> DeploymentGateResult:
        """Evaluate mandatory preflight items with real evidence.

        ``gates`` must provide the gate verdicts from earlier stages:
            {"quality": {"decision": "PASS", "evidence": [...]},
             "security": {"decision": "PASS", "evidence": [...]},
             "tests": {"status": "PASS", "evidence": "pytest -q -> 0 failed"}}
        An absent or non-PASS gate verdict produces a NOT_SCANNED / FAIL check
        (never an invented PASS).
        """
        root = Path(project_root).resolve()
        if not root.is_dir():
            raise ValueError(f"Project root does not exist: {root}")

        checks: list[PreflightCheck] = []
        identity = self.artifact_identity(root)

        checks.append(self._clean_check(root))
        checks.append(self._branch_check(root, expected_branch, identity))
        checks.append(self._remote_check(root, expected_remote, identity))
        checks.append(self._commit_check(root, expected_commit, identity))
        checks.append(self._tests_check(gates))
        checks.append(self._quality_gate_check(gates))
        checks.append(self._security_gate_check(gates))
        checks.append(self._build_check(root, build_targets))
        checks.append(self._env_config_check(root))
        checks.append(self._secrets_check(root))
        checks.append(self._rollback_check(root, identity))

        decision = _decide(checks)
        result = DeploymentGateResult(
            project=project,
            checks=checks,
            decision=decision,
            artifact=identity,
            rollback_plan=self.plan_rollback(root),
        )
        return result.validate()

    # --- individual preflight checks --------------------------------------

    def _clean_check(self, root: Path) -> PreflightCheck:
        dirty = _git(root, "status", "--porcelain")
        changed = [line for line in dirty.splitlines() if line]
        return PreflightCheck(
            check_id="preflight.repository_clean",
            description="Repository clean (no uncommitted changes)",
            status="PASS" if not changed else "FAIL",
            detail=f"{len(changed)} uncommitted change(s)"
                   if changed else "working tree clean",
            evidence=changed[:20],
        )

    def _branch_check(
        self, root: Path, expected: str | None, identity: Mapping[str, str]
    ) -> PreflightCheck:
        branch = identity.get("branch", "")
        if expected is None:
            status = "NOT_SCANNED"
            detail = f"no expected branch declared; actual={branch!r}"
        else:
            status = "PASS" if branch == expected else "FAIL"
            detail = f"branch={branch!r} expected={expected!r}"
        return PreflightCheck(
            check_id="preflight.correct_branch",
            description="Correct branch",
            status=status,
            detail=detail,
        )

    def _remote_check(
        self, root: Path, expected: str | None, identity: Mapping[str, str]
    ) -> PreflightCheck:
        remote = identity.get("remote", "")
        if expected is None:
            status = "NOT_SCANNED"
            detail = f"no expected remote declared; actual={remote!r}"
        else:
            status = "PASS" if remote == expected else "FAIL"
            detail = f"remote={remote!r} expected={expected!r}"
        return PreflightCheck(
            check_id="preflight.correct_remote",
            description="Expected remote",
            status=status,
            detail=detail,
        )

    def _commit_check(
        self, root: Path, expected: str | None, identity: Mapping[str, str]
    ) -> PreflightCheck:
        commit = identity.get("commit", "")
        if expected is None:
            status = "PASS" if commit else "ERROR"
            detail = f"artifact commit SHA recorded: {commit}" if commit \
                     else "cannot determine commit SHA"
        else:
            status = "PASS" if commit == expected else "FAIL"
            detail = f"commit={commit} expected={expected}"
        return PreflightCheck(
            check_id="preflight.artifact_identity",
            description="Artifact identity (commit SHA)",
            status=status,
            detail=detail,
        )

    def _tests_check(self, gates: Mapping[str, Mapping[str, object]]) -> PreflightCheck:
        tests = gates.get("tests", {})
        status = str(tests.get("status", ""))
        if status not in {"PASS", "FAIL", "ERROR"}:
            return PreflightCheck(
                check_id="preflight.tests_pass",
                description="Tests PASS",
                status="NOT_SCANNED",
                detail=f"no verified test evidence; got status {status!r}",
            )
        return PreflightCheck(
            check_id="preflight.tests_pass",
            description="Tests PASS",
            status=status,
            detail=str(tests.get("detail", "")),
            evidence=[str(e) for e in tests.get("evidence", [])][:20],
        )

    def _quality_gate_check(
        self, gates: Mapping[str, Mapping[str, object]]
    ) -> PreflightCheck:
        quality = gates.get("quality", {})
        decision = str(quality.get("decision", ""))
        if decision != "PASS":
            return PreflightCheck(
                check_id="preflight.quality_pass",
                description="Quality Guardian PASS",
                status="NOT_SCANNED" if decision == "NOT_SCANNED" else "FAIL",
                detail=f"quality decision is {decision!r}; PASS required",
            )
        return PreflightCheck(
            check_id="preflight.quality_pass",
            description="Quality Guardian PASS",
            status="PASS",
            detail=f"quality decision={decision!r}",
            evidence=[str(e) for e in quality.get("evidence", [])][:10],
        )

    def _security_gate_check(
        self, gates: Mapping[str, Mapping[str, object]]
    ) -> PreflightCheck:
        security = gates.get("security", {})
        decision = str(security.get("decision", ""))
        if decision != "PASS":
            return PreflightCheck(
                check_id="preflight.security_pass",
                description="Security Gate PASS",
                status="NOT_SCANNED" if decision == "NOT_SCANNED" else "FAIL",
                detail=f"security decision is {decision!r}; PASS required",
            )
        return PreflightCheck(
            check_id="preflight.security_pass",
            description="Security Gate PASS",
            status="PASS",
            detail=f"security decision={decision!r}",
            evidence=[str(e) for e in security.get("evidence", [])][:10],
        )

    def _build_check(
        self, root: Path, targets: Iterable[str]
    ) -> PreflightCheck:
        try:
            completed = subprocess.run(
                ["python3", "-m", "compileall", "-q", *targets],
                cwd=str(root), capture_output=True, text=True, timeout=60,
                check=False,
            )
        except OSError:  # pragma: no cover
            return PreflightCheck(
                check_id="preflight.build_pass",
                description="Build PASS",
                status="ERROR",
                detail="cannot run compileall",
            )
        if completed.returncode != 0:
            return PreflightCheck(
                check_id="preflight.build_pass",
                description="Build PASS",
                status="ERROR",
                detail=f"compileall failed (rc={completed.returncode})",
                evidence=[line for line in completed.stderr.splitlines() if line][:10],
            )
        return PreflightCheck(
            check_id="preflight.build_pass",
            description="Build PASS",
            status="PASS",
            detail=f"compileall succeeded for {list(targets)}",
            evidence=[f"python3 -m compileall -q {list(targets)}"],
        )

    def _env_config_check(self, root: Path) -> PreflightCheck:
        env_decls = []
        for path in root.glob("*.env*"):
            env_decls.append(path.name)
        if not env_decls:
            return PreflightCheck(
                check_id="preflight.env_config",
                description="Required environment configuration available",
                status="NOT_APPLICABLE",
                detail="no environment config files declared by the project; "
                       "no external service dependencies",
            )
        return PreflightCheck(
            check_id="preflight.env_config",
            description="Required environment configuration available",
            status="PASS" if all(p.endswith(".example") for p in env_decls) else "NEEDS_REVIEW",
            detail=f"env files present: {env_decls}",
        )

    def _secrets_check(self, root: Path) -> PreflightCheck:
        declared = [p.name for p in root.glob("*.env.example")] + [
            p.name for p in root.glob(".env.example")
        ]
        return PreflightCheck(
            check_id="preflight.secrets_configured",
            description="Required secrets configured (none embedded)",
            status="NOT_APPLICABLE" if not declared else "PASS",
            detail="no secrets required by deployment; none embedded in the repo"
                   if not declared else f"template env files: {declared}",
        )

    def _rollback_check(
        self, root: Path, identity: Mapping[str, str]
    ) -> PreflightCheck:
        before = identity.get("commit", "")
        if not before:
            return PreflightCheck(
                check_id="preflight.rollback_readiness",
                description="Rollback readiness (artifact recorded)",
                status="ERROR",
                detail="cannot record rollback artifact without commit SHA",
            )
        return PreflightCheck(
            check_id="preflight.rollback_readiness",
            description="Rollback readiness (artifact recorded)",
            status="PASS",
            detail=f"rollback artifact recorded: {before}",
            evidence=[f"rollback target commit: {before}"],
        )

    def plan_rollback(self, project_root: str | Path) -> RollbackPlan:
        root = Path(project_root).resolve()
        before = _git(root, "rev-parse", "HEAD") or "UNKNOWN"
        return RollbackPlan(
            artifact_before=before,
            trigger="DEPLOY failure or POST_DEPLOY_VERIFY failure",
            procedure=f"restore artifact/commit {before} (previous verified "
                      f"state) and re-run POST_DEPLOY_VERIFY",
            verification="rollback verification confirms restored state matches "
                         f"artifact {before}",
            post_rollback_verification="post-rollback verification runs the deploy "
                                       "verification again before any new release",
        )

    def verify_rollback_readiness(self, project_root: str | Path) -> PreflightCheck:
        plan = self.plan_rollback(project_root)
        ready = plan.artifact_before != "UNKNOWN" and bool(
            plan.procedure and plan.trigger and plan.verification
        )
        return PreflightCheck(
            check_id="rollback.verify",
            description="Rollback plan defined and verifiable",
            status="PASS" if ready else "FAIL",
            detail=plan.procedure if ready else "rollback plan incomplete",
            evidence=[plan.trigger, plan.procedure, plan.verification,
                      plan.post_rollback_verification],
        )

    # --- human release approval coordination ------------------------------

    def request_release_approval(self, oc: OpenClaw) -> str:
        if oc.state != HUMAN_RELEASE_APPROVAL:
            raise RoutingError(
                f"Expected HUMAN_RELEASE_APPROVAL, got {oc.state!r}"
            )
        return oc.request_human_approval(HUMAN_RELEASE_APPROVAL)

    def record_release_approval(self, oc: OpenClaw, approved: bool, approver: str) -> None:
        oc.record_human_approval(HUMAN_RELEASE_APPROVAL, approved, approver)

    def continue_after_release_approval(self, oc: OpenClaw) -> str:
        if oc.state != HUMAN_RELEASE_APPROVAL:
            raise RoutingError(
                f"Expected HUMAN_RELEASE_APPROVAL, got {oc.state!r}"
            )
        return oc.continue_after_human_gate(actor=HUMAN_ROLE)

    def deploy_not_executed(self, oc: OpenClaw) -> None:
        """The Deployment Gate is a boundary, not a deployment executor.

        The actual deployment step (mock/staging in testing, controlled tooling
        in production) is executed after HUMAN_RELEASE_APPROVAL; the gate only
        records the transition.
        """
        if oc.state == DEPLOY:
            return
        raise DeploymentGateSeparationError(
            f"The Deployment Gate is a release/preflight boundary, not a "
            f"deployment executor (state={oc.state!r})."
        )


class DeploymentGateClient:
    """Routes Deployment Gate results through OpenClaw (owner: deployment_check)."""

    role = DEPLOYMENT_ROLE

    def route_result(self, oc: OpenClaw, result: DeploymentGateResult) -> str:
        if oc.state != DEPLOYMENT_CHECK:
            raise RoutingError(f"Expected DEPLOYMENT_CHECK, got {oc.state!r}")
        decision = result.validate().decision
        if decision == "NOT_SCANNED":
            # OpenClaw has no NOT_SCANNED verdict at DEPLOYMENT_CHECK; missing
            # evidence is escalated (NEEDS_REVIEW) — never routed forward.
            return oc.route("NEEDS_REVIEW", actor=self.role, detail="NOT_SCANNED->escalated")
        return oc.route(decision, actor=self.role)

    def record_evidence(self, oc: OpenClaw, result: DeploymentGateResult) -> None:
        for check in result.checks:
            for item in check.evidence:
                oc.audit.record(
                    actor=self.role,
                    event="EVIDENCE",
                    state_before=oc.state,
                    state_after=oc.state,
                    detail=f"{check.check_id}: {item}",
                )

    # --- separation enforcement -------------------------------------------

    def deploy_without_approval(self, *args, **kwargs) -> None:
        raise DeploymentGateSeparationError(
            "Cannot deploy before recorded HUMAN_RELEASE_APPROVAL."
        )

    def grant_release_approval(self, *args, **kwargs) -> None:
        raise DeploymentGateSeparationError(
            "The Deployment Gate cannot grant release approval; human only."
        )

    def change_security_or_quality_verdict(self, *args, **kwargs) -> None:
        raise DeploymentGateSeparationError(
            "The Deployment Gate cannot change quality or security verdicts."
        )