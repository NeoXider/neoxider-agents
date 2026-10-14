"""Phase 1 orchestrator facade: real fixture subprocesses, no provider or network calls."""
import concurrent.futures
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(os.environ.get("AGENT_CORE_TEST_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))

from neoxider_agents import orchestrator as sdk
from neoxider_agents.state import Store
from neoxider_agents.process import pid_alive


class OrchestratorApiTests(unittest.TestCase):
    def setUp(self):
        base = Path("C:/AI/outputs/neoxider-parity-sdk-20261014/fixtures") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-sdk"
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="sdk-", dir=str(base))
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        self.work.mkdir()
        self.other = self.base / "other"
        self.other.mkdir()
        self.logs = self.base / "logs"
        self.env = {
            "AGENT_PROVIDER_DIR": (ROOT / "tests/fixtures/providers").as_posix(),
            "FIXTURE_SCRIPT": (ROOT / "tests/fixtures/core-provider.py").as_posix(),
            "AGENT_PARENT": "sdk-tests",
            "AGENT_ORCHESTRATOR_ID": "sdk-tests",
            "AGENT_RETRIES": "0",
            "AGENT_TIMEOUT_SEC": "90",
            "AGENT_SILENCE_SEC": "0",
            "AGENT_ENGINE": "",
            "AGENT_MODEL": "",
            "AGENT_EFFORT": "",
            "AGENT_PROGRESS": "0",
            "AGENT_KEEP_LOGS": "0",
            "AGENT_NOTIFY": "0",
        }
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            try:
                client.close(detach=False)
            except BaseException:
                pass
        deadline = time.monotonic() + 5
        while True:
            try:
                self.temp.cleanup()
                break
            except (PermissionError, OSError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def client(self, **kwargs):
        kwargs.setdefault("poll", 0.05)
        kwargs.setdefault("publish_timeout", 25.0)
        kwargs.setdefault("control_timeout", 25.0)
        env = kwargs.pop("env", self.env)
        client = sdk.AgentClient(logs_root=self.logs, env=env, **kwargs)
        self.clients.append(client)
        return client

    def store(self):
        return Store(self.logs)

    def until(self, predicate, seconds=15, message="condition"):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.025)
        self.fail("deadline exceeded waiting for " + message)

    def release(self, work, turn):
        (work / ("turn.%s.release" % turn)).touch()

    def blocked_env(self, work, turns="1"):
        return {"FIXTURE_BLOCK_TURNS": turns}

    # ---------- typed spawn/status ----------

    def test_spawn_returns_typed_handle_with_real_owner(self):
        client = self.client()
        handle = client.spawn("ORIGINAL PROMPT", engine="fixture", cwd=self.work, name="sdk-spawn-1",
                              env=self.blocked_env(self.work))
        self.assertIsInstance(handle, sdk.TaskHandle)
        self.assertEqual(handle.task, "sdk-spawn-1")
        self.assertTrue(handle.generation, "owner published a generation")
        self.until(lambda: (self.work / "turn.1.started").exists(), message="provider start")
        status = handle.status()
        self.assertIsInstance(status, sdk.TaskStatus)
        self.assertIn(status.state, ("running", "idle"))
        self.assertTrue(status.live)
        self.assertTrue(status.owner_alive)
        self.assertEqual(status.parent, "sdk-tests")
        self.assertEqual(status.engine, "fixture")
        self.assertEqual(status.directory, str(self.work.resolve()))
        # The session id only exists once the provider reports one; a status refresh
        # must publish it onto the live handle instead of leaving it empty.
        self.assertTrue(status.session.startswith("ses_fixture_"))
        self.assertEqual(handle.session, status.session)
        meta = self.store().read("sdk-spawn-1")
        self.assertNotIn("sdk_client", meta)
        binding = json.loads(client._binding_path(handle.task).read_text(encoding="utf-8"))
        self.assertEqual(binding["client_id"], client.client_id)
        self.assertTrue(pid_alive(meta.get("pid"), meta.get("pid_start", "") if meta.get("core_version") else ""))
        self.release(self.work, 1)
        result = handle.wait(timeout=30)
        self.assertEqual(result.state, "done")
        self.assertIn("ORIGINAL PROMPT", result.answer)

    def test_no_silent_model_substitution_and_explicit_request_reported(self):
        client = self.client()
        handle = client.spawn("MODEL CHECK", engine="fixture", cwd=self.other, name="sdk-model-1",
                              env={"FIXTURE_DELAY": "0"})
        self.assertFalse(handle.model_substituted, "an empty request cannot be a substitution")
        self.assertEqual(handle.requested_model, "")
        self.assertEqual(handle.requested_engine, "fixture")
        self.assertEqual(handle.status().model, handle.model)
        self.assertEqual(handle.wait(timeout=30).state, "done")

    def test_startup_failure_raises_and_leaves_no_task(self):
        client = self.client()
        with self.assertRaises(sdk.SpawnError) as caught:
            client.spawn("BOOM", engine="fixture", cwd=self.other, name="sdk-fail-1",
                         env={"FIXTURE_MISSING_CLI": "1"})
        self.assertIn("sdk-fail-1", str(caught.exception))
        self.assertFalse(self.store().path("sdk-fail-1", ".meta").exists())
        self.assertEqual(client.handles(), ())
        self.assertTrue(all(not child.alive() for child in client._children))

    def test_existing_task_name_is_refused(self):
        client = self.client()
        client.spawn("FIRST", engine="fixture", cwd=self.other, name="sdk-dup-1", env={"FIXTURE_DELAY": "0"}).wait(timeout=30)
        with self.assertRaises(ValueError):
            client.spawn("SECOND", engine="fixture", cwd=self.other, name="sdk-dup-1")

    def test_dead_owner_is_reported_honestly_not_faked(self):
        client = self.client()
        store = self.store()
        store.update("sdk-dead-1", engine="fixture", core_version="2", state="running", exit="",
                     pid="0", winpid="", dir=str(self.work.resolve()), parent="sdk-tests",
                     reason="", generation="dead0000000000000000000000000000")
        handle = client.attach("sdk-dead-1")
        status = client.status(handle)
        self.assertEqual(status.state, "stopped")
        self.assertFalse(status.live)
        self.assertFalse(status.owner_alive)
        result = client.result(handle)
        self.assertEqual(result.state, "stopped")
        self.assertTrue(result.settled)
        self.assertEqual(result.session, "")
        self.assertFalse(result.baseline_known, "no start baseline exists for a hand-seeded task")

    # ---------- send / queued messages ----------

    def test_queued_receipt_has_durable_uuid_and_legacy_sequence(self):
        client = self.client()
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.work, name="sdk-queue-1",
                              env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists(), message="first turn")
        receipt = handle.send("FOLLOW UP TEXT")
        self.assertIsInstance(receipt, sdk.SendReceipt)
        self.assertEqual(receipt.task, "sdk-queue-1")
        self.assertTrue(receipt.accepted)
        self.assertTrue(receipt.queued)
        self.assertEqual(receipt.sequence, 1)
        self.assertFalse(receipt.resumed)
        self.assertFalse(receipt.delivered, "delivery is never inferred in Phase 1")
        self.assertFalse(receipt.acknowledged, "acknowledgement is never inferred in Phase 1")
        self.assertRegex(receipt.receipt_id, r"^[0-9a-f]{32}$")
        self.assertEqual(client.receipt(handle, receipt.receipt_id).sequence, receipt.sequence)
        self.assertIn("legacy", receipt.sequence_source)
        self.assertEqual(self.store().path("sdk-queue-1", ".inbox").joinpath("000000000001.msg").read_text(encoding="utf-8"),
                         "FOLLOW UP TEXT")
        self.release(self.work, 1)
        result = handle.wait(timeout=40)
        self.assertEqual(result.state, "done")
        prompt = (self.work / "turn.2.prompt").read_text(encoding="utf-8")
        self.assertIn("Message #1:", prompt)
        self.assertIn("FOLLOW UP TEXT", prompt)
        self.assertEqual(self.store().inbox("sdk-queue-1"), [])

    def test_send_rejects_empty_message(self):
        client = self.client()
        with self.assertRaises(ValueError):
            client.spawn("X", engine="fixture", cwd=self.other, name="sdk-empty-1", env={}).send("   ")

    def test_concurrent_sends_and_status_are_thread_safe(self):
        client = self.client()
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.work, name="sdk-thread-1",
                              env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists(), message="first turn")
        errors = []

        def send(index):
            try:
                client.send(handle, "MESSAGE %s" % index)
            except BaseException as error:  # pragma: no cover - failure detail only
                errors.append(repr(error))

        def poll():
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                client.status(handle)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(send, index) for index in (1, 2)]
            futures.append(pool.submit(poll))
            for future in futures:
                future.result()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(path.name for path in self.store().inbox("sdk-thread-1")),
                         ["000000000001.msg", "000000000002.msg"])
        self.release(self.work, 1)
        self.assertIn(handle.wait(timeout=40).state, ("done", "waiting"))

    # ---------- stop / resume ----------

    def test_stop_reaches_owner_and_retains_files_and_answer(self):
        client = self.client()
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.work, name="sdk-stop-1",
                              env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists(), message="first turn")
        (self.work / "PROGRESS.sdk-stop-1.md").write_text("partial progress", encoding="utf-8")
        result = client.stop(handle)
        self.assertEqual(result.state, "stopped")
        self.assertEqual(result.exit_code, 130)
        self.assertTrue(result.stop_report)
        self.assertTrue((self.work / "PROGRESS.sdk-stop-1.md").is_file())
        self.assertTrue((self.work / "partial.txt").is_file(), "partial provider edits survive stop")
        self.assertTrue(self.store().path("sdk-stop-1", ".answer").is_file())
        meta = self.store().read("sdk-stop-1")
        self.assertFalse(pid_alive(meta.get("pid"), meta.get("pid_start", "") if meta.get("core_version") else ""))

    def test_resume_keeps_session_and_generation_advances(self):
        client = self.client()
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.work, name="sdk-resume-1",
                              env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists(), message="first turn")
        session = handle.status().session
        self.assertTrue(session.startswith("ses_fixture_"))
        first_generation = handle.status().generation
        client.stop(handle)
        resumed = client.resume(handle, "CONTINUE PLEASE")
        self.assertIs(resumed, handle)
        self.assertNotEqual(resumed.generation, first_generation, "a new owner publishes a new generation")
        self.assertEqual(resumed.session, session, "resume keeps the saved session")
        self.until(lambda: (self.work / "turn.2.started").exists(), message="resumed turn")
        self.release(self.work, 2)
        result = resumed.wait(timeout=40)
        self.assertEqual(result.state, "done")
        self.assertIn("CONTINUE PLEASE", (self.work / "turn.2.prompt").read_text(encoding="utf-8"))

    def test_resume_can_change_model_and_effort_of_the_same_session(self):
        client = self.client()
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.work, name="sdk-resume-model",
                              env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists(), message="first turn")
        session = handle.status().session
        client.stop(handle)
        resumed = client.resume(handle, "CONTINUE", model="fixture-model-2", effort="high")
        self.assertEqual(resumed.session, session, "model change keeps the saved session")
        self.until(lambda: (self.work / "turn.2.started").exists(), message="resumed turn")
        meta = self.store().read("sdk-resume-model")
        self.assertTrue(meta.get("model", "").startswith("fixture-model-2"), meta.get("model"))
        self.assertEqual(meta.get("effort"), "high")
        self.until(lambda: (self.work / "turn.2.started").exists(), message="resumed turn")
        self.release(self.work, 2)
        self.assertEqual(resumed.wait(timeout=40).state, "done")

    def test_resume_refuses_unsupported_provider_without_starting_anything(self):
        client = self.client()
        store = self.store()
        store.update("sdk-noresume-1", engine="noresume", core_version="2", state="stopped", exit="130",
                     dir=str(self.work.resolve()), parent="sdk-tests", session="ses_fixed")
        handle = client.attach("sdk-noresume-1")
        with self.assertRaises(sdk.ResumeNotSupported) as caught:
            client.resume(handle, "GO")
        self.assertIn("supports_resume=false", str(caught.exception))
        self.assertTrue(all(not child.alive() for child in client._children))
        with self.assertRaises(sdk.ResumeNotSupported):
            client.send(handle, "GO")

    def test_resume_without_session_is_refused(self):
        client = self.client()
        self.store().update("sdk-nosession-1", engine="fixture", core_version="2", state="stopped", exit="130",
                            dir=str(self.work.resolve()), parent="sdk-tests")
        handle = client.attach("sdk-nosession-1")
        with self.assertRaises(sdk.ResumeError) as caught:
            client.resume(handle)
        self.assertIn("no saved session id", str(caught.exception))
        self.assertEqual(self.store().read("sdk-nosession-1").get("state"), "stopped",
                         "a refused resume changes nothing")

    # ---------- results / honesty ----------

    def test_compact_result_is_honest_about_baseline_and_scope(self):
        client = self.client()
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.work, name="sdk-result-1",
                              owns="partial.txt", env={"FIXTURE_DELAY": "0"})
        result = handle.wait(timeout=30)
        self.assertEqual(result.state, "done")
        self.assertTrue(result.baseline_known)
        self.assertFalse(result.baseline_partial)
        self.assertIn("partial.txt", [entry["path"] for entry in result.changed_files])
        self.assertEqual(set(result.changed_files[0]), {"path", "status", "insertions", "deletions"})
        self.assertEqual(result.scope_declared, True)
        self.assertEqual(result.scope_enforced, False, "declaration plus diff is not enforcement")
        self.assertIn("NOT", result.scope_note)
        self.assertIn("concurrent writers cannot be distinguished", result.attribution)
        self.assertTrue(result.session.startswith("ses_fixture_"))
        self.assertIsNotNone(result.duration_sec)
        payload = result.to_dict()
        self.assertNotIn("history", payload, "compact result drops the control history")
        self.assertNotIn("overlaps", payload)
        self.assertNotIn("before_sha256", payload["changed_files"][0])

    # ---------- events / waiting ----------

    def test_callback_error_does_not_break_the_monitor(self):
        seen = []

        def broken(event):
            raise ValueError("subscriber exploded on " + event.kind)

        client = self.client(on_event=broken)
        client.subscribe(lambda event: seen.append((event.task, event.kind)))
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.other, name="sdk-events-1",
                              env={"FIXTURE_DELAY": "0"})
        self.assertEqual(handle.wait(timeout=30).state, "done")
        self.until(lambda: any(kind == "settled" for _, kind in seen), seconds=10, message="settled event")
        self.assertTrue(any(task == "sdk-events-1" for task, _ in seen))
        self.assertTrue(client.callback_errors, "the failing subscriber is recorded")
        self.assertTrue(all(entry[0] == "sdk-events-1" and entry[1] == "ValueError" for entry in client.callback_errors))

    def test_wait_any_returns_only_settled_handles(self):
        client = self.client()
        slow = client.spawn("SLOW", engine="fixture", cwd=self.work, name="sdk-any-slow",
                            env=self.blocked_env(self.work))
        quick = client.spawn("QUICK", engine="fixture", cwd=self.other, name="sdk-any-quick",
                             env={"FIXTURE_DELAY": "0"})
        self.until(lambda: (self.work / "turn.1.started").exists(), message="slow turn")
        results = client.wait_any([slow, quick], timeout=40)
        settled = {result.task for result in results}
        self.assertIn("sdk-any-quick", settled)
        self.assertNotIn("sdk-any-slow", settled)
        self.assertEqual(quick.state, "done")
        self.release(self.work, 1)
        self.assertEqual(slow.wait(timeout=40).state, "done")

    def test_wait_timeout_is_bounded_and_leaves_the_task_running(self):
        client = self.client()
        handle = client.spawn("SLOW", engine="fixture", cwd=self.work, name="sdk-timeout-1",
                              env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists(), message="slow turn")
        started = time.monotonic()
        with self.assertRaises(sdk.WaitTimeout):
            client.wait(handle, timeout=0.75)
        self.assertLess(time.monotonic() - started, 8)
        self.assertIn(client.status(handle).state, ("running", "idle"))
        self.release(self.work, 1)
        self.assertEqual(handle.wait(timeout=40).state, "done")

    # ---------- cleanup / ownership ----------

    def test_context_manager_stops_owned_workers_but_keeps_results(self):
        handle = None
        client = self.client()
        with client:
            handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.work, name="sdk-clean-1",
                                  env=self.blocked_env(self.work))
            self.until(lambda: (self.work / "turn.1.started").exists(), message="first turn")
            child = client._child_for("sdk-clean-1")
        self.assertFalse(child.alive(), "close() leaves no orphan child")
        meta = self.store().read("sdk-clean-1")
        self.assertEqual(meta.get("state"), "stopped")
        self.assertTrue(self.store().path("sdk-clean-1", ".meta").is_file(), "accepted results are retained")
        self.assertTrue(self.store().path("sdk-clean-1", ".stop").is_file())
        self.assertEqual(client.close().stopped, ("sdk-clean-1",))
        with self.assertRaises(sdk.ClientClosed):
            client.spawn("AFTER CLOSE", engine="fixture", cwd=self.work, name="sdk-closed-1")

    def test_detached_opt_in_is_tracked_but_not_stopped_on_close(self):
        client = self.client()
        live = client.spawn("LIVE", engine="fixture", cwd=self.work, name="sdk-detach-live",
                            env=self.blocked_env(self.work))
        detached = client.spawn("DETACHED", engine="fixture", cwd=self.other, name="sdk-detach-off",
                                env=self.blocked_env(self.other), detach=True)
        self.until(lambda: (self.work / "turn.1.started").exists(), message="live turn")
        self.until(lambda: (self.other / "turn.1.started").exists(), message="detached turn")
        report = client.close()
        self.assertEqual(report.stopped, ("sdk-detach-live",))
        self.assertEqual(report.detached, ("sdk-detach-off",))
        self.assertIsNotNone(client.handle("sdk-detach-off"), "detached handles stay tracked and retained")
        self.assertIn(detached.status().state, ("running", "idle"))
        self.release(self.other, 1)
        self.assertEqual(detached.wait(timeout=40).state, "done")

    def test_close_without_stop_detaches_every_live_worker(self):
        client = self.client()
        client.spawn("LIVE", engine="fixture", cwd=self.work, name="sdk-detach-all",
                     env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists(), message="live turn")
        report = client.close(detach=True)
        self.assertEqual(report.detached, ("sdk-detach-all",))
        self.assertEqual(report.stopped, ())
        client.handle("sdk-detach-all").stop()
        self.release(self.work, 1)

    def test_foreign_parent_task_is_not_controlled(self):
        client = self.client()
        self.store().update("sdk-foreign-1", engine="fixture", state="done", dir=str(self.work.resolve()),
                            parent="someone-else")
        with self.assertRaises(sdk.NotOwnedError):
            client.status("sdk-foreign-1")
        with self.assertRaises(sdk.NotOwnedError):
            client.attach("sdk-foreign-1")
        adopted = client.attach("sdk-foreign-1", adopt=True)
        self.assertEqual(adopted.task, "sdk-foreign-1")

    # ---------- source frame contracts ----------

    def test_module_source_has_no_global_mutation_or_shell(self):
        source = inspect.getsource(sdk)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)
        for pattern in (r"os\.environ\[[^\]]*\]\s*=", r"os\.environ\.update", r"os\.environ\.pop",
                        r"os\.environ\.setdefault", r"putenv", r"sys\.stdout\s*=", r"sys\.stderr\s*=",
                        r"sys\.stdin\s*=", r"contextlib\.redirect_stdout"):
            self.assertIsNone(re.search(pattern, source), "global state mutation: " + pattern)
        launches = self.subprocess_calls(source)
        self.assertGreaterEqual(len(launches), 1, "central tracked launcher")
        self.assertIn("self._launch(", inspect.getsource(sdk.AgentClient._run_control),
                      "controls must use the same tracked launcher")
        for text in launches:
            self.assertIn("launch_kwargs(", text, "every launch must be hidden: " + text[:60])

    def subprocess_calls(self, source):
        """Balanced-paren extraction of every subprocess call in the module source."""
        calls = []
        for match in re.finditer(r"subprocess\.(?:Popen|run)\(", source):
            depth, index = 1, match.end()
            while index < len(source):
                if source[index] == "(":
                    depth += 1
                elif source[index] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            calls.append(source[match.start():index])
        return calls

    def test_launch_kwargs_are_hidden_on_windows(self):
        kwargs = sdk.launch_kwargs()
        if os.name == "nt":
            import subprocess as sp
            self.assertTrue(kwargs.get("creationflags", 0) & sp.CREATE_NO_WINDOW)
            self.assertTrue(kwargs.get("startupinfo") is not None)
        else:
            self.assertEqual(kwargs, {})

    def test_no_env_or_stdout_leak_into_the_host_process(self):
        before_env = dict(os.environ)
        before_stdout, before_stderr = sys.stdout, sys.stderr
        client = self.client()
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.other, name="sdk-leak-1",
                              env={"FIXTURE_DELAY": "0"})
        handle.wait(timeout=30)
        client.close()
        self.assertEqual(dict(os.environ), before_env, "the SDK never mutates os.environ")
        self.assertIs(sys.stdout, before_stdout)
        self.assertIs(sys.stderr, before_stderr)

    def test_capability_contract_reports_integrated_receipts_and_events(self):
        contract = sdk.capabilities()
        self.assertIn("UUID", contract["receipt_id"])
        self.assertFalse(contract["os_sandbox"])
        self.assertFalse(contract["hosted_chat_injection"])
        self.assertIn("Phase 3", contract["events"])
        self.assertIn("Phase 5", contract["write_scope"])
        contract["receipt_id"] = "tampered"
        self.assertIn("UUID", sdk.capabilities()["receipt_id"], "capabilities() returns a copy")

    def test_closed_client_reports_are_idempotent(self):
        client = self.client()
        first = client.close()
        second = client.close()
        self.assertIs(first, second)

    # ---------- root-reported defect regressions ----------

    def test_forged_adoption_and_handle_client_binding_cannot_grant_control(self):
        client = self.client()
        other = self.client(parent="other-owner")
        self.store().update("forged", state="done", parent="other-owner", dir=str(self.work))
        adopted = client.attach("forged", adopt=True)
        fake = sdk.TaskHandle(task="forged", client=client, adopted=True, adopted_parent="other-owner")
        for operation in (client.status, client.result, client.stop,
                          lambda ref: client.send(ref, "X"), lambda ref: client.resume(ref),
                          lambda ref: client.wait(ref, timeout=0)):
            with self.assertRaises(sdk.NotOwnedError):
                operation(fake)
        alien = other.attach("forged")
        alien.client = client  # Public client pointer is no registration provenance.
        with self.assertRaises(sdk.NotOwnedError):
            client.status(alien)
        adopted.owned, adopted.adopted = True, False
        self.assertIn("forged", client.close().retained)

    def test_reparented_owned_child_is_revalidated_and_spared_by_close(self):
        client = self.client()
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.work, name="reparented",
                              env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists())
        child = client._child_for(handle.task)
        self.store().update(handle.task, parent="new-owner", sdk_client=client.client_id)
        handle.adopted, handle.adopted_parent, handle.parent = True, "new-owner", "new-owner"
        for operation in (client.status, client.result, client.stop,
                          lambda ref: client.send(ref, "X"), lambda ref: client.resume(ref),
                          lambda ref: client.wait(ref, timeout=0)):
            with self.assertRaises(sdk.NotOwnedError):
                operation(handle)
        report = client.close()
        self.assertIn(handle.task, report.retained)
        self.assertEqual(report.stopped, ())
        self.assertTrue(child.alive(), "close cannot kill its old child after reparenting")
        self.release(self.work, 1)
        self.until(lambda: child.reaped, message="foreign child exits and resources are released")
        self.assertTrue(child.process.stdout.closed)

    def test_adopted_parent_changes_cannot_be_authorized_by_public_fields(self):
        client = self.client()
        self.store().update("adopt-change", parent="first", state="done", dir=str(self.work))
        handle = client.attach("adopt-change", adopt=True)
        self.store().update(handle.task, parent="second")
        handle.adopted_parent = "second"
        with self.assertRaises(sdk.NotOwnedError):
            client.status(handle)
        self.assertIn(handle.task, client.close().retained)

    def test_close_spares_changed_owner_generation_even_with_same_parent(self):
        client = self.client()
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.work, name="same-parent-new-owner",
                              env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists())
        child = client._child_for(handle.task)
        self.store().update(handle.task, generation="foreign-generation")
        handle.generation = "foreign-generation"  # Public snapshots cannot grant ownership.
        with self.assertRaises(sdk.NotOwnedError):
            client.stop(handle)
        self.assertIn(handle.task, client.close().retained)
        self.assertTrue(child.alive())
        self.release(self.work, 1)
        self.until(lambda: child.reaped, message="changed owner resources released after natural exit")

    def resource_owner(self):
        """Real hidden owner and descendant, with publication before prompt consumption."""
        entry = self.base / "resource-owner.py"
        entry.write_text("\n".join([
            "import os, sys, time, subprocess",
            "from pathlib import Path",
            "sys.path.insert(0, %r)" % str(ROOT),
            "from neoxider_agents.state import Store, atomic_write",
            "from neoxider_agents.process import pid_stamp, hidden_kwargs",
            "args = sys.argv[1:]",
            "name = args[args.index('-t') + 1]",
            "work = Path(args[args.index('-C') + 1])",
            "prompt = Path(args[args.index('--prompt-file') + 1])",
            "store = Store(os.environ['AGENT_CLI_LOGS'])",
            "deadline = time.monotonic() + 120",
            "store.update(name, core_version='2', parent=args[args.index('-P') + 1],",
            "    pid=os.getpid(), pid_start=pid_stamp(os.getpid()), generation='gen-' + str(os.getpid()),",
            "    state='running', engine='fixture', dir=str(work), session='ses_resource')",
            "print('published', flush=True)",
            "while not (work / 'consume').exists() and time.monotonic() < deadline: time.sleep(0.01)",
            "if time.monotonic() >= deadline: sys.exit(124)",
            "text = prompt.read_text(encoding='utf-8')",
            "atomic_write(store.path(name, '.prompt'), text)",
            "atomic_write(store.path(name, '.answer'), 'retained answer')",
            "(work / 'consumed').write_text(text, encoding='utf-8')",
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'],",
            "    stdin=subprocess.DEVNULL, **hidden_kwargs())",
            "(work / 'child.pid').write_text(str(child.pid))",
            "while not (work / 'finish').exists() and time.monotonic() < deadline: time.sleep(0.01)",
            "store.update(name, state='done', exit=0)",
            "while not (work / 'release').exists() and time.monotonic() < deadline: time.sleep(0.01)",
            "child.wait()",
        ]), encoding="utf-8")
        client = self.client(entry=entry)
        handle = client.spawn("EXACT DEFERRED PROMPT", engine="fixture", cwd=self.work, name="resources")
        return client, handle, client._child_for(handle.task)

    def test_real_resources_reaped_by_fake_clock_300s_and_same_handle_resumes(self):
        client, handle, child = self.resource_owner()
        self.assertTrue(child.prompt_path.exists(), "publication cannot imply prompt consumption")
        client._clock = lambda: 100  # the background reaper shares the fake clock
        self.assertTrue(child.alive())
        client._cleanup_children(now=100)
        client._cleanup_children(now=1000)
        self.assertTrue(child.alive(), "an active owner is never idle-cleaned")
        (self.work / "consume").touch()
        self.until(lambda: (self.work / "child.pid").exists())
        self.assertEqual((self.work / "consumed").read_text(encoding="utf-8"), "EXACT DEFERRED PROMPT")
        descendant = int((self.work / "child.pid").read_text())
        (self.work / "finish").touch()
        self.until(lambda: self.store().read(handle.task).get("state") == "done")
        child.settled_at = None
        client._cleanup_children(now=100)
        client._cleanup_children(now=394)
        self.assertTrue(child.alive())
        client._cleanup_children(now=399)
        self.assertTrue(child.reaped, "completed owner resources released before 300s")
        self.assertFalse(pid_alive(descendant))
        self.assertFalse(child.thread.is_alive())
        self.assertTrue(child.process.stdout.closed)
        self.assertFalse(child.prompt_path.exists())
        self.assertEqual(client._children, [])
        if os.name == "nt":
            self.assertTrue(child.process._handle.closed, "native process handle is closed")
        self.assertEqual(handle.result().answer, "retained answer")
        client.entry = str(ROOT / "agent.py")
        generations = {handle.generation}
        for index in range(3):
            self.assertIs(client.resume(handle, "FOLLOW %s" % index), handle)
            self.assertFalse(handle._settled_event.is_set())
            self.assertEqual(handle.wait(timeout=30).session, "ses_resource")
            generations.add(handle.status().generation)
            self.until(lambda: not client._children, message="repeated resume resources reaped")
        self.assertEqual(len(generations), 4)
        self.assertIs(client.handle(handle.task), handle)

    def test_cleanup_spares_reused_pid_generation_and_foreign_owner(self):
        client, handle, child = self.resource_owner()
        (self.work / "consume").touch()
        self.until(lambda: (self.work / "child.pid").exists())
        (self.work / "finish").touch()
        self.until(lambda: self.store().read(handle.task).get("state") == "done")
        original = self.store().read(handle.task)
        for updates in ({"generation": "different"}, {"parent": "foreign"}, {"pid_start": "reused"}):
            self.store().update(handle.task, **updates)
            child.settled_at = 0
            client._cleanup_children(now=300)
            self.assertTrue(child.alive())
            self.store().update(handle.task, **original)
        child.settled_at = 0
        with mock.patch("neoxider_agents.process.pid_stamp", return_value="reused"), \
                mock.patch("neoxider_agents.process.kill_tree") as kill:
            client._cleanup_children(now=300)
            kill.assert_not_called()
        child.settled_at = 0
        client._cleanup_children(now=300)
        self.assertTrue(child.reaped)

    def test_normal_completion_immediately_reaps_provider_pumps_and_handles(self):
        client = self.client()
        handle = client.spawn("DONE", engine="fixture", cwd=self.work, name="immediate-clean",
                              env=dict(self.blocked_env(self.work), FIXTURE_CHILD="1"))
        self.until(lambda: (self.work / "child.pid").exists())
        child = client._child_for(handle.task)
        descendant = int((self.work / "child.pid").read_text())
        provider = self.store().read(handle.task)
        started = time.monotonic()
        self.release(self.work, 1)
        self.assertEqual(handle.wait(timeout=30).state, "done")
        self.until(lambda: child.reaped, seconds=10, message="immediate reaping")
        self.assertLess(time.monotonic() - started, 300)
        self.assertFalse(pid_alive(descendant))
        self.assertFalse(pid_alive(provider["provider_pid"], provider["provider_start"]))
        self.assertFalse(child.thread.is_alive())
        self.assertTrue(child.process.stdout.closed)
        self.assertEqual(client._children, [])
        self.assertTrue(self.store().path(handle.task, ".meta").is_file())

    def test_blocked_subscriber_cannot_delay_resource_reaping(self):
        entered, release = threading.Event(), threading.Event()

        def callback(event):
            if event.kind == "settled":
                entered.set()
                release.wait(20)

        client = self.client(on_event=callback)
        handle = client.spawn("DONE", engine="fixture", cwd=self.work, name="blocked-subscriber",
                              env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists())
        child = client._child_for(handle.task)
        self.release(self.work, 1)
        try:
            self.assertTrue(entered.wait(10))
            self.until(lambda: child.reaped, seconds=5, message="cleanup independent of subscriber")
            self.assertFalse(child.thread.is_alive())
            self.assertEqual(handle.result().state, "done")
        finally:
            release.set()

    def test_explicit_adoption_with_same_parent_still_cannot_autostop(self):
        owner = self.client()
        adopter = self.client()
        handle = owner.spawn("ORIGINAL", engine="fixture", cwd=self.work, name="same-parent-adopt",
                             env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists())
        adopted = adopter.attach(handle.task, adopt=True)
        adopted.owned, adopted.adopted = True, False
        self.assertIn(handle.task, adopter.close().retained)
        self.assertTrue(owner.status(handle).live)
        self.release(self.work, 1)
        self.assertEqual(handle.wait(timeout=30).state, "done")

    def test_close_releases_settled_lingering_owner_before_returning(self):
        client, handle, child = self.resource_owner()
        (self.work / "consume").touch()
        self.until(lambda: (self.work / "child.pid").exists())
        descendant = int((self.work / "child.pid").read_text())
        (self.work / "finish").touch()
        self.until(lambda: self.store().read(handle.task).get("state") == "done")
        self.assertIn(handle.task, client.close().retained)
        self.assertTrue(child.reaped)
        self.assertFalse(pid_alive(descendant))
        self.assertFalse(client._reaper.is_alive())
        self.assertFalse(client._monitor.is_alive())
        self.assertEqual(handle.result().answer, "retained answer")

    def test_concurrent_close_returns_one_idempotent_report(self):
        client = self.client()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(pool.map(lambda _: client.close(), range(2)))
        self.assertIs(reports[0], reports[1])
        self.assertFalse(client._monitor.is_alive())
        self.assertFalse(client._reaper.is_alive())

    def close_event(self, client, name="close-event", kind="born"):
        from neoxider_agents.events import EventStore
        self.store().update(name, state="done", exit="0", parent=client.parent,
                            generation="close-generation", engine="fixture", session="saved-session")
        record = EventStore(self.logs).emit(name, generation="close-generation", parent=client.parent,
                                           kind=kind, data={"state": "done", "exit_code": 0})
        client.attach(name)
        return record

    def test_close_callback_reports_current_consumer_and_finishes_ack(self):
        from neoxider_agents.events import EventStore
        client = self.client()
        returned, reports = threading.Event(), []

        def callback(event):
            reports.append(client.close())
            returned.set()

        client.subscribe(callback, consumer="self-close")
        record = self.close_event(client)
        thread = client._subscriptions[0][2]
        self.assertTrue(returned.wait(5), "close inside callback must not self-join")
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(reports[0].pending_consumers, ("self-close",))
        self.assertFalse(reports[0].in_progress)
        self.assertEqual(EventStore(self.logs).cursor(record["task"], "self-close"), record["seq"])
        self.assertIs(client.close(), reports[0], "reports are snapshots, not silently refreshed")

    def test_callback_close_during_external_join_cannot_wait_on_close_lock(self):
        client = self.client()
        entered, release, joining, callback_returned = [threading.Event() for _ in range(4)]
        reports, outer = [], []

        def callback(event):
            entered.set()
            release.wait(5)
            reports.append(client.close())
            callback_returned.set()

        client.subscribe(callback, consumer="concurrent-callback-close")
        self.close_event(client)
        thread = client._subscriptions[0][2]
        original_join = thread.join

        def join(timeout=None):
            joining.set()
            return original_join(timeout)

        closer = threading.Thread(target=lambda: outer.append(client.close()))
        try:
            self.assertTrue(entered.wait(5))
            with mock.patch.object(thread, "join", side_effect=join):
                closer.start()
                self.assertTrue(joining.wait(5))
                release.set()
                self.assertTrue(callback_returned.wait(1), "callback must bypass the closer's lock")
                closer.join(5)
            self.assertFalse(closer.is_alive())
            self.assertFalse(thread.is_alive())
            self.assertTrue(reports[0].in_progress)
            self.assertEqual(reports[0].pending_consumers, ("concurrent-callback-close",))
            self.assertEqual(outer[0].pending_consumers, ())
            self.assertIs(client.close(), outer[0])
        finally:
            release.set()
            if closer.ident is not None:
                closer.join(5)
            original_join(5)

    def test_close_uses_one_shutdown_deadline_and_reports_uncooperative_consumers(self):
        from neoxider_agents.events import EventStore
        client = self.client()
        release = threading.Event()
        entered = [threading.Event() for _ in range(3)]
        for index, flag in enumerate(entered):
            def callback(event, flag=flag):
                flag.set()
                release.wait(5)
            client.subscribe(callback, consumer="blocked-%s" % index)
        record = self.close_event(client)
        try:
            self.assertTrue(all(flag.wait(5) for flag in entered))
            started = time.monotonic()
            report = client.close(shutdown_timeout=0.2)
            self.assertLess(time.monotonic() - started, 0.5, "budget is shared, not per consumer")
            self.assertEqual(report.pending_consumers, ("blocked-0", "blocked-1", "blocked-2"))
            self.assertFalse(report.in_progress)
            self.assertEqual(report.to_dict()["pending_consumers"], report.pending_consumers)
            self.assertTrue(all(thread.is_alive() for _, _, thread in client._subscriptions))
        finally:
            release.set()
            for _, _, thread in client._subscriptions:
                thread.join(5)
        self.assertTrue(all(not thread.is_alive() for _, _, thread in client._subscriptions))
        for consumer, _, _ in client._subscriptions:
            self.assertEqual(EventStore(self.logs).cursor(record["task"], consumer), record["seq"])

    def test_subscribe_after_close_is_refused_for_both_callback_contracts(self):
        client = self.client()
        client.close()
        for consumer in (None, "closed-consumer"):
            with self.subTest(consumer=consumer), self.assertRaises(sdk.ClientClosed):
                client.subscribe(lambda event: None, consumer=consumer)
        self.assertEqual(client._subscriptions, [])
        self.assertEqual(client._callbacks, [])

    def test_failed_subscriber_start_leaves_no_unstarted_thread_for_close(self):
        client = self.client()
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start thread")):
            with self.assertRaisesRegex(RuntimeError, "cannot start thread"):
                client.subscribe(lambda event: None, consumer="failed-start")
        self.assertEqual(client._subscriptions, [])
        self.assertEqual(client.close().pending_consumers, ())

    def test_legacy_monitor_callback_close_reports_self_without_deadlock(self):
        client = self.client()
        returned, reports = threading.Event(), []

        def callback(event):
            if event.kind == "settled":
                reports.append(client.close())
                returned.set()

        client.subscribe(callback)
        self.close_event(client, kind="terminal")
        self.assertTrue(returned.wait(5))
        client._monitor.join(5)
        self.assertFalse(client._monitor.is_alive())
        self.assertFalse(client._reaper.is_alive())
        self.assertEqual(reports[0].pending_consumers, ())
        self.assertEqual(reports[0].pending_threads, ("neoxider-sdk-monitor",))
        self.assertFalse(reports[0].in_progress)

    def test_close_shutdown_timeout_validation_does_not_close_client(self):
        client = self.client()
        for timeout in (None, True, -1, float("inf"), float("nan"), "2"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                client.close(shutdown_timeout=timeout)
            self.assertFalse(client._closed)
        self.assertEqual(client.close(shutdown_timeout=0).pending_consumers, ())

    def test_handle_to_dict_copies_no_lock_or_client(self):
        client = self.client()
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.other, name="sdk-dict-1",
                              env={"FIXTURE_DELAY": "0"})
        payload = handle.to_dict()
        self.assertEqual(payload["task"], "sdk-dict-1")
        self.assertEqual(payload["client_id"], client.client_id)
        self.assertNotIn("client", payload)
        self.assertNotIn("_settled_event", payload)
        self.assertIsInstance(payload, dict)
        json.dumps(payload)
        self.assertFalse(payload["model_substituted"])
        self.until(lambda: handle.state == "done", message="settle")
        self.assertEqual(handle.wait(timeout=30).state, "done")
        self.assertEqual(handle.to_dict()["state"], "done")

    def test_foreign_handle_cannot_be_controlled_or_closed(self):
        owner = self.client()
        stranger = self.client()
        handle = owner.spawn("ORIGINAL", engine="fixture", cwd=self.work, name="sdk-foreign-handle",
                             env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists(), message="first turn")
        with self.assertRaises(sdk.NotOwnedError):
            stranger.status(handle)
        with self.assertRaises(sdk.NotOwnedError):
            stranger.stop(handle)
        with self.assertRaises(sdk.NotOwnedError):
            stranger.result(handle)
        with self.assertRaises(sdk.NotOwnedError):
            stranger.wait_any([handle], timeout=1)
        report = stranger.close()
        self.assertEqual(report.stopped, ())
        self.assertIn(owner.status(handle).state, ("running", "idle"), "stranger.close() must not stop it")
        self.release(self.work, 1)
        self.assertEqual(handle.wait(timeout=30).state, "done")

    def test_adopt_records_provenance_and_is_never_stopped_on_close(self):
        client = self.client()
        other = self.client(parent="other-owner")
        handle = other.spawn("ORIGINAL", engine="fixture", cwd=self.work, name="sdk-adopt-1",
                             env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists(), message="first turn")
        with self.assertRaises(sdk.NotOwnedError):
            client.attach("sdk-adopt-1")
        adopted = client.attach("sdk-adopt-1", adopt=True)
        self.assertTrue(adopted.adopted)
        self.assertFalse(adopted.owned, "an adopted task is not owned and is never auto-stopped")
        self.assertEqual(adopted.adopted_parent, "other-owner")
        self.assertEqual(client.status(adopted).task, "sdk-adopt-1")
        report = client.close()
        self.assertIn("sdk-adopt-1", report.retained)
        self.assertEqual(report.stopped, ())
        self.assertEqual(self.store().read("sdk-adopt-1").get("state"), "running")
        self.assertTrue(other.status(handle).live)
        self.release(self.work, 1)
        self.assertEqual(handle.wait(timeout=30).state, "done")

    def test_concurrent_sends_deliver_each_payload_exactly_once_in_order(self):
        client = self.client()
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.work, name="sdk-order-1",
                              env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists(), message="first turn")
        payloads = ["FIRST PAYLOAD " + "a" * 4096, "SECOND PAYLOAD " + "b" * 4096]
        barrier = threading.Barrier(len(payloads))
        initial_prompt = client._child_for(handle.task).prompt_path

        receipts, errors = [], []

        def send(text):
            try:
                barrier.wait(10)
                receipts.append(client.send(handle, text))
            except BaseException as error:  # pragma: no cover - failure detail only
                errors.append(repr(error))

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(payloads)) as pool:
            for future in [pool.submit(send, text) for text in payloads]:
                future.result()
        self.assertEqual(errors, [])
        self.assertEqual(len({receipt.receipt_id for receipt in receipts}), 2)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{32}", receipt.receipt_id) for receipt in receipts))
        self.assertEqual(sorted(receipt.sequence for receipt in receipts), [1, 2])
        self.assertTrue(all(receipt.accepted and receipt.queued for receipt in receipts))
        inbox = {path.name: path.read_text(encoding="utf-8") for path in self.store().inbox("sdk-order-1")}
        self.assertEqual(sorted(inbox.values()), sorted(payloads), "each payload stored exactly once")
        self.assertEqual({row["id"] for row in self.store().receipts(handle.task)}, {receipt.receipt_id for receipt in receipts})
        leftovers = [path.name for path in self.logs.glob("sdk-order-1.sdk-*.msg")]
        self.assertEqual(leftovers, [initial_prompt.name], "only the active owner's prompt remains")
        self.release(self.work, 1)
        self.assertEqual(handle.wait(timeout=40).state, "done")
        prompt = (self.work / "turn.2.prompt").read_text(encoding="utf-8")
        self.assertEqual(prompt.count(payloads[0]), 1)
        self.assertEqual(prompt.count(payloads[1]), 1)
        # Thread scheduling is unspecified. Persisted sequence determines delivery order.
        ordered = [inbox[key] for key in sorted(inbox)]
        self.assertLess(prompt.index(ordered[0]), prompt.index(ordered[1]), "inbox order is preserved")
        self.assertTrue(all(client.receipt(handle, receipt.receipt_id).delivered for receipt in receipts))
        self.until(lambda: not list(self.logs.glob("sdk-order-1.sdk-*.msg")), message="owner prompt cleanup")

    def test_two_failing_callbacks_never_recurse_or_repeat(self):
        def first(event):
            raise ValueError("first subscriber failed")

        def second(event):
            raise KeyError("second subscriber failed")

        seen = []
        client = self.client(on_event=first)
        client.subscribe(second)
        client.subscribe(lambda event: seen.append(event.kind))
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.other, name="sdk-cb-1",
                              env={"FIXTURE_DELAY": "0"})
        self.assertEqual(handle.wait(timeout=30).state, "done")
        self.until(lambda: "callback_error" in seen, seconds=10, message="callback_error notice")
        self.assertEqual(seen.count("callback_error"), 1, "one notice per event, not one per failure")
        self.assertEqual(client._monitor.is_alive(), True)
        kinds = sorted(entry[1] for entry in client.callback_errors)
        self.assertEqual(kinds, ["KeyError", "ValueError"])
        self.assertLessEqual(len(client.callback_errors), 64, "the error queue is bounded")

    def test_wait_timeout_zero_is_immediate_and_invalid_values_are_rejected(self):
        client = self.client()
        handle = client.spawn("SLOW", engine="fixture", cwd=self.work, name="sdk-zero-1",
                              env=self.blocked_env(self.work))
        self.until(lambda: (self.work / "turn.1.started").exists(), message="first turn")
        started = time.monotonic()
        with self.assertRaises(sdk.WaitTimeout):
            client.wait(handle, timeout=0)
        self.assertLess(time.monotonic() - started, 5)
        with self.assertRaises(sdk.WaitTimeout):
            client.wait_any([handle], timeout=0)
        for bad in (-1, -0.5, True, False, "5", float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                client.wait(handle, timeout=bad)
            with self.assertRaises(ValueError):
                client.wait_any([handle], timeout=bad)
        with self.assertRaises(sdk.WaitTimeout):
            client.wait_any([handle], timeout=0.05)
        self.release(self.work, 1)
        self.assertEqual(handle.wait(timeout=40).state, "done")

    def test_resume_clears_the_stale_settled_event(self):
        client = self.client()
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.work, name="sdk-event-1",
                              env={"FIXTURE_DELAY": "0"})
        self.assertEqual(handle.wait(timeout=30).state, "done")
        self.assertTrue(handle._settled_event.is_set())
        resumed = client.resume(handle, "CONTINUE PLEASE")
        self.assertFalse(resumed.settled, "a resumed task is live again")
        self.assertFalse(resumed._settled_event.is_set(), "the stale settled event is cleared")
        self.assertEqual(resumed.wait(timeout=40).state, "done")
        self.assertTrue(resumed._settled_event.is_set())

    def test_sdk_binding_sidecar_never_writes_core_metadata(self):
        client = self.client()
        handle = client.spawn("ORIGINAL", engine="fixture", cwd=self.other, name="sdk-meta-1",
                              env={"FIXTURE_DELAY": "0"})
        handle.wait(timeout=30)
        self.until(lambda: not client._children, message="resources reaped")
        store = self.store()

        class ReadOnlyCoreStore(Store):
            def update(self, *args, **kwargs):
                raise AssertionError("SDK must never update core metadata")

        client.store = ReadOnlyCoreStore(self.logs)

        def core_writer():
            # A single core writer: serialization among core writers is a separate owner.
            for step in range(20):
                store.update(handle.task, **{"core_key_%s" % step: "value-%s" % step})

        def sdk_writer():
            for _ in range(20):
                client._write_binding(handle, "sdk-tests", False)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            for future in [pool.submit(core_writer), pool.submit(sdk_writer), pool.submit(sdk_writer)]:
                future.result()
        meta = store.read("sdk-meta-1")
        for step in range(20):
            self.assertEqual(meta.get("core_key_%s" % step), "value-%s" % step)
        self.assertFalse(any(key.startswith("sdk_") for key in meta))
        binding = json.loads(client._binding_path(handle.task).read_text(encoding="utf-8"))
        self.assertEqual(binding["client_id"], client.client_id)
        self.assertEqual(handle.result().state, "done")

    def test_provider_loader_ignores_a_matching_stale_pyc(self):
        root = self.base / "attack-providers"
        package = root / "pystub"
        package.mkdir(parents=True)
        source = ("from neoxider_agents.providers import BaseProvider\n"
                  "MARKER = 'real-source'\n"
                  "class Provider(BaseProvider):\n"
                  "    marker = MARKER\n"
                  "    engine = 'pystub'\n"
                  "    supports_resume = True\n"
                  "    def command(self, model, effort, cwd, session='', chat_only=False):\n"
                  "        return ['true'], {}\n")
        provider_py = package / "provider.py"
        provider_py.write_text(source, encoding="utf-8")
        stale = ("MARKER = 'stale-pyc'\n"
                 "class Provider:\n"
                 "    marker = MARKER\n"
                 "    engine = 'pystub'\n"
                 "    supports_resume = False\n")
        cache = package / "__pycache__"
        cache.mkdir()
        code = compile(stale, str(provider_py), "exec")
        import importlib.util
        import marshal
        import struct
        header = importlib.util.MAGIC_NUMBER + struct.pack("<III", 0, int(provider_py.stat().st_mtime),
                                                            len(provider_py.read_bytes()))
        (cache / ("provider.%s.pyc" % sys.implementation.cache_tag)).write_bytes(header + marshal.dumps(code))
        # The attack is real for an ordinary loader: a header-matching stale .pyc is executed.
        spec = importlib.util.spec_from_file_location("pystub_probe", str(provider_py))
        probed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probed)
        self.assertEqual(probed.MARKER, "stale-pyc", "the fixture pyc really is header-matching")
        client = self.client(env=dict(self.env, AGENT_PROVIDER_DIR=root.as_posix()))
        before = dict(sys.modules)
        loaded = client._provider("pystub")
        self.assertEqual(loaded.marker, "real-source", "the SDK loads the captured source, not the pyc")
        self.assertTrue(loaded.supports_resume)
        self.assertEqual(dict(sys.modules), before, "the loader never registers or replaces modules")

    def test_module_source_never_touches_sys_modules_or_copies_locks(self):
        source = inspect.getsource(sdk)
        self.assertNotIn("sys.modules", source)
        self.assertNotIn("asdict(self)", inspect.getsource(sdk.TaskHandle))
        self.assertNotIn("copy.deepcopy", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
