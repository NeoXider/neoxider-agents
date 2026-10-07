"""Plant Phase 2B storage/control defects only in disposable isolated copies."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
BASE = Path("D:/Temp/agents-ux/storage-proofs") if os.name == "nt" else Path(os.environ.get("TMPDIR", "/tmp")) / "agents-ux-storage-proofs"
MUTATIONS = [
    ("progress-default", "lifecycle.py", "if progress_on:", "if True:", "test_default_run_adds_no_project_files"),
    ("progress-opt-in", "lifecycle.py", "if progress_on:", "if False:", "test_progress_opt_in"),
    ("progress-environment", "lifecycle.py", 'enabled(os.environ.get("AGENT_PROGRESS", "0"))', "False", "test_progress_environment_and_no_progress_alias"),
    ("quiet-output", "runtime.py", 'self.verbose = enabled(store.read(name).get("verbose", "0"))', "self.verbose = True", "test_quiet_default_and_verbose_stream"),
    ("ask-banner", "lifecycle.py", 'quiet_answer = opts.get("ask", False)', "quiet_answer = False", "test_ask_only_answer_and_exit_result"),
    ("raw-unbounded", "logs.py", "if not self.keep and size + len(data) > self.cap:", "if False:", "test_bounded_raw_logs_full_answer"),
    ("keep-log-ignored", "lifecycle.py", 'keep_logs = opts.get("--log") or enabled(os.environ.get("AGENT_KEEP_LOGS", "0"))', "keep_logs = False", "test_keep_log_flag_and_environment"),
    ("ttl-disabled", "logs.py", "expired = time.time() - path.stat().st_mtime >= hours * 3600", "expired = False", "test_ttl_prunes_raw_and_all_views_survive"),
    ("ttl-prunes-kept", "logs.py", 'not force and enabled(data.get("keep_logs", "0"))', "False", "test_persistent_log_ignores_ttl"),
    ("clean-keeps-raw", "views.py", 'paths = [store.path(name, ".md"), store.path(name, ".log"), store.path(name, ".launcher.log")]\n        from .logs import prune_task\n        if store.path(name, ".log").exists():\n            prune_task(store, name, meta, force=True, dry=bool(opts.get("-n") or opts.get("--dry-run")))', 'paths = [store.path(name, ".md"), store.path(name, ".launcher.log")]', "test_clean_removes_persistent_raw_but_retains_session"),
    ("answer-retention-lost", "logs.py", 'path = store.path(name, ".answer")', 'path = store.path(name, ".broken-answer")', "test_bounded_raw_logs_full_answer"),
    ("digest-leaks-secret", "logs.py", "detail=compact(detail, 240)", "detail=str(detail)", "test_metadata_and_control_history_redact_secrets"),
    ("history-lost", "lifecycle.py", 'record_history(store, name, "send", sequence=sequence, now=False, queued=False)', "pass", "test_metadata_and_control_history_redact_secrets"),
    ("usage-discarded", "output.py", "self.usage[key] = self.usage.get(key, 0) + value", "pass", "test_reported_usage_and_cost_only"),
    ("cost-discarded", "output.py", "self.cost = (self.cost or 0) + cost", "self.cost = None", "test_reported_usage_and_cost_only"),
    ("utf8-tail-corrupted", "logs.py", "while data and data[0] & 0xC0 == 0x80:", "while False:", "test_tail_writer_never_exceeds_cap_and_valid_utf8"),
    ("legacy-answer-migration-lost", "logs.py", 'if not store.path(name, ".answer").exists():', "if False:", "test_legacy_answer_preserved_on_ttl"),
    ("notification-visible", "notifications.py", '**hidden_kwargs(executable=argv[0])', "**{}", "test_notifications_hidden_and_failure_optional"),
    ("raw-events-lost", "runtime.py", 'log.write(line)\n        for fragment', 'pass\n        for fragment', "test_raw_provider_event_is_retained_separately_from_digest"),
    ("fan-progress-lost", "../neoxider_agents/fan.py", '"--terminal", "--progress", "--log"', '"--terminal", "--log"', "test_fan_forwards_opt_ins_and_has_no_default_launcher_transcript"),
    ("clean-legacy-answer-lost", "logs.py", 'if not store.path(name, ".answer").exists():', "if False:", "test_clean_legacy_answer_survives"),
    ("structured-ask-raw-leak", "lifecycle.py", 'answer = answer or (last_output(store.path(name, ".log")) if output_filter is None else "")', 'answer = answer or last_output(store.path(name, ".log"))', "test_structured_failed_ask_never_prints_raw_json"),
    ("resume-ownership-bypassed", "lifecycle.py", 'guard_ownership(store, name, resolved["dir"], resolved.get("owns", ""), resolved.get("--strict-owns", False))', 'pass', "test_strict_resume_refuses_running_overlap_before_queueing"),
    ("old-opt-ins-inherited", "lifecycle.py", 'if not opts.get("_new"):', 'if True:', "test_reused_run_resets_opt_ins"),
    ("usage-not-accumulated", "output.py", 'self.usage[key] = self.usage.get(key, 0) + value', 'self.usage[key] = value', "test_opencode_usage_accumulates_and_normalizes_counts"),
    ("auto-name-secret-leak", "cli.py", 'redact(prompt).lower()', 'prompt.lower()', "test_auto_task_name_redacts_prompt_secrets"),
    ("git-project-write", "reporting.py", '"git", "--no-optional-locks", "-C"', '"git", "-C"', "test_git_baseline_disables_index_refresh"),
    ("project-bytecode-written", "../agent.py", 'sys.dont_write_bytecode = True', 'sys.dont_write_bytecode = False', "test_default_entry_never_creates_project_bytecode"),
    ("log-follow-stalls", "views.py", 'if path.stat().st_size < offset:\n                offset = 0', 'if False:\n                offset = 0', "test_log_follow_resumes_after_tail_truncation"),
    ("error-secret-leak", "cli.py", 'message = redact(message)', 'message = message', "test_one_line_errors_redact_credentials"),
    ("opt-in-banner-misleading", "lifecycle.py", 'if not progress_on and not verbose else ""', 'if True else ""', "test_progress_opt_in"),
]


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    selected = set(sys.argv[1:])
    rows = []
    from neoxider_agents.process import hidden_kwargs
    for label, module, original, replacement, test in MUTATIONS:
        if selected and label not in selected:
            continue
        local = BASE / label
        local.mkdir(exist_ok=True)
        for folder in ("neoxider_agents", "providers", "tests", "completions", "bin", "legacy"):
            shutil.copytree(ROOT / folder, local / folder, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("agent.py", "agent.ps1", "activity.py"):
            shutil.copy2(ROOT / name, local / name)
        path = local / "neoxider_agents" / module
        text = path.read_text(encoding="utf-8")
        if original not in text:
            rows.append(dict(defect=label,caught=False,error="source did not match"))
        else:
            path.write_text(text.replace(original, replacement, 1), encoding="utf-8")
            env = dict(os.environ,AGENT_UX_TEST_ROOT=local.as_posix(),AGENT_CLI_LOGS=(local/"state").as_posix(),
                       PYTHONPATH=local.as_posix(),TEMP="D:/Temp/agents-ux",TMP="D:/Temp/agents-ux")
            started = time.monotonic()
            result = subprocess.run([sys.executable,"-m","unittest","test_ux_storage.StorageTests."+test],cwd=str(local/"tests"),
                                    env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=60,**hidden_kwargs())
            output = result.stdout.decode("utf-8","replace")
            (local/"result.txt").write_text(output,encoding="utf-8")
            # Both assertion failures and a defect corrupting UTF-8 are behavior failures.
            valid_error = label == "utf8-tail-corrupted" and "UnicodeDecodeError" in output
            caught = result.returncode != 0 and ("FAIL:" in output or valid_error)
            rows.append(dict(defect=label,test=test,caught=caught,exit=result.returncode,seconds=time.monotonic()-started,evidence=(local/"result.txt").as_posix()))
        print(label,"CAUGHT" if rows[-1]["caught"] else "NOT CAUGHT",flush=True)
        (BASE/"manifest.json").write_text(json.dumps(rows,indent=2),encoding="utf-8")
    return 0 if all(row["caught"] for row in rows) else 1


if __name__ == "__main__":
    sys.path.insert(0,str(ROOT))
    sys.exit(main())
