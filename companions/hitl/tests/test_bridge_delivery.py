"""Security and retry behavior for the native HITL adapter."""
import importlib.util
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The delivery worker reads its queue from HITL_QUEUE, defaulting to the node's
# real /var/lib/smart-node/hitl-queue. On a machine that has native HITL installed
# (the build host does, since it adopted the legacy service) that directory exists
# and the test user cannot stat inside it, so a test that never meant to touch it
# died with PermissionError. Point every test at a private queue before the modules
# are loaded.
os.environ["HITL_QUEUE"] = tempfile.mkdtemp(prefix="hitl-queue-test-")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load("hitl_bridge", "bridge.py")
delivery = load("hitl_delivery", "deliver.py")
bindings = load("hitl_bindings", "recipient_bindings.py")


class RecipientBindingsTest(unittest.TestCase):
    def test_only_operator_file_resolves_actor(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bindings.json"
            path.write_text('{"owner": "alex"}')
            path.chmod(0o640)
            self.assertEqual(bindings.resolve("owner", path), "alex")
            self.assertIsNone(bindings.resolve("other", path))
            path.chmod(0o666)
            with self.assertRaisesRegex(ValueError, "writable"):
                bindings.resolve("owner", path)

    def test_binding_requires_pinned_recipient_and_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "bindings.json"
            path.write_text("{}")
            path.chmod(0o640)
            state = root / "caller"
            (state / "pins").mkdir(parents=True)
            with patch.object(bindings.os, "geteuid", return_value=1000):
                with self.assertRaises(PermissionError):
                    bindings.bind("owner", "alex", path=path, state=state)
            with patch.object(bindings.os, "geteuid", return_value=0):
                with self.assertRaisesRegex(ValueError, "not pinned"):
                    bindings.bind("owner", "alex", path=path, state=state)
            (state / "pins/owner.json").write_text('{"recipient_id": "owner"}')
            with patch.object(bindings.os, "geteuid", return_value=0), patch.object(
                bindings.os, "chown"
            ), patch.object(bindings.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=123)):
                bindings.bind("owner", "alex", path=path, state=state)
            self.assertEqual(bindings.resolve("owner", path), "alex")

    def test_agent_supplied_actor_is_ignored_without_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bridge, "QUEUE", Path(temp) / "bridge"), patch.object(
                bridge, "recipient_actor", return_value=None
            ), patch.object(bridge, "_run", side_effect=AssertionError("signed request created")):
                result = bridge._ask("alice", {"recipient": "owner",
                                               "recipient_actor_id": "mallory",
                                               "title": "Go?", "text": "Deploy",
                                               "reference": "task:7"})
            self.assertEqual(result["status"], "error")
            self.assertIn("binding", result["reason"])


