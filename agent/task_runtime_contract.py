"""Runtime invariants for issue-bound parent operations.

This module is deliberately independent of the model prompt.  It is used by
the delegation and tool-dispatch paths to keep the control issue separate from
the workspace repository and to make a child handoff auditable.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

WAITING_CHILD = "WAITING_CHILD"
ALLOWED_WHILE_WAITING = frozenset({"delegate_task", "status", "wait", "stop", "read_child_result", "heartbeat"})
_HISTORICAL_MARKERS = ("r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "r12", "r13", "diagnostic", "historical")


@dataclass(frozen=True)
class TaskBinding:
    control_task_id: str
    workspace_project: str
    workspace_cwd: str
    active_operation_id: str


@dataclass
class OperationLedger:
    binding: TaskBinding
    max_children: int = 1
    state: str = "RUNNING_PARENT"
    children: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    child_count: int = 0
    violation_log: list[dict[str, Any]] = field(default_factory=list)

    def reject_identity(self, candidate: str, *, authenticated_delta: bool = False) -> None:
        if candidate != self.binding.control_task_id and not authenticated_delta:
            event = {"event": "task_identity_violation", "expected": self.binding.control_task_id, "candidate": candidate}
            self.violation_log.append(event)
            logger.error("task_identity_violation: expected=%s candidate=%s", self.binding.control_task_id, candidate)
            raise ValueError(f"control task identity is immutable: {candidate!r}")

    def begin_child(self, child_scope_key: str) -> str:
        if self.child_count >= self.max_children:
            raise RuntimeError(f"delegate_task rejected: max_children={self.max_children} reached for {self.binding.active_operation_id}")
        ownership = (self.binding.control_task_id, self.binding.active_operation_id, child_scope_key)
        if any(v.get("ownership_key") == ownership for v in self.children.values()):
            raise RuntimeError("delegate_task rejected: duplicate child ownership key")
        child_id = f"child-{uuid.uuid4().hex[:12]}"
        self.children[child_id] = {"ownership_key": ownership, "status": "running"}
        self.child_count += 1
        self.state = "WAITING_CHILD"
        return child_id

    def finish_child(self, child_id: str, result: Dict[str, Any]) -> None:
        child = self.children.get(child_id)
        if child is None:
            raise ValueError("unknown child ownership")
        child.update({"status": "completed", "result": result})
        self.state = "VALIDATING_CHILD_RESULT"

    def abort_child(self, child_id: str) -> None:
        child = self.children.get(child_id)
        if child is not None and child.get("status") == "running":
            child["status"] = "aborted"
            self.child_count = max(0, self.child_count - 1)
            self.state = "RUNNING_PARENT"

    def resume_parent(self, child_id: str) -> None:
        child = self.children.get(child_id)
        if child is None or child.get("status") != "completed":
            raise ValueError("cannot resume before child completion")
        self.state = "RUNNING_PARENT"


_LOCK = threading.RLock()


def ensure_binding(agent: Any) -> OperationLedger:
    """Return the agent's immutable operation ledger, creating it once."""
    with _LOCK:
        ledger = getattr(agent, "_task_runtime_ledger", None)
        if isinstance(ledger, OperationLedger):
            return ledger
        def _text(name: str) -> str:
            value = getattr(agent, name, "")
            return value.strip() if isinstance(value, str) else ""
        binding = TaskBinding(
            control_task_id=_text("_control_task_id"), workspace_project=_text("_workspace_project"),
            workspace_cwd=_text("_workspace_cwd"),
            active_operation_id=_text("_active_operation_id") or f"op-{uuid.uuid4().hex[:12]}",
        )
        if binding.control_task_id:
            ledger = OperationLedger(binding=binding, max_children=int(getattr(agent, "_task_runtime_max_children", 1)))
            setattr(agent, "_task_runtime_ledger", ledger)
            setattr(agent, "_active_operation_id", binding.active_operation_id)
            return ledger
        # Ordinary sessions retain existing delegation semantics; they still get
        # an operation id, but identity checks are opt-in for issue-bound runs.
        configured = getattr(agent, "_task_runtime_max_children", 1)
        configured = configured if isinstance(configured, int) and configured > 0 else 1
        ledger = OperationLedger(TaskBinding("", "", "", binding.active_operation_id), max_children=configured)
        setattr(agent, "_task_runtime_ledger", ledger)
        setattr(agent, "_active_operation_id", binding.active_operation_id)
        return ledger


def make_envelope(binding: TaskBinding, *, child_task_id: str, model: str, provider: str, allowed_paths: Iterable[str], mutation_mode: str, schema: str = "task-envelope.v1") -> dict[str, Any]:
    envelope = {"schema": schema, "control_task_id": binding.control_task_id, "active_operation_id": binding.active_operation_id, "child_task_id": child_task_id, "model": model, "provider": provider, "allowed_paths": sorted(str(p) for p in allowed_paths), "mutation_mode": mutation_mode}
    raw = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    envelope["envelope_sha256"] = hashlib.sha256(raw).hexdigest()
    return envelope


def provenance_join(envelope: dict[str, Any], invocation: dict[str, Any], result: dict[str, Any], receipt: dict[str, Any]) -> bool:
    keys = ("control_task_id", "active_operation_id", "child_task_id", "envelope_sha256", "schema", "model", "provider")
    return all(all(record.get(k) == envelope.get(k) for k in keys) for record in (invocation, result, receipt))


def filter_open_criteria(criteria: Iterable[str], *, spec_v2_active: bool) -> list[str]:
    if not spec_v2_active:
        return list(criteria)
    return [item for item in criteria if not any(marker in str(item).lower() for marker in _HISTORICAL_MARKERS)]


def is_empty_task_delta(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def tool_allowed_while_waiting(tool_name: str) -> bool:
    name = str(tool_name or "").lower()
    return name in ALLOWED_WHILE_WAITING or any(part in name for part in ("status", "wait", "heartbeat", "child_result"))
