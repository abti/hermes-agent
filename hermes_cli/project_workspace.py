"""Canonical Hermes project workspace resolution.

Explicit linked directories are authoritative and fail closed. Unlinked
sessions receive one collision-safe directory below the projects container.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

DEFAULT_PROJECTS_ROOT = Path("/home/hermes/projects")
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_workspace_name(value: str | None) -> str:
    raw = str(value or "").strip()
    name = _NAME_RE.sub("-", raw).strip("-.")
    name = re.sub(r"-{2,}", "-", name)[:80].strip("-.")
    return name or "session"


def _validate_linked_directory(path: Path) -> str:
    if not path.is_absolute():
        raise ValueError(f"linked project directory must be absolute: {path}")
    if not path.is_dir():
        raise FileNotFoundError(f"linked project directory does not exist: {path}")
    if not os.access(path, os.R_OK | os.X_OK):
        raise PermissionError(f"linked project directory is not accessible: {path}")
    return str(path)


def git_root(path: str | Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def resolve_or_create_workspace(
    *,
    linked_directory: str | None = None,
    workspace_path: str | None = None,
    project_name: str | None = None,
    session_id: str | None = None,
    projects_root: str | Path = DEFAULT_PROJECTS_ROOT,
) -> str:
    """Resolve a persisted workspace or allocate a new unlinked workspace."""
    explicit = linked_directory or workspace_path
    if explicit:
        return _validate_linked_directory(Path(explicit).expanduser())

    root = Path(projects_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o750)
    base = sanitize_workspace_name(project_name)
    if base == "session":
        short_id = re.sub(r"[^A-Za-z0-9]", "", str(session_id or ""))[:12]
        base = f"session-{short_id}" if short_id else "session"

    for number in range(1, 10000):
        suffix = "" if number == 1 else f"-{number}"
        candidate = root / f"{base}{suffix}"
        try:
            candidate.mkdir(mode=0o750)
        except FileExistsError:
            continue
        os.chmod(candidate, 0o750)
        return str(candidate)
    raise RuntimeError(f"unable to allocate a workspace under {root}")