class PeerIdentityTest(unittest.TestCase):
    def setUp(self):
        binding = patch.object(bridge, "recipient_actor", return_value="owner")
        binding.start()
        self.addCleanup(binding.stop)

    def test_queue_record_is_group_readable_but_caller_state_is_private(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private = root / "caller" / "manifest.json"
            shared = root / "queue" / "requests" / "request.json"
            bridge._save(private, {"secret": "test"})
            bridge._save(shared, {"request_id": "test"}, shared=True)
            self.assertEqual(os.stat(private).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(shared).st_mode & 0o777, 0o640)

    def test_consumed_reply_is_recovered_after_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            rid = "d" * 36
            root = Path(temp)
            path = root / "bridge/requests" / f"{rid}.json"
            bridge._save(path, {"request_id": rid, "agent": "alice", "reference": "task:7"})

            def fake_run(command, _rid):
                if command == "check":
                    return {"status": "answered", "request_id": rid,
                            "caller_ref": "task:7", "consumed": True}
                return {"status": "rejected", "reason": "replay"}

            with patch.object(bridge, "QUEUE", root / "bridge"), patch.object(bridge, "_run", fake_run):
                bridge.settle(path)
                result = json.loads((root / "bridge/delivery" / f"{rid}.json").read_text())
            self.assertEqual(result["result"]["status"], "answered")

    def test_agent_is_kernel_uid_not_json_field(self):
        sock = SimpleNamespace(getsockopt=lambda *_: struct.pack("3i", 8, 1234, 20))
        with patch.object(bridge.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="agent-alice")):
            self.assertEqual(bridge._actor(sock), "alice")
        with patch.object(bridge.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="root")):
            self.assertIsNone(bridge._actor(sock))

    def test_request_owner_cannot_be_spoofed(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bridge, "QUEUE", Path(temp) / "bridge"):
                path = Path(temp) / "bridge/requests/" / ("a" * 36 + ".json")
                bridge._save(path, {"agent": "alice"})
                self.assertIsNotNone(bridge._owner("a" * 36, "alice"))
                self.assertIsNone(bridge._owner("a" * 36, "bob"))

    def test_pending_link_can_be_recovered_only_by_the_asking_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rid = "a" * 36
            path = root / "requests" / f"{rid}.json"
            bridge._save(path, {"request_id": rid, "agent": "alice",
                                "reference": "task:7", "recipient_actor_id": "owner",
                                "link": "https://hitl.example.com/r/demo"})
            with patch.object(bridge, "QUEUE", root), patch.object(
                bridge, "_run", return_value={"status": "pending"}
            ):
                self.assertEqual(bridge._check("bob", rid)["status"], "error")
                result = bridge._check("alice", rid)
            self.assertEqual(result["link"], "https://hitl.example.com/r/demo")
            self.assertEqual(result["recipient_actor_id"], "owner")

    def test_ask_uses_proved_actor_in_signed_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            captured = {}

            def fake_run(*args):
                captured.update(json.loads(Path(args[3]).read_text()))
                captured["cli_args"] = args
                return {"status": "created", "request_id": "a" * 36,
                        "link": "https://hitl.example.com/r/demo", "expires_at": "later"}

            with patch.object(bridge, "STATE", Path(temp)), patch.object(bridge, "QUEUE", Path(temp) / "bridge"), patch.object(bridge, "_run", fake_run):
                out = bridge.dispatch("alice", {"op": "ask_human", "agent": "bob",
                                                "recipient": "owner", "recipient_actor_id": "mallory",
                                                "title": "Go?",
                                                "text": "Deploy", "reference": "task:7"})
                self.assertEqual(out["status"], "created")
                self.assertEqual(captured["asked_by"], "alice")
                self.assertIn("fresh", captured["cli_args"])
                stored = bridge._owner("a" * 36, "alice")
                self.assertEqual(stored["agent"], "alice")
                self.assertEqual(stored["recipient_actor_id"], "owner")

    def test_existing_signed_request_is_returned_while_binding_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bridge._save(root / "requests" / ("a" * 36 + ".json"), {
                "request_id": "a" * 36, "agent": "alice", "reference": "task:7",
                "recipient": "owner", "recipient_actor_id": "original-owner",
                "link": "https://hitl.example.com/r/demo"})
            with patch.object(bridge, "STATE", root), patch.object(bridge, "QUEUE", root), \
                    patch.object(bridge, "recipient_actor", return_value="original-owner"), \
                    patch.object(bridge, "_run", side_effect=AssertionError("duplicate ask")):
                result = bridge._ask("alice", {"recipient": "owner", "title": "Go?",
                                               "text": "Deploy", "reference": "task:7"})
            self.assertEqual(result["request_id"], "a" * 36)
            self.assertEqual(result["recipient_actor_id"], "original-owner")

    def test_request_whose_binding_moved_is_replaced_by_a_fresh_ask(self):
        # The delivery worker refuses an actor that no longer passes its check, so
        # returning the old request again would hold the link for good.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bridge._save(root / "requests" / ("a" * 36 + ".json"), {
                "request_id": "a" * 36, "agent": "alice", "reference": "task:7",
                "recipient": "owner", "recipient_actor_id": "old-actor",
                "link": "https://hitl.example.com/r/old"})
            fresh = {"status": "created", "request_id": "b" * 36,
                     "link": "https://hitl.example.com/r/new", "expires_at": "later"}
            with patch.object(bridge, "STATE", root), patch.object(bridge, "QUEUE", root), \
                    patch.object(bridge, "recipient_actor", return_value="new-actor"), \
                    patch.object(bridge, "_run", return_value=fresh) as run:
                result = bridge._ask("alice", {"recipient": "owner", "title": "Go?",
                                               "text": "Deploy", "reference": "task:7"})
            run.assert_called_once()
            self.assertEqual(result["request_id"], "b" * 36)
            self.assertEqual(result["recipient_actor_id"], "new-actor")

    def test_session_policy_is_explicit_and_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            seen = []

            def fake_run(*args):
                seen.extend(args)
                return {"status": "hold", "reason": "offline"}

            with patch.object(bridge, "STATE", Path(temp)), patch.object(bridge, "QUEUE", Path(temp) / "bridge"), patch.object(bridge, "_run", fake_run):
                bridge._ask("alice", {"recipient": "owner", "recipient_actor_id": "owner",
                                      "title": "Choose", "text": "A or B", "reference": "task:7",
                                      "assurance": "session", "max_session_hours": 4})
            self.assertIn("4h", seen)

    def test_crash_after_signed_creation_recovers_queue_without_second_ask(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state, queue = root / "state", root / "queue"
            (state / "requests").mkdir(parents=True)
            rid = "f" * 36
            local = {"request_id": rid, "body": {"caller_ref": "task:7",
                                                   "recipient_id": "owner"},
                     "link": "https://hitl.example.com/r/demo"}
            (state / "requests" / f"{rid}.json").write_text(json.dumps(local))
            with patch.object(bridge, "STATE", state), patch.object(bridge, "QUEUE", queue), \
                    patch.object(bridge, "_run", side_effect=AssertionError("second ask")):
                intent = bridge._intent_path("alice", "task:7")
                bridge._save(intent, {"agent": "alice", "reference": "task:7",
                                      "recipient": "owner", "recipient_actor_id": "owner",
                                      "title": "Go?", "started_at": time.time() - 1})
                result = bridge._ask("alice", {"recipient": "owner",
                                               "recipient_actor_id": "owner",
                                               "title": "Go?", "text": "Deploy",
                                               "reference": "task:7"})
                self.assertEqual(result["request_id"], rid)
                self.assertEqual(bridge._owner(rid, "alice")["link"], local["link"])
                self.assertFalse(intent.exists())

    def test_second_ask_reuses_pending_signed_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bridge._save(root / "requests" / ("a" * 36 + ".json"), {
                "request_id": "a" * 36, "agent": "alice", "reference": "task:7",
                "recipient": "owner", "recipient_actor_id": "owner",
                "link": "https://hitl.example.com/r/demo"})
            with patch.object(bridge, "STATE", root), patch.object(bridge, "QUEUE", root), \
                    patch.object(bridge, "_run", side_effect=AssertionError("duplicate ask")):
                result = bridge._ask("alice", {"recipient": "owner",
                                               "recipient_actor_id": "owner",
                                               "title": "Go?", "text": "Deploy",
                                               "reference": "task:7"})
            self.assertEqual(result["status"], "created")
            self.assertEqual(result["request_id"], "a" * 36)


class DeliveryTest(unittest.TestCase):
    def test_owner_with_legacy_agent_type_receives_question(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = {"request_id": "a" * 36, "agent": "alice", "recipient": "owner",
                    "recipient_actor_id": "admin", "reference": "task:7",
                    "title": "Approve release", "text": "Proceed?"}
            path = root / "request.json"
            path.write_text(json.dumps(item))
            task = {"status": "in_progress", "assignee": "alice"}
            calls = []

            def fake_api(method, endpoint, body=None):
                calls.append((method, endpoint, body))
                if endpoint == "/api/hitl/recipient/admin":
                    return {"eligible": True}
                if "/actors/" in endpoint:
                    return {"id": "admin", "type": "agent", "active": True}
                if method == "GET" and endpoint.endswith("/comments"):
                    return []
                if method == "GET":
                    return dict(task)
                if endpoint.endswith("/handoff"):
                    task["assignee"] = body["to_actor"]
                return {}

            with patch.object(delivery, "MARKERS", root / "markers"), patch.object(
                delivery, "QUEUE", root / "queue"
            ), patch.object(delivery, "api", fake_api):
                delivery.process(path)
            self.assertEqual(task["assignee"], "admin")
            self.assertTrue(any(endpoint.endswith("/handoff") for _, endpoint, _ in calls))

    def test_planner_human_type_does_not_admit_unconfigured_actor(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "request.json"
            path.write_text(json.dumps({"request_id": "b" * 36, "agent": "alice",
                                        "recipient": "owner", "recipient_actor_id": "unknown-peer",
                                        "reference": "task:7"}))
            calls = []

            def fake_api(method, endpoint, body=None):
                calls.append((method, endpoint))
                if endpoint == "/api/hitl/recipient/unknown-peer":
                    return {"eligible": False}
                if "/actors/" in endpoint:
                    return {"id": "unknown-peer", "type": "human", "active": True}
                return {"status": "in_progress", "assignee": "alice"}

            with patch.object(delivery, "MARKERS", root / "markers"), patch.object(
                delivery, "QUEUE", root / "queue"
            ), patch.object(delivery, "api", fake_api):
                with self.assertRaisesRegex(ValueError, "not a configured human"):
                    delivery.process(path)
            self.assertFalse(any(method == "POST" for method, _ in calls))

    def test_delivery_uses_narrow_core_api_port(self):
        requests = []

        class Response:
            status = 200

            def read(self):
                return b"{}"

        class Connection:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, method, path, body=None, headers=None):
                requests.append((method, path, json.loads(body) if body else None))

            def getresponse(self):
                return Response()

            def close(self):
                pass

        with patch.object(delivery, "UnixConnection", Connection):
            delivery.api("GET", "/api/planner/api/v1/tasks/7")
            delivery.api("GET", "/api/hitl/recipient/admin")
        self.assertEqual(requests, [("POST", "/api/hitl/planner", {
            "method": "GET", "path": "api/v1/tasks/7", "body": None,
        }), ("GET", "/api/hitl/recipient/admin", None)])
        with self.assertRaises(ValueError):
            delivery.api("POST", "/api/agents/alice/wake", {})
        with self.assertRaises(ValueError):
            delivery.api("POST", "/api/hitl/recipient/admin", {})

    def test_answer_for_different_task_is_not_delivered(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rid = "c" * 36
            item = {"request_id": rid, "agent": "alice", "recipient": "owner",
                    "recipient_actor_id": "owner",
                    "reference": "task:7", "result": {"status": "answered",
                    "request_id": rid, "caller_ref": "task:8"}}
            path = root / "answer.json"
            path.write_text(json.dumps(item))
            task = {"status": "in_progress", "assignee": "owner"}
            calls = []

            def fake_api(method, endpoint, body=None):
                calls.append((method, endpoint))
                if endpoint == "/api/hitl/recipient/owner":
                    return {"eligible": True}
                if "/actors/" in endpoint:
                    return {"type": "human", "active": True}
                return dict(task)

            with patch.object(delivery, "MARKERS", root / "markers"), patch.object(delivery, "api", fake_api):
                with self.assertRaises(ValueError):
                    delivery.process(path)
            self.assertFalse(any(method == "POST" for method, _ in calls))

    def test_request_comment_is_not_duplicated_after_lost_response(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            item = {"request_id": "a" * 36, "agent": "alice", "recipient": "owner",
                    "recipient_actor_id": "owner", "reference": "task:7",
                    "title": "Approve release", "text": "Read the release notes"}
            path = root / "request.json"
            path.write_text(json.dumps(item))
            task = {"status": "in_progress", "assignee": "alice"}
            comments = []
            posts = []

            def fake_api(method, endpoint, body=None):
                if endpoint == "/api/hitl/recipient/owner":
                    return {"eligible": True}
                if "/actors/" in endpoint:
                    return {"type": "human", "active": True}
                if method == "GET" and endpoint.endswith("/comments"):
                    return list(comments)
                if method == "GET":
                    return dict(task)
                if endpoint.endswith("/comments"):
                    posts.append(body)
                    comments.append(body)
                    raise RuntimeError("response lost after Planner commit")
                if endpoint.endswith("/handoff"):
                    task["assignee"] = body["to_actor"]
                return {}

            with patch.object(delivery, "MARKERS", root / "markers"), patch.object(
                delivery, "api", fake_api
            ):
                with self.assertRaisesRegex(RuntimeError, "response lost"):
                    delivery.process(path)
                delivery.process(path)
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]["comment_type"], "workflow:request")
            self.assertEqual(task["assignee"], "owner")

    def test_verified_answer_is_recorded_then_resumed_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rid = "b" * 36
            request = {"request_id": rid, "agent": "alice", "recipient": "owner",
                       "recipient_actor_id": "owner",
                       "reference": "task:7", "title": "Approve release",
                       "text": "Review the release notes"}
            asked = root / "requests.json"
            asked.write_text(json.dumps(request))
            answered = root / "answer.json"
            answered.write_text(json.dumps({**request, "result": {
                "status": "answered", "request_id": rid, "caller_ref": "task:7",
                "answer": {"choice": "approve"}, "proof_token": "hitl-proof-v1.test",
            }}))
            task = {"status": "in_progress", "assignee": "alice"}
            calls = []

            def fake_api(method, path, body=None):
                calls.append((method, path, body))
                if path == "/api/hitl/recipient/owner":
                    return {"eligible": True}
                if "/actors/" in path:
                    return {"type": "human", "active": True}
                if method == "GET" and path.endswith("/comments"):
                    return []
                if method == "GET":
                    return dict(task)
                if method == "POST" and path.endswith("/handoff"):
                    self.assertEqual(task["assignee"], body["from_actor"])
                    task["assignee"] = body["to_actor"]
                return {}

            with patch.object(delivery, "MARKERS", root / "markers"), patch.object(delivery, "QUEUE", root / "queue"), patch.object(delivery, "api", fake_api):
                delivery.process(asked)
                self.assertEqual(task["status"], "in_progress")
                self.assertEqual(task["assignee"], "owner")
                delivery.process(answered)
                self.assertEqual(task["status"], "in_progress")
                self.assertEqual(task["assignee"], "alice")
                count = len(calls)
                delivery.process(answered)
                self.assertEqual(len(calls), count)
                comments = [body for method, path, body in calls
                            if method == "POST" and path.endswith("/comments")]
                self.assertEqual([c["comment_type"] for c in comments],
                                 ["workflow:request", "workflow:answer"])
                self.assertIn("Review the release notes", comments[0]["content"])
                self.assertIn("hitl-proof-v1.test", comments[1]["content"])
                handoffs = [body for method, path, body in calls
                            if method == "POST" and path.endswith("/handoff")]
                self.assertEqual(len(handoffs), 2)
                self.assertTrue(all(handoff["notify"] is False for handoff in handoffs))
                wakes = [call for call in calls if call[0] == "POST" and call[1] == "/api/hitl/wake"]
                self.assertEqual(len(wakes), 1)
                request_post = next(i for i, call in enumerate(calls)
                                    if call[0] == "POST" and call[1].endswith("/comments"))
                handoff_post = next(i for i, call in enumerate(calls)
                                    if call[0] == "POST" and call[1].endswith("/handoff"))
                self.assertLess(request_post, handoff_post)

    def test_retry_after_comment_commit_does_not_duplicate_answer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rid = "e" * 36
            item = {"request_id": rid, "agent": "alice", "recipient": "owner",
                    "recipient_actor_id": "owner", "reference": "task:7",
                    "result": {"status": "answered", "request_id": rid,
                               "caller_ref": "task:7", "answer": {"choice": "approve"}}}
            path = root / "answer.json"
            path.write_text(json.dumps(item))
            task = {"status": "in_progress", "assignee": "owner"}
            comments = []
            posts = []

            def fake_api(method, endpoint, body=None):
                if endpoint == "/api/hitl/recipient/owner":
                    return {"eligible": True}
                if "/actors/" in endpoint:
                    return {"type": "human", "active": True}
                if method == "GET" and endpoint.endswith("/comments"):
                    return list(comments)
                if method == "GET":
                    return dict(task)
                if endpoint.endswith("/comments"):
                    posts.append(body)
                    comments.append(body)
                    if body["comment_type"] == "workflow:answer":
                        raise RuntimeError("response lost after Planner commit")
                elif method == "POST" and endpoint.endswith("/handoff"):
                    self.assertEqual(task["assignee"], body["from_actor"])
                    task["assignee"] = body["to_actor"]
                return {}

            with patch.object(delivery, "MARKERS", root / "markers"), patch.object(delivery, "api", fake_api):
                with self.assertRaisesRegex(RuntimeError, "response lost"):
                    delivery.process(path)
                delivery.process(path)
            self.assertEqual([p["comment_type"] for p in posts],
                             ["workflow:request", "workflow:answer"])
            self.assertEqual(task["status"], "in_progress")


if __name__ == "__main__":
    unittest.main()
