"""Discovery/probe boundaries; real bounded subprocesses, no provider network calls."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents import model_catalog as catalog
from neoxider_agents.cli import parse, dispatch
from neoxider_agents.state import Store


def record(model="opencode/test", variants=None):
    data = {"name": "Test model", "variants": variants if variants is not None else {},
            "capabilities": {"reasoning": True, "toolcall": True, "input": {"image": False, "text": True}},
            "limit": {"context": 16000}, "cost": {"input": 0, "output": 0},
            "headers": {"Authorization": "secret-not-for-output"},
            "options": {"apiKey": "secret-not-for-output"}, "api": {"url": "secret-not-for-output"}}
    return model + "\n" + json.dumps(data) + "\n"


class CatalogueTests(unittest.TestCase):
    def test_multiple_ansi_records_and_allowlist(self):
        models = catalog.parse_verbose("warning\n\x1b[32m" + record(variants={"high": {}, "max": {}}) +
                                       "\x1b[0m" + record("other/second"))
        self.assertEqual([item["id"] for item in models], ["opencode/test", "other/second"])
        self.assertEqual(models[0]["efforts"], ["high", "max"])
        self.assertEqual(models[0]["effort_source"], "cli-advertised")
        self.assertTrue(models[0]["free_advertised"])
        self.assertNotIn("secret-not-for-output", json.dumps(models))
        self.assertEqual(models[1]["effort_source"], "unknown")
        self.assertEqual(models[1]["availability"], "unprobed")
        self.assertIsNone(models[1]["quota"])

    def test_malformed_empty_and_oversized_are_not_fake_models(self):
        for text in ("nothing", "opencode/test\n{invalid", "opencode/test\n[]", "x" * (catalog.MAX_BYTES + 1)):
            with self.subTest(text=text[:35]), self.assertRaises(ValueError):
                catalog.parse_verbose(text)

    def test_invalid_numeric_fields_are_unknown(self):
        model = catalog.normalize("opencode/test", {"cost": {"input": True, "output": float("nan")},
                                                   "limit": {"context": 10 ** 1000}, "variants": []})
        self.assertEqual(model["cost"], {})
        self.assertEqual(model["limits"], {})
        self.assertIsNone(model["free_advertised"])
        self.assertEqual(model["efforts"], [])

    def test_discovery_uses_exact_provider_refresh_and_no_run(self):
        with patch.object(catalog, "executable", return_value=["opencode.exe"]), \
                patch.object(catalog, "bounded_capture", return_value=record()) as capture:
            payload = catalog.catalogue("opencode", "opencode", True)
        self.assertEqual(capture.call_args[0][0], ["opencode.exe", "models", "opencode", "--verbose", "--refresh"])
        self.assertEqual(payload["models"][0]["id"], "opencode/test")

    def test_native_capture_success_timeout_output_limit_and_private_failure(self):
        self.assertEqual(catalog.bounded_capture([sys.executable, "-c", "print('OK')"], 3).strip(), "OK")
        with self.assertRaises(TimeoutError):
            catalog.bounded_capture([sys.executable, "-c", "import time; time.sleep(5)"], .2)
        with patch.object(catalog, "MAX_BYTES", 4096), self.assertRaisesRegex(RuntimeError, "output exceeded"):
            catalog.bounded_capture([sys.executable, "-c", "print('x'*20000)"], 3)
        with self.assertRaises(RuntimeError) as error:
            catalog.bounded_capture([sys.executable, "-c", "print('apiKey=private'); exit(1)"], 3)
        self.assertNotIn("private", str(error.exception))

    def test_cli_json_filters_alias_and_rejects_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            opts, args = parse("models", ["-m", "free", "--json"])
            payload = {"engine": "opencode", "models": [{"id": "opencode/deepseek-v4.1-flash-free"}], "note": "fixture"}
            with patch.object(catalog, "catalogue", return_value=payload), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(dispatch("models", opts, args, store), 0)
            self.assertEqual(json.loads(output.getvalue())["models"][0]["id"], "opencode/deepseek-v4.1-flash-free")
            with patch.object(catalog, "catalogue", return_value=payload), self.assertRaisesRegex(ValueError, "not found"):
                dispatch("models", {"model": "missing"}, [], store)

    def test_gui_model_ids_compatible_details_unknown_on_failure(self):
        spec = importlib.util.spec_from_file_location("catalog_gui_fixture", ROOT / "gui.py")
        gui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gui)
        payload = {"engine": "opencode", "source": "cli-advertised", "models": catalog.parse_verbose(record()), "note": "fixture"}
        with patch.object(catalog, "catalogue", return_value=payload):
            self.assertEqual(gui.engine_models("opencode", True), ["opencode/test"])
            self.assertEqual(gui.engine_model_details("opencode")["models"][0]["effort_source"], "unknown")
        with patch.object(catalog, "catalogue", side_effect=TimeoutError("fixture")):
            fallback = gui.engine_model_details("opencode", True)
        self.assertEqual(fallback["discovery_state"], "error")
        self.assertTrue(all(item["effort_source"] == "unknown" for item in fallback["models"]))


class ProbeTests(unittest.TestCase):
    def test_exact_forwarding_tools_disabled_no_retry_env_restored(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            before = {key: os.environ.get(key) for key in ("AGENT_CHAT_ONLY", "AGENT_TIMEOUT_SEC", "AGENT_OPENCODE_RETRIES")}
            def run(actual_store, name, prompt, opts):
                self.assertEqual(prompt, "Reply OK")
                self.assertEqual(opts["model"], "opencode/deepseek-v4.1-flash-free")
                self.assertEqual(opts["effort"], "max")
                self.assertEqual(os.environ["AGENT_CHAT_ONLY"], "1")
                self.assertEqual(os.environ["AGENT_OPENCODE_RETRIES"], "0")
                self.assertEqual(os.environ["AGENT_TIMEOUT_SEC"], "200")
                self.assertEqual(list(Path(opts["dir"]).iterdir()), [])
                actual_store.update(name, resolved_model=opts["model"], effort=opts["effort"], state="done")
                actual_store.path(name, ".answer").write_text("OK", encoding="utf-8")
                return 0
            with patch("neoxider_agents.lifecycle.run", side_effect=run):
                result, code = catalog.run_probe(store, {"model": "free", "effort": "max", "name": "probe-fixture"})
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "ok")
            self.assertIsNone(result["effective_effort"])
            self.assertEqual(before, {key: os.environ.get(key) for key in before})

    def test_preconditions_do_not_launch_or_overwrite_job(self):
        with tempfile.TemporaryDirectory() as folder, patch("neoxider_agents.lifecycle.run") as run:
            store = Store(folder)
            store.update("existing", state="done")
            store.path("reserved", ".reserve").touch()
            for opts in ({}, {"model": "free", "engine": "gemini"}, {"model": "free", "timeout": "0"},
                         {"model": "free", "timeout": "601"}, {"model": "free", "name": "existing"},
                         {"model": "free", "name": "reserved"}):
                with self.subTest(opts=opts), self.assertRaises(ValueError):
                    catalog.run_probe(store, opts)
            run.assert_not_called()
            self.assertEqual(store.read("existing")["state"], "done")

    def test_failure_classes_and_exact_reply(self):
        for code, reason, answer, status in ((124, "", "", "timeout"), (125, "", "", "silent"),
                (126, "HTTP429 rate limit", "", "limited"), (126, "invalid api key", "", "auth-error"),
                (126, "model not found", "", "model-error"), (1, "network failed", "", "provider-error"),
                (0, "", "", "empty"), (0, "", "OK but...", "unexpected-answer"), (0, "", "OK", "ok"),
                (126, "OpenCode's free tier can only be used from within OpenCode", "", "route-rejected"),
                (3, "provider returned an empty answer; inspect the working tree", "", "empty")):
            with self.subTest(status=status):
                self.assertEqual(catalog.probe_status(code, {"reason": reason}, answer), status)

    def test_error_summary_excludes_headers_and_response_body(self):
        reason = json.dumps({"data": {"message": "Route rejected", "responseHeaders": {"secret": "private"},
                                      "responseBody": "private"}})
        self.assertEqual(catalog.probe_reason(reason), "Route rejected")

    def test_standard_profile_is_explicit_not_a_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            def run(actual_store, name, prompt, opts):
                self.assertEqual(os.environ["AGENT_CHAT_ONLY"], "0")
                actual_store.update(name, resolved_model=opts["model"], effort=opts["effort"], state="done")
                actual_store.path(name, ".answer").write_text("OK", encoding="utf-8")
                return 0
            with patch("neoxider_agents.lifecycle.run", side_effect=run) as launch:
                result, code = catalog.run_probe(store, {"model": "opencode/space-bunny-free", "--standard": True})
            self.assertEqual(code, 0)
            self.assertEqual(result["profile"], "standard")
            launch.assert_called_once()

    def test_route_mismatch_is_not_success_and_json_has_no_provider_stderr(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            def run(actual_store, name, prompt, opts):
                print("private diagnostic", file=sys.stderr)
                actual_store.update(name, resolved_model="other/model", effort="low", state="done")
                actual_store.path(name, ".answer").write_text("OK", encoding="utf-8")
                return 0
            with patch("neoxider_agents.lifecycle.run", side_effect=run), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout, \
                    contextlib.redirect_stderr(io.StringIO()) as stderr:
                code = dispatch("probe", {"model": "free", "effort": "xhigh", "--json": True}, [], store)
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "route-mismatch")
            self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
