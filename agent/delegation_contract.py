"""Runtime-owned contracts for delegated Hermes children.

This module is deliberately independent of profile prompts.  A parent creates
one immutable envelope before a child is built; the envelope is bound to the
child through a ContextVar and is consulted by the common tool dispatcher.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator


_CURRENT: ContextVar[dict[str, Any] | None] = ContextVar(
    "hermes_delegation_contract", default=None
)
_WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "patch", "apply_patch", "file_write",
    "execute_code", "code_execution", "todo_list", "memory", "kanban_create",
    "kanban_edit", "kanban_comment", "kanban_complete", "kanban_block",
})
_MUTATING_COMMAND = re.compile(
    r"(?:git\s+(?:clone|commit|push|reset|checkout|clean|rebase|merge)|"
    r"(?:rm|mv|cp|mkdir|touch|tee|install|chmod|chown|sed\s+-i|perl\s+-i|"
    r"pip\s+install|npm\s+(?:install|ci)|pnpm\s+(?:install|add)|yarn\s+add)|"
    r"(?:systemctl|launchctl|kill|pkill|shutdown|reboot)\b)", re.I
)
_ABS_PATH = re.compile(r"(?<![A-Za-z0-9_.-])/(?:home|Users|private|tmp|var|opt|workspace)[^\s'\"`;,)]*")
_REPO_ISSUE = re.compile(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)")
_SECRET = re.compile(r"(?i)(api[_-]?key|token|password|secret|authorization)\s*[:=]\s*[^\s,}]+")


def _text(value: Any, limit: int = 6000) -> str:
    return str(value or "").strip()[:limit]


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET.sub(lambda m: f"{m.group(1)}=<redacted>", value)
    return value


def _path_candidates(parent: Any) -> list[str]:
    paths: list[str] = []
    values = [
        os.getenv("TERMINAL_CWD"),
        getattr(parent, "terminal_cwd", None),
        getattr(parent, "cwd", None),
    ]
    try:
        from tools.terminal_tool import get_session_cwd
        task_id = getattr(parent, "_current_task_id", None)
        if task_id:
            values.insert(0, get_session_cwd(task_id))
    except Exception:
        pass
    for value in values:
        if value:
            try:
                path = str(Path(value).expanduser().resolve())
            except Exception:
                continue
            if path not in paths and Path(path).is_dir():
                paths.append(path)
    return paths[:4]


def _paths_from_text(text: str) -> list[str]:
    found: list[str] = []
    for raw in _ABS_PATH.findall(text or ""):
        try:
            path = str(Path(raw).expanduser().resolve())
        except Exception:
            continue
        candidate = path if Path(path).is_dir() else str(Path(path).parent)
        if candidate not in found and Path(candidate).is_dir():
            found.append(candidate)
    return found


def build_child_envelope(
    parent: Any,
    *,
    goal: str,
    context: str = "",
    child_task_id: str,
    output_schema: dict[str, Any],
    mutation_mode: str | None = None,
) -> tuple[dict[str, Any], str, str | None]:
    """Build and persist a canonical, redacted envelope and return its hash."""
    source = "\n".join((_text(goal), _text(context)))
    match = _REPO_ISSUE.search(source)
    repo = match.group(1) if match else ""
    issue = int(match.group(2)) if match else None
    parent_id = _text(getattr(parent, "session_id", ""), 256)
    paths = _path_candidates(parent)
    for candidate in _paths_from_text(source):
        if candidate not in paths:
            paths.append(candidate)
    paths = paths[:8]
    mode = mutation_mode or ("read-only" if re.search(r"\b(read[- ]only|no[- ]file[- ]change|no mutation)\b", source, re.I) else "workspace-write")
    envelope: dict[str, Any] = {
        "schema_version": 1,
        "parent_task_id": parent_id,
        "child_task_id": child_task_id,
        "task_identity": {"work_repo": repo, "issue_number": issue},
        "objective": _text(goal),
        "scope": {"in_scope": _text(goal), "out_of_scope": "Do not broaden scope; do not clone unrelated repositories; do not reinterpret empty or garbled input as a task delta."},
        "allowed_paths": paths,
        "mutation_mode": mode,
        "criteria": {"active": _text(context), "expected_artifacts": [], "known_blockers": []},
        "return_schema": output_schema,
        "environment": {"host": os.uname().nodename, "user": os.getenv("USER", "hermes"), "home": os.getenv("HOME", ""), "path_present": bool(os.getenv("PATH")), "credential_locations": ["profile-scoped provider/GitHub configuration"]},
        "model": {"name": _text(getattr(parent, "model", ""), 256), "provider": _text(getattr(parent, "provider", ""), 256)},
        "budgets": {"max_iterations": (getattr(parent, "max_iterations", None) if isinstance(getattr(parent, "max_iterations", None), (int, float)) else None), "timeout_seconds": None, "tool_calls": None},
        "communication": {"return_channel": "parent delegate_task result", "parent_continues_on_failure": True},
        "created_at": int(time.time()),
    }
    redacted = _redact(envelope)
    canonical = json.dumps(redacted, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    redacted["envelope_sha256"] = digest
    evidence_root = os.getenv("HERMES_HOME") or str(Path.home() / ".hermes")
    evidence_dir = os.getenv("HERMES_DELEGATION_EVIDENCE_DIR") or str(Path(evidence_root) / "delegation" / "envelopes")
    evidence_path: str | None = None
    try:
        Path(evidence_dir).mkdir(parents=True, exist_ok=True)
        path = Path(evidence_dir) / f"{child_task_id}.json"
        path.write_text(json.dumps(redacted, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        evidence_path = str(path)
    except OSError:
        evidence_path = None
    return redacted, digest, evidence_path


@contextmanager
def bind(envelope: dict[str, Any]) -> Iterator[None]:
    token = _CURRENT.set(envelope)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current() -> dict[str, Any] | None:
    return _CURRENT.get()


def guard_tool(tool_name: str, args: dict[str, Any]) -> str | None:
    """Return a deterministic block reason, or None when the call is allowed."""
    contract = current()
    if not contract:
        return None
    if tool_name in {"delegate_task", "session_search", "read_terminal", "skill_view"}:
        return None
    if contract.get("mutation_mode") == "read-only" and tool_name in _WRITE_TOOLS:
        return f"delegation scope blocked {tool_name}: child is read-only"
    serialized = json.dumps(args, ensure_ascii=False, default=str)
    if contract.get("mutation_mode") == "read-only" and tool_name in {"terminal", "shell", "run_command"}:
        if _MUTATING_COMMAND.search(serialized):
            return "delegation scope blocked terminal mutation in read-only child"
    allowed = [str(p) for p in contract.get("allowed_paths", []) if p]
    if allowed:
        for raw in _ABS_PATH.findall(serialized):
            try:
                path = str(Path(raw).expanduser().resolve())
            except Exception:
                continue
            if not any(path == root or path.startswith(root.rstrip(os.sep) + os.sep) for root in allowed):
                if not path.startswith("/home/hermes/.hermes"):
                    return f"delegation scope blocked path outside allowlist: {path}"
    return None


def record_violation(reason: str) -> None:
    contract = _CURRENT.get()
    if contract is not None:
        contract.setdefault("scope_violations", []).append(_text(reason, 1000))
