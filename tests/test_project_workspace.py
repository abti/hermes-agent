from pathlib import Path

import pytest

from hermes_cli.project_workspace import (
    git_root,
    resolve_or_create_workspace,
    sanitize_workspace_name,
)


def test_sanitize_and_collision_safe_allocation(tmp_path):
    assert sanitize_workspace_name(" My Project / v1 ") == "My-Project-v1"
    first = resolve_or_create_workspace(
        project_name="My Project / v1", session_id="abc", projects_root=tmp_path
    )
    second = resolve_or_create_workspace(
        project_name="My Project / v1", session_id="def", projects_root=tmp_path
    )
    assert Path(first).name == "My-Project-v1"
    assert Path(second).name == "My-Project-v1-2"
    assert Path(first).parent == tmp_path


def test_unnamed_uses_session_id(tmp_path):
    path = resolve_or_create_workspace(
        project_name="", session_id="20260905_abcdef", projects_root=tmp_path
    )
    assert Path(path).name == "session-20260905abcd"


def test_linked_path_is_exact_and_missing_fails(tmp_path):
    linked = tmp_path / "repo"
    linked.mkdir()
    assert resolve_or_create_workspace(
        linked_directory=str(linked), project_name="ignored", projects_root=tmp_path
    ) == str(linked)
    with pytest.raises(FileNotFoundError):
        resolve_or_create_workspace(
            linked_directory=str(tmp_path / "missing"), projects_root=tmp_path
        )


def test_git_root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert Path(git_root(repo)) == repo
