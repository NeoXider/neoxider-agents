"""Typed facade integrating durable receipts/events, reviewed recovery and runtime homes.

Legacy launch defaults and unnamed status callbacks remain compatible. Named consumers
replay durable events; acknowledgments and verification require explicit caller evidence.
All launchers are tracked/hidden, prompts travel through files, and SDK calls never
mutate os.environ or stdout. No OS write sandbox or hosted chat injection is claimed.
"""
import os
import json
import math
import re
import subprocess
import sys
import threading
import time
import uuid
import codecs
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .state import Store, atomic_write, valid_name

LIVE = ("running", "idle", "orphaned")
SETTLED = ("done", "waiting", "error", "silent", "limited", "stopped", "stalled")
QUEUED = re.compile(r"queued \(#(\d+)\)")

CAPABILITIES = {
    "spawn": "managed hidden agent.py run subprocess; prompt via file; bounded owner publication",
    "send_receipt": "durable journal + queue acceptance; stable caller UUID",
    "receipt_id": "UUID; queryable across transport failure and generations",
    "delivered": "successful invocation completion receipt; no semantic application claim",
    "acknowledged": "explicit caller supplied assistant evidence only",
    "events": "Phase 3 durable lifecycle events, named consumer replay and isolated callbacks",
    "resume": "provider dependent (supports_resume); session id required",
    "write_scope": "declaration + delta reporting only; no OS enforcement (Phase 5)",
    "os_sandbox": False,
    "hosted_chat_injection": False,
    "model_substitution": "none: explicit engine/model/effort are resolved and reported back verbatim",
    "reviewed_resume": "explicit retained report SHA; actual source/session/effect/profile reinspection",
    "runtime_isolation": "opt-in codex/opencode retained pinned native executable and private homes",
    "hard_billing_cap": False,
    "cleanup": "automatic completed-resource reaping within 300s; close() stops owned live workers; durable state retained",
}


def capabilities():
    """A copy of the additive capability contract."""
    return dict(CAPABILITIES)


def launch_kwargs():
    """Windows hidden/console-free launch flags; empty elsewhere. Used by every launch."""
    from .process import hidden_kwargs
    return hidden_kwargs()


class OrchestratorError(RuntimeError):
    def __init__(self, message, task=None, command=None, returncode=None, output="", receipt_id=None):
        super().__init__(message)
        self.task = task
        self.command = command
        self.returncode = returncode
        self.output = output
        self.receipt_id = receipt_id


class SpawnError(OrchestratorError):
    """The wrapper never published an owner: bad preflight, CLI missing, or publication timeout."""


class NotOwnedError(OrchestratorError):
    """The task belongs to another parent/orchestrator."""


class ResumeError(OrchestratorError):
    """Resume was refused; nothing was started."""


class ResumeNotSupported(ResumeError):
    """The provider reports supports_resume=false."""


class WaitTimeout(OrchestratorError):
    """The bounded wait deadline expired; the task keeps running."""


class ClientClosed(OrchestratorError):
    pass


@dataclass(frozen=True)
class SendReceipt:
    task: str
    accepted: bool
    sequence: object = None
    sequence_source: str = ""
    queued: bool = False
    resumed: bool = False
    delivered: bool = False
    acknowledged: bool = False
    receipt_id: object = None
    state: str = ""
    detail: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class TaskStatus:
    task: str
    state: str
    live: bool
    owner_alive: bool
    engine: str = ""
    model: str = ""
    effort: str = ""
    session: str = ""
    generation: str = ""
    directory: str = ""
    parent: str = ""
    exit_code: object = None
    reason: str = ""
    files: object = None
    queued: int = 0
    idle_sec: float = 0.0
    error: str = ""
    last_send_error: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class TaskResult:
    task: str
    settled: bool
    state: str
    exit_code: object = None
    session: str = ""
    engine: str = ""
    model: str = ""
    effort: str = ""
    directory: str = ""
    generation: str = ""
    duration_sec: object = None
    answer: str = ""
    error: str = ""
    reason: str = ""
    changed_files: tuple = ()
    files_changed: int = 0
    owns: tuple = ()
    scope_declared: bool = False
    scope_enforced: bool = False
    scope_note: str = ""
    baseline_known: bool = False
    baseline_partial: bool = False
    baseline_warning: str = ""
    attribution: str = ""
    usage: object = None
    tokens: object = None
    cost_usd: object = None
    limit: object = None
    stop_report: str = ""
    queued: int = 0
    detached: bool = False
    failure: object = None
    verification: object = None
    tests_status: str = "not_run"
    qa_status: str = "not_run"
    cost_facts: tuple = ()
    event_pending: bool = False

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class TaskEvent:
    task: str
    kind: str
    state: str
    detail: str = ""
    at: float = 0.0
    event_id: str = ""
    generation: str = ""
    parent: str = ""
    sequence: int = 0
    data: object = None

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class CloseReport:
    stopped: tuple = ()
    retained: tuple = ()
    detached: tuple = ()
    failed: tuple = ()
    pending_consumers: tuple = ()
    pending_threads: tuple = ()
    in_progress: bool = False

    def to_dict(self):
        return asdict(self)


SCOPE_NOTE = ("Declared --owns limits reported paths to a declaration and diff. It is NOT "
              "enforcement: writes outside the declared globs are neither prevented nor proven "
              "absent, and concurrent writers cannot be distinguished.")

ATTRIBUTION = ("Changes are measured against this task's start in its working directory; "
               "concurrent writers cannot be distinguished.")


class _Child:
    __slots__ = ("task", "process", "stamp", "lines", "thread", "command", "prompt_path",
                 "generation", "parent", "settled_at", "reaped", "lock", "control",
                 "start_failed", "start_error", "cleanup_error")

    def __init__(self, task, process, stamp, lines, command):
        self.task = task
        self.process = process
        self.stamp = stamp
        self.lines = lines
        self.thread = None
        self.command = command
        self.prompt_path = None
        self.generation = ""
        self.parent = ""
        self.settled_at = None
        self.reaped = False
        self.lock = threading.RLock()
        self.control = False
        self.start_failed = False
        self.start_error = ""
        self.cleanup_error = ""

    def tail(self, count=6):
        return " | ".join(list(self.lines)[-count:]).strip()

    def alive(self):
        with self.lock:
            return not self.reaped and self.process.poll() is None


