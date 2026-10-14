"""Durable recovery evidence and review receipts; never launches or replays work.

No local-package imports: inspection must not prune legacy state or import cached
bytecode from concurrently edited workers. All retained bytes are copied verbatim.
"""
from contextlib import contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import uuid


class RecoveryError(ValueError):
    """Evidence is unsafe, incomplete, changed, or not explicitly reviewed."""


MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 20000
MAX_SECONDS = 10
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def resume_projection(report):
    """Stable captured preimages/identity, excluding inspection observations.

    Retained file bytes (including receipt timestamps) remain pinned preimages;
    only report observation fields are excluded, never arbitrary nested keys.
    """
    keys = ("task", "task_root", "metadata", "captures", "current_source",
            "profile", "profile_root", "effects", "inbox_receipts", "git",
            "implementation_sha256")
    try:
        return json.loads(_json({key: report[key] for key in keys}))
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryError("stable resume projection is incomplete") from exc


def _name(task):
    if not isinstance(task, str) or not _NAME.fullmatch(task):
        raise RecoveryError("invalid canonical task name")
    stem = task.split(".", 1)[0].upper()
    if task.endswith(".") or stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[0-9]", stem):
        raise RecoveryError("reserved task name")
    return task


def _path(value):
    value = os.fspath(value)
    value.encode("utf-8", "strict")
    if "\x00" in value or ".." in Path(value).parts:
        raise RecoveryError("unsafe reference path")
    return Path(os.path.abspath(value))


def _check(path):
    for component in reversed((path,) + tuple(path.parents)):
        if os.name == "nt" and ":" in component.name:
            raise RecoveryError("alternate-stream reference refused")
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RecoveryError("linked/reparse reference refused: " + str(component))
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise RecoveryError("hardlinked reference refused: " + str(component))


def _read(path, maximum=MAX_FILE_BYTES):
    _check(path)
    expected = path.lstat()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise RecoveryError("regular reference required")
        if (expected.st_dev, expected.st_ino) != (before.st_dev, before.st_ino):
            raise RecoveryError("reference replaced during open")
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if len(data) > maximum:
        raise RecoveryError("reference exceeds snapshot bound: " + str(path))
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise RecoveryError("reference changed during capture")
    return data


def _sync(directory):
    if os.name != "nt":
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _exclusive(path, data):
    _check(path)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _sync(path.parent)


