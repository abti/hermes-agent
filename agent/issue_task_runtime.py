"""Deterministic GitHub issue-task bootstrap shared by Hermes profiles."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

_ISSUE_URL = re.compile(r"https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/issues/(?P<issue>[0-9]+)", re.I)
_ISSUE_REF = re.compile(r"(?<![A-Za-z0-9_.-])(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<issue>[0-9]+)(?![0-9])")
_ACTIVE_MARKER = re.compile(r"\[#(?P<issue>[0-9]+)\]\s*\[(?P<kind>SPEC|USER DELTA|ORCHESTRATOR REVIEW)[^\]]*\]", re.I)
_TERMINAL_RE = re.compile(r"\b(?:COMPLETE|HARD[ _-]?BLOCKED)\b", re.I)

@dataclass(frozen=True)
class TaskIdentity:
    owner: str
    repo: str
    issue: int

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def key(self) -> str:
        return f"{self.full_name}#{self.issue}"

def parse_task_identity(text: Any) -> Optional[TaskIdentity]:
    if not isinstance(text, str):
        return None
    match = _ISSUE_URL.search(text) or _ISSUE_REF.search(text)
    return None if not match else TaskIdentity(match.group("owner"), match.group("repo"), int(match.group("issue")))

def _run_json(args: list[str], *, cwd: Optional[str] = None) -> Any:
    result = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env={**os.environ, "GH_PAGER": "cat", "PAGER": "cat"})
    return json.loads(result.stdout)

def _run_text(args: list[str], *, cwd: Optional[str] = None) -> str:
    result = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, env={**os.environ, "GH_PAGER": "cat", "PAGER": "cat"})
    return result.stdout.strip()

def _repo_name_from_remote(remote: str) -> str:
    value = remote.strip().removesuffix(".git")
    value = re.sub(r"^git@github\.com:", "", value, flags=re.I)
    value = re.sub(r"^https?://github\.com/", "", value, flags=re.I)
    return value.strip("/").lower()

def _latest_active_contract(identity: TaskIdentity, issue: dict, comments: Iterable[dict]) -> dict:
    candidates: list[dict[str, Any]] = []
    for source, value in [("body", issue.get("body") or ""), *[("comment", c.get("body") or "") for c in comments]]:
        for marker in _ACTIVE_MARKER.finditer(value):
            if int(marker.group("issue")) == identity.issue:
                candidates.append({"source": source, "kind": marker.group("kind"), "marker": marker.group(0), "text": value})
    return candidates[-1] if candidates else {"source": "body", "kind": "implicit", "marker": "", "text": issue.get("body") or ""}

def _state_path(identity: TaskIdentity, root: Optional[str] = None) -> Path:
    base = Path(root or os.environ.get("HERMES_TASK_RUNTIME_DIR", "~/.hermes/task-runtime")).expanduser()
    return base / f"{identity.owner}__{identity.repo}__{identity.issue}.json"

def _write_state(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)

def bootstrap_issue_task(identity: TaskIdentity, *, project_cwd: Optional[str] = None, state_root: Optional[str] = None, start_watcher: bool = True) -> dict[str, Any]:
    """Retrieve authenticated body/comments, validate repo binding, persist state, and watch."""
    repo = _run_json(["gh", "repo", "view", identity.full_name, "--json", "nameWithOwner,isPrivate,defaultBranchRef,url"])
    issue = _run_json(["gh", "issue", "view", str(identity.issue), "--repo", identity.full_name, "--json", "number,title,state,body,comments"])
    comments = _run_json(["gh", "api", "--paginate", f"repos/{identity.full_name}/issues/{identity.issue}/comments"])
    comments = comments if isinstance(comments, list) else []
    if int(issue.get("number", -1)) != identity.issue:
        raise RuntimeError("authenticated issue response did not match requested issue")
    cwd = os.path.abspath(project_cwd or os.getcwd())
    try:
        remote = _run_text(["git", "remote", "get-url", "origin"], cwd=cwd)
    except (OSError, subprocess.CalledProcessError):
        remote = ""
    # The Mac client may start a session from the neutral projects root. Find
    # the already-created exact repo there, but never guess across remotes.
    if _repo_name_from_remote(remote) != identity.full_name.lower():
        root = Path("/home/hermes/projects")
        candidates = [root / identity.repo, root / f"{identity.owner}-{identity.repo}"]
        for candidate in candidates:
            if not candidate.is_dir():
                continue
            try:
                candidate_remote = _run_text(["git", "remote", "get-url", "origin"], cwd=str(candidate))
            except (OSError, subprocess.CalledProcessError):
                continue
            if _repo_name_from_remote(candidate_remote) == identity.full_name.lower():
                cwd, remote = str(candidate), candidate_remote
                break
    if _repo_name_from_remote(remote) != identity.full_name.lower():
        raise RuntimeError(f"task repo mismatch: expected {identity.full_name}, found {_repo_name_from_remote(remote) or '<none>'}")
    state_path = _state_path(identity, state_root)
    state: dict[str, Any] = {
        "schema_version": 1, "task_identity": identity.key, "repo": repo,
        "issue": {"number": identity.issue, "title": issue.get("title"), "state": issue.get("state")},
        "body": issue.get("body") or "", "comments": comments,
        "latest_active_contract": _latest_active_contract(identity, issue, comments),
        "comment_ids": [c.get("id") for c in comments if c.get("id") is not None],
        "project_cwd": cwd, "remote": remote,
        "git_status": _run_text(["git", "status", "--short", "--branch"], cwd=cwd),
        "bootstrapped_at": time.time(), "watcher": {"enabled": False, "pid": None},
    }
    # Publish the initial state before detaching the watcher so a fast child
    # cannot observe a missing state file and record a false startup error.
    _write_state(state_path, state)
    if start_watcher:
        log_path = state_path.with_suffix(".watch.log")
        with log_path.open("a", encoding="utf-8") as log_handle:
            child = subprocess.Popen([sys.executable, "-m", "agent.issue_task_runtime", "--watch", identity.full_name, str(identity.issue), "--state", str(state_path)], cwd=cwd, stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        state["watcher"] = {"enabled": True, "pid": child.pid, "state": str(state_path), "log": str(log_path), "interval_s": 600}
    _write_state(state_path, state)
    return state

def format_task_context(state: dict[str, Any]) -> str:
    contract = state.get("latest_active_contract") or {}
    body = str(state.get("body") or "")[:16000]
    comments = state.get("comments") or []
    comment_text = "\n\n".join(str(c.get("body") or "") for c in comments)[-16000:]
    return ("HERMES TASK RUNTIME (authoritative, injected before model execution)\n"
        f"TASK_IDENTITY: {state['task_identity']}\n"
        "Private GitHub BODY and all authenticated comments were retrieved by runtime; never use public web retrieval for this control plane.\n"
        f"LATEST_ACTIVE_CONTRACT: {contract.get('marker') or contract.get('kind')}\n"
        f"PROJECT_CWD: {state.get('project_cwd')}\nGIT_STATUS: {state.get('git_status')}\n"
        f"WATCHER: {json.dumps(state.get('watcher') or {}, sort_keys=True)}\n\nISSUE BODY:\n{body}\n\nISSUE COMMENTS:\n{comment_text}\n\n"
        "ACKs, milestones, and model stops are checkpoints. Continue to the highest-value unfinished acceptance criterion; use COMPLETE only after criterion-by-criterion evidence.")

def should_auto_continue(state: dict[str, Any], result: dict[str, Any]) -> bool:
    if not isinstance(result, dict) or result.get("failed") or result.get("interrupted"):
        return False
    if _TERMINAL_RE.search(str(result.get("final_response") or "")):
        return False
    return str(state.get("issue", {}).get("state", "")).upper() == "OPEN"

def build_handoff_context(state: dict[str, Any], *, profile: str = "", model: str = "", host: str = "", os_user: str = "hermes", phase: str = "") -> dict[str, Any]:
    """Generate a complete, redacted handoff package from persisted task state."""
    return {
        "task_identity": state.get("task_identity"),
        "active_contract": state.get("latest_active_contract"),
        "issue_body": state.get("body", ""),
        "issue_comments": state.get("comments", []),
        "acceptance_criteria": state.get("acceptance_criteria", []),
        "phase": phase,
        "completed_work": state.get("completed_work", []),
        "open_criteria": state.get("open_criteria", []),
        "project_cwd": state.get("project_cwd"),
        "remote": state.get("remote"),
        "git_status": state.get("git_status"),
        "environment": {"host": host, "os_user": os_user, "profile": profile, "model": model, "credential_locations_only": True},
        "reproduction_and_tests": state.get("reproduction_and_tests", []),
        "blockers": state.get("blockers", []),
        "reporting": {"control_plane": state.get("task_identity"), "return_to_parent": True},
    }

def _watch(identity: TaskIdentity, state_path: Path) -> None:
    while True:
        try:
            comments = _run_json(["gh", "api", "--paginate", f"repos/{identity.full_name}/issues/{identity.issue}/comments"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["comments"] = comments if isinstance(comments, list) else []
            state["comment_ids"] = [c.get("id") for c in state["comments"] if c.get("id") is not None]
            state["last_poll_at"] = time.time()
            _write_state(state_path, state)
        except Exception as exc:
            with state_path.with_suffix(".watch.log").open("a", encoding="utf-8") as log:
                log.write(f"watch poll failed: {type(exc).__name__}\n")
        time.sleep(600)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", nargs=2, metavar=("REPO", "ISSUE"))
    parser.add_argument("--state")
    args = parser.parse_args()
    if args.watch:
        owner, repo = args.watch[0].split("/", 1)
        _watch(TaskIdentity(owner, repo, int(args.watch[1])), Path(args.state).expanduser())

if __name__ == "__main__":
    main()
