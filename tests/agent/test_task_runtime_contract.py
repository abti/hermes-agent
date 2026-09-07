from agent.task_runtime_contract import (
    OperationLedger, TaskBinding, ensure_binding, filter_open_criteria,
    is_empty_task_delta, make_envelope, provenance_join, tool_allowed_while_waiting,
)
from agent.task_runtime_recovery import bounded_429_retry


class Parent:
    _control_task_id = "abti-lab/hermes-deployment#2"
    _workspace_project = "exp-simple-scalp"
    _workspace_cwd = "/home/hermes/projects/exp-simple-scalp"
    _active_operation_id = "op-validation-2"
    _task_runtime_max_children = 1


def test_workspace_repo_cannot_replace_control_task():
    ledger = ensure_binding(Parent())
    assert ledger.binding.control_task_id == "abti-lab/hermes-deployment#2"
    try:
        ledger.reject_identity("exp-simple-scalp#2")
    except ValueError:
        pass
    else:
        raise AssertionError("workspace issue replaced the immutable control identity")


def test_second_child_rejected_and_waiting_child_is_supervised():
    ledger = OperationLedger(TaskBinding("control#2", "workspace", "/workspace", "op-1"), max_children=1)
    child = ledger.begin_child("validation-child")
    assert ledger.state == "WAITING_CHILD"
    try:
        ledger.begin_child("second-child")
    except RuntimeError:
        pass
    else:
        raise AssertionError("second child was accepted")
    assert tool_allowed_while_waiting("delegate_task")
    assert not tool_allowed_while_waiting("search_files")
    ledger.finish_child(child, {"status": "completed"})
    ledger.resume_parent(child)
    assert ledger.binding.active_operation_id == "op-1"
    assert ledger.state == "RUNNING_PARENT"


def test_stale_context_and_empty_delta_are_safe():
    assert filter_open_criteria(["R1 historical diagnostics", "V2.3 delegation"], spec_v2_active=True) == ["V2.3 delegation"]
    assert is_empty_task_delta("") and is_empty_task_delta("   ") and is_empty_task_delta(None)


def test_provenance_tuple_joins_envelope_invocation_result_receipt():
    envelope = make_envelope(TaskBinding("control#2", "workspace", "/workspace", "op-1"), child_task_id="child-1", model="azure/gpt-oss-120b", provider="azure", allowed_paths=["/workspace"], mutation_mode="read-only")
    records = [{k: envelope[k] for k in ("control_task_id", "active_operation_id", "child_task_id", "envelope_sha256", "schema", "model", "provider")} for _ in range(3)]
    assert provenance_join(envelope, *records)
    records[1]["child_task_id"] = "child-2"
    assert not provenance_join(envelope, *records)


def test_429_budget_preserves_ownership_without_model_fallback():
    assert bounded_429_retry(attempt=0, retry_after="3", budget=2) == bounded_429_retry(attempt=0, retry_after=3, budget=2)
    assert bounded_429_retry(attempt=2, budget=2).status == "provider_transient_blocked"
