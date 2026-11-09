"""The Models switch counts only the units this node has.

Voice is a module. On a node without it smart-node-voice.service is not found,
and the switch read "partial", shown as off, while the gateway ran and agents
answered.
"""

from routes import system


def _states(table):
    def fake(unit):
        active, installed = table[unit]
        return {"unit": unit, "active": active, "sub": "", "since": None, "enabled": "", "installed": installed}
    return fake


def test_a_unit_that_is_not_installed_does_not_make_the_switch_partial(monkeypatch):
    monkeypatch.setattr(system, "_unit_state", _states({
        "smart-node-gateway.service": ("active", True),
        "smart-node-runtime.service": ("active", True),
        "smart-node-voice.service": ("inactive", False),
    }))
    s = system._models_state()
    assert s["active"] == "active"
    assert [u["unit"] for u in s["units"]] == ["smart-node-gateway.service", "smart-node-runtime.service"]


def test_an_installed_unit_that_is_down_still_makes_it_partial(monkeypatch):
    monkeypatch.setattr(system, "_unit_state", _states({
        "smart-node-gateway.service": ("active", True),
        "smart-node-runtime.service": ("active", True),
        "smart-node-voice.service": ("inactive", True),
    }))
    assert system._models_state()["active"] == "partial"
