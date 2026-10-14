# Fleet GUI and durable read receipts

Follow-up integration: stable per-submit UUIDs and private UTF-8 file transport are
implemented against the core's supported `--message-id` flag. The previous evidence
wave at the bottom is historical; the final follow-up wave supersedes its pending
CLI integration and legacy assertion failure.

The Fleet tab lives in the existing GUI, beside Tasks and API. It filters tasks by
parent identifier and project directory (the GUI group), shows page-scoped
active/done/attention/error counts, requested and resolved model evidence, cached
file deltas, scope declarations and available stored cost telemetry. Unknown facts
stay unknown. Cost provenance does not establish billing or account quota.

Individual task controls reuse `send`, `stop` and `restart` through the existing
Python launcher. Resume maps to `restart TASK`; no fresh replay, bulk stop, invented
message flags or global configuration changes are performed. Terminal mode remains
off by default. Stale/deleted selections and failed fleet refreshes disable controls.

## Endpoints and security

All new endpoints run through the GUI's existing token authentication. POSTs also
use the existing JSON and same-origin guard for tokenless loopback mode. Configured
token cookies remain HttpOnly/SameSite=Strict; a loopback proxy does not bypass a
configured token. There are no callbacks, network destinations or token bypasses.
The existing static file guard and CSP cover `static/fleet.js`.

* `GET /api/fleet?parent=ID&group=DIRECTORY&limit=100` reads bounded state metadata.
  `parent` is a parent identifier, including `/root/...`; `group` is an exact project
  directory string. Omitted filters include all scanned tasks. Responses contain
  `tasks`, `summary`, `summary_scope: "returned page"`, filter options, `incomplete`
  and `comparison_incomplete` flags. No workspace/Git scans or providers run.
* `GET /api/task/events?task=NAME&after=ENCODED_JSON&limit=16` reads durable events
  from `EventStore.read`. `after` is a task-bound `{"task":"NAME","seq":N}` object,
  URL encoded as JSON; omission starts at zero. Bare numeric cursors, cross-task
  cursors and cursors ahead of the journal head fail. An optional `consumer` is
  validated but a GET never acknowledges anything. Responses preserve the backend's
  task-bound `cursor`, `head`, `more`, `torn` and `truncated` fields. Interior damage
  returns HTTP 409, rather than an empty success. A torn tail is reported and only
  complete records are replayed. Legacy raw-log `/api/stream` remains separate.
* `POST /api/task/events/ack` takes exactly `task`, a bounded string `consumer`, and
  a task-bound `cursor`. Existing task validation precedes `EventStore.ack`.
  Consumer acknowledgement is monotonic, persisted and independent per task/consumer.
  The response cursor remains task-bound. Invalid input returns 400; missing tasks
  return 404; journal corruption returns 409. The ack body cap is 4096 bytes.

Fleet/event JSON is capped at 512 KiB. Fleet pages default to 100 tasks and cap at
200; scans stop after 2000 state directory entries, metadata reads at 64 KiB, and
filter options at 64 values each. At most 2000 task pairs are compared within the
returned candidate page, with four short scope patterns per task. Scope comparisons
are lexical and do not resolve or contact project/network directories. Scope and
delta warnings are evidence of possible/shared paths, not proof of authorship or
write enforcement; shared cached deltas do not establish overlapping lifetimes.
Incomplete comparison flags also cover oversized/truncated scope declarations.

Per task, the compact view exposes 16 cached paths, eight overlap/conflict entries
and 24 latest receipt items. Receipt journals over 2 MiB are unavailable rather
than silently counted. Events default to 16 rows/page and cap at 32; byte bounding
adjusts the cursor to the last returned row, so subsequent polling skips nothing.
The backend currently validates/scans the entire durable journal for a page; these
are response/memory bounds, not a claim of constant read latency.

## Message receipts versus event read acknowledgements

`Store.receipts` supplies persisted message UUIDs and independent acceptance,
delivery and explicit assistant acknowledgement evidence. Schema/task bindings,
timestamps and evidence fields are checked defensively. Corrupt, torn, oversized
or unfamiliar receipt data returns `counts: null` with an unavailable status.
Neither prose/tool output, an event kind nor a task's zero exit code creates a
message acknowledgement or an applied claim.

`POST /api/task/send` now returns `launcher_started: true`, `status: "pending"`,
`accepted: false` and the exact per-submit `message_id` after successful Popen. This
proves launcher creation only. Each GUI submission assigns a fresh stable UUID
before the HTTP request; `--message-id UUID` is forwarded before the `--` separator.
Unknown outcomes are polled and never automatically resent/retried with that ID.
`GET /api/task/receipt?task=NAME&message_id=UUID` authenticates and reads only the
requested receipt, including receipts older than the compact Fleet item tail.
Missing receipt evidence stays pending/unknown, even if another receipt or previous
task answer/exit is successful. Resume also reports pending
launcher creation, rather than completed resumption.
Since 2026-10-14 `send` and `resume` also accept optional `model` / `effort`
(validated model-id text, never option-like) forwarded as `-m` / `-f` for the next turn,
and the Tasks-tab composer (see [GUI.md](GUI.md)) keeps a per-task outbox polling each
UUID's receipt.

Run, send and the legacy reply route transport text through a unique private UTF-8
file using `--prompt-file FILE --`; message/prompt text never enters the launch argv.
Private directories inherit the user's Windows temp-directory access rules and
use restrictive directory/file modes on POSIX. Exact file bytes preserve Unicode,
whitespace and option-looking text. The text cap is explicitly 1 MiB UTF-8; JSON
requests remain bounded by the existing 10 MiB cap. The old command-line length
limit is removed. Failed launches clean their files; successful launches transfer
ownership to the child waiter and retain files until wrapper exit. Tests delay
wrapper reads and hold the child active to prove there is no early deletion.
Run/reply/send toasts/status distinguish launch from persisted acceptance,
delivery and recorded acknowledgment evidence. A source tag/operator-supplied ack
record is not independent proof that an assistant understood or applied the message.

The event consumer is a separate GUI read receipt, not an assistant message ack.
The widget validates each page, attaches rows using `textContent`, and only then
POSTs its cursor. A DOM failure never acknowledges. An ack failure retains pending
progress for retry; the confirmed cursor advances only on the persisted response.
Event IDs deduplicate a partially rendered/replayed prefix. Page IDs and sequences
are checked before rendering. In-flight responses after selection/tab changes
cannot render or acknowledge the old view. Polls are serialized and reconnect from
the confirmed task-bound cursor.

Each tab gets a random consumer namespace using `crypto.getRandomValues`, which
also works on LAN HTTP. Each retained task view has its own consumer; evicted views
start fresh at zero with a new consumer to avoid acknowledging beyond their new
DOM. There are at most 16 retained task views and 200 rendered event rows per view;
older rows are released after persistence succeeds. A full page reload intentionally
starts a fresh view/replay, rather than treating the previous DOM as still read.
The backend consumer cap can eventually reject new views; that failure remains
visible and does not advance the GUI cursor.

## Owned launcher resources and terminal lifetime

`GUI.spawn` tracks only its own Popen objects. A daemon waiter reaps each child as
soon as it exits, closes any owned pipes, releases owned references, and removes
the object from the owned registry. Windows handle cleanup belongs to Popen reference
release; explicitly closing its handle would race legitimate concurrent wait/poll
callers. Private prompt directories are deleted only after child exit. No foreign processes are killed, no active
workers are timed out by this GUI code, and no user terminals are closed. Task
name/session/output artifacts remain owned by the backend and available for resume.

Opted-in Windows consoles run Python directly with `CREATE_NEW_CONSOLE`; there is
no shell `NoExit` or post-completion idle loop. This is a natural-exit contract:
resource release is immediate after owned process exit (tested within three
seconds, below 300 seconds), while visible Windows Terminal/tab lifetime belongs to
the host. No idle-window closure claim or live terminal/browser verification is made.

## Verification handoff

Fixtures use temporary state, an ephemeral loopback HTTP port, simulated control
launches and a short hidden Python process for resource release. No live agents or
providers are started. Node checks use built-in DOM fixtures, not a real browser.
Root retains final live browser QA.

The scope extension authorizes updating the existing send assertion. It now pins
`accepted=false`, `launcher_started=true`, the returned UUID, `--now`/option order,
and exact option-looking file bytes. New HTTP tests prove that durable acceptance
appears only through an exact-UUID poll and that an unrelated/previous receipt
cannot satisfy the current submission. No test is skipped to hide a failure.

Implementation state and verification evidence are kept in this owned document;
`PLAN.md` is outside the exact write scope. No Git operations were used.

### Source hashes and unfiltered verification output (initial historical wave)

Final evidence follows below. Documentation is excluded from its own hash table
to avoid a self-referential hash; backend/helper hashes are read-only snapshots,
not acceptance of other workers' changes.


## Final follow-up evidence (supersedes the previous wave)

All checks pass: 172 legacy GUI tests, 19 HTTP/resource/negative-transport tests,
12 Node fleet checks, existing Node model/toast checks and Python/JavaScript syntax.
All owned source and read-only core snapshots matched before and after this wave.
The CLI message-ID contract was present and verified with the real parser; no
provider was launched and no core file was written. Private text is absent from
launch argv, delayed reads succeed, files survive active wrappers and cleanup occurs
at exit. The ownership registry drops completed processes; no idle terminal or
fixture server remains. Root retains live browser/visible-terminal QA.

The fixed Windows wait/poll regression is covered by a real hidden child whose
caller waits concurrently with the GUI reaper. Private-file launch failures and
invalid JSON/text/UUID/options are negative fixtures, rather than provider calls.
Ack source tags are displayed as recorded evidence; no application/understanding
claim is inferred. Late receipt responses cannot overwrite a newer poll/submission.