@dataclass
class TaskHandle:
    task: str
    client: object = None
    parent: str = ""
    directory: str = ""
    engine: str = ""
    model: str = ""
    effort: str = ""
    resolved_model: str = ""
    requested_engine: str = ""
    requested_model: str = ""
    requested_effort: str = ""
    generation: str = ""
    session: str = ""
    state: str = ""
    queued: int = 0
    exit_code: object = None
    error: str = ""
    owns: tuple = ()
    detached: bool = False
    owned: bool = True
    adopted: bool = False
    adopted_parent: str = ""
    spawn_id: str = ""
    spawned_at: float = 0.0
    settled: bool = False
    _settled_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    @property
    def model_substituted(self):
        """True only when an explicit model resolved to a different provider id."""
        return bool(self.requested_model and self.resolved_model and self.resolved_model != self.requested_model)

    def status(self):
        return self._client().status(self)

    def result(self):
        return self._client().result(self)

    def wait(self, timeout=None):
        return self._client().wait(self, timeout=timeout)

    def stop(self, reason=None):
        return self._client().stop(self, reason=reason)

    def send(self, message, now=False, **kwargs):
        return self._client().send(self, message, now=now, **kwargs)

    def resume(self, message=None, fresh=False, **kwargs):
        return self._client().resume(self, message=message, fresh=fresh, **kwargs)

    def detach(self):
        self.detached = True
        return self

    def to_dict(self):
        """Public snapshot only: the client, its locks and the wait event are never copied."""
        return {
            "task": self.task,
            "client_id": self.client.client_id if self.client is not None else "",
            "parent": self.parent,
            "directory": self.directory,
            "engine": self.engine,
            "model": self.model,
            "effort": self.effort,
            "resolved_model": self.resolved_model,
            "requested_engine": self.requested_engine,
            "requested_model": self.requested_model,
            "requested_effort": self.requested_effort,
            "generation": self.generation,
            "session": self.session,
            "state": self.state,
            "queued": self.queued,
            "exit_code": self.exit_code,
            "error": self.error,
            "owns": tuple(self.owns),
            "detached": self.detached,
            "owned": self.owned,
            "adopted": self.adopted,
            "adopted_parent": self.adopted_parent,
            "spawn_id": self.spawn_id,
            "spawned_at": self.spawned_at,
            "settled": self.settled,
            "model_substituted": self.model_substituted,
        }

    def _client(self):
        if self.client is None:
            raise OrchestratorError("handle '%s' is not bound to a client" % self.task, task=self.task)
        return self.client


