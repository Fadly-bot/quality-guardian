"""Quality Guardian integration for Development F.

Development F integrates the EXISTING Quality Guardian component at
``/home/fadly_03/quality-guardian``. This module is a client that:

- invokes the Quality Guardian (with project/context),
- receives and validates the audit result,
- preserves the evidence model,
- records evidence,
- routes PASS forward / failure back to repair through OpenClaw.

It does NOT rebuild the Quality Guardian. It does NOT grant the Quality
Guardian any additional permission (no deploy, no approval, no modification).
The Quality Guardian remains an independent, read-only quality/audit agent and
remains distinct from the Security Gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .orchestrator import OpenClaw, RoutingError
from .workflow import (
    DEPLOYMENT_CHECK,
    QUALITY_AUDIT,
    SECURITY_AUDIT,
)

QUALITY_GUARDIAN_LOCATION = "/home/fadly_03/quality-guardian"
QUALITY_GUARDIAN_ROLE = "quality_guardian"

VALID_AUDIT_STATUSES = frozenset(
    {"PASS", "FAIL", "ERROR", "NOT_APPLICABLE", "NOT_SCANNED"}
)
VALID_RELEASE_DECISIONS = frozenset({"PASS", "BLOCK", "NEEDS_REVIEW"})


class QualityIntegrationError(Exception):
    """Base error for Quality Guardian integration."""


class InvocationError(QualityIntegrationError):
    """Raised when the Quality Guardian cannot be invoked."""


class ValidationError(QualityIntegrationError):
    """Raised when an audit result fails validation."""


class ReadOnlyError(QualityIntegrationError):
    """Raised on any attempt to use the Quality Guardian to modify the project."""


@dataclass
class AuditEvidence:
    """A single piece of objective audit evidence."""

    source: str
    target: str
    detail: str

    @property
    def text(self) -> str:
        return f"{self.source}: target={self.target} | {self.detail}"


@dataclass
class QualityAuditResult:
    """Validated audit result produced by the Quality Guardian."""

    project: str
    status: str
    decision: str
    scanner_statuses: dict[str, str] = field(default_factory=dict)
    evidence: list[AuditEvidence] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def verify_preserves_evidence_model(self) -> None:
        """True evidence model: CLEAN status requires coverage evidence.

        NOT_SCANNED is never silently upgraded to PASS and no scanner cleaned
        without proof of target coverage.
        """
        for scanner, status in self.scanner_statuses.items():
            if status == "PASS":
                covered = any(
                    ev.source == scanner and ev.target for ev in self.evidence
                )
                if not covered:
                    raise ValidationError(
                        f"Scanner {scanner!r} PASS without target-coverage "
                        f"evidence (CLEAN is not NOT_SCANNED)."
                    )


class QualityGuardianClient:
    """Development F client for the existing Quality Guardian component.

    ``invoker`` is the delegate that actually runs the Quality Guardian agent
    against ``project_root`` and returns its raw output. Without a configured
    invoker the client refuses to claim a successful invocation.
    """

    readonly = True

    def __init__(
        self,
        invoker: Callable[[Path, Mapping[str, object]], Mapping[str, object]]
        | None = None,
    ) -> None:
        self._invoker = invoker

    # --- invocation -------------------------------------------------------

    def invoke(
        self, project_root: str | Path, context: Mapping[str, object]
    ) -> QualityAuditResult:
        """Invoke the Quality Guardian with project/context (read-only)."""
        root = Path(project_root).resolve()
        if not root.is_dir():
            raise ValueError(f"Project root does not exist: {root}")
        if self._invoker is None:
            raise InvocationError(
                "No Quality Guardian invoker configured; refusing to invent an "
                "audit result."
            )
        raw = self._invoker(root, context)
        return self.validate_result(raw)

    # --- validation -------------------------------------------------------

    def validate_result(self, raw: Mapping[str, object]) -> QualityAuditResult:
        """Validate the Quality Guardian's output before it enters the flow."""
        status = raw.get("status")
        decision = raw.get("decision")
        if status not in VALID_AUDIT_STATUSES:
            raise ValidationError(f"Invalid audit status: {status!r}")
        if decision not in VALID_RELEASE_DECISIONS:
            raise ValidationError(f"Invalid release decision: {decision!r}")

        scanner_statuses = dict(raw.get("scanner_statuses", {}))
        for value in scanner_statuses.values():
            if value not in VALID_AUDIT_STATUSES:
                raise ValidationError(f"Invalid scanner status: {value!r}")

        evidence = [AuditEvidence(**item) for item in raw.get("evidence", [])]

        result = QualityAuditResult(
            project=str(raw.get("project", "")),
            status=status,
            decision=decision,
            scanner_statuses=scanner_statuses,
            evidence=evidence,
            findings=list(raw.get("findings", [])),
            risks=list(raw.get("risks", [])),
        )
        result.verify_preserves_evidence_model()
        return result

    # --- read-only enforcement -------------------------------------------

    def modify_project(self, project_root: str | Path, *args, **kwargs) -> None:
        """Quality Guardian is read-only. Any modification attempt is refused."""
        raise ReadOnlyError(
            "Quality Guardian is read-only: NO code edit, NO file modification, "
            "NO commit, NO push, NO deploy."
        )

    # --- evidence ---------------------------------------------------------

    def record_evidence(
        self, oc: OpenClaw, result: QualityAuditResult, actor: str
    ) -> None:
        """Record the audit evidence into the orchestrator's audit log."""
        for evidence in result.evidence:
            oc.audit.record(
                actor=actor,
                event="EVIDENCE",
                state_before=oc.state,
                state_after=oc.state,
                detail=evidence.text,
            )

    # --- routing ----------------------------------------------------------

    def route_result(self, oc: OpenClaw, result: QualityAuditResult) -> str:
        """Route the validated audit result through OpenClaw.

        PASS routes forward to the Security Gate. FAIL/ERROR route back to
        repair. NOT_SCANNED and NEEDS_REVIEW escalate (never forward as PASS).
        """
        if oc.state != QUALITY_AUDIT:
            raise RoutingError(f"Expected QUALITY_AUDIT, got {oc.state!r}")

        status = result.status
        decision = result.decision

        # Status takes precedence over decision: an unfinished scan (ERROR),
        # a scan that did not cover its target (NOT_SCANNED), and a confirmed
        # failure (FAIL) are never routed forward as PASS.
        if status == "NOT_SCANNED":
            verdict = "NOT_SCANNED"
        elif status == "ERROR":
            verdict = "ERROR"
        elif status == "FAIL" or (status == "PASS" and decision == "BLOCK"):
            verdict = "FAIL"
        elif status == "PASS" and decision == "NEEDS_REVIEW":
            verdict = "NEEDS_REVIEW"
        elif status == "PASS" and decision == "PASS":
            verdict = "PASS"
        else:
            raise ValidationError(
                f"Unroutable quality result: status={status!r} decision={decision!r}"
            )
        return oc.route(verdict, actor=QUALITY_GUARDIAN_ROLE)


class SecurityGateError(Exception):
    """Raised when an integration violates the Quality/Security boundary."""


def route_security_separation(
    oc: OpenClaw, result: QualityAuditResult
) -> str:
    """Guarantee Quality Guardian can never act as the Security Gate.

    The Quality Guardian's output routes the quality gate only. A security
    verdict is produced by the Security Gate (Phase 8 integration), never by
    the Quality Guardian.
    """
    if getattr(result, "decision", None) in {"DEPLOY", "RELEASE"}:
        raise SecurityGateError(
            "Quality Guardian cannot decide deployment or release; that belongs "
            "to the Security Gate, Deployment Check, and human approval."
        )
    return DEPLOYMENT_CHECK if oc.state == SECURITY_AUDIT else oc.state