| File | Ownership | SHA-256 |
| --- | --- | --- |
| gui.py | owned | `1cb458b7ba00b7ce5cf3bc420b6cf1cb64cb9e6bc16f7d1468a1dc1d59d5776b` |
| gui.html | owned | `d093deab3feb80f66d18978a4486b6fde0077cc004d457315d9ccc22e9299737` |
| static/app.js | owned | `1def1effdf2999a02d32487d8da2ae8e5a9cd9e2ca30344c5638f843d50f4baf` |
| static/fleet.js | owned | `36e2f5062324a49313538d82415326fb5bd9687ed12b233271e64a761ba81e7a` |
| static/chat.js | owned | `aaba371e79d7d874abcf63cdeab2c3b7b51b754a66404b13c1532fac756990fd` |
| static/style.css | owned | `9da08c55f2476d841cc31f489e91f23b0c3e1e07a8358481ae306efe35595e87` |
| static/i18n.js | owned | `7c1e015b59e6f1138da8de3b7d7c6cbed34997d5dca8cf42e3b49d662191c38c` |
| tests/test_fleet_gui.py | owned | `1430faf2d78883f94f2f0ff19646511edead492a8c294546fe85b6ea5c4c3253` |
| tests/test_fleet.js | owned | `b6ed8396e85dbfb30d918432887564adc7d02e332aa84427d2f08420cbeab103` |
| tests/test_gui.py | owned | `3551cb4fbdf7cdd0f6c6613a4c994dc5367a1d89962b5cdc32b95c530324a424` |
| neoxider_agents/cli.py | read-only core snapshot | `aba92f46509744992cf0ee09c92c14d5248ac0717da0b2084e8093a1f7addbd7` |
| neoxider_agents/lifecycle.py | read-only core snapshot | `284d9c00b12ee06f07c80d02f2c15a342ec77bc396a3d942347e283b7eed5408` |
| neoxider_agents/state.py | read-only core snapshot | `903c55506f8e6c93ca66db4b44ef4fe690a770f3c5b8afd296ddbe0ab54c40ab` |
| neoxider_agents/events.py | read-only core snapshot | `69572b7641928d1587bcf4c4296a21e37af84ca175cc56ca366fc67832dd7257` |
| neoxider_agents/message_receipts.py | read-only core snapshot | `419d0907ee409c04d095253aee08815a75413cb29e05bd7829851f9f687d6a74` |

#### C:\Users\User\AppData\Local\Programs\Python\Python313\python.exe -m unittest discover -s tests -p test_gui.py -v

Exit code: 0

