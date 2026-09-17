"""Development F agent registry.

The registry is the official catalog of every Development F agent. Each entry
describes the agent (agent_id, name, department, role, capabilities, inputs,
outputs, permissions, forbidden_actions, contract_version, invocation_method,
status, availability) and, for existing components, points to the component
location. The registry never copies component source code.

Capability (what an agent can do) and permission (what an agent is allowed to
do) are deliberately separate and validated independently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

AVAILABLE = "AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"
DEGRADED = "DEGRADED"
DISABLED = "DISABLED"

REQUIRED_AGENTS = frozenset(
    {
        "ai_council",
        "project_council",
        "coding_agent",
        "handoff_agent",
        "quality_guardian",
        "security_gate",
        "deployment_check",
        "openclaw",
    }
)

REQUIRED_FIELDS = (
    "agent_id",
    "name",
    "department",
    "role",
    "capabilities",
    "inputs",
    "outputs",
    "permissions",
    "forbidden_actions",
    "contract_version",
    "invocation_method",
    "status",
    "availability",
)

_AVAILABILITY_VALUES = {AVAILABLE, UNAVAILABLE, DEGRADED, DISABLED}

# Existing component locations (registry describes and routes; never copies).
COMPONENT_LOCATIONS = {
    "handoff_agent": "/home/fadly_03/handoff-agent",
    "quality_guardian": "/home/fadly_03/quality-guardian",
}


class RegistryError(Exception):
    """Base registry error."""


class UnknownAgent(RegistryError):
    """Raised when an agent_id is not registered."""


class CapabilityError(RegistryError):
    """Raised when required capabilities are missing."""


class PermissionError_Registry(RegistryError):
    """Raised when a requested permission is not granted."""


class ContractVersionError(RegistryError):
    """Raised when the requested contract version does not match."""


class AvailabilityError(RegistryError):
    """Raised when an agent is not AVAILABLE."""


class InvocationError(RegistryError):
    """Raised when the invocation method does not match."""


@dataclass
class AgentRecord:
    agent_id: str
    name: str
    department: str
    role: str
    capabilities: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    forbidden_actions: list[str] = field(default_factory=list)
    contract_version: str = ""
    invocation_method: str = ""
    status: str = ""
    availability: str = AVAILABLE
    location: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "AgentRecord":
        return cls(
            agent_id=str(data["agent_id"]),
            name=str(data["name"]),
            department=str(data["department"]),
            role=str(data["role"]),
            capabilities=list(data.get("capabilities", [])),
            inputs=list(data.get("inputs", [])),
            outputs=list(data.get("outputs", [])),
            permissions=list(data.get("permissions", [])),
            forbidden_actions=list(data.get("forbidden_actions", [])),
            contract_version=str(data.get("contract_version", "")),
            invocation_method=str(data.get("invocation_method", "")),
            status=str(data.get("status", "")),
            availability=str(data.get("availability", AVAILABLE)),
            location=data.get("location"),
        )


class AgentRegistry:
    """Official Development F agent registry."""

    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            path = Path(__file__).parent / "agent_registry.json"
        self._records: dict[str, AgentRecord] = {}
        self._load(Path(path))

    @classmethod
    def from_records(cls, records: Mapping[str, Mapping[str, object]]) -> "AgentRegistry":
        reg = cls.__new__(cls)
        reg._records = {
            agent_id: AgentRecord.from_mapping(data)
            for agent_id, data in records.items()
        }
        return reg

    def _load(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover
            raise RegistryError(f"Cannot load registry from {path}: {exc}") from exc
        agents = data.get("agents", {})
        for agent_id, entry in agents.items():
            entry["agent_id"] = agent_id
            self._records[agent_id] = AgentRecord.from_mapping(entry)

    # --- lookup -----------------------------------------------------------

    def lookup(self, agent_id: str) -> AgentRecord:
        record = self._records.get(agent_id)
        if record is None:
            raise UnknownAgent(f"Unknown agent_id: {agent_id!r}")
        return record

    def has_agent(self, agent_id: str) -> bool:
        return agent_id in self._records

    def agents(self) -> list[AgentRecord]:
        return list(self._records.values())

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self._records

    # --- validation -------------------------------------------------------

    def validate_capabilities(self, agent_id: str, required: Iterable[str]) -> None:
        record = self.lookup(agent_id)
        missing = [caps for caps in required if caps not in record.capabilities]
        if missing:
            raise CapabilityError(
                f"{agent_id} missing capabilities: {sorted(missing)}"
            )

    def validate_permissions(self, agent_id: str, requested: Iterable[str]) -> None:
        record = self.lookup(agent_id)
        denied = [perm for perm in requested if perm not in record.permissions]
        if denied:
            raise PermissionError_Registry(
                f"{agent_id} lacks permissions: {sorted(denied)}"
            )

    def validate_contract_version(self, agent_id: str, version: str) -> None:
        record = self.lookup(agent_id)
        if record.contract_version != version:
            raise ContractVersionError(
                f"{agent_id} contract_version is {record.contract_version!r}, "
                f"requested {version!r}"
            )

    def validate_availability(self, agent_id: str) -> None:
        record = self.lookup(agent_id)
        if record.availability != AVAILABLE:
            raise AvailabilityError(
                f"{agent_id} is {record.availability}, not {AVAILABLE}"
            )

    def validate_invocation(self, agent_id: str, method: str) -> None:
        record = self.lookup(agent_id)
        if record.invocation_method != method:
            raise InvocationError(
                f"{agent_id} invocation is {record.invocation_method!r}, "
                f"requested {method!r}"
            )

    def availability_of(self, agent_id: str) -> str:
        return self.lookup(agent_id).availability

    def require_complete(self) -> list[str]:
        """Return every agent_id missing a required field."""
        problems = []
        for agent_id in sorted(REQUIRED_AGENTS):
            record = self.lookup(agent_id)
            for field_name in REQUIRED_FIELDS:
                if getattr(record, field_name) in (None, "", []):
                    problems.append(f"{agent_id}:{field_name}")
            if record.location:
                records_location = record.location
                if records_location != COMPONENT_LOCATIONS.get(agent_id):
                    problems.append(
                        f"{agent_id}: location mismatch {records_location!r}"
                    )
        return problems