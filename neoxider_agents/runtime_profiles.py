"""Retained, fail-closed provider homes. No wrapper integration or authentication copy.

Integrity seals detect corruption, not a malicious process running as the same user.
Path checks cannot eliminate concurrent replacement by that user (see the contract).
"""
import ctypes
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import uuid
import unicodedata
import warnings

from .state import valid_name


class RuntimeProfileError(RuntimeError):
    """A profile cannot safely be created, resumed or destroyed."""


_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SYSTEM_KEYS = frozenset(("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
                          "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
                          "TZ", "HOME", "USERPROFILE"))
_PATHS = ("config", "cache", "data", "state")
_SEEDS = {"opencode": {"config/opencode/opencode.json", "cache/opencode/models.json"},
          "codex": {"state/models.json"}}
_MAX_FILE = 1024 * 1024
_SEED_PRIVATE_FIELDS = frozenset((
    "secret", "secrets", "password", "passwords", "credential", "credentials",
    "auth", "authentication", "authorization", "authkey", "authmethod",
    "token", "tokens", "accesstoken", "refreshtoken", "idtoken", "bearertoken",
    "sessiontoken", "clientsecret", "privatekey", "apikey", "apikeys",
    "header", "headers", "authorizationheader", "env", "environment", "envvars",
    "environmentvariables", "mcp", "plugin", "plugins", "command", "commands",
    "cmd", "exec", "execute", "executable", "shell", "script", "scripts"))
_SEED_POLICY_FIELDS = frozenset((
    "tool", "tools", "permission", "permissions", "agent", "agents", "system",
    "prompt", "prompts", "hook", "hooks", "subagent", "subagents",
    "toolpolicy", "toolpolicies", "toolspolicy", "toolpermissions"))
_SEED_CAPABILITY_FIELDS = frozenset((
    "modeltoolcapability", "toolcapability", "toolcall", "toolcalling",
    "supportstoolcalls", "supportstoolcalling"))
_MODEL_SELECTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.:/-]*\Z")


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _check(path, directory=None):
    """lstat every existing component; never resolve away a link."""
    path = Path(path)
    for part in reversed((path,) + tuple(path.parents)):
        s = part.lstat()
        if stat.S_ISLNK(s.st_mode) or getattr(s, "st_file_attributes", 0) & 0x400:
            raise RuntimeProfileError("linked/reparse path refused: %s" % part)
        if stat.S_ISREG(s.st_mode) and s.st_nlink != 1:
            raise RuntimeProfileError("hardlink refused: %s" % part)
    s = path.lstat()
    if directory is True and not stat.S_ISDIR(s.st_mode):
        raise RuntimeProfileError("directory required: %s" % path)
    if directory is False and not stat.S_ISREG(s.st_mode):
        raise RuntimeProfileError("regular file required: %s" % path)
    return s


