"""Offline failure trust, cost provenance and source-bound verification contracts."""
from dataclasses import FrozenInstanceError, asdict
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from neoxider_agents.diagnostics import (
    classify_failure, FailureReport, CostFact, CostPolicy, CostGuard, VerificationReceipt,
    compact_result, source_hash,
)

SHA = "a" * 64
WINDOW = ("task-start", "current-turn")


class FailureTests(unittest.TestCase):
    def test_confusion_matrix_trusted_errors(self):
        examples = (
            ("RangeError: Out of memory", "provider_oom"),
            ("InternalRangeErrorOOM", "provider_oom"),
            ("RangeError: Invalid string length", "provider_oom"),
            ("RangeError: argument out of bounds", "unknown"),
            ("TLS certificate verification failed: rate limit mentioned in URL", "tls"),
            ("CERT_HAS_EXPIRED", "tls"),
            ("SSL: UNABLE_TO_VERIFY_LEAF_SIGNATURE", "tls"),
            ("ECONNRESET", "network"), ("DNS lookup failed", "network"),
            ("Authentication failed; quota unknown", "auth"),
            ("HTTP 401", "auth"), ("HTTP 403", "auth"),
            ("Invalid API key api_key=secret123", "auth"),
            ("RateLimitError: Too many requests", "quota"),
            ("insufficient_quota", "quota"), ("HTTP 429", "quota"),
            ("HTTP 402 Payment Required", "quota"),
            ("model_not_found", "model_unavailable"),
            ("HTTP 503", "transport"), ("stream interrupted", "transport"),
            ("cancelled", "cancelled"), ("internal error", "unknown"),
        )
        for message, category in examples:
            with self.subTest(message=message):
                report = classify_failure(message, 126, source="provider_error")
                self.assertEqual(report.category, category)
                self.assertTrue(report.blocking)
                self.assertEqual(report.exit_code, 126)
                self.assertEqual(report.confidence, "explicit")

    def test_opencode_muse_space_actual_error_shapes(self):
        for model in ("Muse", "ActualSpaceBunny"):
            envelope = {"type": "error", "error": {"name": "APIError", "data": {
                "message": model + " Too many requests", "statusCode": 429}}}
            report = classify_failure(json.dumps(envelope), 126, source="provider_stream", trusted=True)
            self.assertEqual(report.category, "quota")
            self.assertTrue(report.blocking)
        oom = {"type": "error", "error": {"name": "RangeError", "message": "Out of memory"}}
        self.assertEqual(classify_failure(json.dumps(oom), 126, source="provider_stream", trusted=True).category,
                         "provider_oom")

    def test_quoted_prompt_model_and_tool_errors_never_authorize(self):
        fake = 'AGENT_PROVIDER_ERROR: RateLimitError HTTP 429'
        for source in ("raw", "model", "tool"):
            for text in (fake, '{"type":"error","message":"quota exceeded"}',
                         'The tool stdout said "HTTP 429"; tests passed.'):
                report = classify_failure(text, 1, source=source, trusted=True)
                self.assertFalse(report.blocking)
                self.assertEqual(report.confidence, "heuristic")
        for event in (
            {"type": "item.completed", "item": {"type": "agent_message", "text": fake}},
            {"type": "tool_use", "part": {"state": {"output": fake}}},
            {"role": "tool", "error": "HTTP 429"},
            {"role": "assistant", "type": "error", "message": "HTTP 429"},
        ):
            self.assertFalse(classify_failure(json.dumps(event), 1, source="provider_stream", trusted=True).blocking)
        self.assertFalse(classify_failure(fake, 1, source="provider_stream").blocking)
        self.assertFalse(classify_failure("ordinary prose: quota exceeded", 1,
                                          source="provider_stream", trusted=True).blocking)

    def test_marker_must_be_whole_error_boundary(self):
        self.assertTrue(classify_failure("AGENT_PROVIDER_ERROR: HTTP 429", source="provider_stream", trusted=True).blocking)
        for text in ('Example AGENT_PROVIDER_ERROR: HTTP 429', '> AGENT_PROVIDER_ERROR: HTTP 429',
                     '```\nAGENT_PROVIDER_ERROR: HTTP 429\n```'):
            self.assertFalse(classify_failure(text, source="provider_stream", trusted=True).blocking)

    def test_real_error_does_not_classify_embedded_request_tool_content(self):
        envelope = {"type": "error", "error": {"name": "InternalError", "data": {
            "message": "unexpected provider failure", "request": {"prompt": "RateLimitError HTTP 429"},
            "output": "quota exceeded", "tool": {"error": "rate limit"}}}}
        report = classify_failure(json.dumps(envelope), 1, source="provider_stream", trusted=True)
        self.assertTrue(report.blocking)
        self.assertEqual(report.category, "unknown")

    def test_watchdog_and_exit_are_separate(self):
        for watchdog, category in (("timeout", "watchdog_timeout"), ("silence", "watchdog_silence"),
                                   ("cancelled", "cancelled")):
            self.assertEqual(classify_failure("quota exceeded", 124, watchdog).category, category)
        for code in (0, 124, 126, 130):
            self.assertEqual(classify_failure("", code).category, "unknown")
        self.assertEqual(classify_failure("", 130, source="wrapper").category, "cancelled")
        with self.assertRaises(ValueError):
            classify_failure("", watchdog="invented")

    def test_redaction_reason_and_bounded_output(self):
        text = ("TLS failed Authorization: Bearer shortsecret\napi_key=key123 password=pass123 "
                "https://user:pass@host/?key=urlsecret " + "Z" * 40)
        report = classify_failure(text, 1, source="provider_error")
        serialized = json.dumps(asdict(report))
        for credential in ("shortsecret", "key123", "pass123", "user:pass", "urlsecret", "Z" * 40):
            self.assertNotIn(credential, serialized)
        self.assertIn("TLS failed", report.reason)
        long = classify_failure("out of memory " + "x " * 100000, source="provider_error")
        self.assertLessEqual(len(long.reason), 4096)
        self.assertTrue(long.reason_truncated)
        url = "https://user:pass123" + "%2F" * 3000 + "@host/"
        self.assertNotIn("pass123", classify_failure(url).reason)


