import json
from pathlib import Path

from agent.delegation_contract import bind, build_child_envelope, guard_tool


class Parent:
    session_id = "parent-1"
    model = "azure/gpt-oss-120b"
    provider = "azure"
    terminal_cwd = "/tmp"


def test_envelope_is_redacted_and_hashed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DELEGATION_EVIDENCE_DIR", str(tmp_path))
    envelope, digest, path = build_child_envelope(
        Parent(),
        goal="Inspect abti-lab/hermes-deployment#2 read-only",
        context="token=super-secret",
        child_task_id="sa-test",
        output_schema={"type": "object"},
    )
    assert envelope["task_identity"] == {"work_repo": "abti-lab/hermes-deployment", "issue_number": 2}
    assert digest == envelope["envelope_sha256"]
    assert path and Path(path).exists()
    assert "super-secret" not in json.dumps(envelope)


def test_read_only_scope_blocks_mutation_and_allows_reads():
    envelope = {"mutation_mode": "read-only", "allowed_paths": ["/tmp"]}
    with bind(envelope):
        assert guard_tool("terminal", {"command": "git clone https://x/y"})
        assert guard_tool("write_file", {"path": "/tmp/a"})
        assert guard_tool("terminal", {"command": "git status --short"}) is None


def test_empty_steer_like_input_is_not_a_scope_delta():
    envelope = {"mutation_mode": "read-only", "allowed_paths": ["/tmp"]}
    with bind(envelope):
        assert guard_tool("terminal", {"command": ""}) is None
        assert envelope.get("scope_violations") is None