```text
test_done_state_maps_to_done_emoji (test_gui.ActivityEmojiTests.test_done_state_maps_to_done_emoji) ... ok
test_error_state_maps_to_error_emoji (test_gui.ActivityEmojiTests.test_error_state_maps_to_error_emoji) ... ok
test_running_state_reads_log_for_activity_hint (test_gui.ActivityEmojiTests.test_running_state_reads_log_for_activity_hint) ... ok
test_unknown_non_running_state_falls_back_to_bullet (test_gui.ActivityEmojiTests.test_unknown_non_running_state_falls_back_to_bullet) ... ok
test_waiting_state_maps_to_waiting_emoji (test_gui.ActivityEmojiTests.test_waiting_state_maps_to_waiting_emoji) ... ok
test_get_test_api_is_404_too (test_gui.ApiTestRouteRemovedTests.test_get_test_api_is_404_too) ... ok
test_kind_api_test_rows_still_display_from_cli_meta (test_gui.ApiTestRouteRemovedTests.test_kind_api_test_rows_still_display_from_cli_meta) ... ok
test_post_test_api_is_404_not_found (test_gui.ApiTestRouteRemovedTests.test_post_test_api_is_404_not_found) ... ok
test_concurrent_start_loser_does_not_accept_winners_registry (test_gui.BridgeReadyIdentityTests.test_concurrent_start_loser_does_not_accept_winners_registry) ... ok
test_registry_from_spawned_child_is_accepted (test_gui.BridgeReadyIdentityTests.test_registry_from_spawned_child_is_accepted) ... ok
test_windows_bash_descendant_registry_is_accepted (test_gui.BridgeReadyIdentityTests.test_windows_bash_descendant_registry_is_accepted) ... ok
test_route_forwards_body_to_restart_bridge (test_gui.BridgeRestartRouteTests.test_route_forwards_body_to_restart_bridge) ... ok
test_route_forwards_instance_id_to_stop_bridge (test_gui.BridgeStopRouteTests.test_route_forwards_instance_id_to_stop_bridge) ... ok
test_default_payload_caps_huge_blocks_full_does_not (test_gui.DialogPayloadTests.test_default_payload_caps_huge_blocks_full_does_not) ... ok
test_default_payload_is_last_n_steps_with_has_more (test_gui.DialogPayloadTests.test_default_payload_is_last_n_steps_with_has_more) ... ok
test_endpoint_requires_task_param (test_gui.DialogPayloadTests.test_endpoint_requires_task_param) ... ok
test_endpoint_serves_dialog_json (test_gui.DialogPayloadTests.test_endpoint_serves_dialog_json)
H.do_GET for /api/dialog, driven through a fake handler (no real socket). ... ok
test_missing_log_is_empty_payload_not_exception (test_gui.DialogPayloadTests.test_missing_log_is_empty_payload_not_exception) ... ok
test_payload_fields_and_single_step (test_gui.DialogPayloadTests.test_payload_fields_and_single_step) ... ok
test_cache_first_loads_persistent_snapshot_and_starts_refresh_without_waiting (test_gui.DoctorTests.test_cache_first_loads_persistent_snapshot_and_starts_refresh_without_waiting) ... ok
test_doctor_frontend_renders_structured_limits_and_keeps_raw_toggle (test_gui.DoctorTests.test_doctor_frontend_renders_structured_limits_and_keeps_raw_toggle) ... ok
test_failed_refresh_keeps_last_good_raw_snapshot (test_gui.DoctorTests.test_failed_refresh_keeps_last_good_raw_snapshot) ... ok
test_parses_legacy_doctor_text_into_engine_and_limit_rows (test_gui.DoctorTests.test_parses_legacy_doctor_text_into_engine_and_limit_rows) ... ok
test_idle_counts_as_live (test_gui.EffStateTests.test_idle_counts_as_live) ... ok
test_missing_state_key_defaults_to_question_mark (test_gui.EffStateTests.test_missing_state_key_defaults_to_question_mark) ... ok
test_other_states_pass_through_unchanged (test_gui.EffStateTests.test_other_states_pass_through_unchanged) ... ok
test_pid_alive_returns_none_when_no_pid_recorded (test_gui.EffStateTests.test_pid_alive_returns_none_when_no_pid_recorded) ... ok
test_pid_alive_says_an_impossible_pid_is_dead (test_gui.EffStateTests.test_pid_alive_says_an_impossible_pid_is_dead) ... ok
test_pid_alive_says_this_very_process_is_alive (test_gui.EffStateTests.test_pid_alive_says_this_very_process_is_alive) ... ok
test_running_with_dead_pid_is_stalled_even_with_a_fresh_log (test_gui.EffStateTests.test_running_with_dead_pid_is_stalled_even_with_a_fresh_log) ... ok
test_running_with_fresh_mtime_stays_running (test_gui.EffStateTests.test_running_with_fresh_mtime_stays_running) ... ok
test_running_with_live_pid_and_fresh_log_is_running (test_gui.EffStateTests.test_running_with_live_pid_and_fresh_log_is_running) ... ok
test_running_with_live_pid_and_silent_log_is_idle_not_stalled (test_gui.EffStateTests.test_running_with_live_pid_and_silent_log_is_idle_not_stalled) ... ok
test_running_with_no_log_mtime_stays_running (test_gui.EffStateTests.test_running_with_no_log_mtime_stays_running) ... ok
test_running_with_stale_mtime_and_unknown_pid_becomes_stalled (test_gui.EffStateTests.test_running_with_stale_mtime_and_unknown_pid_becomes_stalled) ... ok
test_hours (test_gui.FmtDurTests.test_hours) ... ok
test_minutes_and_seconds (test_gui.FmtDurTests.test_minutes_and_seconds) ... ok
test_none_and_negative_are_empty (test_gui.FmtDurTests.test_none_and_negative_are_empty) ... ok
test_round_minute_drops_zero_seconds (test_gui.FmtDurTests.test_round_minute_drops_zero_seconds) ... ok
test_rounding_near_minute_boundary (test_gui.FmtDurTests.test_rounding_near_minute_boundary) ... ok
test_sub_ten_seconds_one_decimal (test_gui.FmtDurTests.test_sub_ten_seconds_one_decimal) ... ok
test_whole_seconds_no_decimal (test_gui.FmtDurTests.test_whole_seconds_no_decimal) ... ok
test_configured_token_is_required_even_on_loopback (test_gui.GuiAuthTests.test_configured_token_is_required_even_on_loopback) ... ok
test_loopback_forms_recognised (test_gui.GuiAuthTests.test_loopback_forms_recognised) ... ok
test_network_client_without_the_token_is_rejected (test_gui.GuiAuthTests.test_network_client_without_the_token_is_rejected) ... ok
test_no_token_configured_means_open_panel (test_gui.GuiAuthTests.test_no_token_configured_means_open_panel) ... ok
test_run_endpoint_is_gated_too (test_gui.GuiAuthTests.test_run_endpoint_is_gated_too)
/api/run spawns a real subagent -- the whole reason the token exists. ... ok
test_token_accepted_from_query_header_or_cookie (test_gui.GuiAuthTests.test_token_accepted_from_query_header_or_cookie) ... ok
test_failed_health_probe_is_debug_gated_and_leaks_no_url (test_gui.GuiLoggingTests.test_failed_health_probe_is_debug_gated_and_leaks_no_url) ... ok
test_kill_pid_failure_warns_with_exception_type_only (test_gui.GuiLoggingTests.test_kill_pid_failure_warns_with_exception_type_only) ... ok
test_kill_pid_nonzero_returncode_warns_with_status_only (test_gui.GuiLoggingTests.test_kill_pid_nonzero_returncode_warns_with_status_only) ... ok
test_request_lines_route_to_debug_only (test_gui.GuiLoggingTests.test_request_lines_route_to_debug_only) ... ok
test_token_in_request_line_is_redacted (test_gui.GuiLoggingTests.test_token_in_request_line_is_redacted) ... ok
test_invalid_or_non_object_json_is_400 (test_gui.GuiPostBodyGuardTests.test_invalid_or_non_object_json_is_400) ... ok
test_malformed_and_negative_content_length_are_400 (test_gui.GuiPostBodyGuardTests.test_malformed_and_negative_content_length_are_400) ... ok
test_oversized_content_length_is_413_before_any_read (test_gui.GuiPostBodyGuardTests.test_oversized_content_length_is_413_before_any_read) ... ok
test_valid_empty_body_reaches_routing (test_gui.GuiPostBodyGuardTests.test_valid_empty_body_reaches_routing) ... ok
test_bool_and_float_ports_and_unknown_provider_never_spawn (test_gui.GuiSecurityRegressionTests.test_bool_and_float_ports_and_unknown_provider_never_spawn) ... ok
test_frontend_has_no_inline_handlers_and_contains_race_and_secret_guards (test_gui.GuiSecurityRegressionTests.test_frontend_has_no_inline_handlers_and_contains_race_and_secret_guards) ... ok
test_protected_bridge_restart_requires_key_before_stopping (test_gui.GuiSecurityRegressionTests.test_protected_bridge_restart_requires_key_before_stopping) ... ok
test_provider_limits_response_cannot_overwrite_a_new_engine_selection (test_gui.GuiSecurityRegressionTests.test_provider_limits_response_cannot_overwrite_a_new_engine_selection) ... ok
test_task_names_never_escape_logdir_or_become_options (test_gui.GuiSecurityRegressionTests.test_task_names_never_escape_logdir_or_become_options) ... ok
test_token_bootstrap_redirects_to_queryless_url_and_uses_httponly_cookie (test_gui.GuiSecurityRegressionTests.test_token_bootstrap_redirects_to_queryless_url_and_uses_httponly_cookie) ... ok
test_tokenless_local_post_requires_json_and_same_origin_headers (test_gui.GuiSecurityRegressionTests.test_tokenless_local_post_requires_json_and_same_origin_headers) ... ok
test_choose_port_moves_off_a_port_held_by_a_foreign_server (test_gui.IsOurPanelTests.test_choose_port_moves_off_a_port_held_by_a_foreign_server)
A plain listening socket that never speaks HTTP stands in for the unrelated ... ok
test_choose_port_uses_the_asked_port_when_free (test_gui.IsOurPanelTests.test_choose_port_uses_the_asked_port_when_free) ... ok
test_nothing_listening_is_not_our_panel (test_gui.IsOurPanelTests.test_nothing_listening_is_not_our_panel) ... ok
test_port_available_agrees_that_a_free_port_is_free (test_gui.IsOurPanelTests.test_port_available_agrees_that_a_free_port_is_free) ... ok
test_malformed_values_are_refused_without_touching_a_backend (test_gui.KillPidNonPositiveTests.test_malformed_values_are_refused_without_touching_a_backend) ... ok
test_numeric_zero_and_minus_one_are_refused (test_gui.KillPidNonPositiveTests.test_numeric_zero_and_minus_one_are_refused) ... 2026-10-14 03:04:36,186 WARNING kill_pid: refusing non-positive pid 0
2026-10-14 03:04:36,186 WARNING kill_pid: refusing non-positive pid -1
ok
test_refusal_of_nonpositive_pid_logs_a_sanitized_warning (test_gui.KillPidNonPositiveTests.test_refusal_of_nonpositive_pid_logs_a_sanitized_warning) ... ok
test_string_zero_and_minus_one_are_refused (test_gui.KillPidNonPositiveTests.test_string_zero_and_minus_one_are_refused) ... 2026-10-14 03:04:36,186 WARNING kill_pid: refusing non-positive pid 0
2026-10-14 03:04:36,186 WARNING kill_pid: refusing non-positive pid -1
ok
test_bool_pids_are_refused_without_touching_a_backend (test_gui.KillPidTypeStrictnessTests.test_bool_pids_are_refused_without_touching_a_backend) ... 2026-10-14 03:04:36,187 WARNING kill_pid: refusing non-integer pid True
2026-10-14 03:04:36,187 WARNING kill_pid: refusing non-integer pid False
ok
test_float_pids_are_refused_without_touching_a_backend (test_gui.KillPidTypeStrictnessTests.test_float_pids_are_refused_without_touching_a_backend) ... 2026-10-14 03:04:36,187 WARNING kill_pid: refusing non-integer pid 1.0
2026-10-14 03:04:36,187 WARNING kill_pid: refusing non-integer pid 1.9
2026-10-14 03:04:36,187 WARNING kill_pid: refusing non-integer pid -2.0
2026-10-14 03:04:36,187 WARNING kill_pid: refusing non-integer pid -2.5
2026-10-14 03:04:36,187 WARNING kill_pid: refusing non-integer pid 0.5
2026-10-14 03:04:36,187 WARNING kill_pid: refusing non-integer pid 4242.0
2026-10-14 03:04:36,187 WARNING kill_pid: refusing non-integer pid 4242.999
2026-10-14 03:04:36,187 WARNING kill_pid: refusing non-integer pid inf
2026-10-14 03:04:36,187 WARNING kill_pid: refusing non-integer pid nan
ok
test_positive_int_pid_still_reaches_the_backend_once (test_gui.KillPidTypeStrictnessTests.test_positive_int_pid_still_reaches_the_backend_once) ... ok
test_positive_string_pid_is_still_accepted (test_gui.KillPidTypeStrictnessTests.test_positive_string_pid_is_still_accepted) ... ok
test_refusal_of_bool_or_float_pid_logs_a_sanitized_warning (test_gui.KillPidTypeStrictnessTests.test_refusal_of_bool_or_float_pid_logs_a_sanitized_warning) ... ok
test_defaults_are_loopback_port_8765_no_token (test_gui.LanTokenTests.test_defaults_are_loopback_port_8765_no_token) ... ok
test_env_host_also_needs_a_token (test_gui.LanTokenTests.test_env_host_also_needs_a_token) ... ok
test_env_port_used_when_no_argument (test_gui.LanTokenTests.test_env_port_used_when_no_argument) ... ok
test_lan_with_a_token_binds_all_interfaces (test_gui.LanTokenTests.test_lan_with_a_token_binds_all_interfaces) ... ok
test_lan_without_a_token_refuses_to_start (test_gui.LanTokenTests.test_lan_without_a_token_refuses_to_start) ... ok
test_localhost_flag_overrides_env_host_and_needs_no_token (test_gui.LanTokenTests.test_localhost_flag_overrides_env_host_and_needs_no_token) ... ok
test_positional_port_wins_over_env (test_gui.LanTokenTests.test_positional_port_wins_over_env) ... ok
test_token_can_come_from_the_environment (test_gui.LanTokenTests.test_token_can_come_from_the_environment) ... ok
test_unknown_argument_is_an_explicit_error (test_gui.LanTokenTests.test_unknown_argument_is_an_explicit_error) ... ok
test_lists_locales_from_scratch_dir (test_gui.ListLocalesTests.test_lists_locales_from_scratch_dir) ... ok
test_malformed_locale_file_is_skipped_not_raised (test_gui.ListLocalesTests.test_malformed_locale_file_is_skipped_not_raised) ... ok
test_missing_locales_dir_returns_empty_list (test_gui.ListLocalesTests.test_missing_locales_dir_returns_empty_list) ... ok
test_claude_stream_json_envelope (test_gui.ParseDialogTests.test_claude_stream_json_envelope) ... ok
test_codex_jsonl_stream (test_gui.ParseDialogTests.test_codex_jsonl_stream) ... ok
test_current_format_steps_prompt_and_text (test_gui.ParseDialogTests.test_current_format_steps_prompt_and_text) ... ok
test_empty_log_gives_no_steps (test_gui.ParseDialogTests.test_empty_log_gives_no_steps) ... ok
test_json_looking_prose_is_not_misparsed_as_a_stream (test_gui.ParseDialogTests.test_json_looking_prose_is_not_misparsed_as_a_stream) ... ok
test_kimi_jsonl_stream_tool_result_attached (test_gui.ParseDialogTests.test_kimi_jsonl_stream_tool_result_attached) ... ok
test_leading_blank_line_creates_no_phantom_step (test_gui.ParseDialogTests.test_leading_blank_line_creates_no_phantom_step) ... ok
test_legacy_codex_plaintext_blocks (test_gui.ParseDialogTests.test_legacy_codex_plaintext_blocks) ... ok
test_legacy_codex_wall_time_duration (test_gui.ParseDialogTests.test_legacy_codex_wall_time_duration) ... ok
test_no_timestamps_means_no_duration (test_gui.ParseDialogTests.test_no_timestamps_means_no_duration) ... ok
test_parse_output_blocks_never_raises_on_none_like_input (test_gui.ParseDialogTests.test_parse_output_blocks_never_raises_on_none_like_input) ... ok
test_step_duration_from_next_header_and_mtime (test_gui.ParseDialogTests.test_step_duration_from_next_header_and_mtime) ... ok
test_unrecognised_format_falls_back_to_raw_text (test_gui.ParseDialogTests.test_unrecognised_format_falls_back_to_raw_text) ... ok
test_restart_aborts_on_expected_id_mismatch_without_spawning (test_gui.RestartBridgeTests.test_restart_aborts_on_expected_id_mismatch_without_spawning) ... ok
test_restart_aborts_on_held_unverifiable_bridge_without_spawning (test_gui.RestartBridgeTests.test_restart_aborts_on_held_unverifiable_bridge_without_spawning) ... ok
test_restart_aborts_on_kill_failure_without_spawning (test_gui.RestartBridgeTests.test_restart_aborts_on_kill_failure_without_spawning) ... 2026-10-14 03:04:36,235 WARNING stop_bridge: refusing to drop registry for port 54230 -- its recorded process did not terminate
ok
test_restart_legacy_record_without_instance_id_fails_closed_honestly (test_gui.RestartBridgeTests.test_restart_legacy_record_without_instance_id_fails_closed_honestly)
An id-less legacy record cannot authorize a kill or restart. ... ok
test_restart_preflights_claude_model_and_effort_before_stopping (test_gui.RestartBridgeTests.test_restart_preflights_claude_model_and_effort_before_stopping) ... ok
test_restart_preflights_provider_network_rule_before_stopping (test_gui.RestartBridgeTests.test_restart_preflights_provider_network_rule_before_stopping) ... ok
test_restart_proceeds_after_stale_registry_prune_on_same_port (test_gui.RestartBridgeTests.test_restart_proceeds_after_stale_registry_prune_on_same_port) ... ok
test_restart_rejects_every_unpassable_launch_value_before_stopping (test_gui.RestartBridgeTests.test_restart_rejects_every_unpassable_launch_value_before_stopping) ... ok
test_each_retention_regression_check_catches_a_production_defect (test_gui.RetainedGuiDefectProofTests.test_each_retention_regression_check_catches_a_production_defect) ... ok
test_compact_digest_remains_visible_after_raw_log_pruning (test_gui.RetainedTaskTests.test_compact_digest_remains_visible_after_raw_log_pruning) ... ok
test_digest_activity_emoji_survives_pruned_raw_log (test_gui.RetainedTaskTests.test_digest_activity_emoji_survives_pruned_raw_log) ... ok
test_final_answer_survives_pruning_without_becoming_provider_json (test_gui.RetainedTaskTests.test_final_answer_survives_pruning_without_becoming_provider_json) ... ok
test_gui_progress_is_opt_in_and_uses_unambiguous_flag (test_gui.RetainedTaskTests.test_gui_progress_is_opt_in_and_uses_unambiguous_flag) ... ok
test_gui_read_prunes_expired_log_preserves_answer_and_honors_opt_ins (test_gui.RetainedTaskTests.test_gui_read_prunes_expired_log_preserves_answer_and_honors_opt_ins) ... ok
test_limited_and_silent_keep_reason_and_have_explicit_icons (test_gui.RetainedTaskTests.test_limited_and_silent_keep_reason_and_have_explicit_icons) ... ok
test_new_artifact_suffixes_preserve_path_guard (test_gui.RetainedTaskTests.test_new_artifact_suffixes_preserve_path_guard) ... ok
test_prompt_title_survives_pruned_and_rotated_logs (test_gui.RetainedTaskTests.test_prompt_title_survives_pruned_and_rotated_logs) ... ok
test_retained_activity_is_the_liveness_clock (test_gui.RetainedTaskTests.test_retained_activity_is_the_liveness_clock) ... ok
test_retained_answer_cache_refreshes_when_answer_changes (test_gui.RetainedTaskTests.test_retained_answer_cache_refreshes_when_answer_changes) ... ok
test_retained_answer_obeys_default_payload_cap (test_gui.RetainedTaskTests.test_retained_answer_obeys_default_payload_cap) ... ok
test_absolute_path_traversal_is_rejected_403 (test_gui.ServeStaticTraversalGuardTests.test_absolute_path_traversal_is_rejected_403) ... ok
test_directory_traversal_via_dotdot_is_rejected_403 (test_gui.ServeStaticTraversalGuardTests.test_directory_traversal_via_dotdot_is_rejected_403) ... ok
test_legitimate_path_is_served_200 (test_gui.ServeStaticTraversalGuardTests.test_legitimate_path_is_served_200) ... ok
test_missing_file_within_root_is_404 (test_gui.ServeStaticTraversalGuardTests.test_missing_file_within_root_is_404) ... ok
test_background_spawn_is_windowless_by_default (test_gui.SpawnVisibilityTests.test_background_spawn_is_windowless_by_default) ... ok
test_terminal_true_is_the_only_visible_console_opt_in (test_gui.SpawnVisibilityTests.test_terminal_true_is_the_only_visible_console_opt_in) ... ok
test_api_key_is_forwarded_only_in_child_environment (test_gui.StartBridgeTests.test_api_key_is_forwarded_only_in_child_environment) ... ok
test_existing_git_bash_directory_is_accepted_on_windows (test_gui.StartBridgeTests.test_existing_git_bash_directory_is_accepted_on_windows) ... ok
test_lan_without_a_key_is_rejected_before_spawn (test_gui.StartBridgeTests.test_lan_without_a_key_is_rejected_before_spawn) ... ok
test_launch_fails_closed_when_popen_rejects_argv_or_environment (test_gui.StartBridgeTests.test_launch_fails_closed_when_popen_rejects_argv_or_environment) ... 2026-10-14 03:04:37,023 WARNING bridge launch failed before process creation (OSError)
2026-10-14 03:04:37,023 WARNING bridge launch failed before process creation (ValueError)
ok
test_legitimate_opaque_values_and_existing_directory_are_forwarded_verbatim (test_gui.StartBridgeTests.test_legitimate_opaque_values_and_existing_directory_are_forwarded_verbatim) ... ok
test_no_key_means_no_flag_and_stays_the_open_bridge (test_gui.StartBridgeTests.test_no_key_means_no_flag_and_stays_the_open_bridge) ... ok
test_bad_port_is_refused (test_gui.StopBridgeTests.test_bad_port_is_refused) ... ok
test_bool_or_float_recorded_pid_fails_closed_and_keeps_registry (test_gui.StopBridgeTests.test_bool_or_float_recorded_pid_fails_closed_and_keeps_registry)
Matching identity, but the recorded pid is a bool/float: kill_pid must refuse the ... 2026-10-14 03:04:37,035 WARNING kill_pid: refusing non-integer pid True
2026-10-14 03:04:37,035 WARNING stop_bridge: refusing to drop registry for port 54254 -- its recorded process did not terminate
2026-10-14 03:04:37,047 WARNING kill_pid: refusing non-integer pid 1.5
2026-10-14 03:04:37,047 WARNING stop_bridge: refusing to drop registry for port 54255 -- its recorded process did not terminate
ok
test_busy_bridge_with_unreachable_health_preserves_file_and_errors (test_gui.StopBridgeTests.test_busy_bridge_with_unreachable_health_preserves_file_and_errors) ... ok
test_expected_instance_id_against_a_record_missing_one_is_a_mismatch (test_gui.StopBridgeTests.test_expected_instance_id_against_a_record_missing_one_is_a_mismatch) ... ok
test_expected_instance_id_equal_to_the_registry_kills_normally (test_gui.StopBridgeTests.test_expected_instance_id_equal_to_the_registry_kills_normally) ... ok
test_free_port_with_unreachable_health_prunes_the_stale_registry (test_gui.StopBridgeTests.test_free_port_with_unreachable_health_prunes_the_stale_registry) ... ok
test_kill_pid_failure_keeps_registry_and_fails_closed (test_gui.StopBridgeTests.test_kill_pid_failure_keeps_registry_and_fails_closed) ... 2026-10-14 03:04:37,103 WARNING stop_bridge: refusing to drop registry for port 54262 -- its recorded process did not terminate
ok
test_live_bridge_with_different_identity_never_kills (test_gui.StopBridgeTests.test_live_bridge_with_different_identity_never_kills) ... ok
test_live_bridge_without_health_identity_fails_closed_and_keeps_file (test_gui.StopBridgeTests.test_live_bridge_without_health_identity_fails_closed_and_keeps_file) ... ok
test_live_bridge_without_publishable_identity_fails_closed_and_keeps_file (test_gui.StopBridgeTests.test_live_bridge_without_publishable_identity_fails_closed_and_keeps_file) ... ok
test_matching_identity_kills_and_removes_the_registry_file (test_gui.StopBridgeTests.test_matching_identity_kills_and_removes_the_registry_file) ... ok
test_missing_pid_keeps_registry_and_fails_closed (test_gui.StopBridgeTests.test_missing_pid_keeps_registry_and_fails_closed) ... 2026-10-14 03:04:37,156 WARNING stop_bridge: refusing to drop registry for port 54269 -- no usable process id in its registry entry
ok
test_nonpositive_pid_fails_closed_and_keeps_registry (test_gui.StopBridgeTests.test_nonpositive_pid_fails_closed_and_keeps_registry)
Identity matches, but the recorded pid is unusable (0): the real kill_pid must ... 2026-10-14 03:04:37,170 WARNING stop_bridge: refusing to drop registry for port 54270 -- no usable process id in its registry entry
ok
test_stale_expected_instance_id_refuses_without_touching_anything (test_gui.StopBridgeTests.test_stale_expected_instance_id_refuses_without_touching_anything) ... ok
test_controls_require_token_and_same_origin (test_gui.TaskControlRouteTests.test_controls_require_token_and_same_origin) ... ok
test_invalid_inputs_and_missing_task_never_launch (test_gui.TaskControlRouteTests.test_invalid_inputs_and_missing_task_never_launch) ... ok
test_last_activity_uses_the_shared_digest_without_exposing_arguments (test_gui.TaskControlRouteTests.test_last_activity_uses_the_shared_digest_without_exposing_arguments) ... ok
test_send_preserves_option_looking_text_and_forwards_now (test_gui.TaskControlRouteTests.test_send_preserves_option_looking_text_and_forwards_now) ... ok
test_stop_failure_and_launch_failure_are_reported (test_gui.TaskControlRouteTests.test_stop_failure_and_launch_failure_are_reported) ... ok
test_stop_uses_wrapper_and_waits_for_its_result (test_gui.TaskControlRouteTests.test_stop_uses_wrapper_and_waits_for_its_result) ... ok
test_stopped_and_undelivered_inbox_are_visible (test_gui.TaskControlRouteTests.test_stopped_and_undelivered_inbox_are_visible) ... ok
test_run_terminal_is_hidden_by_default_and_visible_only_for_true (test_gui.TerminalOptInRouteTests.test_run_terminal_is_hidden_by_default_and_visible_only_for_true) ... ok
test_string_false_cannot_accidentally_open_run_or_reply_terminal (test_gui.TerminalOptInRouteTests.test_string_false_cannot_accidentally_open_run_or_reply_terminal) ... ok
test_already_unix_path_passes_through (test_gui.ToGitBashPathTests.test_already_unix_path_passes_through) ... ok
test_empty_string_passes_through (test_gui.ToGitBashPathTests.test_empty_string_passes_through) ... ok
test_lowercases_drive_letter (test_gui.ToGitBashPathTests.test_lowercases_drive_letter) ... ok
test_none_like_falsy_input_passes_through (test_gui.ToGitBashPathTests.test_none_like_falsy_input_passes_through) ... ok
test_windows_backslash_drive_path (test_gui.ToGitBashPathTests.test_windows_backslash_drive_path) ... ok
test_windows_forward_slash_drive_path (test_gui.ToGitBashPathTests.test_windows_forward_slash_drive_path) ... ok
test_empty_title_falls_back_to_default (test_gui.TopicEmojiTests.test_empty_title_falls_back_to_default) ... ok
test_none_title_falls_back_to_default (test_gui.TopicEmojiTests.test_none_title_falls_back_to_default) ... ok
test_russian_keyword_matches_same_topic (test_gui.TopicEmojiTests.test_russian_keyword_matches_same_topic) ... ok
test_title_matching_bug_topic (test_gui.TopicEmojiTests.test_title_matching_bug_topic) ... ok
test_title_matching_nothing_falls_back_to_default (test_gui.TopicEmojiTests.test_title_matching_nothing_falls_back_to_default) ... ok
test_coerce_port_accepts_only_1_65535 (test_gui.WaitTimeoutAndPortTests.test_coerce_port_accepts_only_1_65535) ... ok
test_start_bridge_rejects_a_bad_port_without_spawning (test_gui.WaitTimeoutAndPortTests.test_start_bridge_rejects_a_bad_port_without_spawning) ... ok
test_wait_timeout_garbage_falls_back_to_default (test_gui.WaitTimeoutAndPortTests.test_wait_timeout_garbage_falls_back_to_default) ... ok
test_wait_timeout_is_clamped_to_finite_0_300 (test_gui.WaitTimeoutAndPortTests.test_wait_timeout_is_clamped_to_finite_0_300) ... ok

----------------------------------------------------------------------
Ran 172 tests in 5.229s

OK
```