@contextmanager
def exclusive_lock(path, timeout=2):
    """OS-owned advisory lock; retained inode, automatically released on death."""
    _check(path)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RecoveryError("recovery exclusive lock busy")
                time.sleep(.01)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _owner(meta):
    """Creation-time mismatch proves a reused PID; missing proof blocks safely."""
    raw = meta.get("winpid") or meta.get("pid", "")
    expected = meta.get("pid_start", "")
    result = {"pid": raw, "expected_creation": expected,
              "creation": "", "command_sha256": None, "command_available": False}
    try:
        pid = int(raw)
        if pid <= 0:
            raise ValueError()
    except (TypeError, ValueError):
        result["status"] = "unverifiable"
        return result
    alive, creation = None, ""
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            alive = False if ctypes.get_last_error() == 87 else None
        else:
            try:
                times = [wintypes.FILETIME() for _ in range(4)]
                if kernel.GetProcessTimes(handle, *[ctypes.byref(x) for x in times]):
                    creation = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
                code = wintypes.DWORD()
                if kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                    alive = code.value == 259
                # Query available identity proof without shelling out to WMI.
                query = kernel.QueryFullProcessImageNameW
                query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
                size = wintypes.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                if query(handle, 0, buf, ctypes.byref(size)):
                    result["executable"] = buf.value
                try:
                    class UnicodeString(ctypes.Structure):
                        _fields_ = [("length", wintypes.USHORT), ("maximum", wintypes.USHORT),
                                    ("buffer", ctypes.c_void_p)]
                    native_query = ctypes.WinDLL("ntdll").NtQueryInformationProcess
                    native_query.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                             wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
                    native_query.restype = ctypes.c_long
                    required = wintypes.ULONG()
                    native_query(handle, 60, None, 0, ctypes.byref(required))
                    if ctypes.sizeof(UnicodeString) <= required.value <= 65536:
                        storage = ctypes.create_string_buffer(required.value)
                        status = native_query(handle, 60, storage, required.value, ctypes.byref(required))
                        if status >= 0:
                            info = UnicodeString.from_buffer(storage)
                            offset = (info.buffer or 0) - ctypes.addressof(storage)
                            if info.length % 2 == 0 and 0 <= offset <= len(storage) - info.length:
                                raw_command = bytes(storage[offset:offset + info.length])
                                command = raw_command.decode("utf-16-le").encode("utf-8")
                                result.update(command_available=True, command_sha256=_sha(command))
                except (OSError, AttributeError, UnicodeError):
                    pass  # Protected processes/older systems may deny command proof.
            finally:
                kernel.CloseHandle(handle)
    else:
        try:
            fields = Path("/proc/%s/stat" % pid).read_text().rsplit(")", 1)[1].split()
            creation, alive = fields[19], fields[0] != "Z"
            command = Path("/proc/%s/cmdline" % pid).read_bytes()
            result.update(command_available=True, command_sha256=_sha(command))
        except FileNotFoundError:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            except PermissionError:
                pass
        except (OSError, IndexError):
            pass
    result["creation"] = creation
    result["status"] = ("dead" if alive is False else "reused_pid" if expected and creation and expected != creation
                        else "live_owner" if alive and expected and creation else "unverifiable")
    proof = meta.get("owner_command_sha256")
    if proof and result["status"] == "live_owner" and result["command_available"] and proof != result["command_sha256"]:
        result["status"] = "unverifiable"
    return result