def _private(path):
    if os.name != "nt":
        os.chmod(str(path), 0o700 if path.is_dir() else 0o600)
        return
    # Owner Rights SID: protected DACL grants only the object's owner full access.
    # Inherit this restriction into provider-created children, without source ACL edits.
    from ctypes import wintypes
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = adv.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    setter = adv.SetFileSecurityW
    setter.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    setter.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    sd = ctypes.c_void_p()
    acl = "D:P(A;OICI;FA;;;OW)" if path.is_dir() else "D:P(A;;FA;;;OW)"
    if not convert(acl, 1, ctypes.byref(sd), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not setter(str(path), 0x80000004, sd):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(sd)


def _read(path):
    _check(path, False)
    with path.open("rb") as stream:
        data = stream.read(_MAX_FILE + 1)
    if len(data) > _MAX_FILE:
        raise RuntimeProfileError("oversized profile file")
    return data


def _write(path, data):
    stream = path.open("xb")
    try:
        with stream:
            _private(path)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        _check(path, False)
        path.unlink()  # Only the file this call exclusively created.
        raise


def _executable(path):
    path = Path(path)
    if not path.is_absolute():
        raise RuntimeProfileError("executable must be absolute")
    _check(path, False)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return {"path": str(path), "sha256": digest.hexdigest()}


def _public_seed(data, destination):
    """Config selects models only; catalogs describe public capabilities, not policy."""
    def pairs(items):
        result = {}
        for key, child in items:
            if key in result:
                raise RuntimeProfileError("duplicate seed JSON field refused")
            result[key] = child
        return result
    def nonfinite(unused):
        raise RuntimeProfileError("non-finite seed JSON value refused")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RuntimeProfileError("invalid public seed JSON") from exc
    if destination == "config/opencode/opencode.json":
        if not isinstance(value, dict) or set(value) - {"model", "small_model"}:
            raise RuntimeProfileError("config seed allows only model/small_model selectors")
        for selector in value.values():
            if not isinstance(selector, str) or len(selector) > 256 or not _MODEL_SELECTOR.fullmatch(selector):
                raise RuntimeProfileError("invalid public model selector")
    elif not isinstance(value, (dict, list)):
        raise RuntimeProfileError("model catalog must be a JSON object or array")
    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", key).casefold())
                if normalized in _SEED_PRIVATE_FIELDS or normalized in _SEED_POLICY_FIELDS:
                    raise RuntimeProfileError("non-public seed field refused")
                if normalized in _SEED_CAPABILITY_FIELDS and not isinstance(child, bool):
                    raise RuntimeProfileError("model tool capability must be a boolean")
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            if re.search(r"\{(?:env|file):|\$\{[^}]+\}|://[^/\s]*@|\b(?:sk-|Bearer\s)", item, re.I):
                raise RuntimeProfileError("credential-like seed value refused")
    try:
        visit(value)
    except RecursionError as exc:
        raise RuntimeProfileError("seed JSON nesting too deep") from exc
    return data


class RuntimeProfiles:
    """Only opencode and codex have implemented supported home knobs.

    root's parent must exist. Existing roots must carry this module's ownership
    marker. create never reuses a task; load/validate/environment resume it.
    environment(base_env) supplies launch values and never serializes or mutates them.
    create(base_env) is deprecated and unused; passing it emits DeprecationWarning.
    """
    def __init__(self, root):
        self.root = Path(os.path.abspath(os.fspath(root)))

    def _task(self, task):
        if not isinstance(task, str):
            raise RuntimeProfileError("unsafe task name")
        try:
            valid_name(task)
        except ValueError as exc:
            raise RuntimeProfileError("unsafe task name") from exc
        stem = task.split(".", 1)[0]
        if task.endswith((".", " ")) or stem.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[0-9]", stem, re.I):
            raise RuntimeProfileError("reserved task name")
        return self.root / task

    @contextmanager
    def _name_lock(self, task, owner):
        """Casefold guard on all platforms; OS releases the advisory lock on crash.

        Guard files stay in the owned root. Never unlink an advisory lock's inode.
        Legacy task-directory spelling is preserved, including uppercase names.
        """
        key = task.casefold()
        guard = self.root / (".name-" + _hash(key.encode("ascii")) + ".lock")
        expected = _bytes({"root_id": owner["id"], "casefold": key})
        deadline = time.monotonic() + 2
        try:
            fd = os.open(str(guard), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            _check(guard, False)
            fd = os.open(str(guard), os.O_RDWR)
            created = False
        with os.fdopen(fd, "r+b") as stream:
            if created:
                _private(guard)
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
                        raise RuntimeProfileError("name guard busy")
                    time.sleep(0.01)
            try:
                _check(guard, False)
                stream.seek(0)
                current = stream.read(_MAX_FILE + 1)
                if not current:
                    stream.seek(0)
                    stream.write(expected)
                    stream.flush()
                    os.fsync(stream.fileno())
                elif current != expected:
                    raise RuntimeProfileError("name guard incomplete/mismatched")
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _root(self, create=False):
        if create:
            _check(self.root.parent, True)
            try:
                self.root.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                _private(self.root)
                _write(self.root / ".runtime-owner", _bytes({"schema": 1,
                       "root": str(self.root), "id": uuid.uuid4().hex}))
        _check(self.root, True)
        # Another creator may be between mkdir and publishing the root marker.
        deadline = time.monotonic() + (2 if create else 0)
        while True:
            try:
                marker = json.loads(_read(self.root / ".runtime-owner"))
                break
            except (FileNotFoundError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeProfileError("root ownership marker missing/invalid")
                time.sleep(0.01)
        if set(marker) != {"schema", "root", "id"} or marker["schema"] != 1 or marker["root"] != str(self.root) or not re.fullmatch(r"[0-9a-f]{32}", marker["id"]):
            raise RuntimeProfileError("root ownership mismatch")
        return marker

    def create(self, task, engine, executable, version=None, seed_files=None,
               base_env=None, credential_env_keys=()):
        if base_env is not None:
            warnings.warn("create(base_env) is deprecated and unused; pass it to environment()",
                          DeprecationWarning, stacklevel=2)
        path = self._task(task)
        if engine not in _SEEDS:
            raise RuntimeProfileError("unsupported provider runtime home: %s" % engine)
        pin = _executable(executable)
        if version is not None and (not isinstance(version, str) or len(version) > 256 or any(ord(c) < 32 for c in version)):
            raise RuntimeProfileError("invalid supplied version")
        keys = list(credential_env_keys)
        if len(keys) > 64 or any(not isinstance(k, str) or len(k) > 128 or not _KEY.fullmatch(k) or k.upper() in _SYSTEM_KEYS or k.upper().startswith(("XDG_", "CODEX_", "OPENCODE_")) for k in keys) or len(keys) != len(set(keys)):
            raise RuntimeProfileError("invalid credential allowlist")
        seeds = {}
        for name, source in (seed_files or {}).items():
            if name not in _SEEDS[engine]:
                raise RuntimeProfileError("seed destination not allowlisted")
            seeds[name] = _public_seed(_read(Path(os.path.abspath(os.fspath(source)))), name)
        owner = self._root(create=True)
        with self._name_lock(task, owner):
            if any(p.name.casefold() == task.casefold() for p in self.root.iterdir()):
                raise RuntimeProfileError("task profile/case alias already exists")
            try:
                path.mkdir(mode=0o700)  # Atomic reservation; never overwrite.
            except FileExistsError:
                raise RuntimeProfileError("task profile already exists")
        try:
            _private(path)
            for name in _PATHS:
                child = path / name
                child.mkdir(mode=0o700)
                _private(child)
            for name, data in seeds.items():
                target = path / name
                if not target.parent.exists():
                    target.parent.mkdir(mode=0o700)
                    _private(target.parent)
                _write(target, data)
            manifest = {"schema": 1, "task": task, "provider": engine,
                        "executable": pin, "version": {"value": version,
                        "provenance": "supplied-unverified" if version is not None else "unknown"},
                        "paths": {n: str(path / n) for n in _PATHS},
                        "credential_env_keys": keys,
                        "seeds": {n: _hash(d) for n, d in seeds.items()}}
            raw = _bytes(manifest)
            _write(path / "manifest.json", raw)
            _write(path / ".profile-owner", _bytes({"schema": 1, "root_id": owner["id"],
                   "task": task, "path": str(path), "manifest_sha256": _hash(raw)}))
        except BaseException:
            # Our exclusive reservation only. Refuse cleanup if paths became links.
            self._remove_tree(path)
            raise
        return self.load(task)

    def _metadata(self, task):
        owner = self._root()
        path = self._task(task)
        _check(path, True)
        seal = json.loads(_read(path / ".profile-owner"))
        raw = _read(path / "manifest.json")
        expected = {"schema": 1, "root_id": owner["id"], "task": task,
                    "path": str(path), "manifest_sha256": _hash(raw)}
        if seal != expected:
            raise RuntimeProfileError("profile ownership/integrity mismatch")
        manifest = json.loads(raw)
        if manifest.get("schema") != 1 or manifest.get("task") != task or manifest.get("provider") not in _SEEDS:
            raise RuntimeProfileError("unsupported/mismatched manifest")
        if manifest.get("paths") != {n: str(path / n) for n in _PATHS}:
            raise RuntimeProfileError("runtime path mismatch")
        return manifest

    def load(self, task):
        return self.validate(task)

    def validate(self, task):
        try:
            manifest = self._metadata(task)
            pin = manifest["executable"]
            if _executable(pin["path"]) != pin:
                raise RuntimeProfileError("provider executable changed")
            for path in manifest["paths"].values():
                _check(Path(path), True)
                for current, dirs, files in os.walk(path, followlinks=False):
                    for name in dirs + files:
                        _check(Path(current) / name)
            for name, digest in manifest["seeds"].items():
                if name not in _SEEDS[manifest["provider"]] or _hash(_read(self._task(task) / name)) != digest:
                    raise RuntimeProfileError("seed changed")
            self._receipt(task, manifest)
            return manifest
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeProfileError("profile validation failed: %s" % type(exc).__name__) from exc

    def environment(self, task, base_env=None):
        manifest = self.validate(task)
        source = os.environ if base_env is None else base_env
        keys = _SYSTEM_KEYS | {k.upper() for k in manifest["credential_env_keys"]}
        env = {k: v for k, v in source.items() if k.upper() in keys}
        paths = manifest["paths"]
        if manifest["provider"] == "opencode":
            env.update({"XDG_%s_HOME" % n.upper(): paths[n] for n in _PATHS})
            # Launch scope only: never mutate parent environment/global settings.
            env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
            if "cache/opencode/models.json" in manifest["seeds"]:
                env["OPENCODE_MODELS_PATH"] = str(self._task(task) / "cache/opencode/models.json")
        else:
            env["CODEX_HOME"] = paths["state"]
        return env

    def version_receipt(self, task):
        """Read an optional observed version receipt without changing the manifest."""
        return self._receipt(task, self.validate(task))

    def _receipt(self, task, manifest):
        target = self._task(task) / "version-receipt.json"
        if not os.path.lexists(str(target)):
            return None
        try:
            envelope = json.loads(_read(target))
            receipt = envelope["receipt"]
            if envelope.get("sha256") != _hash(_bytes(receipt)) or receipt.get("schema") != 1 or receipt.get("task") != task or receipt.get("provider") != manifest["provider"] or receipt.get("executable") != manifest["executable"] or receipt.get("manifest_sha256") != _hash(_read(self._task(task) / "manifest.json")):
                raise RuntimeProfileError("version receipt integrity/pin mismatch")
            return receipt
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeProfileError("invalid version receipt") from exc

    def probe_version(self, task, timeout=5, max_bytes=4096, base_env=None):
        """Explicit --version only; bounded, hidden, shell-free, credential-free.

        Receipt records observed numeric version and hashes, not arbitrary output.
        This executes the pinned binary: it does not prove vendor authenticity.
        Caller must serialize against launches/cleanup; no SDK lease is implied.
        """
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 30 or not isinstance(max_bytes, int) or not 32 <= max_bytes <= 65536:
            raise RuntimeProfileError("invalid version probe bounds")
        manifest = self.validate(task)
        if self.version_receipt(task) is not None:
            raise RuntimeProfileError("version receipt already exists; probe is immutable")
        source = os.environ if base_env is None else base_env
        env = self.environment(task, {k: v for k, v in source.items() if k.upper() in _SYSTEM_KEYS})
        pin = manifest["executable"]
        output = bytearray()
        complete = threading.Event()
        overflow = threading.Event()
        errors = []
        process = reader = job = None
        options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NO_WINDOW | 4}
        try:
            if os.name == "nt":
                from .windows import Job, resume
                job = Job()
            process = subprocess.Popen([pin["path"], "--version"], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       shell=False, cwd=str(self._task(task)), env=env,
                                       close_fds=True, **options)
            if job:
                job.assign(process._handle)
                resume(process.pid)
            def drain():
                try:
                    while True:
                        block = os.read(process.stdout.fileno(), min(1024, max_bytes + 1 - len(output)))
                        if not block:
                            break
                        output.extend(block)
                        if len(output) > max_bytes:
                            overflow.set()
                            break
                except OSError as exc:
                    errors.append(exc)
                finally:
                    complete.set()
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            deadline = time.monotonic() + timeout
            while process.poll() is None or not complete.is_set():
                if overflow.is_set() or errors or time.monotonic() >= deadline:
                    raise RuntimeProfileError("version probe output overflow/read failure/timeout")
                time.sleep(0.01)
            if overflow.is_set() or errors or process.returncode != 0:
                raise RuntimeProfileError("version probe failed")
            text = bytes(output).decode("utf-8", errors="strict").strip()
            match = re.fullmatch(r"(?:[A-Za-z][A-Za-z0-9_. -]{0,64} )?v?(\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.-]{1,64})?)", text)
            if not match:
                raise RuntimeProfileError("version probe did not return one version line")
            if self.validate(task)["executable"] != pin:
                raise RuntimeProfileError("executable changed during probe")
            receipt = {"schema": 1, "task": task, "provider": manifest["provider"],
                       "executable": pin, "version": match.group(1),
                       "provenance": "observed-executable-output", "argv": ["--version"],
                       "returncode": 0, "output_sha256": _hash(bytes(output)),
                       "manifest_sha256": _hash(_read(self._task(task) / "manifest.json"))}
            _write(self._task(task) / "version-receipt.json",
                   _bytes({"receipt": receipt, "sha256": _hash(_bytes(receipt))}))
            return receipt
        except (OSError, UnicodeError) as exc:
            raise RuntimeProfileError("version probe launch/read/write failed") from exc
        finally:
            try:
                if job:
                    job.close()  # KILL_ON_JOB_CLOSE also reaches children after leader exit.
                elif process:
                    import signal
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            finally:
                if process:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=2)
                    if reader:
                        reader.join(timeout=2)
                    if process.stdout:
                        process.stdout.close()

    @staticmethod
    def _remove_tree(path):
        # Validate the entire tree before deleting anything. Do not follow links.
        _check(path, True)
        items = []
        for current, dirs, files in os.walk(str(path), followlinks=False):
            for name in dirs + files:
                child = Path(current) / name
                _check(child)
                items.append(child)
        for child in sorted(items, key=lambda p: len(p.parts), reverse=True):
            _check(child)
            if child.is_dir():
                child.rmdir()
            else:
                child.unlink()
        _check(path, True)
        path.rmdir()

    def cleanup(self, task, live=False):
        """Destroy after CALLER assertion of stopped status; no active PID check.

        live=False is not evidence of inactivity. Safe automatic cleanup requires
        future SDK lease integration checking real PID + creation stamp/ownership.
        """
        if live:
            raise RuntimeProfileError("cannot destroy a live task profile")
        # Executable drift must not prevent explicit destruction, but marker/seal must match.
        try:
            self._metadata(task)
            target = self._task(task)
            if target.parent != self.root or target == self.root:
                raise RuntimeProfileError("cleanup target outside root")
            self._remove_tree(target)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeProfileError("profile cleanup refused: %s" % type(exc).__name__) from exc