#### C:\Users\User\AppData\Local\Programs\Python\Python313\python.exe -m unittest discover -s tests -p test_fleet_gui.py -v

Exit code: 0

```text
test_auth_token_cookie_csrf_and_network_host_guards (test_fleet_gui.FleetHTTPTests.test_auth_token_cookie_csrf_and_network_host_guards) ... ok
test_corrupt_events_never_become_successful_empty_replay_or_ack (test_fleet_gui.FleetHTTPTests.test_corrupt_events_never_become_successful_empty_replay_or_ack) ... ok
test_corrupt_torn_or_unknown_receipts_have_unknown_counts_not_zero (test_fleet_gui.FleetHTTPTests.test_corrupt_torn_or_unknown_receipts_have_unknown_counts_not_zero) ... ok
test_cross_task_scalar_ahead_and_invalid_consumer_cursors_fail (test_fleet_gui.FleetHTTPTests.test_cross_task_scalar_ahead_and_invalid_consumer_cursors_fail) ... ok
test_declared_overlap_and_shared_delta_are_honest_cached_evidence (test_fleet_gui.FleetHTTPTests.test_declared_overlap_and_shared_delta_are_honest_cached_evidence) ... ok
test_fleet_integrates_filters_current_state_models_cost_and_cached_delta (test_fleet_gui.FleetHTTPTests.test_fleet_integrates_filters_current_state_models_cost_and_cached_delta) ... ok
test_individual_controls_inherit_auth_csrf_without_launching_real_agents (test_fleet_gui.FleetHTTPTests.test_individual_controls_inherit_auth_csrf_without_launching_real_agents) ... ok
test_json_body_limits_malformed_and_unknown_fields (test_fleet_gui.FleetHTTPTests.test_json_body_limits_malformed_and_unknown_fields) ... ok
test_receipts_show_uuid_and_separate_acceptance_delivery_explicit_ack (test_fleet_gui.FleetHTTPTests.test_receipts_show_uuid_and_separate_acceptance_delivery_explicit_ack) ... ok
test_request_uuid_poll_never_guesses_latest_receipt_or_task_exit_success (test_fleet_gui.FleetHTTPTests.test_request_uuid_poll_never_guesses_latest_receipt_or_task_exit_success) ... ok
test_run_reply_send_private_file_transport_preserves_utf8_option_text_and_large_bodies (test_fleet_gui.FleetHTTPTests.test_run_reply_send_private_file_transport_preserves_utf8_option_text_and_large_bodies) ... ok
test_send_is_launcher_pending_with_exact_uuid_file_transport_and_known_restart (test_fleet_gui.FleetHTTPTests.test_send_is_launcher_pending_with_exact_uuid_file_transport_and_known_restart) ... ok
test_summary_and_event_json_are_byte_bounded_without_skipping_events (test_fleet_gui.FleetHTTPTests.test_summary_and_event_json_are_byte_bounded_without_skipping_events) ... ok
test_task_bound_paging_and_ack_are_persisted_independent_consumers (test_fleet_gui.FleetHTTPTests.test_task_bound_paging_and_ack_are_persisted_independent_consumers) ... ok
test_torn_tail_is_reported_without_replaying_raw_logs (test_fleet_gui.FleetHTTPTests.test_torn_tail_is_reported_without_replaying_raw_logs) ... ok
test_transport_failure_and_invalid_uuid_text_or_options_never_launch_or_leak (test_fleet_gui.FleetHTTPTests.test_transport_failure_and_invalid_uuid_text_or_options_never_launch_or_leak) ... ok
test_traversal_missing_tasks_and_static_fleet_auth (test_fleet_gui.FleetHTTPTests.test_traversal_missing_tasks_and_static_fleet_auth) ... ok
test_owned_process_is_reaped_and_pipes_closed_immediately_after_exit (test_fleet_gui.SpawnResourceTests.test_owned_process_is_reaped_and_pipes_closed_immediately_after_exit) ... ok
test_private_prompt_survives_delayed_wrapper_read_and_active_process_then_is_released (test_fleet_gui.SpawnResourceTests.test_private_prompt_survives_delayed_wrapper_read_and_active_process_then_is_released) ... ok

----------------------------------------------------------------------
Ran 19 tests in 10.284s

OK
```

