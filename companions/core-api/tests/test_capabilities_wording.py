"""What the agent is told about what is missing: a thing to add, not a thing that
cannot be done. It told him Google was out of reach while the
node has a Drive integration waiting to be installed."""
import glob
import json
import os
from pathlib import Path

from lib import agent_fs

ROOT = Path(__file__).resolve().parents[3]


def test_a_missing_module_is_something_to_install_and_not_ready(monkeypatch, tmp_path):
    manifests, on = tmp_path / "m", tmp_path / "on"
    manifests.mkdir(); on.mkdir()
    (manifests / "mail.manifest.json").write_text(json.dumps({"name": "mail", "title": "Channels", "provides": "email"}))
    monkeypatch.setattr(agent_fs, "COMPANIONS_DIR", str(on))
    monkeypatch.setattr(agent_fs, "MANIFESTS_DIR", str(manifests), raising=False)
    text = agent_fs.node_facts()
    assert "Not installed here: Channels" in text
    assert "install" in text.split("Not installed here:")[1].lower()
    assert "not working yet" in text


def test_nothing_tells_the_agent_a_thing_cannot_be_done():
    assert "cannot do it" not in (Path(agent_fs.__file__)).read_text(encoding="utf-8")
    caps = (ROOT / "node-versioning" / "capabilities.md").read_text(encoding="utf-8")
    assert "do not propose" not in caps and "Not in the build" not in caps


def test_work_goes_in_the_reply_and_a_colleagues_state_is_not_guessed(monkeypatch, tmp_path):
    manifests = tmp_path / "m"
    manifests.mkdir()
    (manifests / "mail.manifest.json").write_text(json.dumps({"name": "mail", "title": "Channels"}))
    monkeypatch.setattr(agent_fs, "COMPANIONS_DIR", str(tmp_path))
    monkeypatch.setattr(agent_fs, "MANIFESTS_DIR", str(manifests))
    text = agent_fs.node_facts()
    assert "in your reply" in text and "where to create" in text
    assert "do not know" in text
    assert "what result you expect" in text
    assert "theirs to decide" in text