class CostTests(unittest.TestCase):
    def fact(self, amount, kind="provider_reported", window=WINDOW, currency="USD"):
        return CostFact(kind, amount, currency, "offline fixture", window)

    def guard(self, **kwargs):
        return CostGuard(CostPolicy("caller", "task", kwargs.pop("cap", "1.00"),
                                   measurement_window=WINDOW, **kwargs))

    def test_unknown_zero_and_provenance_are_distinct(self):
        unknown = CostFact("provider_reported", provenance="no provider cost", input_tokens=0)
        self.assertIsNone(unknown.amount)
        self.assertEqual(unknown.input_tokens, 0)
        for kind in ("catalog_estimate", "provider_reported", "confirmed_billing"):
            self.assertEqual(self.fact(0, kind).kind, kind)
        estimate = self.guard().evaluate([self.fact(0, "catalog_estimate")])
        self.assertTrue(estimate.stop)
        self.assertIsNone(estimate.known_cost)
        reported = self.guard().evaluate([self.fact(0)])
        self.assertFalse(reported.stop)
        self.assertEqual(reported.known_cost, Decimal(0))
        self.assertEqual(reported.evidence_kind, "provider_reported")

    def test_exact_decimal_edges_no_implicit_cap_raise(self):
        guard = self.guard()
        for amount, stops in (("0.99", False), ("1.00", True), ("1.00000000000000000001", True)):
            self.assertEqual(guard.evaluate([self.fact(amount)]).stop, stops)
            self.assertEqual(guard.policy.cap, Decimal("1.00"))
        self.assertTrue(self.guard(cap=0).evaluate([self.fact(0)]).stop)
        self.assertEqual(guard.evaluate([self.fact("1.00")]).reason, "no_cost_headroom")
        self.assertTrue(self.guard(cap=0).evaluate([self.fact("0.000001")]).stop)
        with self.assertRaises(FrozenInstanceError):
            guard.policy.cap = Decimal(100)

    def test_estimates_never_sum_or_override_real_cost(self):
        decision = self.guard().evaluate([self.fact(10000, "catalog_estimate"), self.fact("0.6"), self.fact("0.6")])
        self.assertFalse(decision.stop)
        self.assertEqual(decision.known_cost, Decimal("0.6"))
        for facts in ([self.fact(2), self.fact(0, "confirmed_billing")],
                      [self.fact(0, "confirmed_billing"), self.fact(2)]):
            self.assertTrue(self.guard().evaluate(facts).stop)

    def test_uncertainty_and_hard_cap_enforcement(self):
        self.assertTrue(self.guard().evaluate([]).stop)
        self.assertFalse(self.guard(uncertainty="allow").evaluate([]).stop)
        self.assertFalse(self.guard(cap=None).evaluate([]).stop)
        hard = self.guard(hard_billing_cap=True)
        self.assertTrue(hard.evaluate([self.fact(0, "confirmed_billing")]).stop)
        self.assertFalse(hard.evaluate([self.fact(0, "confirmed_billing")], hard_cap_enforceable=True).stop)
        self.assertTrue(hard.evaluate([], hard_cap_enforceable=True).stop)
        self.assertTrue(self.guard().evaluate([self.fact(0, currency="EUR")]).uncertain)
        self.assertTrue(self.guard().evaluate([self.fact(0, window=("other", "turn"))]).uncertain)
        self.assertTrue(self.guard().evaluate([self.fact(0, window=None)]).uncertain)

    def test_hard_cap_allow_never_bypasses_unknown_or_unenforceable(self):
        guard = CostGuard(CostPolicy("caller", "task", "0.10", measurement_window=("a", "b"),
                                    hard_billing_cap=True, uncertainty="allow"))
        self.assertTrue(guard.evaluate([], hard_cap_enforceable=False).stop)
        self.assertTrue(guard.evaluate([], hard_cap_enforceable=True).stop)
        reported = CostFact("provider_reported", 0, "USD", "fixture", ("a", "b"))
        self.assertTrue(guard.evaluate([reported], hard_cap_enforceable=False).stop)
        self.assertFalse(guard.evaluate([reported], hard_cap_enforceable=True).stop)
        at_cap = CostFact("confirmed_billing", "0.10", "USD", "fixture", ("a", "b"))
        self.assertTrue(guard.evaluate([at_cap], hard_cap_enforceable=True).stop)
        self.assertEqual(guard.policy.cap, Decimal("0.10"))

    def test_invalid_facts_rejected_and_inputs_not_mutated(self):
        for amount in (-1, "NaN", "Infinity", True, "invalid"):
            with self.assertRaises(ValueError):
                self.fact(amount)
        for kwargs in ({"currency": "usd"}, {"provenance": ""}, {"input_tokens": True},
                       {"measurement_window": ("only",)}, {"measurement_window": "ab"},
                       {"input_tokens": 10 ** 100}):
            params = dict(kind="provider_reported", amount=0, currency="USD", provenance="fixture")
            params.update(kwargs)
            with self.assertRaises(ValueError):
                CostFact(**params)
        window = ["start", "end"]
        fact = CostFact("catalog_estimate", 0, "USD", "api_key=hide123", window)
        window[0] = "changed"
        self.assertEqual(fact.measurement_window, ("start", "end"))
        self.assertNotIn("hide123", fact.provenance)