#### node tests/test_fleet.js

Exit code: 0

```text
PASS: safe task, receipt and event rendering; ack follows DOM success
PASS: failed ack retries persisted cursor without duplicate rows; reconnect uses confirmed cursor
PASS: partial DOM failure never acknowledges; duplicate prefix is not displayed twice
PASS: malformed/cross-task cursors and records fail without acknowledgment
PASS: late event fetch after selecting another task or leaving tab never renders/acks
PASS: individual controls use current task and legacy paths; sends carry one stable UUID and remain pending
PASS: refresh removes deleted selection and failed refresh disables controls honestly
PASS: filter race rejects stale responses and fetches updated filter
PASS: view bounds old rendered events after persisted ack; independent consumers
PASS: evicted views replay with a fresh consumer and do not acknowledge past their DOM
PASS: receipt polling correlates the submitted UUID, rejects unrelated receipts and never resends unknown outcomes
PASS: late receipt responses cannot overwrite newer persisted facts or a newer submission
```

#### node tests/test_models.js

Exit code: 0

```text
PASS: model-specific modes, unknown hints, metadata, stale response guard
```

#### node tests/test_toast.js

Exit code: 0

```text
ok - toast history coalescing
```

#### C:\Users\User\AppData\Local\Programs\Python\Python313\python.exe -m py_compile gui.py tests/test_fleet_gui.py tests/test_gui.py

Exit code: 0

```text
```

#### node --check static/app.js

Exit code: 0

```text
```

#### node --check static/fleet.js

Exit code: 0

```text
```

#### node --check static/chat.js

Exit code: 0

```text
```

#### node --check static/i18n.js

Exit code: 0

```text
```




## Archived initial evidence (superseded by final follow-up above)

Recorded 2026-10-14. All source snapshots matched before and after the initial
verification wave. Owned sources are final at these hashes. The 15 Python fleet
checks, 10 Node fleet checks, existing model/toast checks and syntax checks pass.
The unmodified GUI suite has 171 passes and the one conflicting acceptance
assertion failure (172 total); that check exits 1. Fixture servers shut down and
short owned processes exited. No worker/provider or idle terminal is left running.
Live browser/visible-terminal QA and CLI message-ID correlation remain with Root.

| File | Ownership | SHA-256 |
| --- | --- | --- |
| gui.py | owned | `1f0482d182014a77bf117ae0ac8ee85948ba8f42a45775554689b9bae0ee1c19` |
| gui.html | owned | `744fb9f7aa362356ffb1970aa2b2df98a0332f1523c67438c185a59ca644ff55` |
| static/app.js | owned | `5fc10cd5714f59cc8f9d7450ee3c5b55e3c161d24be201834675aecdb32097b4` |
| static/fleet.js | owned | `286a560c82e1e8c3c9675c10f1000b14019d3f214fa2d25ea4a5b4b8ba5b451b` |
| static/style.css | owned | `65bf4434006bc2853deda98814fd1f028d2a0541ae6920552db0fbcec59a2ab9` |
| static/i18n.js | owned | `84e6cc8f28adb490b903c3fb9c9891810bf6a7de24220e74131b98fc266a68d5` |
| tests/test_fleet_gui.py | owned | `ab90b324f30e9a9ad72bc646da97b1c3e5c5960df69fdf86343bb1955f08a871` |
| tests/test_fleet.js | owned | `0f225930c57c054edc1b652464aedb2d8d7e6495330c60fe756a89b54cb2e3a6` |
| neoxider_agents/events.py | read-only snapshot | `69572b7641928d1587bcf4c4296a21e37af84ca175cc56ca366fc67832dd7257` |
| neoxider_agents/state.py | read-only snapshot | `c412640a64cc537ecaa421666006ed4ba097673ad940ab66d58f7988adac7397` |
| neoxider_agents/message_receipts.py | read-only snapshot | `419d0907ee409c04d095253aee08815a75413cb29e05bd7829851f9f687d6a74` |
| tests/test_gui.py | read-only snapshot | `a505c11f0859789cb2ea0ecad24b451ffec592a91a0df78b5a76ecb21fc5c5cd` |

#### C:\Users\User\AppData\Local\Programs\Python\Python313\python.exe -m unittest discover -s tests -p test_fleet_gui.py -v

Exit code: 0

```text
test_auth_token_cookie_csrf_and_network_host_guards (test_fleet_gui.FleetHTTPTests.test_auth_token_cookie_csrf_and_network_host_guards) ... ok
test_corrupt_events_never_become_successful_empty_replay_or_ack (test_fleet_gui.FleetHTTPTests.test_corrupt_events_never_become_successful_empty_replay_or_ack) ... ok
test_corrupt_torn_or_unknown_receipts_have_unknown_counts_not_zero (test_fleet_gui.FleetHTTPTests.test_corrupt_torn_or_unknown_receipts_have_unknown_counts_not_zero) ... ok
test_cross_task_scalar_ahead_and_invalid_consumer_cursors_fail (test_fleet_gui.FleetHTTPTests.test_cross_task_scalar_ahead_and_invalid_consumer_cursors_fail) ... ok
test_declared_overlap_and_shared_delta_are_honest_cached_evidence (test_fleet_gui.FleetHTTPTests.test_declared_overlap_and_shared_delta_are_honest_cached_evidence) ... ok
test_fleet_integrates_filters_current_state_models_cost_and_cached_delta (test_fleet_gui.FleetHTTPTests.test_fleet_integrates_filters_current_state_models_cost_and_cached_delta) ... ok
test_individual_controls_inherit_auth_csrf_without_launching_real_agents (test_fleet_gui.FleetHTTPTests.test_individual_controls_inherit_auth_csrf_without_launching_real_agents) ... ok
test_json_body_limits_malformed_and_unknown_fields (test_fleet_gui.FleetHTTPTests.test_json_body_limits_malformed_and_unknown_fields) ... ok
test_receipts_show_uuid_and_separate_acceptance_delivery_explicit_ack (test_fleet_gui.FleetHTTPTests.test_receipts_show_uuid_and_separate_acceptance_delivery_explicit_ack) ... ok
test_send_is_launcher_pending_and_resume_uses_known_restart_without_uuid_flags (test_fleet_gui.FleetHTTPTests.test_send_is_launcher_pending_and_resume_uses_known_restart_without_uuid_flags) ... ok
test_summary_and_event_json_are_byte_bounded_without_skipping_events (test_fleet_gui.FleetHTTPTests.test_summary_and_event_json_are_byte_bounded_without_skipping_events) ... ok
test_task_bound_paging_and_ack_are_persisted_independent_consumers (test_fleet_gui.FleetHTTPTests.test_task_bound_paging_and_ack_are_persisted_independent_consumers) ... ok
test_torn_tail_is_reported_without_replaying_raw_logs (test_fleet_gui.FleetHTTPTests.test_torn_tail_is_reported_without_replaying_raw_logs) ... ok
test_traversal_missing_tasks_and_static_fleet_auth (test_fleet_gui.FleetHTTPTests.test_traversal_missing_tasks_and_static_fleet_auth) ... ok
test_owned_process_is_reaped_and_pipes_closed_immediately_after_exit (test_fleet_gui.SpawnResourceTests.test_owned_process_is_reaped_and_pipes_closed_immediately_after_exit) ... ok

----------------------------------------------------------------------
Ran 15 tests in 7.960s

OK
```

