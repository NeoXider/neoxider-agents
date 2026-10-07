"""Run each activity regression against a planted defect in an isolated source copy.

No live CLI, no repository edits. Evidence is written under D:/Temp/agents-ux on
Windows (a dedicated temp subtree elsewhere). Run: python tests/prove_activity_defects.py
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
MUTATIONS = [
    ("test_codex_digest", 'if itype == "command_execution":', 'if itype == "BROKEN_command_execution":'),
    ("test_opencode_digest", 'if tag == "tool_use":', 'if tag == "BROKEN_tool_use":'),
    ("test_claude_digest", 'if tag == "stream_event":', 'if tag == "BROKEN_stream_event":'),
    ("test_kimi_digest", 'for call in message.get("tool_calls") or []:', 'for call in []:'),
    ("test_redaction_in_raw_and_plain", 'value = AUTH.sub(r"\\1[REDACTED]", str(text))', 'return str(text)\n    value = AUTH.sub(r"\\1[REDACTED]", str(text))'),
    ("test_reasoning_not_exposed_in_raw", 'result[key] = "thinking"', 'result[key] = item'),
    ("test_sidecar_capture_and_summary", '"event": safe_event(event)', '"event": event'),
    ("test_summary_reads_only_tail", 'start = max(0, end - size)', 'start = 0'),
    ("test_summary_of_event_larger_than_tail_is_still_activity", 'if path.stat().st_size:', 'if False:'),
    ("test_follow_waits_for_settlement_and_final_append", 'if not opts.follow:', 'if True:'),
    ("test_follow_keeps_append_between_snapshot_and_print", '    pending = b""\n    while True:', '    offset = source.stat().st_size\n    pending = b""\n    while True:'),
    ("test_default_last_25_and_plain_note", 'type=int, default=25', 'type=int, default=24'),
    ("test_errors_retries_and_other_tools", 'yield "retry" if "retry" in tag else "error", compact(err)', 'yield "error", "error hidden"'),
    ("test_capture_optional_failure_preserves_filter", 'except OSError:\n        # Optional diagnostic capture must never invalidate the provider\'s answer.\n        pass', 'except OSError:\n        raise'),
    ("test_provider_emitters_capture_without_changing_answer", 'if not target or not isinstance(event, dict):', 'if True:'),
]


def import_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    base = Path("D:/Temp/agents-ux") if os.name == "nt" else Path(tempfile.gettempdir()) / "agents-oc"
    base.mkdir(parents=True, exist_ok=True)
    original = (ROOT / "activity.py").read_text(encoding="utf-8")
    evidence = []
    with tempfile.TemporaryDirectory(prefix="activity-defects-", dir=base) as temp:
        scratch = Path(temp)
        shutil.copytree(ROOT / "providers", scratch / "providers")
        (scratch / "tests" / "fixtures").mkdir(parents=True)
        for fixture in (ROOT / "tests" / "fixtures").glob("activity-*.jsonl"):
            shutil.copy2(fixture, scratch / "tests" / "fixtures" / fixture.name)
        shutil.copy2(ROOT / "stream_text_filter.py", scratch / "stream_text_filter.py")
        for name, before, after in MUTATIONS:
            if original.count(before) != 1:
                raise RuntimeError("mutation anchor must occur exactly once: " + name)
            (scratch / "activity.py").write_text(original.replace(before, after), encoding="utf-8")
            # Ensure imports use this exact mutant even if Python caches same-length source.
            cache = scratch / "__pycache__"
            if cache.exists():
                shutil.rmtree(cache)
            import_file("activity", scratch / "activity.py")
            import_file("stream_text_filter", scratch / "stream_text_filter.py")
            suite_module = import_file("activity_defect_suite", ROOT / "tests" / "test_activity.py")
            suite_module.ROOT = scratch
            output = io.StringIO()
            suite = unittest.TestSuite([suite_module.ActivityTests(name)])
            result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
            caught = not result.wasSuccessful() and not result.skipped
            evidence.append({"test": name, "defect": after.splitlines()[0], "caught": caught, "output": output.getvalue()})
            print(("CAUGHT " if caught else "MISSED ") + name)
    target = base / "activity-defect-proofs.json"
    target.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print("Evidence: " + str(target))
    return 0 if all(proof["caught"] for proof in evidence) else 1


if __name__ == "__main__":
    raise SystemExit(main())