class VerificationTests(unittest.TestCase):
    def receipt(self, code=0, kind="tests", by="caller", evidence="external_command", result="OK"):
        return VerificationReceipt(("python", "-m", "unittest"), code, result, SHA, by, evidence, kind)

    def test_signed32_and_unsigned_dword_exit_codes_are_preserved(self):
        for code in (None, -(2 ** 31), -1073741502, -1, 0, 7, 2 ** 31 - 1,
                     2 ** 31, 3221225794, 2 ** 32 - 1):
            with self.subTest(code=code):
                failure = FailureReport("unknown", "native exit", "wrapper", "explicit", True, code)
                receipt = self.receipt(code)
                report = compact_result({"state": "error", "exit_code": code},
                                        [receipt], SHA, failure=failure)
                restored = json.loads(json.dumps(report))
                self.assertEqual(restored["exit_code"], code)
                self.assertEqual(restored["failure"]["exit_code"], code)
                self.assertEqual(restored["verification"][0]["exit_code"], code)
                self.assertEqual(restored["tests_status"],
                                 "not_run" if code is None else "passed" if code == 0 else "failed")

    def test_exit_code_types_and_overflow_are_rejected(self):
        for code in (True, False, 0.0, 3221225794.0, float("nan"), float("inf"),
                     "3221225794", {}, -(2 ** 31) - 1, 2 ** 32, 2 ** 64):
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    FailureReport("unknown", "native exit", "wrapper", "explicit", True, code)
                with self.assertRaises(ValueError):
                    self.receipt(code)
                with self.assertRaises(ValueError):
                    classify_failure("", code)
                self.assertIsNone(compact_result({"exit_code": code})["exit_code"])

    def test_native_crash_status_does_not_imply_quota_or_success(self):
        for code in (3221225794, -1073741502):
            with self.subTest(code=code):
                failure = classify_failure("", code, source="wrapper")
                self.assertEqual(failure.category, "unknown")
                self.assertEqual(failure.exit_code, code)
                self.assertFalse(failure.blocking and failure.category == "quota")
                result = {"state": "error", "exit_code": code, "final_answer": "Tests PASS"}
                report = compact_result(result, [self.receipt(code)], SHA, failure=failure)
                self.assertEqual(report["state"], "error")
                self.assertEqual(report["tests_status"], "failed")
                self.assertEqual(result["exit_code"], code)

    @unittest.skipUnless(os.name == "nt", "native DWORD exits require Windows")
    def test_real_windows_dword_subprocess_exit_serialization(self):
        for code in (2 ** 31, 3221225794, 2 ** 32 - 1):
            with self.subTest(code=code):
                script = ("import ctypes; "
                          "exit_process = ctypes.WinDLL('kernel32').ExitProcess; "
                          "exit_process.argtypes = [ctypes.c_uint32]; "
                          "exit_process.restype = None; "
                          "print('controlled native exit fixture', flush=True); "
                          "exit_process(%s)" % code)
                argv = (sys.executable, "-B", "-c", script)
                ran = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
                self.assertEqual(ran.returncode, code, ran.stdout)
                self.assertIn(b"controlled native exit fixture", ran.stdout)
                receipt = VerificationReceipt(argv, ran.returncode, ran.stdout.decode(), SHA,
                                              "caller", "external_command", "tests")
                failure = classify_failure(ran.stdout.decode(), ran.returncode, source="wrapper")
                report = json.loads(json.dumps(compact_result(
                    {"state": "error", "exit_code": ran.returncode}, [receipt], SHA, failure=failure)))
                self.assertEqual(report["exit_code"], code)
                self.assertEqual(report["failure"]["exit_code"], code)
                self.assertEqual(report["failure"]["category"], "unknown")
                self.assertEqual(report["verification"][0]["exit_code"], code)
                self.assertEqual(report["tests_status"], "failed")
                self.assertEqual(report["verification"][0]["result_sha256"],
                                 hashlib.sha256(ran.stdout).hexdigest())

    def test_real_command_exit_and_source_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.py"
            path.write_text("value = 1\n", encoding="utf-8")
            before = source_hash(directory, ["source.py"])
            for code in (0, 7):
                argv = (sys.executable, "-c", "import sys; print('external result'); sys.exit(%s)" % code)
                ran = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
                receipt = VerificationReceipt(argv, ran.returncode, ran.stdout.decode(), before,
                                              "caller", "external_command", "tests")
                self.assertEqual(receipt.status(before), "passed" if code == 0 else "failed")
                self.assertEqual(receipt.exit_code, code)
            path.write_text("value = 2\n", encoding="utf-8")
            after = source_hash(directory, ["source.py"])
            self.assertNotEqual(before, after)
            self.assertEqual(receipt.status(after), "stale")
            with self.assertRaises(ValueError):
                source_hash(directory, ["../outside"])

    def test_source_hash_detects_inflight_growth_and_same_size_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.py"
            real_sha256 = hashlib.sha256
            for mutation in ("grow", "same_size"):
                path.write_bytes(b"original source")
                stamp = path.stat().st_mtime_ns
                altered = []

                class MutatingHash:
                    def __init__(self):
                        self.digest = real_sha256()

                    def update(self, block):
                        self.digest.update(block)
                        if block == b"original source" and not altered:
                            altered.append(True)
                            path.write_bytes(b"modified source" if mutation == "same_size" else b"larger modified source")
                            os.utime(path, ns=(stamp, stamp + 1000000000))

                    def hexdigest(self):
                        return self.digest.hexdigest()

                with patch("neoxider_agents.diagnostics.hashlib.sha256", side_effect=MutatingHash):
                    with self.assertRaisesRegex(ValueError, "changed while hashing"):
                        source_hash(directory, ["source.py"])
                self.assertTrue(altered)
                # A failed hash closes the descriptor, including on Windows.
                path.unlink()

    def test_source_hash_regular_file_and_size_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.py").write_bytes(b"12345")
            for kwargs in ({"max_files": 0}, {"max_bytes": 4}, {"max_file_bytes": 4}):
                with self.assertRaises(ValueError):
                    source_hash(root, ["source.py"], **kwargs)
            source_hash(root, ["source.py"], max_files=1, max_bytes=5, max_file_bytes=5)
            with self.assertRaises(ValueError):
                source_hash(root, ["."])
            with self.assertRaises(ValueError):
                source_hash(root, ["source.py:stream"])
            with self.assertRaises(ValueError):
                source_hash(root, ["source.py"], max_bytes=True)
            with self.assertRaises(ValueError):
                source_hash(root, ("source.py" for unused in range(3)), max_files=2)
            with self.assertRaises(ValueError):
                source_hash(root, ["x" * 4097])
            if hasattr(os, "mkfifo"):
                os.mkfifo(str(root / "pipe"))
                with self.assertRaises(ValueError):
                    source_hash(root, ["pipe"])

    def test_source_hash_rejects_hardlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.py").write_text("source", encoding="utf-8")
            try:
                os.link(str(root / "source.py"), str(root / "alias.py"))
            except OSError as error:
                self.skipTest("filesystem cannot create hardlinks: %s" % error)
            with self.assertRaisesRegex(ValueError, "exactly one link"):
                source_hash(root, ["source.py"])

    def test_source_hash_rejects_observed_reparse_points(self):
        import types
        from neoxider_agents import diagnostics
        fixture = types.SimpleNamespace(st_mode=0o100644, st_file_attributes=0x400, st_nlink=1)
        with patch.object(diagnostics.os, "lstat", return_value=fixture):
            with self.assertRaisesRegex(ValueError, "reparse"):
                diagnostics._source_stat("fixture", regular=True)
        fixture = types.SimpleNamespace(st_mode=0o120777, st_file_attributes=0, st_nlink=1)
        with patch.object(diagnostics.os, "lstat", return_value=fixture):
            with self.assertRaisesRegex(ValueError, "symlink"):
                diagnostics._source_stat("fixture", regular=True)

    def test_source_hash_detects_replacement_before_open_and_closes(self):
        from neoxider_agents import diagnostics
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "source.py"
            path.write_bytes(b"original")
            replacement = root / "replacement.py"
            replacement.write_bytes(b"modified")
            real_open = diagnostics.os.open

            def replacing_open(name, flags):
                os.replace(str(replacement), str(path))
                return real_open(name, flags)

            with patch.object(diagnostics.os, "open", side_effect=replacing_open):
                with self.assertRaisesRegex(ValueError, "changed before hashing"):
                    source_hash(root, ["source.py"])
            path.unlink()

    def test_source_hash_open_flags_and_failure_close(self):
        from neoxider_agents import diagnostics
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.py").write_bytes(b"source")
            real_open, real_close = diagnostics.os.open, diagnostics.os.close
            opened, closed = [], []

            def record_open(name, flags):
                fd = real_open(name, flags)
                opened.append((fd, flags))
                return fd

            def record_close(fd):
                closed.append(fd)
                real_close(fd)

            with patch.object(diagnostics.os, "open", side_effect=record_open):
                source_hash(root, ["source.py"])
            if hasattr(os, "O_NOFOLLOW"):
                self.assertTrue(opened[0][1] & os.O_NOFOLLOW)
            with patch.object(diagnostics.os, "open", side_effect=record_open), \
                    patch.object(diagnostics.os, "fdopen", side_effect=OSError("fixture")), \
                    patch.object(diagnostics.os, "close", side_effect=record_close):
                with self.assertRaises(OSError):
                    source_hash(root, ["source.py"])
            self.assertEqual(closed, [opened[-1][0]])

    def test_source_hash_rejects_symlink_file_and_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real").mkdir()
            (root / "real/source.py").write_text("source", encoding="utf-8")
            try:
                os.symlink(str(root / "real/source.py"), str(root / "alias.py"))
                os.symlink(str(root / "real"), str(root / "alias"), target_is_directory=True)
            except OSError as error:
                self.skipTest("symlink creation unavailable: %s" % error)
            for name in ("alias.py", "alias/source.py"):
                with self.assertRaises(ValueError):
                    source_hash(root, [name])

    def test_task_zero_and_model_only_never_mean_tests_passed(self):
        result = {"task": "task", "state": "done", "exit_code": 0, "final_answer": "All tests PASS"}
        for receipts in ((), [self.receipt(by="model_assertion", evidence="model_assertion")], [self.receipt(code=None)]):
            report = compact_result(result, receipts, SHA)
            self.assertTrue(report["tests_not_run"])
            self.assertEqual(report["tests_status"], "not_run")
            self.assertEqual(report["qa_status"], "not_run")
        self.assertEqual(result["state"], "done")
        self.assertEqual(compact_result(result, [self.receipt(5)], SHA)["tests_status"], "failed")
        self.assertEqual(compact_result(result, [self.receipt()], "b" * 64)["tests_status"], "stale")
        mixed = compact_result(result, [self.receipt(), self.receipt(by="model_assertion", evidence="model_assertion")], SHA)
        self.assertEqual(mixed["tests_status"], "passed")
        self.assertFalse(mixed["tests_not_run"])

    def test_receipt_exact_argv_output_hash_redaction(self):
        text = "api_key=private123\n" + "line\n" * 4000
        receipt = self.receipt(result=text)
        self.assertEqual(receipt.command, ("python", "-m", "unittest"))
        self.assertEqual(receipt.result_sha256, hashlib.sha256(text.encode()).hexdigest())
        self.assertTrue(receipt.result_truncated)
        self.assertLessEqual(len(receipt.result), 4096)
        self.assertNotIn("private123", receipt.result)
        for command in (("python", "api_key=private123"), ("x" * 4097,)):
            with self.assertRaises(ValueError):
                VerificationReceipt(command, 0, "", SHA, "caller", "external_command")
        with self.assertRaises(ValueError):
            self.receipt(by="model_assertion")

    def test_compact_delta_full_hash_baseline_and_declared_scope(self):
        changes = [{"path": "src/a.py", "status": "modified", "before_sha256": SHA, "after_sha256": "b" * 64},
                   {"path": "src/b.py", "status": "deleted", "before_sha256": SHA}]
        result = {"changed_files": changes, "baseline_known": True, "baseline_partial": True,
                  "baseline_warning": "scan budget exceeded", "scope_declared": ["src/**"]}
        report = compact_result(result, current_source_hash=SHA, max_files=1)
        self.assertEqual(report["changed_files_count"], 2)
        self.assertEqual(report["changed_files_omitted"], 1)
        self.assertEqual(report["changed_files"][0]["after_sha256"], "b" * 64)
        self.assertTrue(report["baseline_partial"])
        self.assertFalse(report["scope_enforced"])
        changes[1]["before_sha256"] = "c" * 64
        self.assertNotEqual(report["change_sha256"], compact_result(result, max_files=1)["change_sha256"])

    def test_streaming_report_bounds_late_failed_receipt(self):
        def changes():
            for i in range(20000):
                yield {"path": "src/%s.py" % i, "after_sha256": SHA, "status": "added", "raw": "ignored"}
        def receipts():
            for i in range(1000):
                yield self.receipt(7 if i == 999 else 0)
        tracemalloc.start()
        try:
            report = compact_result({"changed_files": changes(), "final_answer": "word " * 100000},
                                    receipts(), SHA, max_files=2, max_receipts=1)
            unused, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(report["changed_files_count"], 20000)
        self.assertEqual(report["verification_omitted"], 999)
        self.assertEqual(report["tests_status"], "failed")
        self.assertLess(len(json.dumps(report)), 15000)
        self.assertLess(peak, 5 * 1024 * 1024)

    def test_no_global_mutation(self):
        environment, stdout = dict(os.environ), sys.stdout
        classify_failure("HTTP 429")
        self.receipt()
        CostGuard(CostPolicy("caller", "task")).evaluate([])
        compact_result({})
        self.assertEqual(dict(os.environ), environment)
        self.assertIs(sys.stdout, stdout)

    def test_compact_costs_failure_and_unknown_values(self):
        policy = CostPolicy("caller", "task", "0.50", measurement_window=WINDOW)
        reported = CostFact("provider_reported", "0.60", "USD", "fixture", WINDOW)
        estimate = CostFact("catalog_estimate", 0, "USD", "catalog", WINDOW)
        decision = CostGuard(policy).evaluate([estimate, reported])
        report = compact_result({"state": "limited", "exit_code": 126},
                                failure=classify_failure("Out of memory", 126, source="provider_error"),
                                cost_facts=[estimate, reported], cost_decision=decision)
        self.assertEqual(report["state"], "limited")
        self.assertEqual(report["failure"]["category"], "provider_oom")
        self.assertEqual(report["cost_facts"][0]["kind"], "catalog_estimate")
        self.assertEqual(report["cost_facts"][1]["amount"], "0.60")
        self.assertEqual(report["cost_decision"]["cap"], "0.50")
        self.assertTrue(report["cost_decision"]["stop"])
        json.dumps(report)
        self.assertIsNone(compact_result({"exit_code": {"raw": "ignored"}})["exit_code"])

    def test_large_outputs_retained_memory_is_bounded(self):
        output = "ordinary output " * 500000
        tracemalloc.start()
        try:
            receipt = self.receipt(result=output)
            report = compact_result({"final_answer": output}, [receipt], SHA)
            unused, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(receipt.result_truncated)
        self.assertLess(len(json.dumps(report)), 15000)
        self.assertLess(peak, 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