#### node tests/test_fleet.js

Exit code: 0

```text
PASS: safe task, receipt and event rendering; ack follows DOM success
PASS: failed ack retries persisted cursor without duplicate rows; reconnect uses confirmed cursor
PASS: partial DOM failure never acknowledges; duplicate prefix is not displayed twice
PASS: malformed/cross-task cursors and records fail without acknowledgment
PASS: late event fetch after selecting another task or leaving tab never renders/acks
PASS: individual controls use current task and legacy paths; sends remain pending with no invented UUID flag
PASS: refresh removes deleted selection and failed refresh disables controls honestly
PASS: filter race rejects stale responses and fetches updated filter
PASS: view bounds old rendered events after persisted ack; independent consumers
PASS: evicted views replay with a fresh consumer and do not acknowledge past their DOM
```

#### C:\Users\User\AppData\Local\Programs\Python\Python313\python.exe -m unittest discover -s tests -p test_gui.py -v

Exit code: 1

```text
test_done_state_maps_to_done_emoji (test_gui.ActivityEmojiTests.test_done_state_maps_to_done_emoji) ... ok
test_error_state_maps_to_error_emoji (test_gui.ActivityEmojiTests.test_error_state_maps_to_error_emoji) ... ok
test_running_state_reads_log_for_activity_hint (test_gui.ActivityEmojiTests.test_running_state_reads_log_for_activity_hint) ... ok
test_unknown_non_running_state_falls_back_to_bullet (test_gui.ActivityEmojiTests.test_unknown_non_running_state_falls_back_to_bullet) ... ok
test_waiting_state_maps_to_waiting_emoji (test_gui.ActivityEmojiTests.test_waiting_state_maps_to_waiting_emoji) ... ok
test_get_test_api_is_404_too (test_gui.ApiTestRouteRemovedTests.test_get_test_api_is_404_too) ... ok
test_kind_api_test_rows_still_display_from_cli_meta (test_gui.ApiTestRouteRemovedTests.test_kind_api_test_rows_still_display_from_cli_meta) ... ok
test_post_test_api_is_404_not_found (test_gui.ApiTestRouteRemovedTests.test_post_test_api_is_404_not_found) ... ok
test_concurrent_start_loser_does_not_accept_winners_registry (test_gui.BridgeReadyIdentityTests.test_concurrent_start_loser_does_not_accept_winners_registry) ... ok
test_registry_from_spawned_child_is_accepted (test_gui.BridgeReadyIdentityTests.test_registry_from_spawned_child_is_accepted) ... ok
test_windows_bash_descendant_registry_is_accepted (test_gui.BridgeReadyIdentityTests.test_windows_bash_descendant_registry_is_accepted) ... ok
test_route_forwards_body_to_restart_bridge (test_gui.BridgeRestartRouteTests.test_route_forwards_body_to_restart_bridge) ... ok
test_route_forwards_instance_id_to_stop_bridge (test_gui.BridgeStopRouteTests.test_route_forwards_instance_id_to_stop_bridge) ... ok
test_default_payload_caps_huge_blocks_full_does_not (test_gui.DialogPayloadTests.test_default_payload_caps_huge_blocks_full_does_not) ... ok
test_default_payload_is_last_n_steps_with_has_more (test_gui.DialogPayloadTests.test_default_payload_is_last_n_steps_with_has_more) ... ok
test_endpoint_requires_task_param (test_gui.DialogPayloadTests.test_endpoint_requires_task_param) ... ok
test_endpoint_serves_dialog_json (test_gui.DialogPayloadTests.test_endpoint_serves_dialog_json)
H.do_GET for /api/dialog, driven through a fake handler (no real socket). ... ok
test_missing_log_is_empty_payload_not_exception (test_gui.DialogPayloadTests.test_missing_log_is_empty_payload_not_exception) ... ok
test_payload_fields_and_single_step (test_gui.DialogPayloadTests.test_payload_fields_and_single_step) ... ok
test_cache_first_loads_persistent_snapshot_and_starts_refresh_without_waiting (test_gui.DoctorTests.test_cache_first_loads_persistent_snapshot_and_starts_refresh_without_waiting) ... ok
test_doctor_frontend_renders_structured_limits_and_keeps_raw_toggle (test_gui.DoctorTests.test_doctor_frontend_renders_structured_limits_and_keeps_raw_toggle) ... ok
test_failed_refresh_keeps_last_good_raw_snapshot (test_gui.DoctorTests.test_failed_refresh_keeps_last_good_raw_snapshot) ... ok
test_parses_legacy_doctor_text_into_engine_and_limit_rows (test_gui.DoctorTests.test_parses_legacy_doctor_text_into_engine_and_limit_rows) ... ok
test_idle_counts_as_live (test_gui.EffStateTests.test_idle_counts_as_live) ... ok
test_missing_state_key_defaults_to_question_mark (test_gui.EffStateTests.test_missing_state_key_defaults_to_question_mark) ... ok
test_other_states_pass_through_unchanged (test_gui.EffStateTests.test_other_states_pass_through_unchanged) ... ok
test_pid_alive_returns_none_when_no_pid_recorded (test_gui.EffStateTests.test_pid_alive_returns_none_when_no_pid_recorded) ... ok
test_pid_alive_says_an_impossible_pid_is_dead (test_gui.EffStateTests.test_pid_alive_says_an_impossible_pid_is_dead) ... ok
test_pid_alive_says_this_very_process_is_alive (test_gui.EffStateTests.test_pid_alive_says_this_very_process_is_alive) ... ok
test_running_with_dead_pid_is_stalled_even_with_a_fresh_log (test_gui.EffStateTests.test_running_with_dead_pid_is_stalled_even_with_a_fresh_log) ... ok
test_running_with_fresh_mtime_stays_running (test_gui.EffStateTests.test_running_with_fresh_mtime_stays_running) ... ok
test_running_with_live_pid_and_fresh_log_is_running (test_gui.EffStateTests.test_running_with_live_pid_and_fresh_log_is_running) ... ok
test_running_with_live_pid_and_silent_log_is_idle_not_stalled (test_gui.EffStateTests.test_running_with_live_pid_and_silent_log_is_idle_not_stalled) ... ok
test_running_with_no_log_mtime_stays_running (test_gui.EffStateTests.test_running_with_no_log_mtime_stays_running) ... ok
test_running_with_stale_mtime_and_unknown_pid_becomes_stalled (test_gui.EffStateTests.test_running_with_stale_mtime_and_unknown_pid_becomes_stalled) ... ok
test_hours (test_gui.FmtDurTests.test_hours) ... ok
test_minutes_and_seconds (test_gui.FmtDurTests.test_minutes_and_seconds) ... ok
test_none_and_negative_are_empty (test_gui.FmtDurTests.test_none_and_negative_are_empty) ... ok
test_round_minute_drops_zero_seconds (test_gui.FmtDurTests.test_round_minute_drops_zero_seconds) ... ok
test_rounding_near_minute_boundary (test_gui.FmtDurTests.test_rounding_near_minute_boundary) ... ok
test_sub_ten_seconds_one_decimal (test_gui.FmtDurTests.test_sub_ten_seconds_one_decimal) ... ok
test_whole_seconds_no_decimal (test_gui.FmtDurTests.test_whole_seconds_no_decimal) ... ok
test_configured_token_is_required_even_on_loopback (test_gui.GuiAuthTests.test_configured_token_is_required_even_on_loopback) ... ok
test_loopback_forms_recognised (test_gui.GuiAuthTests.test_loopback_forms_recognised) ... ok
test_network_client_without_the_token_is_rejected (test_gui.GuiAuthTests.test_network_client_without_the_token_is_rejected) ... ok
test_no_token_configured_means_open_panel (test_gui.GuiAuthTests.test_no_token_configured_means_open_panel) ... ok
test_run_endpoint_is_gated_too (test_gui.GuiAuthTests.test_run_endpoint_is_gated_too)
/api/run spawns a real subagent -- the whole reason the token exists. ... ok
test_token_accepted_from_query_header_or_cookie (test_gui.GuiAuthTests.test_token_accepted_from_query_header_or_cookie) ... ok
test_failed_health_probe_is_debug_gated_and_leaks_no_url (test_gui.GuiLoggingTests.test_failed_health_probe_is_debug_gated_and_leaks_no_url) ... ok
test_kill_pid_failure_warns_with_exception_type_only (test_gui.GuiLoggingTests.test_kill_pid_failure_warns_with_exception_type_only) ... ok
test_kill_pid_nonzero_returncode_warns_with_status_only (test_gui.GuiLoggingTests.test_kill_pid_nonzero_returncode_warns_with_status_only) ... ok
test_request_lines_route_to_debug_only (test_gui.GuiLoggingTests.test_request_lines_route_to_debug_only) ... ok
test_token_in_request_line_is_redacted (test_gui.GuiLoggingTests.test_token_in_request_line_is_redacted) ... ok
test_invalid_or_non_object_json_is_400 (test_gui.GuiPostBodyGuardTests.test_invalid_or_non_object_json_is_400) ... ok
test_malformed_and_negative_content_length_are_400 (test_gui.GuiPostBodyGuardTests.test_malformed_and_negative_content_length_are_400) ... ok
test_oversized_content_length_is_413_before_any_read (test_gui.GuiPostBodyGuardTests.test_oversized_content_length_is_413_before_any_read) ... ok
test_valid_empty_body_reaches_routing (test_gui.GuiPostBodyGuardTests.test_valid_empty_body_reaches_routing) ... ok
test_bool_and_float_ports_and_unknown_provider_never_spawn (test_gui.GuiSecurityRegressionTests.test_bool_and_float_ports_and_unknown_provider_never_spawn) ... ok
test_frontend_has_no_inline_handlers_and_contains_race_and_secret_guards (test_gui.GuiSecurityRegressionTests.test_frontend_has_no_inline_handlers_and_contains_race_and_secret_guards) ... ok
test_protected_bridge_restart_requires_key_before_stopping (test_gui.GuiSecurityRegressionTests.test_protected_bridge_restart_requires_key_before_stopping) ... ok
test_provider_limits_response_cannot_overwrite_a_new_engine_selection (test_gui.GuiSecurityRegressionTests.test_provider_limits_response_cannot_overwrite_a_new_engine_selection) ... ok
test_task_names_never_escape_logdir_or_become_options (test_gui.GuiSecurityRegressionTests.test_task_names_never_escape_logdir_or_become_options) ... ok
test_token_bootstrap_redirects_to_queryless_url_and_uses_httponly_cookie (test_gui.GuiSecurityRegressionTests.test_token_bootstrap_redirects_to_queryless_url_and_uses_httponly_cookie) ... ok
test_tokenless_local_post_requires_json_and_same_origin_headers (test_gui.GuiSecurityRegressionTests.test_tokenless_local_post_requires_json_and_same_origin_headers) ... ok
test_choose_port_moves_off_a_port_held_by_a_foreign_server (test_gui.IsOurPanelTests.test_choose_port_moves_off_a_port_held_by_a_foreign_server)
A plain listening socket that never speaks HTTP stands in for the unrelated ... ok
test_choose_port_uses_the_asked_port_when_free (test_gui.IsOurPanelTests.test_choose_port_uses_the_asked_port_when_free) ... ok
test_nothing_listening_is_not_our_panel (test_gui.IsOurPanelTests.test_nothing_listening_is_not_our_panel) ... ok
test_port_available_agrees_that_a_free_port_is_free (test_gui.IsOurPanelTests.test_port_available_agrees_that_a_free_port_is_free) ... ok
test_malformed_values_are_refused_without_touching_a_backend (test_gui.KillPidNonPositiveTests.test_malformed_values_are_refused_without_touching_a_backend) ... ok
test_numeric_zero_and_minus_one_are_refused (test_gui.KillPidNonPositiveTests.test_numeric_zero_and_minus_one_are_refused) ... 2026-10-14 02:29:28,698 WARNING kill_pid: refusing non-positive pid 0
2026-10-14 02:29:28,698 WARNING kill_pid: refusing non-positive pid -1
ok
test_refusal_of_nonpositive_pid_logs_a_sanitized_warning (test_gui.KillPidNonPositiveTests.test_refusal_of_nonpositive_pid_logs_a_sanitized_warning) ... ok
test_string_zero_and_minus_one_are_refused (test_gui.KillPidNonPositiveTests.test_string_zero_and_minus_one_are_refused) ... 2026-10-14 02:29:28,698 WARNING kill_pid: refusing non-positive pid 0
2026-10-14 02:29:28,698 WARNING kill_pid: refusing non-positive pid -1
ok
test_bool_pids_are_refused_without_touching_a_backend (test_gui.KillPidTypeStrictnessTests.test_bool_pids_are_refused_without_touching_a_backend) ... 2026-10-14 02:29:28,698 WARNING kill_pid: refusing non-integer pid True
2026-10-14 02:29:28,699 WARNING kill_pid: refusing non-integer pid False
ok
test_float_pids_are_refused_without_touching_a_backend (test_gui.KillPidTypeStrictnessTests.test_float_pids_are_refused_without_touching_a_backend) ... 2026-10-14 02:29:28,699 WARNING kill_pid: refusing non-integer pid 1.0
2026-10-14 02:29:28,699 WARNING kill_pid: refusing non-integer pid 1.9
2026-10-14 02:29:28,699 WARNING kill_pid: refusing non-integer pid -2.0
2026-10-14 02:29:28,699 WARNING kill_pid: refusing non-integer pid -2.5
2026-10-14 02:29:28,699 WARNING kill_pid: refusing non-integer pid 0.5
2026-10-14 02:29:28,699 WARNING kill_pid: refusing non-integer pid 4242.0
2026-10-14 02:29:28,699 WARNING kill_pid: refusing non-integer pid 4242.999
2026-10-14 02:29:28,699 WARNING kill_pid: refusing non-integer pid inf
2026-10-14 02:29:28,699 WARNING kill_pid: refusing non-integer pid nan
ok
test_positive_int_pid_still_reaches_the_backend_once (test_gui.KillPidTypeStrictnessTests.test_positive_int_pid_still_reaches_the_backend_once) ... ok
test_positive_string_pid_is_still_accepted (test_gui.KillPidTypeStrictnessTests.test_positive_string_pid_is_still_accepted) ... ok
test_refusal_of_bool_or_float_pid_logs_a_sanitized_warning (test_gui.KillPidTypeStrictnessTests.test_refusal_of_bool_or_float_pid_logs_a_sanitized_warning) ... ok
test_defaults_are_loopback_port_8765_no_token (test_gui.LanTokenTests.test_defaults_are_loopback_port_8765_no_token) ... ok
test_env_host_also_needs_a_token (test_gui.LanTokenTests.test_env_host_also_needs_a_token) ... ok
test_env_port_used_when_no_argument (test_gui.LanTokenTests.test_env_port_used_when_no_argument) ... ok
test_lan_with_a_token_binds_all_interfaces (test_gui.LanTokenTests.test_lan_with_a_token_binds_all_interfaces) ... ok
test_lan_without_a_token_refuses_to_start (test_gui.LanTokenTests.test_lan_without_a_token_refuses_to_start) ... ok
test_localhost_flag_overrides_env_host_and_needs_no_token (test_gui.LanTokenTests.test_localhost_flag_overrides_env_host_and_needs_no_token) ... ok
test_positional_port_wins_over_env (test_gui.LanTokenTests.test_positional_port_wins_over_env) ... ok
test_token_can_come_from_the_environment (test_gui.LanTokenTests.test_token_can_come_from_the_environment) ... ok
test_unknown_argument_is_an_explicit_error (test_gui.LanTokenTests.test_unknown_argument_is_an_explicit_error) ... ok
test_lists_locales_from_scratch_dir (test_gui.ListLocalesTests.test_lists_locales_from_scratch_dir) ... ok
test_malformed_locale_file_is_skipped_not_raised (test_gui.ListLocalesTests.test_malformed_locale_file_is_skipped_not_raised) ... ok
test_missing_locales_dir_returns_empty_list (test_gui.ListLocalesTests.test_missing_locales_dir_returns_empty_list) ... ok
test_claude_stream_json_envelope (test_gui.ParseDialogTests.test_claude_stream_json_envelope) ... ok
test_codex_jsonl_stream (test_gui.ParseDialogTests.test_codex_jsonl_stream) ... ok
test_current_format_steps_prompt_and_text (test_gui.ParseDialogTests.test_current_format_steps_prompt_and_text) ... ok
test_empty_log_gives_no_steps (test_gui.ParseDialogTests.test_empty_log_gives_no_steps) ... ok
test_json_looking_prose_is_not_misparsed_as_a_stream (test_gui.ParseDialogTests.test_json_looking_prose_is_not_misparsed_as_a_stream) ... ok
test_kimi_jsonl_stream_tool_result_attached (test_gui.ParseDialogTests.test_kimi_jsonl_stream_tool_result_attached) ... ok
test_leading_blank_line_creates_no_phantom_step (test_gui.ParseDialogTests.test_leading_blank_line_creates_no_phantom_step) ... ok
test_legacy_codex_plaintext_blocks (test_gui.ParseDialogTests.test_legacy_codex_plaintext_blocks) ... ok
test_legacy_codex_wall_time_duration (test_gui.ParseDialogTests.test_legacy_codex_wall_time_duration) ... ok
test_no_timestamps_means_no_duration (test_gui.ParseDialogTests.test_no_timestamps_means_no_duration) ... ok
test_parse_output_blocks_never_raises_on_none_like_input (test_gui.ParseDialogTests.test_parse_output_blocks_never_raises_on_none_like_input) ... ok
test_step_duration_from_next_header_and_mtime (test_gui.ParseDialogTests.test_step_duration_from_next_header_and_mtime) ... ok
test_unrecognised_format_falls_back_to_raw_text (test_gui.ParseDialogTests.test_unrecognised_format_falls_back_to_raw_text) ... ok
test_restart_aborts_on_expected_id_mismatch_without_spawning (test_gui.RestartBridgeTests.test_restart_aborts_on_expected_id_mismatch_without_spawning) ... ok
test_restart_aborts_on_held_unverifiable_bridge_without_spawning (test_gui.RestartBridgeTests.test_restart_aborts_on_held_unverifiable_bridge_without_spawning) ... ok
test_restart_aborts_on_kill_failure_without_spawning (test_gui.RestartBridgeTests.test_restart_aborts_on_kill_failure_without_spawning) ... 2026-10-14 02:29:28,735 WARNING stop_bridge: refusing to drop registry for port 64059 -- its recorded process did not terminate
ok
test_restart_legacy_record_without_instance_id_fails_closed_honestly (test_gui.RestartBridgeTests.test_restart_legacy_record_without_instance_id_fails_closed_honestly)
An id-less legacy record cannot authorize a kill or restart. ... ok
test_restart_preflights_claude_model_and_effort_before_stopping (test_gui.RestartBridgeTests.test_restart_preflights_claude_model_and_effort_before_stopping) ... ok
test_restart_preflights_provider_network_rule_before_stopping (test_gui.RestartBridgeTests.test_restart_preflights_provider_network_rule_before_stopping) ... ok
test_restart_proceeds_after_stale_registry_prune_on_same_port (test_gui.RestartBridgeTests.test_restart_proceeds_after_stale_registry_prune_on_same_port) ... ok
test_restart_rejects_every_unpassable_launch_value_before_stopping (test_gui.RestartBridgeTests.test_restart_rejects_every_unpassable_launch_value_before_stopping) ... ok
test_each_retention_regression_check_catches_a_production_defect (test_gui.RetainedGuiDefectProofTests.test_each_retention_regression_check_catches_a_production_defect) ... ok
test_compact_digest_remains_visible_after_raw_log_pruning (test_gui.RetainedTaskTests.test_compact_digest_remains_visible_after_raw_log_pruning) ... ok
test_digest_activity_emoji_survives_pruned_raw_log (test_gui.RetainedTaskTests.test_digest_activity_emoji_survives_pruned_raw_log) ... ok
test_final_answer_survives_pruning_without_becoming_provider_json (test_gui.RetainedTaskTests.test_final_answer_survives_pruning_without_becoming_provider_json) ... ok
test_gui_progress_is_opt_in_and_uses_unambiguous_flag (test_gui.RetainedTaskTests.test_gui_progress_is_opt_in_and_uses_unambiguous_flag) ... ok
test_gui_read_prunes_expired_log_preserves_answer_and_honors_opt_ins (test_gui.RetainedTaskTests.test_gui_read_prunes_expired_log_preserves_answer_and_honors_opt_ins) ... ok
test_limited_and_silent_keep_reason_and_have_explicit_icons (test_gui.RetainedTaskTests.test_limited_and_silent_keep_reason_and_have_explicit_icons) ... ok
test_new_artifact_suffixes_preserve_path_guard (test_gui.RetainedTaskTests.test_new_artifact_suffixes_preserve_path_guard) ... ok
test_prompt_title_survives_pruned_and_rotated_logs (test_gui.RetainedTaskTests.test_prompt_title_survives_pruned_and_rotated_logs) ... ok
test_retained_activity_is_the_liveness_clock (test_gui.RetainedTaskTests.test_retained_activity_is_the_liveness_clock) ... ok
test_retained_answer_cache_refreshes_when_answer_changes (test_gui.RetainedTaskTests.test_retained_answer_cache_refreshes_when_answer_changes) ... ok
test_retained_answer_obeys_default_payload_cap (test_gui.RetainedTaskTests.test_retained_answer_obeys_default_payload_cap) ... ok
test_absolute_path_traversal_is_rejected_403 (test_gui.ServeStaticTraversalGuardTests.test_absolute_path_traversal_is_rejected_403) ... ok
test_directory_traversal_via_dotdot_is_rejected_403 (test_gui.ServeStaticTraversalGuardTests.test_directory_traversal_via_dotdot_is_rejected_403) ... ok
test_legitimate_path_is_served_200 (test_gui.ServeStaticTraversalGuardTests.test_legitimate_path_is_served_200) ... ok
test_missing_file_within_root_is_404 (test_gui.ServeStaticTraversalGuardTests.test_missing_file_within_root_is_404) ... ok
test_background_spawn_is_windowless_by_default (test_gui.SpawnVisibilityTests.test_background_spawn_is_windowless_by_default) ... ok
test_terminal_true_is_the_only_visible_console_opt_in (test_gui.SpawnVisibilityTests.test_terminal_true_is_the_only_visible_console_opt_in) ... ok
test_api_key_is_forwarded_only_in_child_environment (test_gui.StartBridgeTests.test_api_key_is_forwarded_only_in_child_environment) ... ok
test_existing_git_bash_directory_is_accepted_on_windows (test_gui.StartBridgeTests.test_existing_git_bash_directory_is_accepted_on_windows) ... ok
test_lan_without_a_key_is_rejected_before_spawn (test_gui.StartBridgeTests.test_lan_without_a_key_is_rejected_before_spawn) ... ok
test_launch_fails_closed_when_popen_rejects_argv_or_environment (test_gui.StartBridgeTests.test_launch_fails_closed_when_popen_rejects_argv_or_environment) ... 2026-10-14 02:29:29,174 WARNING bridge launch failed before process creation (OSError)
2026-10-14 02:29:29,174 WARNING bridge launch failed before process creation (ValueError)
ok
test_legitimate_opaque_values_and_existing_directory_are_forwarded_verbatim (test_gui.StartBridgeTests.test_legitimate_opaque_values_and_existing_directory_are_forwarded_verbatim) ... ok
test_no_key_means_no_flag_and_stays_the_open_bridge (test_gui.StartBridgeTests.test_no_key_means_no_flag_and_stays_the_open_bridge) ... ok
test_bad_port_is_refused (test_gui.StopBridgeTests.test_bad_port_is_refused) ... ok
test_bool_or_float_recorded_pid_fails_closed_and_keeps_registry (test_gui.StopBridgeTests.test_bool_or_float_recorded_pid_fails_closed_and_keeps_registry)
Matching identity, but the recorded pid is a bool/float: kill_pid must refuse the ... 2026-10-14 02:29:29,182 WARNING kill_pid: refusing non-integer pid True
2026-10-14 02:29:29,182 WARNING stop_bridge: refusing to drop registry for port 64069 -- its recorded process did not terminate
2026-10-14 02:29:29,188 WARNING kill_pid: refusing non-integer pid 1.5
2026-10-14 02:29:29,188 WARNING stop_bridge: refusing to drop registry for port 64070 -- its recorded process did not terminate
ok
test_busy_bridge_with_unreachable_health_preserves_file_and_errors (test_gui.StopBridgeTests.test_busy_bridge_with_unreachable_health_preserves_file_and_errors) ... ok
test_expected_instance_id_against_a_record_missing_one_is_a_mismatch (test_gui.StopBridgeTests.test_expected_instance_id_against_a_record_missing_one_is_a_mismatch) ... ok
test_expected_instance_id_equal_to_the_registry_kills_normally (test_gui.StopBridgeTests.test_expected_instance_id_equal_to_the_registry_kills_normally) ... ok
test_free_port_with_unreachable_health_prunes_the_stale_registry (test_gui.StopBridgeTests.test_free_port_with_unreachable_health_prunes_the_stale_registry) ... ok
test_kill_pid_failure_keeps_registry_and_fails_closed (test_gui.StopBridgeTests.test_kill_pid_failure_keeps_registry_and_fails_closed) ... 2026-10-14 02:29:29,223 WARNING stop_bridge: refusing to drop registry for port 64075 -- its recorded process did not terminate
ok
test_live_bridge_with_different_identity_never_kills (test_gui.StopBridgeTests.test_live_bridge_with_different_identity_never_kills) ... ok
test_live_bridge_without_health_identity_fails_closed_and_keeps_file (test_gui.StopBridgeTests.test_live_bridge_without_health_identity_fails_closed_and_keeps_file) ... ok
test_live_bridge_without_publishable_identity_fails_closed_and_keeps_file (test_gui.StopBridgeTests.test_live_bridge_without_publishable_identity_fails_closed_and_keeps_file) ... ok
test_matching_identity_kills_and_removes_the_registry_file (test_gui.StopBridgeTests.test_matching_identity_kills_and_removes_the_registry_file) ... ok
test_missing_pid_keeps_registry_and_fails_closed (test_gui.StopBridgeTests.test_missing_pid_keeps_registry_and_fails_closed) ... 2026-10-14 02:29:29,256 WARNING stop_bridge: refusing to drop registry for port 64080 -- no usable process id in its registry entry
ok
test_nonpositive_pid_fails_closed_and_keeps_registry (test_gui.StopBridgeTests.test_nonpositive_pid_fails_closed_and_keeps_registry)
Identity matches, but the recorded pid is unusable (0): the real kill_pid must ... 2026-10-14 02:29:29,263 WARNING stop_bridge: refusing to drop registry for port 64081 -- no usable process id in its registry entry
ok
test_stale_expected_instance_id_refuses_without_touching_anything (test_gui.StopBridgeTests.test_stale_expected_instance_id_refuses_without_touching_anything) ... ok
test_controls_require_token_and_same_origin (test_gui.TaskControlRouteTests.test_controls_require_token_and_same_origin) ... ok
test_invalid_inputs_and_missing_task_never_launch (test_gui.TaskControlRouteTests.test_invalid_inputs_and_missing_task_never_launch) ... ok
test_last_activity_uses_the_shared_digest_without_exposing_arguments (test_gui.TaskControlRouteTests.test_last_activity_uses_the_shared_digest_without_exposing_arguments) ... ok
test_send_preserves_option_looking_text_and_forwards_now (test_gui.TaskControlRouteTests.test_send_preserves_option_looking_text_and_forwards_now) ... FAIL
test_stop_failure_and_launch_failure_are_reported (test_gui.TaskControlRouteTests.test_stop_failure_and_launch_failure_are_reported) ... ok
test_stop_uses_wrapper_and_waits_for_its_result (test_gui.TaskControlRouteTests.test_stop_uses_wrapper_and_waits_for_its_result) ... ok
test_stopped_and_undelivered_inbox_are_visible (test_gui.TaskControlRouteTests.test_stopped_and_undelivered_inbox_are_visible) ... ok
test_run_terminal_is_hidden_by_default_and_visible_only_for_true (test_gui.TerminalOptInRouteTests.test_run_terminal_is_hidden_by_default_and_visible_only_for_true) ... ok
test_string_false_cannot_accidentally_open_run_or_reply_terminal (test_gui.TerminalOptInRouteTests.test_string_false_cannot_accidentally_open_run_or_reply_terminal) ... ok
test_already_unix_path_passes_through (test_gui.ToGitBashPathTests.test_already_unix_path_passes_through) ... ok
test_empty_string_passes_through (test_gui.ToGitBashPathTests.test_empty_string_passes_through) ... ok
test_lowercases_drive_letter (test_gui.ToGitBashPathTests.test_lowercases_drive_letter) ... ok
test_none_like_falsy_input_passes_through (test_gui.ToGitBashPathTests.test_none_like_falsy_input_passes_through) ... ok
test_windows_backslash_drive_path (test_gui.ToGitBashPathTests.test_windows_backslash_drive_path) ... ok
test_windows_forward_slash_drive_path (test_gui.ToGitBashPathTests.test_windows_forward_slash_drive_path) ... ok
test_empty_title_falls_back_to_default (test_gui.TopicEmojiTests.test_empty_title_falls_back_to_default) ... ok
test_none_title_falls_back_to_default (test_gui.TopicEmojiTests.test_none_title_falls_back_to_default) ... ok
test_russian_keyword_matches_same_topic (test_gui.TopicEmojiTests.test_russian_keyword_matches_same_topic) ... ok
test_title_matching_bug_topic (test_gui.TopicEmojiTests.test_title_matching_bug_topic) ... ok
test_title_matching_nothing_falls_back_to_default (test_gui.TopicEmojiTests.test_title_matching_nothing_falls_back_to_default) ... ok
test_coerce_port_accepts_only_1_65535 (test_gui.WaitTimeoutAndPortTests.test_coerce_port_accepts_only_1_65535) ... ok
test_start_bridge_rejects_a_bad_port_without_spawning (test_gui.WaitTimeoutAndPortTests.test_start_bridge_rejects_a_bad_port_without_spawning) ... ok
test_wait_timeout_garbage_falls_back_to_default (test_gui.WaitTimeoutAndPortTests.test_wait_timeout_garbage_falls_back_to_default) ... ok
test_wait_timeout_is_clamped_to_finite_0_300 (test_gui.WaitTimeoutAndPortTests.test_wait_timeout_is_clamped_to_finite_0_300) ... ok

======================================================================
FAIL: test_send_preserves_option_looking_text_and_forwards_now (test_gui.TaskControlRouteTests.test_send_preserves_option_looking_text_and_forwards_now)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\AI\work\neoxider-agents-native-parity-20261014\tests\test_gui.py", line 1767, in test_send_preserves_option_looking_text_and_forwards_now
    self.assertTrue(result["accepted"])
    ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^
AssertionError: None is not true

----------------------------------------------------------------------
Ran 172 tests in 4.756s

FAILED (failures=1)
```

#### node tests/test_models.js

Exit code: 0

```text
PASS: model-specific modes, unknown hints, metadata, stale response guard
```

#### node tests/test_toast.js

Exit code: 0

```text
ok - toast history coalescing
```

#### C:\Users\User\AppData\Local\Programs\Python\Python313\python.exe -m py_compile gui.py tests/test_fleet_gui.py

Exit code: 0

```text
```

#### node --check static/app.js

Exit code: 0

```text
```

#### node --check static/fleet.js

Exit code: 0

```text
```

#### node --check static/i18n.js

Exit code: 0

```text
```


