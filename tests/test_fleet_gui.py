"""Fleet HTTP/security integration against isolated state and an ephemeral loopback server.

No CLI agents, provider calls, user state, configuration or fixed ports are involved.
Run: python -m unittest discover -s tests -p test_fleet_gui.py -v
"""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("fleet_gui", ROOT / "gui.py")
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)
from neoxider_agents.events import EventStore
from neoxider_agents.state import Store


class FleetHTTPTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="fleet-http-")
        self.logdir = patch.object(gui, "LOGDIR", self.scratch.name)
        self.logdir.start()
        self.auth = patch.object(gui, "GUI_TOKEN", "")
        self.auth.start()
        self.store = Store(self.scratch.name)
        self.store.update("one", state="done", parent="/root/fleet", dir="C:/fixture", engine="codex",
                          resolved_model="resolved", model_requested="requested", files=1,
                          delta_known=1, changed_names="src/a.py", owns="src/**", cost_usd="0.25")
        self.store.update("two", state="stopped", parent="/root/other", dir="C:/fixture", engine="codex")
        self.events = EventStore(self.scratch.name)
        self.events.emit("one", kind="task.done", data={"detail": "<img onerror=bad>"})
        self.events.emit("one", kind="message.accepted", data={"detail": "receipt is separate"})
        self.events.emit("two", kind="task.stopped", data={})
        self.server = gui.Srv(("127.0.0.1", 0), gui.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.auth.stop()
        self.logdir.stop()
        self.scratch.cleanup()

    def request(self, method, path, body=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        payload = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        values = {"Content-Type": "application/json"}
        values.update(headers or {})
        try:
            connection.request(method, path, payload, values)
            response = connection.getresponse()
            data = response.read()
            return response.status, json.loads(data), dict(response.getheaders()), data
        finally:
            connection.close()

    def get_events(self, task="one", after=None, **kwargs):
        query = {"task": task, **kwargs}
        if after is not None:
            query["after"] = json.dumps(after)
        return self.request("GET", "/api/task/events?" + urlencode(query))

    def ack(self, task="one", cursor=None, consumer="gui-test", **kwargs):
        return self.request("POST", "/api/task/events/ack", {
            "task": task, "cursor": cursor if cursor is not None else {"task": task, "seq": 1},
            "consumer": consumer}, **kwargs)

    def test_fleet_integrates_filters_current_state_models_cost_and_cached_delta(self):
        code, data, headers, _ = self.request("GET", "/api/fleet")
        self.assertEqual(code, 200)
        self.assertEqual(data["summary"], dict(active=0, done=1, attention=1, error=0))
        self.assertEqual(data["summary_scope"], "returned page")
        one = next(task for task in data["tasks"] if task["name"] == "one")
        self.assertEqual((one["model_requested"], one["model_resolved"]), ("requested", "resolved"))
        self.assertEqual(one["cost"]["amount"], .25)
        self.assertIn("telemetry", one["cost"]["provenance"])
        self.assertEqual(one["delta"]["paths"], ["src/a.py"])
        self.assertFalse(one["controls"]["stop"])
        self.assertTrue(one["controls"]["resume"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        query = urlencode(dict(parent="/root/fleet", group="C:/fixture"))
        self.assertEqual([task["name"] for task in self.request("GET", "/api/fleet?" + query)[1]["tasks"]], ["one"])

    def test_task_bound_paging_and_ack_are_persisted_independent_consumers(self):
        code, page, _, _ = self.get_events(limit=1)
        self.assertEqual(code, 200)
        self.assertEqual(page["cursor"], dict(task="one", seq=1))
        self.assertTrue(page["more"])
        self.assertEqual(self.events.cursor("one", "gui-test"), 0)  # A GET never acknowledges.
        self.assertEqual(self.ack(cursor=page["cursor"])[0], 200)
        self.assertEqual(EventStore(self.scratch.name).cursor("one", "gui-test"), 1)
        self.assertEqual(self.events.cursor("one", "gui-other"), 0)
        second = self.get_events(after=page["cursor"])[1]
        self.assertEqual([event["seq"] for event in second["events"]], [2])
        self.assertEqual(self.ack(cursor=second["cursor"])[0], 200)
        self.assertFalse(self.ack(cursor=page["cursor"])[1]["advanced"])
        self.assertEqual(self.events.cursor("one", "gui-test"), 2)

    def test_cross_task_scalar_ahead_and_invalid_consumer_cursors_fail(self):
        for cursor in ({"task": "two", "seq": 1}, 1, {"task": "one", "seq": True},
                       {"task": "one", "seq": 99}, {"task": "one", "seq": 1, "extra": 0}):
            with self.subTest(cursor=cursor):
                self.assertEqual(self.get_events(after=cursor)[0], 400)
                self.assertEqual(self.ack(cursor=cursor)[0], 400)
        for consumer in ("../outside", "a" * 65, "", [], True, 123):
            self.assertEqual(self.ack(consumer=consumer)[0], 400)
        self.assertEqual(self.get_events(consumer="../outside")[0], 400)
        self.assertEqual(self.get_events(after={"task": "one", "seq": "1"})[0], 200)
        self.assertEqual(self.events.cursor("one", "gui-test"), 0)

    def test_auth_token_cookie_csrf_and_network_host_guards(self):
        gui.GUI_TOKEN = "fixture-secret"
        for method, path, body in (("GET", "/api/fleet", None), ("GET", "/api/task/events?task=one", None),
                                   ("POST", "/api/task/events/ack", dict(task="one", consumer="gui-test", cursor=dict(task="one", seq=1)))):
            self.assertEqual(self.request(method, path, body)[0], 401)
            self.assertEqual(self.request(method, path, body, {"X-Agent-Token": "fixture-secret"})[0], 200)
            self.assertEqual(self.request(method, path, body, {"Cookie": "agent_gui_token=fixture-secret"})[0], 200)
        self.assertFalse(gui.gui_authorized("10.0.0.5", {}, "/api/fleet"))
        gui.GUI_TOKEN = ""
        self.assertEqual(self.ack(headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.ack(headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.ack(headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.ack(headers={"Content-Type": "text/plain"})[0], 415)

    def test_individual_controls_inherit_auth_csrf_without_launching_real_agents(self):
        with patch.object(gui, "spawn") as spawn, patch.object(gui.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            for action in ("send", "stop", "resume"):
                path, body = "/api/task/" + action, {"task": "one", "message": "go"}
                gui.GUI_TOKEN = "fixture-secret"
                self.assertEqual(self.request("POST", path, body)[0], 401)
                self.assertEqual(self.request("POST", path, body, {"X-Agent-Token": "fixture-secret"})[0], 200)
                gui.GUI_TOKEN = ""
                self.assertEqual(self.request("POST", path, body, {"Origin": "https://evil.example"})[0], 403)
            self.assertEqual(spawn.call_count, 2)

    def test_json_body_limits_malformed_and_unknown_fields(self):
        self.assertEqual(self.request("POST", "/api/task/events/ack", raw=b"x" * (gui.EVENT_BODY_CAP + 1))[0], 413)
        for raw in (b"[]", b"null", b"{", b'{"task":"one"}'):
            self.assertEqual(self.request("POST", "/api/task/events/ack", raw=raw)[0], 400)
        self.assertEqual(self.request("POST", "/api/task/events/ack", raw=b"{}", headers={"Content-Length": "bad"})[0], 400)
        self.assertEqual(self.request("POST", "/api/task/events/ack", raw=b"{}", headers={"Content-Length": "9" * 5000})[0], 413)
        body = dict(task="one", consumer="gui-test", cursor=dict(task="one", seq=1), callback="https://outside")
        self.assertEqual(self.request("POST", "/api/task/events/ack", body)[0], 400)

    def test_traversal_missing_tasks_and_static_fleet_auth(self):
        for task in ("../outside", "C:/outside", "--all-mine", "a\\b"):
            self.assertEqual(self.get_events(task=task)[0], 400)
            self.assertEqual(self.ack(task=task)[0], 400)
        self.assertEqual(self.get_events(task="missing")[0], 404)
        self.assertEqual(self.ack(task="missing")[0], 404)
        gui.GUI_TOKEN = "fixture-secret"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", "/static/fleet.js")
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
        finally:
            connection.close()

    def test_corrupt_events_never_become_successful_empty_replay_or_ack(self):
        with self.events.path("one").open("ab") as stream:
            stream.write(b'{"bad":true}\n')
        self.assertEqual(self.get_events()[0], 409)
        self.assertEqual(self.ack()[0], 409)

    def test_torn_tail_is_reported_without_replaying_raw_logs(self):
        with self.events.path("one").open("ab") as stream:
            stream.write(b'{"incomplete"')
        (Path(self.scratch.name) / "one.log").write_text("RAW SECRET LOG", encoding="utf-8")
        code, page, _, raw = self.get_events()
        self.assertEqual(code, 200)
        self.assertTrue(page["torn"])
        self.assertEqual(len(page["events"]), 2)
        self.assertNotIn(b"RAW SECRET LOG", raw)

    def test_receipts_show_uuid_and_separate_acceptance_delivery_explicit_ack(self):
        receipt = self.store.enqueue_receipt("one", "Message says acknowledged but is not evidence")
        identifier = receipt["id"]
        view = gui.fleet_payload()["tasks"][0]["receipts"]
        self.assertEqual(view["status"], "ok")
        self.assertEqual(view["items"][0]["id"], identifier)
        self.assertEqual(view["counts"], dict(accepted=1, delivered=0, acknowledged=0))
        paths = self.store.inbox("one")
        self.store.begin_delivery("one", paths, generation="turn-1", turn=1)
        self.store.finish_delivery("one", paths, generation="turn-1", delivered=True)
        view = gui.fleet_payload()["tasks"][0]["receipts"]
        self.assertEqual(view["counts"], dict(accepted=1, delivered=1, acknowledged=0))
        self.store.acknowledge_message("one", identifier, source="assistant", text="Explicit acknowledgment")
        self.assertEqual(gui.fleet_payload()["tasks"][0]["receipts"]["counts"], dict(accepted=1, delivered=1, acknowledged=1))

    def test_corrupt_torn_or_unknown_receipts_have_unknown_counts_not_zero(self):
        self.store.enqueue_receipt("one", "private-message")
        path = self.store.path("one", ".receipts.jsonl")
        original = path.read_bytes()
        for tail in (b"BROKEN\n", b'{"torn"'):
            path.write_bytes(original + tail)
            view = gui.fleet_payload()["tasks"][0]["receipts"]
            self.assertEqual(view["status"], "unavailable")
            self.assertIsNone(view["counts"])
            self.assertNotIn("private-message", json.dumps(view))
        path.write_bytes(original)
        with patch.object(Store, "receipts", return_value=[{"state": "new-schema"}]):
            self.assertIsNone(gui.fleet_payload()["tasks"][0]["receipts"]["counts"])

    def test_send_is_launcher_pending_with_exact_uuid_file_transport_and_known_restart(self):
        with patch.object(gui, "spawn") as spawn:
            code, result, _, _ = self.request("POST", "/api/task/send", {"task": "one", "message": "--help"})
            self.assertEqual(code, 200)
            self.assertTrue(result["launcher_started"])
            self.assertEqual(result["status"], "pending")
            self.assertFalse(result["accepted"])
            self.assertRegex(result["message_id"], r"^[0-9a-f]{32}$")
            args = spawn.call_args.args[0]
            self.assertEqual(args[:4], ["send", "one", "--message-id", result["message_id"]])
            self.assertEqual(args[-1], "--")
            self.assertIn("--prompt-file", args)
            self.assertNotIn("--help", args)
            self.assertEqual(self.request("POST", "/api/task/resume", {"task": "one"})[0], 200)
            spawn.assert_called_with(["restart", "one"], terminal=False)
            self.assertEqual(self.request("POST", "/api/task/resume", {"task": "one", "terminal": "yes"})[0], 400)

    def test_summary_and_event_json_are_byte_bounded_without_skipping_events(self):
        for index in range(6):
            self.events.emit("one", kind="large", data={"text": "x" * 120000, "index": index})
        code, page, _, raw = self.get_events(limit=32)
        self.assertEqual(code, 200)
        self.assertLessEqual(len(raw), gui.FLEET_JSON_CAP)
        self.assertTrue(page["more"])
        follow = self.get_events(after=page["cursor"])[1]
        self.assertEqual([event["seq"] for event in page["events"] + follow["events"]], list(range(1, 9)))
        code, data, _, raw = self.request("GET", "/api/fleet?limit=1")
        self.assertEqual(code, 200)
        self.assertEqual(len(data["tasks"]), 1)
        self.assertTrue(data["incomplete"])
        self.assertEqual(sum(data["summary"].values()), 1)
        self.assertLessEqual(len(raw), gui.FLEET_JSON_CAP)

    def test_request_uuid_poll_never_guesses_latest_receipt_or_task_exit_success(self):
        old = self.store.enqueue_receipt("one", "old unrelated message")
        identifier = "b" * 32
        with patch.object(gui, "spawn"):
            result = self.request("POST", "/api/task/send", {"task": "one", "message": "new", "message_id": identifier})[1]
        self.assertFalse(result["accepted"])
        self.assertEqual(result["message_id"], identifier)
        url = "/api/task/receipt?" + urlencode(dict(task="one", message_id=identifier))
        self.assertIsNone(self.request("GET", url)[1]["receipt"])
        self.store.enqueue_receipt("one", "new", message_id=identifier)
        polled = self.request("GET", url)[1]["receipt"]
        self.assertEqual(polled["id"], identifier)
        self.assertNotEqual(polled["id"], old["id"])
        self.assertTrue(polled["accepted"])
        self.assertFalse(polled["delivered"] or polled["acknowledged"])
        self.assertIsNone(self.request("GET", url.replace("task=one", "task=two"))[1]["receipt"])
        self.assertEqual(self.request("GET", url.replace(identifier, "../outside"))[0], 400)
        gui.GUI_TOKEN = "fixture-secret"
        self.assertEqual(self.request("GET", url)[0], 401)

    def test_run_reply_send_private_file_transport_preserves_utf8_option_text_and_large_bodies(self):
        from neoxider_agents.cli import parse, text_prompt
        text = " \t--help\nПривет😀\n " + "x" * 30001
        captured = []
        def capture(args, terminal=False):
            path = Path(args[args.index("--prompt-file") + 1])
            captured.append((args, path, path.read_bytes()))
            opts, operands = parse(args[0], args[1:])
            self.assertEqual(text_prompt(opts, operands, 0 if args[0] == "run" else 1), text)
        with patch.object(gui, "spawn", side_effect=capture):
            for route, body in (("/api/run", dict(prompt=text)), ("/api/reply", dict(task="one", answer=text)),
                                ("/api/task/send", dict(task="one", message=text))):
                self.assertEqual(self.request("POST", route, body)[0], 200)
                args, path, payload = captured[-1]
                self.assertEqual(payload, text.encode("utf-8"))
                self.assertFalse(any(text in argument or "Привет" in argument for argument in args))
                self.assertFalse(path.exists())
                self.assertEqual(args[-1], "--")
        self.assertEqual(gui._PROMPT_FILES, {})

    def test_transport_failure_and_invalid_uuid_text_or_options_never_launch_or_leak(self):
        captured = []
        def fail(args, terminal=False):
            captured.append(Path(args[args.index("--prompt-file") + 1]))
            raise OSError("secret body must not leak")
        with patch.object(gui, "spawn", side_effect=fail):
            for route, body in (("/api/run", dict(prompt="private")), ("/api/reply", dict(task="one", answer="private")),
                                ("/api/task/send", dict(task="one", message="private"))):
                code, data, _, _ = self.request("POST", route, body)
                self.assertEqual(code, 500)
                self.assertNotIn("secret", json.dumps(data))
                self.assertFalse(captured[-1].parent.exists())
        with patch.object(gui, "spawn") as spawn:
            for identifier in ("--help", True, [], "x" * 32):
                self.assertEqual(self.request("POST", "/api/task/send", dict(task="one", message="go", message_id=identifier))[0], 400)
            for value in ([], True, "\0bad", "x" * (gui.PROMPT_BODY_CAP + 1)):
                self.assertEqual(self.request("POST", "/api/run", dict(prompt=value))[0], 400)
            for field in ("dir", "model", "effort"):
                self.assertEqual(self.request("POST", "/api/run", dict(prompt="valid", **{field: []}))[0], 400)
            spawn.assert_not_called()

    def test_declared_overlap_and_shared_delta_are_honest_cached_evidence(self):
        for task in ("one", "two"):
            self.store.update(task, state="running", pid=os.getpid(), winpid=os.getpid(), owns="src/**",
                              changed_names="src/a.py", delta_known=1, files=1)
        tasks = gui.fleet_payload()["tasks"]
        self.assertEqual(tasks[0]["declared_overlap"], ["two"])
        self.assertIn("unverified", tasks[0]["conflicts"][0]["note"])
        self.assertIn("do not prove", gui.fleet_payload()["attribution"])


class SpawnResourceTests(unittest.TestCase):
    def test_private_prompt_survives_delayed_wrapper_read_and_active_process_then_is_released(self):
        with tempfile.TemporaryDirectory(prefix="gui-wrapper-fixture-") as directory:
            root = Path(directory)
            output, gate = root / "read.bin", root / "exit.gate"
            script = root / "wrapper.py"
            script.write_text("import sys,time\nfrom pathlib import Path\ntime.sleep(.1)\np=Path(sys.argv[sys.argv.index('--prompt-file')+1])\nPath(sys.argv[sys.argv.index('--output')+1]).write_bytes(p.read_bytes())\nwhile not Path(sys.argv[sys.argv.index('--gate')+1]).exists(): time.sleep(.01)\n", encoding="utf-8")
            with patch.object(gui, "SK", str(script)):
                child = gui.spawn_prompt(["fixture", "--output", str(output), "--gate", str(gate)], " --help\n秘密😀 ")
            prompt = Path(child.args[child.args.index("--prompt-file") + 1])
            try:
                deadline = time.monotonic() + 3
                while not output.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(output.read_bytes(), " --help\n秘密😀 ".encode("utf-8"))
                self.assertTrue(prompt.exists())
                self.assertIsNone(child.poll())
            finally:
                gate.touch()
                child.wait(timeout=3)
            deadline = time.monotonic() + 3
            while prompt.parent.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(prompt.parent.exists())
            self.assertNotIn(id(child), gui._SPAWNED)

    def test_owned_process_is_reaped_and_pipes_closed_immediately_after_exit(self):
        child = subprocess.Popen([sys.executable, "-c", "print('fixture')"], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, **gui._hidden_windows_kwargs())
        gui._track_spawn(child)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with gui._SPAWN_LOCK:
                retained = id(child) in gui._SPAWNED
            if not retained:
                break
            time.sleep(.02)
        self.assertFalse(retained)
        self.assertEqual(child.returncode, 0)
        self.assertTrue(child.stdin.closed and child.stdout.closed and child.stderr.closed)
        self.assertNotIn("NoExit", gui.spawn.__doc__ or "")


if __name__ == "__main__":
    unittest.main()
