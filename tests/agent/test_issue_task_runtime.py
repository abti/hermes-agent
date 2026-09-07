from agent.issue_task_runtime import TaskIdentity, parse_task_identity

def test_parse_url():
    assert parse_task_identity('work https://github.com/acme/trading/issues/7 now') == TaskIdentity('acme', 'trading', 7)

def test_parse_reference():
    assert parse_task_identity('execute abti-lab/exp-simple-scalp#2') == TaskIdentity('abti-lab', 'exp-simple-scalp', 2)

def test_ignore_non_issue_text():
    assert parse_task_identity('github.com/acme/trading') is None
