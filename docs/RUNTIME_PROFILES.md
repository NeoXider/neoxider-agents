# Retained provider runtime profiles (phase 6)

`neoxider_agents/runtime_profiles.py` is integrated with optional CLI
`--isolate-runtime` and SDK `spawn(..., isolate_runtime=True)`. See
[Core integration](CORE_INTEGRATION.md#retained-isolated-runtimes).
Python 3.8+, standard library only. Backend creation/validation do not launch a provider. Only
an explicit optional version probe launches the pinned executable with `--version`.
No API/global setting is changed, and no authentication/home directory is copied.

```python
from neoxider_agents.runtime_profiles import RuntimeProfiles

profiles = RuntimeProfiles("C:/private/agent-profiles")  # parent already exists
manifest = profiles.create("task-01", "opencode", "C:/tools/opencode.exe",
                           version="1.2.3", credential_env_keys=("OPENAI_API_KEY",))
env = profiles.environment("task-01")
# Launch manifest['executable']['path'] with env, then retain the profile on exit.
# Resume uses load/environment on the same task, never create or a new XDG home.
profiles.validate("task-01")
# Only after independently establishing the task is stopped:
profiles.cleanup("task-01", live=False)
```

`create(task, engine, executable, version=None, seed_files=None, base_env=None,
credential_env_keys=())` atomically reserves one task directory; collisions fail.
Names reuse `state.valid_name`: 1–128 ASCII characters, an initial letter/digit,
then letters/digits/dots/underscores/hyphens. `foo.v2` and 128-character names
remain valid. Trailing dots/spaces and Windows device stems (also `con.v2`)
are refused. Case aliases collide on every platform, including concurrent creators;
the original legacy directory spelling is retained. Per-casefold advisory guard
files stay in the owned root; deleting their inodes while another creator holds
them would break serialization. OS locks are released when their owner exits.
`load` and `validate` return newly parsed, validated manifest dictionaries.
`environment(task, base_env=None)` reconstructs the same absolute runtime paths.
`create(base_env=...)` is deprecated and unused; a non-None value emits
`DeprecationWarning`. It neither configures launches nor gets retained. Supply
launch values to `environment(task, base_env=...)`. Every environment call reads
the current selected credentials, so credential rotation does not change the pin.
`cleanup(task, live=False)` explicitly destroys the profile and invalidates resume.
Do not recreate a destroyed task and expect its old session to exist.

## Provider contract

| Provider | Implemented mapping | Authentication |
| --- | --- | --- |
| OpenCode | Distinct `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME` | Explicit allowlisted credential environment or separately authorized private provisioning |
| Codex | `CODEX_HOME` = private task `state` directory | Same opt-in rule; global `auth.json` is never copied |
| Claude, Gemini, Kimi, unknown | Explicit unsupported error | No substitute `HOME` recovery |

OpenCode launch environments set `OPENCODE_DISABLE_MODELS_FETCH=1`; a seeded
catalog also sets `OPENCODE_MODELS_PATH` inside the profile. These are per-launch
values, not settings changes. Inherited OpenCode/Codex/XDG override variables are
discarded, including `OPENCODE_DB`, so they cannot redirect session storage.
OpenCode may use data for its SQLite state even though a separate state path is
provided. The four paths remain distinct and stable.

Knob references: [OpenCode CLI environment variables](https://opencode.ai/docs/cli/)
and [OpenCode global paths source](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/global/index.ts);
[Codex advanced configuration](https://developers.openai.com/codex/config-advanced/)
documents `CODEX_HOME`. Supporting another provider requires its actual supported
storage knob and an acceptance test. Generic HOME rewrites are insufficient.

The manifest pins the absolute executable path and SHA-256. Missing, changed,
linked or hardlinked executables fail closed; PATH lookup is never used. A supplied
version is labelled `supplied-unverified`, never observed or verified. An omitted
version is `unknown`. `probe_version(task, timeout=5, max_bytes=4096, base_env=None)`
is an explicit optional probe, never automatically called by create/resume. It runs
only `[pinned_executable, "--version"]`, with no shell or credential variables, in
the retained runtime environment. Timeout is bounded to 30 seconds and combined
stdout/stderr to 64 KiB (defaults 5 seconds/4 KiB). Windows creation is hidden and
suspended until assigned to a private kill-on-close Job; POSIX uses a new process
group. Success/failure/timeout closes that process tree and reaps its leader; cleanup
waits add at most two seconds each for the leader and reader. No global signal
handlers or launcher lifetime Job are installed by this probe.

An accepted single version line yields an immutable `version-receipt.json` containing
the observed numeric version, executable pin, manifest/output SHA-256 and receipt
integrity hash. Arbitrary output is not persisted. The receipt's provenance is
`observed-executable-output`, not vendor verification. `version_receipt(task)` reads
it; load validates it when present. Re-probing an existing receipt is refused.
The supplied version in the original manifest remains unverified and unchanged.
Timeout, excessive output, nonzero exit, malformed output or executable drift
produces no successful receipt. A receipt write interrupted by a crash is refused
on resume rather than silently accepted. Executable hashing does not verify vendor provenance or interpreter,
shim target and transitive dependencies. Callers should pin the actual executable,
not a launcher script, and validate immediately before a hidden, shell-free launch.

## Privacy and ownership

Only ordinary launch variables (`PATH`, `PATHEXT`, `SYSTEMROOT`, `WINDIR`,
`COMSPEC`, `TEMP`, `TMP`, `TMPDIR`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`, `HOME`,
`USERPROFILE`) plus named credential keys pass through. The parent mapping and
`os.environ` are unchanged. Credential names are retained; their values never are.
Credential keys cannot override system/provider path variables. The credential
allowlist is bounded to 64 names of at most 128 characters each. HOME/USERPROFILE
are passed unchanged, not used as isolation substitutes. This is storage isolation,
not a sandbox: providers may still read project files, `.env`, or other accessible
files. Downstream provider credential persistence is outside this module's control.

`seed_files` is a destination-to-source-path mapping. Only public JSON is accepted:
OpenCode `config/opencode/opencode.json`, `cache/opencode/models.json`; Codex
`state/models.json`. There is no directory copy and no auth/config TOML import.
Files are bounded to 1 MiB, hashed, and validated again on resume. Validation occurs
before root/task creation, preserving an existing ownership marker on rejection.
OpenCode config seeds accept only `model` and `small_model`, each a bounded
`provider/model` selector string. Empty config objects are allowed. Provider config,
options, metadata, tools and policy belong in separately authorized caller launch
configuration; they are not accepted as public config seeds.

Model catalog seeds accept JSON objects/arrays describing public models, including
boolean `modeltoolcapability`/`tool_call`, numeric `max_tokens`, tokenizer names and
other descriptive metadata. Known tool capability fields must be booleans, not
nested tool-policy objects. Field rejection uses exact normalized names rather
than substring matching, so `tokenizer`, `agentic`, and `prompt_price` are not
mistaken for credentials or policy. At every catalog depth, case/punctuation/Unicode
forms of `tools`, `permission`, `permissions`, `agent`, `agents`, `system`, `prompt`,
`hooks`, `subagent` and related policy names are rejected, along with credential,
auth, headers, env, MCP, plugin and command fields. Duplicate keys, non-finite JSON
constants and env/file expansion values are refused. Public capability metadata
does not authorize tools or relax permissions.

These checks cannot prove arbitrary strings contain no secret: the caller must
supply genuinely public data. Source permissions/ACLs are never modified.

New roots receive an ownership marker. Existing unowned roots are refused, even
when empty. Profile and root markers bind exact task/path/root identity; the profile
marker seals the immutable manifest. POSIX creation uses 0700 directories and
0600 files. Windows uses protected owner-only DACLs, inherited by new children;
failure to set permissions aborts creation. Provider runtime/session files remain
writable; the manifest is immutable by API, not a read-only filesystem artifact.
Checksums detect corruption and accidental tampering, not a malicious same-user
process rewriting both manifest and marker. Root/marker loss requires operator
inspection; the implementation never silently adopts or relocates a profile.

Every existing path component is checked with lstat; symlinks, junctions and other
Windows reparse points are refused, as are regular files with multiple hardlinks.
Cleanup validates ownership/seal and preflights the complete tree before unlinking
only descendants of the exact task directory. A linked descendant refuses cleanup
without deleting the earlier ordinary files. Executable drift does not block explicit
destruction, but marker/manifest tampering does. `live=True` fails. `live=False` is
only the caller's assertion, including when omitted: it performs NO active-process
check and is not evidence that cleanup is safe. This module has no process registry.
SDK completed-process cleanup is integrated and retains the profile/session.
Explicit `client.cleanup_runtime(handle)` separately verifies the retained actual
lease, PID creation stamps, generation, parent, inactive processes and ownership
locks before invoking backend destruction. Backend `live=False` alone is still
insufficient. Callers must serialize direct backend cleanup and optional probing
against every active runtime writer. Profiles are never automatically TTL-deleted.

There is no claim of race-proof protection against concurrent hostile replacement
between checks and filesystem operations, particularly on Windows. Keep roots under
a trusted private parent and serialize cleanup against launch/create/runtime writers.
Exclusive mkdir protects same-task creation collisions, not cleanup/launch races.
Disk failures abort creation and remove the exclusively reserved task tree when its
paths remain safe; a process crash can leave an incomplete reservation that is refused
on resume and must be inspected rather than overwritten. Profiles have no automatic TTL.

## Verification

Run `python -B -m unittest discover -s tests -p test_runtime_profiles.py -v`.
Tests include real concurrent child processes, same-task collision, disjoint paths,
distinct SQLite writers and retained session-marker resume; executable drift,
manifest tamper, link cleanup, seed restrictions, environment/secret handling,
unsupported providers, live refusal and disk failure; dotted/128-character legacy
names, sequential/concurrent case aliases, deprecated create environment handling,
and version receipts. The actual executable probe test runs Python `--version`,
not OpenCode; additional real child fixtures cover timeout, overflow and nonzero
exit and assert shell-free hidden launch plus credential exclusion.
Seed regressions cover the previously accepted tools/permission config, nested
case/disguised policy fields, preserved ownership markers, safe catalog metadata,
strict model selectors and duplicate-key policy hiding.
Fixture subprocesses use `CREATE_NO_WINDOW` on Windows, no shell, bounded waits,
and registered kill/reap cleanup. They do not prove OpenCode's own session protocol.
Two concurrent real OpenCode runs and subsequent real-session resume remain a Root
live-provider gate when a usable free route becomes available. Rate limits are not
a live PASS. No live provider session is run by this suite.
