"""Run planted source defects on disposable copies; passing mutants fail acceptance."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
BASE = Path("D:/Temp/agents-ux/core-proofs") if os.name == "nt" else Path(os.environ.get("TMPDIR", "/tmp")) / "agents-core-proofs"
MUTATIONS = [
    ("suffix-file", "cli.py", 'if "prompt_file" in opts:', 'if False:', "test_prompt_file_suffix_exact_utf8"),
    ("help-side-effects", "cli.py", 'usage(command)\n        return 0\n    if command in ("gui",', 'return 1\n    if command in ("gui",', "test_help_is_read_only_for_every_command"),
    ("queued-send", "lifecycle.py", 'if state in ("running", "idle"):', 'if False:', "test_running_send_queued_and_drained_before_return"),
    ("drop-success-inbox", "lifecycle.py", 'for path in delivered:\n                    path.unlink()', 'for path in delivered:\n                    pass', "test_running_send_queued_and_drained_before_return"),
    ("interrupt-stop", "lifecycle.py", 'action="interrupt"', 'action="stop"', "test_interrupt_resumes_same_launcher_and_session"),
    ("interrupt-replay", "runtime.py", 'self.store.path(self.name, ".control").unlink()', 'pass', "test_interrupt_resumes_same_launcher_and_session"),
    ("stop-notification", "lifecycle.py", 'print("".join(notes) + block, end="", flush=True)', 'pass', "test_stop_reaches_launcher_and_preserves_inbox_progress"),
    ("stop-exit-code", "lifecycle.py", 'else STOP_EXIT', 'else 0', "test_stop_reaches_launcher_and_preserves_inbox_progress"),
    ("closed-pipe-orphan", "runtime.py", 'return stdout_closed()', 'return False', "test_closed_launcher_stdout_stops_provider"),
    ("orphan-state", "lifecycle.py", 'if data.get("core_version") and reconcile:', 'if False:', "test_hard_launcher_kill_reconciles_meta_and_resume"),
    ("hard-timeout-code", "runtime.py", '"watchdog", 124', '"watchdog", 125', "test_timeout_and_silence_print_stop_block_with_original_codes"),
    ("silent-timeout-code", "runtime.py", '"watchdog", 125', '"watchdog", 124', "test_timeout_and_silence_print_stop_block_with_original_codes"),
    ("restart-fresh-context", "lifecycle.py", 'return execute(store, name, resolved, provider, model, effort, "", prompt, False, [])', 'return execute(store, name, resolved, provider, model, effort, store.session(name), prompt, False, [])', "test_restart_same_session_and_fresh_original"),
    ("preflight-missing-directory", "lifecycle.py", 'if not Path(result["dir"]).is_dir():', 'if False:', "test_preflight_failure_preserves_state_exit_answer"),
    ("failed-batch-deleted", "lifecycle.py", 'if code == 0 and action != "interrupt":', 'if action != "interrupt":', "test_failed_drain_retains_durable_batch"),
    ("resume-refusal", "lifecycle.py", 'if not provider.supports_resume:', 'if False:', "test_nonresumable_refuses_without_queue"),
    ("unbounded-drain", "lifecycle.py", 'turns < number_env("AGENT_INBOX_MAX_TURNS", 32)', 'turns < 1000', "test_inbox_drain_bound_and_flush"),
    ("clean-loses-inbox", "views.py", 'if store.inbox(name) and not (opts.get("--all") or opts.get("--purge")):', 'if False:', "test_pending_seen_stopped_and_clean_protection"),
    ("unowned-bulk-stop", "views.py", 'return meta.get("parent") == parent if parent else meta.get("orchestrator") == orchestrator if orchestrator else True', 'return True', "test_stop_owned_only_and_requires_owner"),
    ("last-seen", "views.py", 'store.seen(name)', 'pass', "test_last_reads_final_marker_and_defuses_prompt_marker"),
    ("full-log-read", "state.py", 'size = min(pos, 65536)', 'size = pos', "test_long_log_memory_and_tail"),
    ("question-missed", "lifecycle.py", 'return True\n        if re.search', 'return False\n        if re.search', "test_question_detector_closing_window"),
    ("meta-injection", "state.py", 'or "\\n" in str(value) or "\\r" in str(value)', '', "test_metadata_validation_and_concurrent_atomic_updates"),
    ("unknown-flag-accepted", "cli.py", 'elif argument.startswith("-"):', 'elif False:', "test_unknown_flags_and_literal_double_dash"),
    ("logical-cwd", "runtime.py", 'PWD=self.directory,', '', "test_native_cwd_and_logical_pwd_agree"),
    ("legacy-meta-unreadable", "state.py", 'key, value = line.split("=", 1)', 'key, value = line.split("=", 1); value = "broken"', "test_legacy_reads_core_and_core_reads_legacy_layout"),
    ("orphan-provider-missed", "lifecycle.py", 'if pid_alive(data.get("provider_pid"), data.get("provider_start", "")):', 'if False:', "test_orphan_live_provider_detected_and_stopped"),
    ("stream-truncates-answer", "state.py", 'if not skip:\n                yield line', 'if not skip:\n                yield line\n                return', "test_large_answer_last_streams_without_truncation"),
    ("log-loses-long-step", "views.py", 'offset = log_offset(path, int(opts.get("-n", 0)), bool(opts.get("-l")))', 'offset = max(0, path.stat().st_size - 1048576)', "test_log_streams_last_step_long_lines_and_zero"),
    ("owner-lock-waits-forever", "state.py", 'if self.timeout is not None else None', 'if self.timeout else None', "test_owner_lock_zero_fails_without_waiting"),
    ("wait-ignores-environment", "cli.py", 'os.environ.get("AGENT_WAIT_TIMEOUT", 0)', '0', "test_wait_honors_environment_defaults"),
    ("missing-cli-late-preflight", "lifecycle.py", 'provider.command(model, effort, resolved["dir"], session=session, chat_only=os.environ.get("AGENT_CHAT_ONLY") == "1")', 'pass', "test_missing_cli_preflight_with_option_before_name_preserves_state"),
    ("wrong-error-target", "cli.py", 'else args[0] if args else ""', 'else next((arg for arg in argv if not arg.startswith("-")), "")', "test_missing_cli_preflight_with_option_before_name_preserves_state"),
    ("fan-owner-race", "fan.py", 'for name, process, stamp in launched:', 'for name, process, stamp in []:', "test_fan_returns_only_after_owner_publication"),
    ("retry-stop-delayed", "runtime.py", 'if turn.action == "stop":', 'if False:', "test_stop_during_retry_delay_reaches_launcher"),
    ("fan-wait-kill-orphan", "runtime.py", '(own_meta.get("wait_pid") and not pid_alive(own_meta["wait_pid"], own_meta.get("wait_start", "")))', 'False', "test_killed_tracked_fan_wait_stops_owned_tree"),
    ("wait-hides-stop-code", "views.py", 'if state == "stopped":\n                    code = 130', 'if False:\n                    code = 130', "test_wait_on_stopped_task_reports_block_and_exit_130"),
    ("legacy-control-steals-native-owner", "../legacy/control_migration.sh", '[ "$native" = 1 ] || return 0', 'return 0', "test_legacy_send_queues_to_native_owner_without_replacing_it"),
    ("legacy-delta-invented", "reporting.py", 'else "unknown (legacy task; no start snapshot)"', 'else "0"', "test_live_legacy_run_is_stoppable_resumable_by_core"),
    ("stdout-output-marker-missing", "lifecycle.py", 'print(MARK)', 'pass', "test_run_stdout_keeps_session_and_output_marker"),
    ("fast-provider-error-lost", "lifecycle.py", 'or (output_filter and output_filter.final_failure(code))', 'or (code and output_filter and output_filter.final_failure(code))', "test_provider_error_tag_on_fast_zero_exit_still_limited"),
]


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    results = []
    selected = set(sys.argv[1:])
    for name, module, original, replacement, test in MUTATIONS:
        if selected and name not in selected:
            continue
        copy = BASE / name
        copy.mkdir(exist_ok=True)
        for folder in ("neoxider_agents", "providers", "tests", "legacy", "bin", "completions"):
            shutil.copytree(ROOT / folder, copy / folder, dirs_exist_ok=True)
        for entry in ("agent.py", "activity.py", "gui.py", "openai_server.py"):
            shutil.copy2(ROOT / entry, copy / entry)
        path = copy / "neoxider_agents" / module
        text = path.read_text(encoding="utf-8")
        if original not in text:
            results.append(dict(defect=name, caught=False, error="source mutation did not match"))
            continue
        path.write_text(text.replace(original, replacement, -1 if name == "resume-refusal" else 1), encoding="utf-8")
        env = dict(os.environ, AGENT_CORE_TEST_ROOT=copy.as_posix(), PYTHONPATH=copy.as_posix())
        started = time.monotonic()
        try:
            run = subprocess.run([sys.executable, "-m", "unittest", "test_agent_core.CoreTests." + test],
                                 cwd=str(copy / "tests"), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=130, creationflags=0x08000000 if os.name == "nt" else 0)
            output = run.stdout.decode("utf-8", "replace")
            caught = run.returncode != 0 and "FAIL:" in output
            (BASE / (name + ".txt")).write_text(output, encoding="utf-8")
            results.append(dict(defect=name, test=test, caught=caught, exit=run.returncode, seconds=time.monotonic()-started))
        except subprocess.TimeoutExpired:
            results.append(dict(defect=name, test=test, caught=False, error="proof harness timeout"))
        print(name, "CAUGHT" if results[-1]["caught"] else "NOT CAUGHT", flush=True)
        (BASE / "manifest.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0 if all(row["caught"] for row in results) else 1


if __name__ == "__main__":
    sys.exit(main())