class RecoveryStore:
    """Owned durable journal root, separate from the retained legacy task root."""
    def __init__(self, root):
        self.root = _path(root)
        _check(self.root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with exclusive_lock(self.root / ".lock"):
            marker = self.root / "owner.json"
            if not marker.exists():
                if any(p.name != ".lock" for p in self.root.iterdir()):
                    raise RecoveryError("nonempty unowned recovery root")
                _exclusive(marker, _json({"schema": 1, "root": str(self.root), "id": uuid.uuid4().hex}))
            self.owner = json.loads(_read(marker))
            if self.owner.get("schema") != 1 or self.owner.get("root") != str(self.root):
                raise RecoveryError("recovery root ownership mismatch")
            for folder in ("records", "blobs"):
                target = self.root / folder
                _check(target)
                target.mkdir(exist_ok=True, mode=0o700)

    def _history(self):
        _check(self.root)
        if json.loads(_read(self.root / "owner.json")) != self.owner:
            raise RecoveryError("recovery owner changed")
        _check(self.root / "records")
        records = []
        for path in (self.root / "records").iterdir():
            try:
                envelope = json.loads(_read(path))
                value = envelope["record"]
                if envelope["sha256"] != _sha(_json(value)) or value["root_id"] != self.owner["id"] or path.name != value["id"] + ".json":
                    raise RecoveryError("journal integrity mismatch")
                records.append(value)
            except (ValueError, KeyError, TypeError) as exc:
                raise RecoveryError("incomplete/corrupt retained journal: " + str(path)) from exc
        records.sort(key=lambda v: v["sequence"])
        previous = None
        for index, record in enumerate(records, 1):
            if record["sequence"] != index or record["previous_sha256"] != previous:
                raise RecoveryError("history order/hash chain mismatch")
            previous = _sha(_json(record))
        return records

    def history(self, task=None):
        if task is not None:
            _name(task)
        with exclusive_lock(self.root / ".lock"):
            return [r for r in self._history() if task is None or r["task"] == task]

    def _record(self, task, kind, payload):
        records = self._history()
        record = {"schema": 1, "id": uuid.uuid4().hex, "root_id": self.owner["id"],
                  "sequence": len(records) + 1, "previous_sha256": _sha(_json(records[-1])) if records else None,
                  "task": _name(task), "kind": kind, "at_ns": time.time_ns(), "payload": payload}
        digest = _sha(_json(record))
        _exclusive(self.root / "records" / (record["id"] + ".json"), _json({"sha256": digest, "record": record}))
        return {"id": record["id"], "sha256": digest, "record": record}

    def record_intent(self, task, payload):
        with exclusive_lock(self.root / ".lock"):
            return self._record(task, "intent", payload)

    def _blob(self, data):
        digest = _sha(data)
        path = self.root / "blobs" / digest
        if path.exists():
            if _sha(_read(path)) != digest:
                raise RecoveryError("captured blob corrupt")
        else:
            _exclusive(path, data)
        return {"ref": "blobs/" + digest, "sha256": digest, "size": len(data)}

    def capture_reference(self, path):
        path = _path(path)
        with exclusive_lock(self.root / ".lock"):
            return dict(self._blob(_read(path)), source=str(path))

    def _refs(self, refs):
        if not isinstance(refs, (list, tuple)) or not refs:
            raise RecoveryError("explicit captured outcome/evidence references required")
        for ref in refs:
            if not isinstance(ref, dict) or not _SHA.fullmatch(str(ref.get("sha256", ""))) or ref.get("ref") != "blobs/" + ref["sha256"]:
                raise RecoveryError("unrecognized outcome reference")
            data = _read(self.root / ref["ref"])
            if _sha(data) != ref["sha256"] or len(data) != ref.get("size"):
                raise RecoveryError("outcome reference changed")
        return list(refs)

    def _effects(self, task):
        effects, inventory = {}, None
        for record in self._history():
            if record["task"] != task:
                continue
            p = record["payload"]
            if record["kind"] == "effect":
                effects[p["effect_id"]] = dict(p, record_sha256=_sha(_json(record)))
            elif record["kind"] == "resolution":
                effect = effects.get(p["effect_id"])
                if effect and effect["record_sha256"] == p["effect_sha256"]:
                    effect["resolution"] = p
            elif record["kind"] == "inventory":
                inventory = dict(p, record_sha256=_sha(_json(record)))
        return {"effects": effects, "inventory": inventory,
                "operator_review": bool(inventory and inventory.get("scope") == "unobserved_external_effects"
                                         and inventory.get("coverage") == "unknown"
                                         and inventory.get("decision") == "continue_without_automatic_replay"),
                "coverage": "explicit-journal-only" if effects else "unknown",
                "unobserved_coverage": "unknown"}

    def record_effect(self, task, effectID, intentSHA, status, outcomeRefs=()):
        _name(task)
        if not isinstance(effectID, str) or not effectID or len(effectID) > 256:
            raise RecoveryError("invalid effect ID")
        if status not in ("intended", "unknown", "confirmed"):
            raise RecoveryError("unrecognized effect status")
        with exclusive_lock(self.root / ".lock"):
            if not any(r["task"] == task and r["kind"] == "intent" and _sha(_json(r)) == intentSHA for r in self._history()):
                raise RecoveryError("effect intent is not retained for this task")
            refs = self._refs(outcomeRefs) if outcomeRefs or status == "confirmed" else []
            previous = self._effects(task)["effects"].get(effectID)
            if previous and (previous["intent_sha256"] != intentSHA or
                             previous["status"] == "confirmed" and status != "confirmed" or
                             previous["status"] == "unknown" and status == "intended"):
                raise RecoveryError("effect intent/status cannot regress")
            return self._record(task, "effect", {"effect_id": effectID, "intent_sha256": intentSHA,
                                                "status": status, "outcome_refs": refs})

    def declare_effect_inventory(self, task, evidence_refs):
        """Compatibility name for operator review; never proves full inventory."""
        return self.review_unobserved_effects(task, evidence_refs)

    def review_unobserved_effects(self, task, evidence_refs):
        """Explicit operator evidence accepting unknown coverage without replay."""
        with exclusive_lock(self.root / ".lock"):
            return self._record(task, "inventory", {"evidence_refs": self._refs(evidence_refs),
                                "scope": "unobserved_external_effects", "coverage": "unknown",
                                "decision": "continue_without_automatic_replay"})

    def resolve_effect(self, task, effectID, effectSHA, evidence_refs, decision="do_not_replay"):
        if decision != "do_not_replay":
            raise RecoveryError("resolution must explicitly forbid replay")
        with exclusive_lock(self.root / ".lock"):
            effect = self._effects(task)["effects"].get(effectID)
            if not effect or effect["record_sha256"] != effectSHA:
                raise RecoveryError("effect changed before Root resolution")
            return self._record(task, "resolution", {"effect_id": effectID, "effect_sha256": effectSHA,
                                "decision": decision, "evidence_refs": self._refs(evidence_refs)})

    def replay_guard(self, task, effectID):
        """Even confirmed/resolved effects cannot be automatically replayed."""
        with exclusive_lock(self.root / ".lock"):
            effect = self._effects(_name(task))["effects"].get(effectID)
            if effect is None:
                raise RecoveryError("effect has no retained intent")
            raise RecoveryError("automatic external-effect replay refused: " + effect["status"])

    def inspect(self, store, task, runtime_profiles=None):
        return inspect_recovery(store, task, runtime_profiles, recovery_store=self)

    def authorize_resume(self, reportSHA, currentfreshreport):
        """Records evidence of explicit review; no execution capability is minted."""
        if not isinstance(currentfreshreport, dict):
            raise RecoveryError("fresh inspection report required")
        with exclusive_lock(self.root / ".lock"):
            records = self._history()
            original = next((r["payload"] for r in records if r["kind"] == "inspection" and
                             _sha(_json(r["payload"])) == reportSHA), None)
            fresh = currentfreshreport
            if original is None or not any(r["kind"] == "inspection" and r["payload"] == fresh for r in records):
                raise RecoveryError("inspection not retained in this recovery store")
            # Independently recapture now; a supplied fresh report is not authority.
            now = _inspect(self, Path(fresh["task_root"]), fresh["task"], fresh.get("profile_root"))
            for report in (original, fresh, now):
                if report["blockers"]:
                    raise RecoveryError("resume blocked: " + ", ".join(report["blockers"]))
            projections = [resume_projection(report) for report in (original, fresh, now)]
            if projections[0] != projections[1] or projections[1] != projections[2]:
                raise RecoveryError("inspection binding changed; inspect and review again")
            return self._record(fresh["task"], "review", {"reviewed_report_sha256": reportSHA,
                          "fresh_report_sha256": _sha(_json(fresh)), "binding_sha256": now["binding_sha256"],
                          "task_root": fresh["task_root"], "generation": now["metadata"].get("generation"),
                          "session": now["metadata"].get("session"), "scope": "review_receipt_only",
                          "coverage": now["effects"]["coverage"], "unobserved_coverage": "unknown",
                          "operator_review_sha256": now["effects"]["inventory"]["record_sha256"],
                          "external_effect_replay": False})


def _inspect(recovery, task_root, task, profile_root):
    task_root = _path(task_root)
    _check(task_root)
    blockers, partial, captures = [], [], {}
    budget = {"files": 0, "bytes": 0, "start": time.monotonic()}

    def capture(path, label, required=False):
        path = _path(path)
        if budget["files"] >= MAX_FILES or budget["bytes"] >= MAX_TOTAL_BYTES or time.monotonic() - budget["start"] >= MAX_SECONDS:
            partial.append("snapshot budget exhausted: " + label)
            return None
        try:
            data = _read(path, min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - budget["bytes"]))
        except FileNotFoundError:
            captures[label] = {"source": str(path), "missing": True}
            if required:
                blockers.append("missing:" + label)
            return None
        except (OSError, RecoveryError) as exc:
            partial.append(label + ": " + str(exc))
            return None
        budget["files"] += 1
        budget["bytes"] += len(data)
        captures[label] = dict(recovery._blob(data), source=str(path))
        return data

    def tree(root, prefix, excluded=()):
        _check(root)
        if not root.is_dir():
            raise RecoveryError("snapshot directory missing: " + str(root))
        for directory, dirs, files in os.walk(str(root), followlinks=False,
                                               onerror=lambda exc: partial.append(str(exc))):
            base = Path(directory)
            for name in list(dirs):
                path = base / name
                if any(path == item or item in path.parents for item in excluded):
                    dirs.remove(name)
                    continue
                try:
                    _check(path)
                except RecoveryError as exc:
                    partial.append(str(exc))
                    dirs.remove(name)
            for name in sorted(files):
                path = base / name
                capture(path, prefix + path.relative_to(root).as_posix())
            if budget["files"] >= MAX_FILES or budget["bytes"] >= MAX_TOTAL_BYTES or time.monotonic() - budget["start"] >= MAX_SECONDS:
                partial.append("tree traversal budget exhausted: " + prefix)
                break

    raw = capture(task_root / (task + ".meta"), "metadata", True)
    aliases = [p.name for p in task_root.iterdir() if p.name.casefold() == (task + ".meta").casefold()]
    if aliases != [task + ".meta"]:
        blockers.append("canonical_task_spelling_mismatch")
    metadata = {}
    if raw is not None:
        try:
            for line in raw.decode("utf-8-sig").splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    if key in metadata:
                        raise RecoveryError("duplicate metadata key")
                    metadata[key] = value
        except (UnicodeError, RecoveryError):
            blockers.append("invalid_metadata")
    for key in ("generation", "session", "dir"):
        if not metadata.get(key):
            blockers.append("missing_metadata:" + key)
    owner = _owner(metadata)
    if owner["status"] in ("live_owner", "unverifiable"):
        blockers.append(owner["status"])
    # Capture every retained task sidecar, without state.read (which prunes logs).
    for path in sorted(task_root.iterdir(), key=lambda p: p.name):
        if not path.name.startswith(task + ".") or path.name == task + ".meta":
            continue
        try:
            _check(path)
            if path.is_dir():
                tree(path, "retained/" + path.name + "/")
            else:
                capture(path, "retained/" + path.name)
        except (OSError, RecoveryError) as exc:
            partial.append(str(exc))
    prompt = metadata.get("original_prompt")
    if prompt and _path(prompt) != task_root / (task + ".original.prompt"):
        blockers.append("original_prompt_reference_mismatch")
    capture(task_root / (task + ".original.prompt"), "original_prompt", True)
    capture(task_root / (task + ".answer"), "last_answer")
    receipts_raw = capture(task_root / (task + ".receipts.jsonl"), "inbox_receipts")
    receipts = []
    if receipts_raw:
        try:
            if not receipts_raw.endswith(b"\n"):
                raise RecoveryError("incomplete receipt tail")
            latest_receipts = {}
            for line in receipts_raw.decode("utf-8").splitlines():
                receipt = json.loads(line)
                if (not isinstance(receipt, dict) or receipt.get("schema") != 1 or
                    receipt.get("task") != task or
                    not re.fullmatch(r"[0-9a-f]{32}", str(receipt.get("id", ""))) or
                    type(receipt.get("sequence")) is not int or receipt["sequence"] < 1 or
                    type(receipt.get("rev")) is not int or receipt["rev"] < 1 or
                    receipt.get("state") not in ("intent", "accepted", "delivered", "acknowledged", "rolled_back") or
                    not re.fullmatch(r"sha256:[0-9a-f]{64}", str(receipt.get("digest", "")))):
                    raise RecoveryError("unrecognized receipt")
                key = receipt["id"]
                previous = latest_receipts.get(key)
                if previous and (receipt["rev"] <= previous["rev"] or receipt["digest"] != previous["digest"] or receipt["sequence"] != previous["sequence"]):
                    raise RecoveryError("receipt identity/order changed")
                latest_receipts[key] = receipt
            receipts = list(latest_receipts.values())
            for receipt in receipts:
                if receipt.get("delivery_status") in ("unknown", "attempting"):
                    blockers.append("inbox_execution_may_have_happened")
                elif receipt.get("delivery_status") not in ("pending", "delivered"):
                    blockers.append("unrecognized_inbox_outcome")
                if receipt.get("delivery_status") == "delivered" and (not receipt.get("delivered_epoch") or
                           receipt.get("state") not in ("delivered", "acknowledged")):
                    blockers.append("unrecognized_inbox_outcome")
                if receipt.get("state") == "intent":
                    blockers.append("inbox_acceptance_incomplete")
                payload = captures.get("retained/" + task + ".inbox/%012d.msg" % receipt["sequence"])
                if payload and not payload.get("missing") and payload["sha256"] != receipt["digest"].split(":", 1)[1]:
                    blockers.append("inbox_payload_digest_mismatch")
        except (ValueError, UnicodeError) as exc:
            blockers.append("invalid_inbox_receipts:" + str(exc))
    baseline_raw = capture(task_root / (task + ".baseline.json"), "baseline", True)
    baseline = {}
    if baseline_raw:
        try:
            baseline = json.loads(baseline_raw.decode("utf-8"))
            if not isinstance(baseline, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            blockers.append("invalid_baseline")
            baseline = {}
    if baseline.get("partial"):
        blockers.append("baseline_partial")
    initial = baseline.get("files", baseline)
    current = {}
    source = metadata.get("dir")
    if source:
        source = _path(source)
        try:
            _check(source)
            if not source.is_dir():
                raise RecoveryError("source tree missing")
            excluded = (task_root, recovery.root)
            for directory, dirs, files in os.walk(str(source), followlinks=False,
                                                   onerror=lambda exc: partial.append(str(exc))):
                base = Path(directory)
                for name in list(dirs):
                    path = base / name
                    if name in (".git", "__pycache__") or any(path == root or root in path.parents for root in excluded):
                        dirs.remove(name)
                        continue
                    try:
                        _check(path)
                    except RecoveryError as exc:
                        partial.append(str(exc))
                        dirs.remove(name)
                for name in sorted(files):
                    path = base / name
                    if any(path == root or root in path.parents for root in excluded):
                        continue
                    relative = path.relative_to(source).as_posix()
                    data = capture(path, "source/" + relative)
                    if data is not None:
                        current[relative] = {"sha256": _sha(data), "size": len(data), "type": "file"}
                if budget["files"] >= MAX_FILES or time.monotonic() - budget["start"] >= MAX_SECONDS:
                    partial.append("source traversal budget exhausted")
                    break
        except (OSError, RecoveryError) as exc:
            partial.append(str(exc))
    delta = []
    if isinstance(initial, dict):
        for name in sorted(set(initial) | set(current)):
            if Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name or ":" in name:
                blockers.append("unsafe_baseline_reference")
                continue
            before, after = initial.get(name), current.get(name)
            if before is not None and (not isinstance(before, dict) or before.get("fingerprint") or
                                       not _SHA.fullmatch(str(before.get("sha256", "")))):
                blockers.append("baseline_preimage_unverifiable")
                continue
            if before and before.get("content"):
                content = str(before["content"])
                if not _SHA.fullmatch(content):
                    blockers.append("unsafe_baseline_preimage")
                else:
                    preimage = capture(task_root / (task + ".baseline.files") / content, "preimage/" + name, True)
                    if preimage is not None and _sha(preimage) != before.get("sha256"):
                        blockers.append("baseline_preimage_hash_mismatch")
            if (before or {}).get("sha256") != (after or {}).get("sha256"):
                delta.append({"path": name, "before": before, "after": after,
                              "absence_proven": not bool(partial)})
    else:
        blockers.append("invalid_baseline_files")
    git = {"available": False, "commands": []}
    if source and any((p / ".git").exists() for p in (source,) + tuple(source.parents)):
        # Child-only environment; no global config, index or source writes.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        commands = (("status", "--porcelain=v1", "-z", "--untracked-files=all"),
                    ("diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD", "--"))
        for args in commands:
            argv = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                    "-C", str(source)] + list(args)
            try:
                # Spool under the owned recovery root; never buffer arbitrary Git output.
                with tempfile.TemporaryFile(dir=str(recovery.root)) as output, tempfile.TemporaryFile(dir=str(recovery.root)) as error:
                    proc = subprocess.run(argv, stdout=output, stderr=error, env=env, timeout=5,
                              **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
                    output.seek(0)
                    error.seek(0)
                    out, err = output.read(MAX_FILE_BYTES + 1), error.read(MAX_FILE_BYTES + 1)
                if len(out) > MAX_FILE_BYTES or len(err) > MAX_FILE_BYTES:
                    partial.append("git output bound exceeded")
                else:
                    git["commands"].append({"argv": argv, "exit": proc.returncode,
                                            "stdout": recovery._blob(out), "stderr": recovery._blob(err)})
                    git["available"] = True
                if proc.returncode:
                    blockers.append("git_inspection_failed")
            except (OSError, subprocess.TimeoutExpired) as exc:
                partial.append("git inspection failed: " + str(exc))
    profile = None
    if profile_root:
        profile_root = _path(profile_root)
        try:
            root_raw = capture(profile_root / ".runtime-owner", "profile_root_owner", True)
            manifest_raw = capture(profile_root / task / "manifest.json", "profile_manifest", True)
            seal_raw = capture(profile_root / task / ".profile-owner", "profile_seal", True)
            if any(d is None for d in (root_raw, manifest_raw, seal_raw)):
                raise RecoveryError("runtime profile capture incomplete")
            root_owner, profile, seal = [json.loads(d.decode("utf-8")) for d in (root_raw, manifest_raw, seal_raw)]
            expected = {"schema": 1, "root_id": root_owner["id"], "task": task,
                        "path": str(profile_root / task), "manifest_sha256": _sha(manifest_raw)}
            if seal != expected or profile.get("task") != task or profile.get("provider") != metadata.get("engine"):
                raise RecoveryError("runtime profile seal/task/provider mismatch")
            if root_owner.get("root") != str(profile_root) or root_owner.get("schema") != 1 or profile.get("schema") != 1:
                raise RecoveryError("runtime profile root/schema mismatch")
            expected_paths = {n: str(profile_root / task / n) for n in ("config", "cache", "data", "state")}
            if profile.get("paths") != expected_paths:
                raise RecoveryError("runtime profile path mismatch")
            for path in expected_paths.values():
                tree(_path(path), "profile_files/" + Path(path).name + "/")
            for relative, expected_digest in profile.get("seeds", {}).items():
                if Path(relative).is_absolute() or ".." in Path(relative).parts or ":" in relative or "\\" in relative:
                    raise RecoveryError("unsafe profile seed reference")
                seed = capture(profile_root / task / relative, "profile_seed/" + relative, True)
                if seed is None or _sha(seed) != expected_digest:
                    raise RecoveryError("runtime profile seed changed")
            executable = profile["executable"]
            path = _path(executable["path"])
            if not Path(executable["path"]).is_absolute():
                raise RecoveryError("runtime executable path must be absolute")
            _check(path)
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                before_exe = os.fstat(stream.fileno())
                for block in iter(lambda: stream.read(65536), b""):
                    digest.update(block)
                    if time.monotonic() - budget["start"] >= MAX_SECONDS:
                        raise RecoveryError("runtime executable hash time budget exceeded")
                after_exe = os.fstat(stream.fileno())
            if (before_exe.st_size, before_exe.st_mtime_ns) != (after_exe.st_size, after_exe.st_mtime_ns):
                raise RecoveryError("runtime executable changed during hash")
            if digest.hexdigest() != executable["sha256"]:
                raise RecoveryError("runtime executable SHA drift")
            profile = dict(profile, observed_executable_sha256=digest.hexdigest())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            blockers.append("profile_mismatch:" + str(exc))
    else:
        blockers.append("runtime_profile_unverified")
    effects = recovery._effects(task)
    if not effects["operator_review"]:
        blockers.append("external_inventory_unknown")
        blockers.append("operator_review_required:unobserved_effects")
    for effect in effects["effects"].values():
        if effect["status"] in ("intended", "unknown") and not effect.get("resolution"):
            blockers.append("execution_may_have_happened:" + effect["effect_id"])
        try:
            if effect["outcome_refs"]:
                recovery._refs(effect["outcome_refs"])
            if effect.get("resolution"):
                recovery._refs(effect["resolution"]["evidence_refs"])
        except RecoveryError:
            blockers.append("effect_evidence_changed")
    if effects["inventory"]:
        try:
            recovery._refs(effects["inventory"]["evidence_refs"])
        except (OSError, RecoveryError):
            blockers.append("inventory_evidence_changed")
    # Pin the executing implementation to the source bytes that will be reviewed.
    source_bytes = capture(_path(__file__), "inspection_implementation", True)
    if source_bytes is not None and _sha(source_bytes) != _COMPILED_SOURCE_SHA:
        blockers.append("inspection_source_hash_drift")
    if partial:
        blockers.append("snapshot_partial")
        for entry in delta:
            entry["absence_proven"] = False
    try:
        if raw is not None and _read(task_root / (task + ".meta")) != raw:
            blockers.append("metadata_changed_during_capture")
    except (OSError, RecoveryError):
        blockers.append("metadata_changed_during_capture")
    binding = {"task": task, "task_root": str(task_root), "metadata": metadata,
               "captures": captures, "current_source": current, "profile": profile,
               "profile_root": str(profile_root) if profile_root else None,
               "effects": effects, "inbox_receipts": receipts, "git": git,
               "implementation_sha256": _COMPILED_SOURCE_SHA}
    report = dict(binding, owner=owner, delta=delta, blockers=sorted(set(blockers)),
                  snapshot={"partial": bool(partial), "reasons": partial,
                            "bounds": {"files": MAX_FILES, "file_bytes": MAX_FILE_BYTES,
                                       "total_bytes": MAX_TOTAL_BYTES, "seconds": MAX_SECONDS},
                            "files": budget["files"], "bytes": budget["bytes"],
                            "excluded": [".git", "__pycache__", "task_root", "recovery_root"]},
                  binding_sha256=_sha(_json(resume_projection(binding))), schema=1, id=uuid.uuid4().hex,
                  captured_at_ns=time.time_ns(), automatic_replay=False)
    return report


def inspect_recovery(store, task, runtime_profiles=None, *, recovery_store=None):
    """Capture actual sidecars/source/journal. store needs only a root attribute.

    Default recovery storage is <store.root>/recovery. runtime_profiles needs only
    root; no provider/session mutation, legacy state methods or model calls occur.
    """
    task = _name(task)
    task_root = _path(store.root)
    recovery = recovery_store or RecoveryStore(task_root / "recovery")
    profile_root = runtime_profiles.root if runtime_profiles is not None else None
    with exclusive_lock(recovery.root / ".lock"):
        recovery._record(task, "inspection_intent", {"task_root": str(task_root)})
        report = _inspect(recovery, task_root, task, profile_root)
        recovery._record(task, "inspection", report)
        return report


# Source-compiled fixtures set __file__ and compile these exact captured bytes.
# No package/PYC import or global sys.modules manipulation is needed.
_COMPILED_SOURCE_SHA = _sha(_read(_path(__file__), 2 * 1024 * 1024))
