from operations import REGISTRY


def test_run_command_always_dangerous():
    assert REGISTRY["run_command"].is_dangerous({}) is True


def test_node_status_never_dangerous():
    assert REGISTRY["node_status"].is_dangerous({}) is False


def test_service_control_danger_depends_on_action():
    sc = REGISTRY["service_control"]
    assert sc.is_dangerous({"action": "status"}) is False
    assert sc.is_dangerous({"action": "restart"}) is True
    assert sc.is_dangerous({"action": "stop"}) is True