class AgentClient:
    """One stable facade: spawn/send/status/stop/resume plus TaskHandle and TaskResult."""

    def __init__(self, logs_root=None, python=None, entry=None, parent=None, env=None,
                 poll=0.2, publish_timeout=30.0, control_timeout=60.0, launcher_output=200,
                 on_event=None, detach=False, consumer=None):
        from .cli import ROOT
        self.store = Store(logs_root)
        self.python = python or sys.executable
        self.entry = str(entry or Path(ROOT) / "agent.py")
        self.client_id = "sdk-" + uuid.uuid4().hex[:12]
        self.base_env = dict(os.environ)
        self.env = dict(env or {})
        self.parent = parent or self.env.get("AGENT_PARENT") or self.env.get("AGENT_ORCHESTRATOR_ID") \
            or self.base_env.get("AGENT_PARENT") or self.base_env.get("AGENT_ORCHESTRATOR_ID") or self.client_id
        self.poll = float(poll)
        if self.poll <= 0 or not math.isfinite(self.poll):
            raise ValueError("AgentClient: poll must be positive")
        self.publish_timeout = float(publish_timeout)
        self.control_timeout = float(control_timeout)
        if min(self.publish_timeout, self.control_timeout) <= 0 or not all(
                math.isfinite(value) for value in (self.publish_timeout, self.control_timeout)):
            raise ValueError("AgentClient: timeouts must be positive")
        self.launcher_output = int(launcher_output)
        self.detach_on_close = bool(detach)
        self.callback_errors = deque(maxlen=64)
        self._callbacks = []
        self._subscriptions = []
        self._handles = {}
        self._bindings = {}  # name -> (handle, original parent, explicit adoption, owner token)
        self._children = []
        self._lock = threading.RLock()
        self._event_lock = threading.RLock()
        self._close_lock = threading.RLock()
        self._wake = threading.Event()
        self._clock = time.monotonic  # injectable so tests can drive the 300s gate deterministically
        self._closing = threading.Event()
        self._closed = False
        self._close_report = None
        if on_event:
            self.subscribe(on_event, consumer=consumer)
        self._monitor = threading.Thread(target=self._monitor_loop, name="neoxider-sdk-monitor", daemon=True)
        self._monitor.start()
        self._reaper = threading.Thread(target=self._resource_loop, name="neoxider-sdk-cleanup", daemon=True)
        self._reaper.start()

    # ---------- contracts ----------

    @staticmethod
    def capabilities():
        return capabilities()

    def subscribe(self, callback, consumer=None):
        """A named consumer replays durable events, acking only after callback return.

        Omit consumer for the legacy in-process status callback contract.
        Each durable subscriber owns an independent thread; no callback runs on the reaper.
        """
        if consumer is not None:
            from .events import valid_consumer, valid_parent
            try:
                consumer = valid_consumer(consumer)
            except ValueError:
                # Parent labels may contain '/', while consumer sidecars cannot.
                consumer = "consumer-" + uuid.uuid5(uuid.NAMESPACE_URL,
                    "neoxider-consumer:" + valid_parent(consumer)).hex
            cancel = threading.Event()
            thread = threading.Thread(target=self._subscription_loop, args=(callback, consumer, cancel),
                                      name="neoxider-sdk-events", daemon=True)
            with self._lock:
                self._check_open()
                if any(who == consumer and not flag.is_set() for who, flag, _ in self._subscriptions):
                    raise ValueError("consumer already attached to this client")
                self._subscriptions.append((consumer, cancel, thread))
                try:
                    thread.start()  # close must never observe an unstarted subscriber
                except BaseException:
                    self._subscriptions.pop()
                    raise
            return cancel.set
        with self._lock:
            self._check_open()
            self._callbacks.append(callback)

        def unsubscribe():
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)
        return unsubscribe

    def _subscription_loop(self, callback, consumer, cancel):
        from .events import EventStore
        events = EventStore(self.store.root)
        while not cancel.is_set() and not self._closed:
            for handle in self.handles():
                if cancel.is_set() or self._closed:
                    break
                try:
                    meta = self._read_owned(handle.task, handle)
                    if meta.get("event_pending") == "1":
                        self.store.publish_events(handle.task)
                    page = events.read(handle.task, after=events.cursor(handle.task, consumer))
                    for record in page:
                        if cancel.is_set() or self._closed:
                            break
                        if record["parent"] != self._bindings[handle.task][1]:
                            raise NotOwnedError("durable event belongs to another parent", task=handle.task)
                        data = record["data"]
                        callback(TaskEvent(handle.task, record["kind"], data.get("state", ""), "",
                                           record["epoch"], record["id"], record["generation"],
                                           record["parent"], record["seq"], data))
                        events.ack(handle.task, consumer, record["seq"])
                except BaseException as error:
                    self._record_error(handle.task, type(error).__name__, str(error))
                    cancel.set()  # failed event remains unacknowledged for the next attach
                    break
            cancel.wait(min(self.poll, 0.25))

    def events(self, ref, after=None, limit=100):
        from .events import EventStore
        name, handle = self._resolve(ref)
        meta = self._read_owned(name, handle)
        if meta.get("event_pending") == "1":
            self.store.publish_events(name)
        return EventStore(self.store.root).read(name, after=after, limit=limit)

    def _emit(self, event):
        with self._event_lock:
            self._emit_once(event)

    def _emit_once(self, event):
        """One pass, never recursive: a failing subscriber is recorded, not re-invoked."""
        with self._lock:
            callbacks = list(self._callbacks)
        if not callbacks:
            return
        healthy, failures = [], []
        for callback in callbacks:
            try:
                callback(event)
                healthy.append(callback)
            except BaseException as error:
                failures.append((callback, type(error).__name__, str(error)))
                with self._lock:
                    if callback in self._callbacks:
                        self._callbacks.remove(callback)
        if not failures or event.kind == "callback_error":
            for _, kind, message in failures:  # a failed notification is recorded, never re-sent
                self._record_error(event.task, kind, message)
            return
        for _, kind, message in failures:
            self._record_error(event.task, kind, message)
        detail = "; ".join("%s: %s" % (kind, message) for _, kind, message in failures)
        notice = TaskEvent(event.task, "callback_error", event.state, detail, time.time())
        for callback in healthy:  # only subscribers that handled THIS event are told
            try:
                callback(notice)
            except BaseException as error:
                self._record_error(event.task, type(error).__name__, str(error))
                with self._lock:
                    if callback in self._callbacks:
                        self._callbacks.remove(callback)

    def _record_error(self, task, kind, message):
        with self._lock:
            self.callback_errors.append((task, kind, message))

    # ---------- spawning ----------

    def spawn(self, prompt, engine=None, model=None, effort=None, cwd=None, name=None, owns=None,
              strict_owns=False, parent=None, timeout=None, progress=False, terse=False, verbose=False,
              keep_logs=False, notify=False, detach=False, env=None, isolate_runtime=False,
              runtime_profile_root=None, credential_env_keys=(), hard_cost_cap=None):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("spawn: needs a non-empty prompt string")
        self._check_open()
        if hard_cost_cap is not None:
            raise OrchestratorError("hard billing caps are unsupported; launch refused")
        task = self._reserve_name(name)
        directory = self._directory(cwd)
        parent_id = parent or self.parent
        argv = ["run", "-t", task, "-C", directory, "-P", parent_id]
        if engine:
            argv += ["-e", str(engine)]
        if isolate_runtime:
            argv.append("--isolate-runtime")
            if runtime_profile_root:
                argv += ["--runtime-profile-root", str(runtime_profile_root)]
            if credential_env_keys:
                argv += ["--credential-env-keys", ",".join(credential_env_keys)]
        if model:
            argv += ["-m", str(model)]
        if effort:
            argv += ["-f", str(effort)]
        if owns:
            argv += ["--owns", str(owns)]
        if strict_owns:
            argv.append("--strict-owns")
        if progress:
            argv.append("--progress")
        if not terse:
            argv.append("--no-terse")
        if verbose:
            argv.append("--verbose")
        if keep_logs:
            argv.append("--log")
        if notify:
            argv.append("--notify")
        prompt_path = self._message_path(task)
        atomic_write(prompt_path, prompt)
        argv += ["--prompt-file", prompt_path.as_posix()]
        spawn_id = uuid.uuid4().hex[:12]
        child_env = self._child_env(env, detached=True)
        try:
            child = self._launch(task, argv, child_env, prompt_path=prompt_path)
        except BaseException:
            # A retained failed-start child owns the prompt until checked reap
            # releases it after actual exit; only unlink when nothing is retained.
            retained = self._child_for(task)
            if retained is None or not getattr(retained, "start_failed", False):
                self._unlink(prompt_path)
            raise
        try:
            meta = self._await_owner(child, task, self.publish_timeout, "spawn")
        except BaseException:
            self._discard(child)
            self._unlink(prompt_path)
            raise
        handle = self._make_handle(task, child, meta, parent_id, directory, spawn_id,
                                   engine, model, effort, owns, detach)
        try:
            with self._lock:
                # The child was admitted before close and is in close cleanup;
                # refuse to publish a post-close handle for it instead of orphaning it.
                self._check_open()
                self._register(handle, meta, adopted=False)
        except BaseException:
            self._discard(child)
            raise
        self._emit(TaskEvent(task, "spawned", handle.state, "owner pid %s generation %s" % (child.process.pid, handle.generation), time.time()))
        return handle

    def send(self, ref, message, now=False, timeout=None, message_id=None,
             reviewed=False, reviewed_report_sha=None):
        """Accept under the inbox lock before optional transport; never retry with a new ID."""
        if not isinstance(message, str) or not message.strip():
            raise ValueError("send: needs a non-empty message string")
        self._check_open()
        self._deadline(timeout)
        from .message_receipts import valid_id
        identifier = valid_id(message_id) if message_id is not None else uuid.uuid4().hex
        name, handle = self._resolve(ref)
        detail, resumed = "", False
        acceptance_started = False
        try:
            from .state import Lock
            from .runtime import private_provider, pinned_argv
            from .lifecycle import reviewed_resume, queued_event
            options = {"--reviewed": reviewed, "reviewed_report_sha": reviewed_report_sha}
            reviewed_resume(self.store, name, options)
            with Lock(self.store.path(name, ".inbox")):
                meta = self._read_owned(name, handle)
                state = self._state(name, meta)
                self._require_resume(name, meta) if state not in ("running", "idle") else None
                provider = self._provider(meta.get("engine", ""))
                if not provider.supports_resume:
                    raise ResumeNotSupported("engine cannot resume", task=name)
                environment = self._child_env()
                pinned = None
                if meta.get("runtime_profile_root"):
                    from .runtime_profiles import RuntimeProfiles
                    profiles = RuntimeProfiles(meta["runtime_profile_root"])
                    manifest = profiles.validate(name)
                    environment = profiles.environment(name, environment)
                    pinned = pinned_argv(meta, manifest)
                provider = private_provider(provider, environment, pinned)
                provider.command(meta.get("resolved_model", ""), meta.get("effort", ""), meta.get("dir", ""))
                if now and (state not in ("running", "idle") or not meta.get("core_version") or not self.store.session(name, meta)):
                    raise ResumeError("send --now requires a running Python owner and saved session", task=name)
                acceptance_started = True
                record = self.store.enqueue_receipt_locked(name, message, identifier)
                queued_event(self.store, name, record)
                if now:
                    from .process import signal_task
                    atomic_write(self.store.path(name, ".control"), json.dumps(dict(action="interrupt",
                        generation=meta.get("generation"), by="orchestrator", reason="send --now")))
                    signal_task(meta)
            # The owner's terminal transition also holds .inbox. If enqueue lost that
            # race, launch a tracked safe drain after durability, never replay unknown attempts.
            current = self._read_owned(name, handle)
            if self._state(name, current) not in ("running", "idle") and record["delivery_status"] == "pending":
                argv = ["send", name, "--flush", "--pending-only", "-P", self._bindings[name][1]]
                if reviewed or reviewed_report_sha:
                    # Enqueue changes the reviewed projection; no launch under an old review.
                    detail = "accepted; review the changed inbox before resuming"
                else:
                    child = self._launch(name, argv, self._child_env(detached=True))
                    try:
                        self._await_owner(child, name, self.publish_timeout, "send")
                        resumed = True
                    except OrchestratorError as error:
                        # The durable receipt is still the authoritative acceptance.
                        detail = str(error)
            self._refresh(name)
        except (OSError, RuntimeError, ValueError) as error:
            if not acceptance_started or isinstance(error, (NotOwnedError, ResumeError)):
                raise
            try:
                record = self.store.receipt(name, identifier)
            except (OSError, RuntimeError, ValueError):
                raise OrchestratorError("send outcome unknown; query receipt_id=%s: %s" % (identifier, error), task=name, receipt_id=identifier) from error
            from .message_receipts import digest
            if record and record["digest"] != digest(message):
                raise
            if not record or record["state"] not in ("accepted", "delivered", "acknowledged"):
                raise OrchestratorError("send acceptance unfinished; query receipt_id=%s: %s" % (identifier, error), task=name, receipt_id=identifier) from error
            detail = "accepted durably; transport failed: " + str(error)
        return self._send_receipt(name, record, resumed=resumed, detail=detail)

    def _send_receipt(self, name, record, resumed=False, detail=""):
        return SendReceipt(task=name, accepted=record["state"] in ("accepted", "delivered", "acknowledged"),
            sequence=record["sequence"], sequence_source="durable receipt journal (legacy sequence retained)",
            queued=record["state"] in ("accepted", "acknowledged") and record["delivery_status"] != "delivered", resumed=resumed,
            delivered=record["delivery_status"] == "delivered", acknowledged=record["state"] == "acknowledged",
            receipt_id=record["id"], state=record["state"], detail=detail)

    def receipt(self, ref, message_id):
        name, handle = self._resolve(ref)
        self._read_owned(name, handle)
        record = self.store.receipt(name, message_id)
        return self._send_receipt(name, record) if record else None

    def receipts(self, ref):
        name, handle = self._resolve(ref)
        self._read_owned(name, handle)
        return tuple(self._send_receipt(name, record) for record in self.store.receipts(name))

    def acknowledge(self, ref, message_id, evidence, source="assistant"):
        name, handle = self._resolve(ref)
        self._read_owned(name, handle)
        return self._send_receipt(name, self.store.acknowledge_message(name, message_id, source=source, text=evidence))

    def inspect_recovery(self, ref):
        from .lifecycle import inspect_recovery
        name, handle = self._resolve(ref)
        self._read_owned(name, handle)
        return inspect_recovery(self.store, name)

    def cleanup_runtime(self, ref):
        """Destroy only after exact retained lease identities are inactive.

        Completed-resource reaping retains profiles and native sessions.
        """
        from .state import Lock
        from .process import pid_alive, pid_stamp
        from .runtime_profiles import RuntimeProfiles, RuntimeProfileError
        name, handle = self._resolve(ref)
        with Lock(self.store.path(name, ".owner"), timeout=0):
            with Lock(self.store.path(name, ".inbox")):
                meta = self._read_owned(name, handle)
                lease = json.loads(meta.get("runtime_lease") or "{}")
                if not meta.get("runtime_profile_root") or any(lease.get(key) != value for key, value in
                        (("task", name), ("generation", meta.get("generation")), ("parent", meta.get("parent", "")))):
                    raise RuntimeProfileError("no matching actual runtime lease")
                for kind in ("owner", "provider"):
                    pid, stamp = lease.get(kind + "_pid"), lease.get(kind + "_start")
                    if not pid or not stamp:
                        raise RuntimeProfileError("runtime lease is live or identity cannot be proven inactive")
                    if os.name == "nt":
                        import ctypes
                        from . import windows
                        process = windows.OpenProcess(windows.PROCESS_QUERY | windows.SYNCHRONIZE, False, int(pid))
                        if process:
                            try:
                                inactive = windows.creation_stamp(process) == str(stamp) and windows.WaitForSingleObject(process, 0) == 0
                            finally:
                                windows.CloseHandle(process)
                        else:
                            inactive = ctypes.get_last_error() == 87  # invalid PID; access denied is unknown
                    else:
                        current = pid_stamp(pid)
                        inactive = not pid_alive(pid) and (not current or current == str(stamp))
                    if not inactive:
                        raise RuntimeProfileError("runtime lease is live or identity cannot be proven inactive")
                if self._state(name, meta) in LIVE:
                    raise RuntimeProfileError("task remains active")
                return RuntimeProfiles(meta["runtime_profile_root"]).cleanup(name, live=False)

    def resume(self, ref, message=None, fresh=False, reviewed=False, reviewed_report_sha=None,
               model=None, effort=None):
        """Resume the saved session in a new owner, or replay the original prompt with fresh=True.

        model/effort change the model or thinking mode for this and later turns of the same session;
        they are passed to the provider exactly as given, never substituted."""
        self._check_open()
        name, handle = self._resolve(ref)
        meta = self._read_owned(name, handle)
        state = self._state(name, meta)
        if state in ("running", "idle"):
            raise OrchestratorError("resume: task '%s' is already %s; use send() or stop() first" % (name, state), task=name)
        if not fresh:
            self._require_resume(name, meta)
        if reviewed or reviewed_report_sha:
            from .lifecycle import reviewed_resume
            reviewed_resume(self.store, name, {"--reviewed": reviewed, "reviewed_report_sha": reviewed_report_sha, "--fresh": fresh})
        message_path = None
        argv = ["restart", name]
        if reviewed or reviewed_report_sha:
            argv.append("--reviewed")
            argv += ["--reviewed-report-sha", reviewed_report_sha]
        if model:
            argv += ["-m", str(model)]
        if effort:
            argv += ["-f", str(effort)]
        if fresh:
            argv.append("--fresh")
        elif message:
            message_path = (self.store.root / "sdk-prompts" / (uuid.uuid4().hex + ".msg")) if reviewed or reviewed_report_sha else self._message_path(name)
            atomic_write(message_path, message)
            argv += ["--prompt-file", message_path.as_posix()]
        argv += ["-P", self._bindings[name][1]]
        try:
            child = self._launch(name, argv, self._child_env(detached=True), prompt_path=message_path)
            try:
                self._await_owner(child, name, self.publish_timeout, "resume")
                with self._lock:
                    # The child was admitted before close and is in close cleanup;
                    # refuse to hand back a post-close resume instead of orphaning it.
                    self._check_open()
            except BaseException:
                self._discard(child)
                raise
        except BaseException:
            if message_path:
                retained = self._child_for(name)
                if retained is None or not getattr(retained, "start_failed", False):
                    self._unlink(message_path)
            raise
        self._refresh(name)
        return self.handle(name) or handle

    # ---------- inspection ----------

    def status(self, ref):
        name, handle = self._resolve(ref)
        meta = self._read_owned(name, handle)
        state = self._state(name, meta)
        from .lifecycle import owner_alive
        # A provider can publish its ready barrier before the log pump has consumed
        # its session line. Bound this initial synchronization to a live provider.
        if state in ("running", "idle") and owner_alive(meta) and not meta.get("session"):
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and owner_alive(meta):
                session = self.store.session(name, meta)
                if session:
                    meta = dict(meta, session=session)
                    break
                self._wake.wait(0.025)
                meta = self._read_owned(name, handle)
                if meta.get("session"):
                    break
        idle = 0.0
        activity = meta.get("activity_epoch")
        if activity:
            try:
                idle = max(0.0, time.time() - float(activity))
            except (TypeError, ValueError):
                idle = 0.0
        if handle is not None:
            # A live handle must reflect the metadata it just read: session ids appear
            # only after the provider reports one, generations change on resume.
            handle.state = state
            handle.session = meta.get("session", "") or self.store.session(name, meta)
            handle.generation = meta.get("generation", "")
            handle.engine = meta.get("engine", handle.engine)
            handle.model = meta.get("model", handle.model)
            handle.effort = meta.get("effort", handle.effort)
            handle.resolved_model = meta.get("resolved_model", handle.resolved_model)
            handle.queued = len(self.store.inbox(name))
            handle.exit_code = _int(meta.get("exit"))
            _mark_settled(handle)
        return TaskStatus(task=name, state=state, live=state in LIVE, owner_alive=bool(owner_alive(meta)),
                          engine=meta.get("engine", ""), model=meta.get("model", ""), effort=meta.get("effort", ""),
                          session=meta.get("session", "") or self.store.session(name, meta),
                          generation=meta.get("generation", ""), directory=meta.get("dir", ""),
                          parent=meta.get("parent", ""), exit_code=_int(meta.get("exit")),
                          reason=meta.get("reason", ""), files=_int(meta.get("files")),
                          queued=len(self.store.inbox(name)), idle_sec=round(idle, 3),
                          error=handle.error if handle else "", last_send_error=meta.get("last_send_error", ""))

    def result(self, ref, verification_receipts=(), current_source_hash=None, cost_facts=(), cost_decision=None):
        """Compact retained result built from the same collector as `neoxider result --json`."""
        from .ux_tracking import collect_result
        name, handle = self._resolve(ref)
        self._read_owned(name, handle)
        data = collect_result(self.store, name, verification_receipts=verification_receipts,
                              current_source_hash=current_source_hash, cost_facts=cost_facts, cost_decision=cost_decision)
        changes = tuple({"path": entry.get("path", ""), "status": entry.get("status", ""),
                         "insertions": entry.get("insertions"), "deletions": entry.get("deletions")}
                        for entry in data.get("changed_files", []))
        meta = self.store.read(name) or {}
        owns = tuple(part.strip() for part in str(meta.get("owns", "")).split(",") if part.strip())
        if handle is not None:
            handle.state = data.get("state", handle.state)
            handle.session = data.get("session", handle.session)
            handle.exit_code = data.get("exit_code")
            _mark_settled(handle)
        return TaskResult(task=data.get("task", name), settled=data.get("state") not in LIVE, state=data.get("state", ""),
                          exit_code=data.get("exit_code"), session=data.get("session", ""), engine=data.get("engine", ""),
                          model=data.get("model", ""), effort=meta.get("effort", ""), directory=data.get("directory", ""),
                          generation=meta.get("generation", ""), duration_sec=data.get("duration_sec"),
                          answer=data.get("final_answer", "") or "", error=data.get("stop_report", "") or "",
                          reason=meta.get("reason", ""), changed_files=changes,
                          files_changed=len(changes), owns=owns, scope_declared=bool(owns), scope_enforced=False,
                          scope_note=SCOPE_NOTE, baseline_known=bool(data.get("baseline_known")),
                          baseline_partial=bool(data.get("baseline_partial")),
                          baseline_warning=data.get("baseline_warning", "") or "",
                          attribution=data.get("attribution", ATTRIBUTION) or ATTRIBUTION,
                          usage=data.get("usage"), tokens=data.get("tokens"), cost_usd=data.get("cost_usd"),
                          limit=data.get("limit"), stop_report=data.get("stop_report", "") or "",
                          queued=len(self.store.inbox(name)), detached=bool(handle.detached) if handle else False,
                          failure=data["diagnostics"].get("failure"), verification=data["diagnostics"].get("verification"),
                          tests_status=data["diagnostics"]["tests_status"], qa_status=data["diagnostics"]["qa_status"],
                          cost_facts=tuple(data["diagnostics"]["cost_facts"]), event_pending=meta.get("event_pending") == "1")

    def stop(self, ref, reason=None):
        name, handle = self._resolve(ref)
        meta = self._read_owned(name, handle)
        state = self._state(name, meta)
        if state not in LIVE:
            return self.result(name)
        argv = ["stop", name]
        code, output = self._run_control(argv, timeout=self.control_timeout, task=name)
        child = self._child_for(name)
        if child is not None:
            self._await_exit(child, 10)
        self._refresh(name)
        result = self.result(name)
        if result.state not in ("stopped", "error", "silent", "limited") and code != 0:
            raise OrchestratorError("stop: task '%s' stayed %s (rc=%s)" % (name, result.state, code),
                                    task=name, command=" ".join(argv), returncode=code, output=output)
        if reason and handle is not None:
            handle.error = ""
        return result

    # ---------- waiting ----------

    def wait(self, ref, timeout=None):
        results = self._wait([ref], timeout=timeout, want_all=True)
        if not results:
            raise WaitTimeout("wait: '%s' did not settle within %ss" % (self._resolve(ref)[0], timeout), task=self._resolve(ref)[0])
        return results[0]

    def wait_any(self, refs, timeout=None, want_all=False):
        return self._wait(list(refs), timeout=timeout, want_all=want_all)

    def _wait(self, refs, timeout=None, want_all=True):
        deadline = self._deadline(timeout)
        entries = [self._resolve(ref) for ref in refs]
        if not entries:
            return []
        settled, pending = [], list(entries)
        while pending:
            for entry in list(pending):
                name = entry[0]
                state = self._state(name, self._read_owned(name, entry[1]))
                if state not in LIVE:
                    meta = self.store.read(name)
                    if meta.get("event_integration") == "1" and meta.get("state") in SETTLED:
                        intent = json.loads(meta.get("terminal_intent") or "{}")
                        if intent.get("generation") != meta.get("generation"):
                            from .lifecycle import owner_alive
                            if owner_alive(meta):
                                continue  # metadata is published before the final notification
                            raise OrchestratorError("terminal event intent missing; inspect recovery before accepting completion", task=name)
                    if meta.get("event_pending") == "1" and not self.store.publish_events(name):
                        raise OrchestratorError("terminal event publication pending: " + meta.get("event_error", ""), task=name)
                    settled.append((name, state))
                    pending.remove(entry)
                    if not want_all:
                        break
            if not pending or (settled and not want_all):
                break
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise WaitTimeout("wait: still running: %s" % " ".join(name for name, _ in pending),
                                  task=pending[0][0])
            self._wake.clear()
            self._wake.wait(min(self.poll, remaining) if remaining is not None else self.poll)
        return [self.result(name) for name, _ in settled]

    # ---------- lifecycle ----------

    def handles(self):
        with self._lock:
            return tuple(self._handles.values())

    def handle(self, name):
        with self._lock:
            return self._handles.get(name)

    def attach(self, name, adopt=False):
        """Bind an existing task (same parent unless adopt=True) to this client."""
        name = valid_name(name)
        with self._lock:
            handle = self._handles.get(name)
        if handle is not None:
            self._read_owned(name, handle)
            return handle
        meta = self._read_meta(name)
        if not meta:
            raise OrchestratorError("attach: no such task '%s'" % name, task=name)
        foreign = meta.get("parent", "") != self.parent
        if foreign and not adopt:
            raise NotOwnedError("attach: task '%s' belongs to parent '%s'" % (name, meta.get("parent", "")), task=name)
        handle = self._make_handle(name, self._child_for(name), meta, meta.get("parent", "") or self.parent,
                                   meta.get("dir", ""), "", meta.get("engine", ""), "", "", meta.get("owns", ""),
                                   meta.get("sdk_detached") == "1", owned=not foreign and not adopt,
                                   adopted=bool(adopt), adopted_parent=meta.get("parent", "") if adopt else "")
        self._register(handle, meta, adopted=bool(adopt))
        return handle

    def _binding_path(self, name):
        return self.store.path(name, ".%s.json" % self.client_id)

    def _write_binding(self, handle, parent, adopted):
        # Additive private sidecar only. SDK ownership never rewrites core metadata.
        atomic_write(self._binding_path(handle.task), json.dumps({
            "client_id": self.client_id, "task": handle.task, "parent": parent,
            "spawn_id": handle.spawn_id, "adopted": adopted, "detached": handle.detached,
        }, sort_keys=True))

    def _register(self, handle, meta, adopted):
        parent = meta.get("parent", "")
        self._write_binding(handle, parent, adopted)
        with self._lock:
            self._handles[handle.task] = handle
            self._bindings[handle.task] = (handle, parent, adopted, self._owner_token(meta))

    def close(self, detach=None, shutdown_timeout=2.0):
        """Stop owned work, then wait a shared bounded budget for callback/ACK teardown."""
        if shutdown_timeout is None:
            raise ValueError("shutdown_timeout must be finite and non-negative")
        self._deadline(shutdown_timeout)  # reuse the public finite timeout validation
        current = threading.current_thread()
        with self._lock:
            callback_thread = current is self._monitor or any(
                current is thread for _, _, thread in self._subscriptions)
        # A closer may be joining this very callback. Never wait for its close lock.
        if callback_thread and not self._close_lock.acquire(blocking=False):
            with self._lock:
                return self._close_report or CloseReport(
                    pending_consumers=tuple(who for who, _, thread in self._subscriptions
                                            if thread.is_alive()),
                    pending_threads=tuple(thread.name for thread in (self._monitor, self._reaper)
                                          if thread.is_alive()), in_progress=True)
        if not callback_thread:
            self._close_lock.acquire()
        try:
            return self._close_once(detach, shutdown_timeout)
        finally:
            self._close_lock.release()

    def _close_once(self, detach=None, shutdown_timeout=2.0):
        """Stop client-owned live workers, keep accepted results, report everything."""
        with self._lock:
            if self._close_report is not None:
                return self._close_report
            self._closed = True
            subscriptions = list(self._subscriptions)
            for _, cancel, _ in subscriptions:
                cancel.set()
            tracked = list(self._handles.items())
            children = list(self._children)
        keep = self.detach_on_close if detach is None else bool(detach)
        stopped, retained, detached, failed = [], [], [], []
        spared = set()
        for name, handle in tracked:
            try:
                meta = self._read_owned(name, handle)
                if self._bindings[name][2]:
                    retained.append(name)  # explicit adoption never grants automatic stop
                    spared.add(name)
                    continue
                if keep or handle.detached:
                    detached.append(name)
                    spared.add(name)
                    continue
                if self._state(name, meta) not in LIVE:
                    retained.append(name)
                    continue
                self.stop(name)
                stopped.append(name)
            except NotOwnedError:
                retained.append(name)
                spared.add(name)  # reparented after launch: never kill through our old child
            except BaseException as error:
                failed.append((name, "%s: %s" % (type(error).__name__, error)))
                spared.add(name)
        for child in children:
            if child.task in spared:
                try:
                    self._reap(child)  # exited resources can still be released safely
                except BaseException as error:
                    child.cleanup_error = "%s: %s" % (type(error).__name__, error)
                    failed.append((child.task, "%s: %s" % (type(error).__name__, error)))
                continue
            # Includes launches admitted just before close that never registered a
            # handle, and retained failed-start children: stop() cannot see them,
            # so identity-checked _kill must run.
            # A PID that fails the stamp check is never touched here.
            # Secondary cleanup errors retain ownership and are reported, never
            # mistaken for a full release.
            try:
                self._kill(child)
            except BaseException as error:
                child.cleanup_error = "%s: %s" % (type(error).__name__, error)
                failed.append((child.task, "%s: %s" % (type(error).__name__, error)))
        if not self._children:
            self._closing.set()
        self._wake.set()
        deadline = time.monotonic() + shutdown_timeout
        threads = [thread for _, _, thread in subscriptions] + [self._monitor, self._reaper]
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
        self._close_report = CloseReport(
            tuple(stopped), tuple(retained), tuple(detached), tuple(failed),
            pending_consumers=tuple(who for who, _, thread in subscriptions if thread.is_alive()),
            pending_threads=tuple(thread.name for thread in (self._monitor, self._reaper)
                                  if thread.is_alive()))
        return self._close_report

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        try:
            self.close()
        except BaseException:
            pass
        return False

    # ---------- internals ----------

    def _check_open(self):
        if self._closed:
            raise ClientClosed("client is closed; create a new AgentClient")

    def _directory(self, cwd):
        directory = str(Path(str(cwd)).resolve()) if cwd else os.getcwd()
        if not Path(directory).is_dir():
            raise ValueError("working directory does not exist: " + directory)
        return directory

    def _reserve_name(self, name):
        task = valid_name(name or ("sdk-" + uuid.uuid4().hex[:12]))
        if self.store.path(task, ".meta").exists():
            raise ValueError("spawn: task '%s' already exists in %s; pass a fresh name" % (task, self.store.root))
        return task

    def _resolve(self, ref):
        if isinstance(ref, TaskHandle):
            if ref.client is not self or self.handle(ref.task) is not ref:
                raise NotOwnedError("handle '%s' belongs to another AgentClient ('%s')" % (
                    ref.task, ref.client.client_id if ref.client is not None else "detached"), task=ref.task)
            return ref.task, ref
        name = valid_name(str(ref))
        return name, self.handle(name) or self.attach(name)

    def _read_meta(self, name, timeout=1.0):
        """Read task metadata, tolerating a transient unreadable rewrite on Windows."""
        deadline = time.monotonic() + float(timeout)
        while True:
            data = self.store.read(name)
            if data or time.monotonic() >= deadline:
                return data
            self._wake.wait(0.02)
            self._wake.clear()

    def _read_owned(self, name, handle=None):
        meta = self._read_meta(name)
        if not meta:
            raise OrchestratorError("no such task '%s'" % name, task=name)
        with self._lock:
            binding = self._bindings.get(name)
            registered = self._handles.get(name)
        if (not binding or registered is not binding[0] or handle is not registered
                or handle.client is not self or handle.task != name
                or meta.get("parent", "") != binding[1]):
            raise NotOwnedError("task '%s' has no current registered ownership (parent '%s')" % (
                name, meta.get("parent", "")), task=name)
        if not binding[2] and self._owner_token(meta) != binding[3]:
            # An SDK-launched resume may publish before _await_owner refreshes its token.
            with self._lock:
                children = list(self._children)
            published_here = any(child.task == name and child.command[2] in ("run", "send", "restart")
                                 and str(child.process.pid) == meta.get("pid")
                                 and child.stamp == meta.get("pid_start")
                                 and (not child.generation or child.generation == meta.get("generation"))
                                 for child in children)
            if not published_here:
                raise NotOwnedError("task '%s' has a different owner generation" % name, task=name)
        return meta

    @staticmethod
    def _owner_token(meta):
        return (meta.get("pid", ""), meta.get("pid_start", ""), meta.get("generation", ""))

    def _state(self, name, meta=None):
        from .lifecycle import effective
        data = self.store.read(name) if meta is None else meta
        return effective(self.store, name, data)

    def _child_env(self, env=None, detached=False):
        result = dict(self.base_env)
        result.update(self.env)
        result.update(env or {})
        result["AGENT_CLI_LOGS"] = str(self.store.root)
        result["PYTHONIOENCODING"] = "utf-8"
        result["PYTHONUTF8"] = "1"
        result["PYTHONDONTWRITEBYTECODE"] = "1"
        if detached:
            result["AGENT_DETACHED"] = "1"
            result["AGENT_LAUNCHER_PID"] = ""
        return result

    def _launch(self, task, argv, env, prompt_path=None, control=False):
        """Single atomic admission point: refuse non-control launches once closed.

        The closed check, Popen, _children registration and pump start share one
        lock hold so close() either observes a fully-tracked child for cleanup or
        the launch never starts. The child is registered BEFORE Thread.start with
        thread None, so close never joins an unstarted thread. Only the short
        Popen and thread start run under the lock: no publication waits, no
        callbacks. A pump start failure preserves the primary start error and
        retains a marked failed-start child until a CHECKED termination/reap
        succeeds; kill/wait errors never drop ownership. Pipes, the Windows
        Popen handle and the prompt are released only AFTER actual exit via the
        existing _kill/_reap. Control launches stay admitted so close() can stop
        workers and detached handles remain stoppable afterwards; they reap
        synchronously in _run_control.
        """
        from .process import pid_stamp
        if argv[0] in ("send", "restart", "stop"):
            self._read_owned(task, self.handle(task))
        with self._lock:
            if self._closed and not control:
                raise ClientClosed("client is closed; create a new AgentClient")
            command = [self.python, self.entry] + [str(argument) for argument in argv]
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, env=env, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, close_fds=True, encoding="utf-8",
                                           errors="replace", **launch_kwargs())
            except OSError as error:
                raise SpawnError("spawn: could not launch agent.py (%s)" % error, task=task,
                                 command=" ".join(command), returncode=1)
            child = _Child(task, process, pid_stamp(process.pid), deque(maxlen=max(1, self.launcher_output)), command)
            child.prompt_path = prompt_path
            child.control = control
            if "-P" in argv:
                child.parent = argv[argv.index("-P") + 1]
            child.thread = None
            self._children.append(child)
            thread = threading.Thread(target=self._pump, args=(child,), name="neoxider-sdk-launcher", daemon=True)
            try:
                thread.start()
            except BaseException as primary:
                # Preserve the primary start error; retain the failed-start child
                # until checked termination/reap succeeds. Never drop ownership
                # on a secondary kill/wait error: record it and keep the child.
                child.start_failed = True
                child.start_error = "%s: %s" % (type(primary).__name__, primary)
                try:
                    self._kill(child)
                except BaseException as secondary:
                    child.cleanup_error = "%s: %s" % (type(secondary).__name__, secondary)
                raise
            child.thread = thread
        return child

    def _pump(self, child):
        from .providers import diagnostic_pipe_read
        stream = child.process.stdout
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        pause = threading.Event()
        try:
            while True:
                block = diagnostic_pipe_read(stream)
                if block == b"" or (block is None and not child.alive()):
                    break
                if block is None:
                    pause.wait(0.02)
                    continue
                pending += decoder.decode(block)
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    child.lines.append(line[-65536:].rstrip("\r"))
                pending = pending[-65536:]
            pending += decoder.decode(b"", final=True)
            if pending:
                child.lines.append(pending.rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
            self._wake.set()

    def _run_control(self, argv, timeout=None, task=None, prompt_path=None):
        self._deadline(timeout)
        limit = self.control_timeout if timeout is None else float(timeout)
        child = self._launch(task, argv, self._child_env(), prompt_path=prompt_path, control=True)
        if not self._await_exit(child, limit):
            self._discard(child)
            raise OrchestratorError("control command timed out after %.1fs: %s" % (limit, " ".join(argv[1:])),
                                    task=task, command=" ".join(child.command))
        child.thread.join(1)
        output = "\n".join(child.lines)
        self._reap(child)
        return child.process.returncode, output

    def _await_owner(self, child, task, timeout, phase):
        """Bounded wait until this exact wrapper publishes itself as the owner."""
        deadline = time.monotonic() + float(timeout)
        while True:
            meta = self.store.read(task)
            if (meta.get("pid") == str(child.process.pid) and meta.get("pid_start") == child.stamp
                    and meta.get("parent", "") == child.parent and meta.get("core_version") == "2"):
                child.generation = meta.get("generation", "")
                child.parent = meta.get("parent", "")
                with self._lock:
                    binding = self._bindings.get(task)
                    if binding and binding[1] == child.parent:
                        self._bindings[task] = binding[:3] + (self._owner_token(meta),)
                return meta
            if not child.alive():
                code = child.process.returncode
                self._discard(child)
                raise SpawnError("%s: task '%s' exited (rc=%s) before publishing its owner%s" % (
                    phase, task, code, (": " + child.tail()) if child.tail() else ""),
                    task=task, command=" ".join(child.command), returncode=code, output=child.tail())
            if time.monotonic() >= deadline:
                raise SpawnError("%s: task '%s' did not publish an owner within %.1fs%s" % (
                    phase, task, float(timeout), (": " + child.tail()) if child.tail() else ""),
                    task=task, command=" ".join(child.command), returncode=None, output=child.tail())
            self._wake.clear()
            self._wake.wait(0.05)

    def _child_for(self, task):
        with self._lock:
            for child in reversed(self._children):
                if child.task == task:
                    return child
        return None

    def _await_exit(self, child, timeout):
        deadline = time.monotonic() + float(timeout)
        while child.alive() and time.monotonic() < deadline:
            self._wake.wait(0.05)
            self._wake.clear()
        return not child.alive()

    def _kill(self, child):
        from .process import kill_tree, pid_stamp
        # Only a captured, still-matching process identity may be terminated.
        # A PID that fails the stamp check (reused/gone) is never touched here.
        # Secondary errors propagate so ownership is retained, never dropped.
        if child.alive() and child.stamp and pid_stamp(child.process.pid) == child.stamp:
            kill_tree(child.process.pid, stamp=child.stamp)
            try:
                child.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                return
        self._reap(child)

    def _reap(self, child):
        """Release finished launcher resources immediately; retain only typed/durable state.

        Pipes, the Windows Popen handle and the prompt are released only AFTER
        actual exit. Any secondary release error propagates so the child stays
        tracked and cleanup is retried; successful reap unlinks the prompt.
        """
        with child.lock:
            if child.reaped:
                return True
            if child.process.poll() is None:
                return False
        if child.thread is not None and child.thread is not threading.current_thread():
            child.thread.join(0.1)
            if child.thread.is_alive():
                return False
        with child.lock:
            if child.reaped:
                return True
            child.process.wait(timeout=0)
            if child.process.stdout:
                child.process.stdout.close()
            if os.name == "nt":
                child.process._handle.Close()
            if child.prompt_path:
                self._unlink(child.prompt_path)
            child.reaped = True
        with self._lock:
            if child in self._children:
                self._children.remove(child)
        return True

    def _cleanup_children(self, now=None, force=False):
        """300s settled-resource gate, independent of model turns or public handle flags."""
        from .process import pid_stamp
        now = self._clock() if now is None else now
        with self._lock:
            children = list(self._children)
        for child in children:
            if self._reap(child):
                continue
            if getattr(child, "start_failed", False):
                # A pre-handle failed-start child never registered an owner
                # generation: retry purely by original PID + creation identity.
                # No public handle/generation may be required. A stamp mismatch
                # (reused/gone PID) is never killed; an exited original is
                # reaped, releasing its exact process/stdout/Windows handle.
                try:
                    if child.alive():
                        if child.stamp and pid_stamp(child.process.pid) == child.stamp:
                            self._kill(child)
                        else:
                            self._reap(child)
                    else:
                        self._reap(child)
                except BaseException as error:
                    child.cleanup_error = "%s: %s" % (type(error).__name__, error)
                continue
            if child.control:
                continue
            try:
                handle = self.handle(child.task)
                meta = self._read_owned(child.task, handle)
                if not child.generation and meta.get("pid") == str(child.process.pid) and meta.get("pid_start") == child.stamp and meta.get("parent", "") == child.parent:
                    child.generation = meta.get("generation", "")
                if not child.generation:
                    continue
                same = (meta.get("pid") == str(child.process.pid)
                        and meta.get("pid_start") == child.stamp
                        and meta.get("generation") == child.generation
                        and meta.get("parent", "") == child.parent)
                if not same or self._state(child.task, meta) in LIVE or self._bindings[child.task][2]:
                    child.settled_at = None
                    continue
                if child.settled_at is None:
                    child.settled_at = now
                # Reserve one polling interval: release by 300s, including cleanup waits.
                if (force or now - child.settled_at >= 295) and pid_stamp(child.process.pid) == child.stamp:
                    self._kill(child)
            except (NotOwnedError, OrchestratorError, OSError, ValueError):
                child.settled_at = None

    def _discard(self, child):
        try:
            self._kill(child)
        except BaseException as error:
            # Never mask the caller's primary failure and never drop ownership:
            # retain the child for retry/close and record the secondary error.
            child.cleanup_error = "%s: %s" % (type(error).__name__, error)

    def _require_resume(self, name, meta):
        engine = meta.get("engine", "")
        try:
            provider = self._provider(engine)
        except Exception as error:
            raise ResumeError("resume: unknown engine '%s' (%s)" % (engine, error), task=name)
        if not provider.supports_resume:
            raise ResumeNotSupported("resume: engine '%s' cannot resume (supports_resume=false); "
                                     "spawn a fresh task instead" % engine, task=name)
        if not self.store.session(name, meta):
            raise ResumeError("resume: no saved session id for '%s'; spawn a fresh task instead" % name, task=name)

    def _provider(self, engine):
        """Load provider code from the CAPTURED source in a private namespace.

        No module registry mutation and no bytecode-cache lookup, so a stale or hostile .pyc with a
        matching size/mtime header can never define this client's provider behaviour.
        """
        from . import providers
        root = self.env.get("AGENT_PROVIDER_DIR") or self.base_env.get("AGENT_PROVIDER_DIR") or providers.ROOT / "providers"
        if not re.fullmatch(r"[A-Za-z0-9_-]+", engine):
            raise ValueError("invalid provider name: " + engine)
        path = Path(root) / engine / "provider.py"
        if not path.is_file():
            raise ValueError("unknown engine '%s': expected %s/<engine>/provider.py" % (engine, root))
        namespace = {"__name__": "neoxider_sdk_provider_" + engine, "__file__": str(path),
                     "__builtins__": __builtins__, "__package__": ""}
        exec(compile(path.read_text(encoding="utf-8-sig"), str(path), "exec"), namespace)
        factory = namespace.get("Provider") or namespace.get("provider")
        provider = factory() if callable(factory) else None
        if provider is None:
            raise ValueError("provider '%s' exposes no Provider class or provider factory" % engine)
        provider.engine = engine
        return provider

    def _message_path(self, name):
        """One prompt file per call: concurrent sends never share or unlink each other's text."""
        return self.store.path(name, ".sdk-%s.msg" % uuid.uuid4().hex)

    def _inbox_sequence(self, name):
        try:
            return int((self.store.path(name, ".inbox") / "sequence").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _note_error(self, name, detail):
        handle = self.handle(name)
        if handle is not None and detail:
            handle.error = detail

    def _refresh(self, name):
        handle = self.handle(name)
        if handle is None:
            return None
        meta = self._read_owned(name, handle)
        if not meta:
            return handle
        state = self._state(name, meta)
        handle.state = state
        handle.session = meta.get("session", "")
        handle.generation = meta.get("generation", "")
        handle.engine = meta.get("engine", handle.engine)
        handle.model = meta.get("model", handle.model)
        handle.exit_code = _int(meta.get("exit"))
        handle.queued = len(self.store.inbox(name))
        _mark_settled(handle)
        return handle

    def _make_handle(self, task, child, meta, parent, directory, spawn_id, engine, model, effort, owns, detach,
                     owned=True, adopted=False, adopted_parent=""):
        state = self._state(task, meta)
        handle = TaskHandle(task=task, client=self, parent=parent, directory=directory,
                            engine=meta.get("engine", ""), model=meta.get("model", ""),
                            effort=meta.get("effort", ""), resolved_model=meta.get("resolved_model", ""),
                            requested_engine=engine or "", requested_model=model or "", requested_effort=effort or "",
                            generation=meta.get("generation", ""), session=meta.get("session", "") or self.store.session(task, meta),
                            state=state, queued=len(self.store.inbox(task)), exit_code=_int(meta.get("exit")),
                            owns=tuple(part.strip() for part in str(owns or meta.get("owns", "")).split(",") if part.strip()),
                            detached=bool(detach) or meta.get("sdk_detached") == "1", owned=owned, adopted=adopted,
                            adopted_parent=adopted_parent,
                            spawn_id=spawn_id, spawned_at=time.time(), settled=state not in LIVE)
        _mark_settled(handle)
        return handle

    def _deadline(self, timeout):
        """None waits without a deadline; 0 polls once; negatives and bools are rejected."""
        if timeout is None:
            return None
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be None or a non-negative number of seconds")
        if timeout < 0 or not math.isfinite(timeout):
            raise ValueError("timeout must be finite and non-negative")
        return time.monotonic() + float(timeout)

    def _monitor_loop(self):
        previous = {}
        terminal_cursors = {}
        from .events import EventStore
        events = EventStore(self.store.root)
        while not self._closing.is_set():
            self._closing.wait(min(self.poll, 1.0))
            if self._closing.is_set():
                break
            if self._closed and not self._children:
                break
            with self._lock:
                tracked = list(self._handles.items())
            for name, handle in tracked:
                try:
                    meta = self._read_owned(name, handle)
                    state = self._state(name, meta)
                    queued = len(self.store.inbox(name))
                    last = previous.get(name)
                    handle.state, handle.queued = state, queued
                    _mark_settled(handle)
                    if meta.get("session"):
                        handle.session = meta.get("session", "")
                    if meta.get("generation"):
                        handle.generation = meta.get("generation", "")
                    page = events.read(name, after=terminal_cursors.get(name, 0)) if self._callbacks else ()
                    for record in page:
                        if record["kind"] == "terminal" and record["parent"] == self._bindings[name][1]:
                            data = record["data"]
                            self._emit(TaskEvent(name, "settled", data["state"],
                                "exit %s" % data.get("exit_code", ""), record["epoch"], record["id"],
                                record["generation"], record["parent"], record["seq"], data))
                        terminal_cursors[name] = record["seq"]
                    if last is None:
                        previous[name] = (state, queued)
                        if handle.settled and meta.get("event_integration") != "1":
                            self._emit(TaskEvent(name, "settled", state, "legacy retained outcome", time.time()))
                        continue
                    if state != last[0]:
                        previous[name] = (state, queued)
                        self._wake.set()
                        if not handle.settled or meta.get("event_integration") != "1":
                            self._emit(TaskEvent(name, "settled" if handle.settled else "state", state,
                                                 "owner pid %s exit %s" % (meta.get("pid", ""), meta.get("exit", "")), time.time()))
                    elif queued != last[1]:
                        previous[name] = (state, queued)
                        self._emit(TaskEvent(name, "queued", state, "%s undelivered message(s)" % queued, time.time()))
                except BaseException as error:  # monitor continuity beats single-tick failure
                    self._emit(TaskEvent(name, "monitor_error", handle.state,
                                         "%s: %s" % (type(error).__name__, error), time.time()))

    def _resource_loop(self):
        # Subscriber code must never delay resource cleanup. No model/CLI idle turns.
        while not self._closing.wait(min(self.poll, 1.0)):
            try:
                self._cleanup_children()
            except Exception as error:
                self._record_error("", type(error).__name__, str(error))
            if self._closed and not self._children:
                self._closing.set()
                self._wake.set()

    @staticmethod
    def _unlink(path):
        try:
            Path(path).unlink()
        except OSError:
            pass


def _mark_settled(handle):
    """Keep the wait event in step with the observed state (a resume makes a task live again)."""
    handle.settled = handle.state not in LIVE
    if handle.settled:
        handle._settled_event.set()
    else:
        handle._settled_event.clear()


def _first_line(text):
    for line in str(text or "").splitlines():
        if line.strip() and not line.startswith("[neoxider] warning"):
            return line.strip()
    return ""


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _effort(model):
    for known in ("-low", "-medium", "-high", "-xhigh", "-minimal"):
        if str(model).endswith(known):
            return known[1:]
    return ""
