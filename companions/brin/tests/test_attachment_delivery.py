import importlib.util
import json
from pathlib import Path
import sys

import pytest

from core import attachment_delivery


PNG_ID = "9011deb3-c7f5-403a-b980-d16f4d9c4b7a"
PDF_ID = "7a720727-6a21-43a7-b0ea-b9485fc1dd9b"
TXT_ID = "28448709-8185-4fde-bbce-f75014476fd5"
AUDIO_ID = "c9cadc39-714b-44fd-814d-1729dde6558d"


def marker(name, ctype, size, aid):
    return f"[attachment: {name}, {ctype}, {size} bytes, id {aid}]"


@pytest.mark.asyncio
async def test_supported_handles_become_exact_retrieval_and_read_instructions():
    out = await attachment_delivery.content_for_agent(
        "Please compare these.\n"
        + marker("shot.png", "image/png", 20, PNG_ID) + "\n"
        + marker("reading.pdf", "application/pdf", 30, PDF_ID) + "\n"
        + marker("prices.csv", "text/csv", 40, TXT_ID)
    )
    assert "Please compare these." in out
    assert "[attachment:" not in out
    assert out.count(attachment_delivery.ATTACHMENT_READ) == 3
    assert PNG_ID in out and PDF_ID in out and TXT_ID in out
    assert "built-in Read tool on the absolute path" in out
    assert "access and approved status" in out


@pytest.mark.asyncio
async def test_audio_and_video_get_an_explicit_limitation_not_a_command():
    out = await attachment_delivery.content_for_agent(
        marker("meeting.mp3", "audio/mpeg", 80, AUDIO_ID)
    )
    assert "meeting.mp3" in out
    assert "cannot inspect audio or video" in out
    assert "do not invent or infer" in out
    assert attachment_delivery.ATTACHMENT_READ not in out


def test_only_trailing_composer_markers_are_consumed():
    pasted = "--- pasted text ---\n" + marker("inside.png", "image/png", 1, PNG_ID)
    text, found = attachment_delivery.split_trailing_attachments(pasted + "\nmy words")
    assert text == pasted + "\nmy words"
    assert found == []


def load_reader():
    path = Path(__file__).parents[2] / "srv-skeleton/tools/attachments/attachment-read.py"
    spec = importlib.util.spec_from_file_location("attachment_reader", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_retire():
    path = Path(__file__).parents[1] / "retire_attachment_stopgaps.py"
    spec = importlib.util.spec_from_file_location("retire_attachment_stopgaps", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reader_materializes_api_bytes_with_a_content_type_suffix(tmp_path, monkeypatch, capsys):
    reader = load_reader()
    calls = []

    def request(path):
        calls.append(path)
        if path.endswith("/content"):
            return 200, {"content-type": "image/png"}, b"\x89PNG\r\n\x1a\nbytes"
        return 200, {"content-type": "application/json"}, json.dumps({
            "id": PNG_ID, "filename": "screenshot", "content_type": "image/png",
            "status": "approved",
        }).encode()

    monkeypatch.setattr(reader, "request", request)
    monkeypatch.setattr(reader, "cache_root", lambda: tmp_path)
    monkeypatch.setattr(reader.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(sys, "argv", ["attachment-read.py", PNG_ID])
    reader.main()
    path = Path(capsys.readouterr().out.strip())
    assert path.name == "screenshot.png"
    assert path.read_bytes().startswith(b"\x89PNG")
    assert path.stat().st_mode & 0o777 == 0o600
    assert calls == [f"/api/attachments/{PNG_ID}", f"/api/attachments/{PNG_ID}/content"]


def test_reader_refuses_audio_before_fetching_content(monkeypatch, capsys):
    reader = load_reader()
    calls = []

    def request(path):
        calls.append(path)
        return 200, {}, json.dumps({
            "id": AUDIO_ID, "filename": "meeting.mp3", "content_type": "audio/mpeg",
            "status": "approved",
        }).encode()

    monkeypatch.setattr(reader, "request", request)
    monkeypatch.setattr(reader.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(sys, "argv", ["attachment-read.py", AUDIO_ID])
    with pytest.raises(SystemExit):
        reader.main()
    assert calls == [f"/api/attachments/{AUDIO_ID}"]
    assert "audio and video cannot be inspected" in capsys.readouterr().err


def test_retire_removes_all_four_acl_shapes_without_unlinking_files(tmp_path, monkeypatch):
    retire = load_retire()
    data = tmp_path / "data"
    store = data / "attachments"
    desks = store / "desks"
    desk = desks / "desk-one"
    desk.mkdir(parents=True)
    attachment = desk / "attachment.png"
    attachment.write_bytes(b"same inode stays")

    monkeypatch.setattr(retire, "DATA_ROOT", data)
    monkeypatch.setattr(retire, "STORE", store)
    monkeypatch.setattr(
        retire,
        "_acl",
        lambda path: (["group:smart-node-agents:r-x"]
                      + (["default:group:smart-node-agents:r-x"]
                         if path.is_dir() and (path == desks or desks in path.parents)
                         else [])),
    )
    removed = []
    monkeypatch.setattr(retire, "_remove_acl", lambda path, spec: removed.append((path, spec)))

    retire.retire_acl()

    assert (data, "g:smart-node-agents") in removed
    assert (store, "g:smart-node-agents") in removed
    assert (attachment, "g:smart-node-agents") in removed
    assert (desks, "d:g:smart-node-agents") in removed
    assert (desk, "d:g:smart-node-agents") in removed
    assert attachment.read_bytes() == b"same inode stays"
