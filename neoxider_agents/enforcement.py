"""Fail-closed enforced-scope boundary, separate from legacy ownership hints.

No enforced backend is currently accepted. Validation below is preflight,
not a security boundary: it cannot prevent a concurrent path replacement.
Actual launch ALWAYS raises before constructing or starting a subprocess.
"""
import os
import stat
from dataclasses import dataclass
from pathlib import Path


class EnforcementError(RuntimeError):
    def __init__(self, code, message, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}

    def to_dict(self):
        return {"code": self.code, "message": str(self),
                "evidence": self.evidence}


class UnsafeScope(EnforcementError):
    pass


class EnforcementUnavailable(EnforcementError):
    pass


@dataclass(frozen=True)
class ScopeRequest:
    workspace: str
    writable: tuple
    network: bool = False
    credentials: bool = False


def capabilities():
    from .windows_enforcement import probe
    evidence = probe()
    return {"enforced_write_scope": False, "legacy_owns": "declaration-only",
            "launch": "fail-closed", "probe": evidence}


def _reject(code, message):
    raise UnsafeScope(code, message)


def _checked_path(path):
    """Fresh lstat each component, without resolving through existing links."""
    if not path.is_absolute():
        _reject("absolute-workspace-required", "Workspace must be absolute.")
    if ".." in path.parts:
        _reject("traversal", "Parent traversal is not an exact scope.")
    chain = list(reversed(path.parents)) + [path]
    for item in chain:
        try:
            info = item.lstat()
        except FileNotFoundError:
            _reject("missing-path", "Scope paths must already exist: %s" % item)
        except OSError as exc:
            raise UnsafeScope("path-inspection-failed", str(exc)) from exc
        if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & 0x400):
            _reject("link-or-reparse", "Links/reparse points are not accepted: %s" % item)
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            _reject("hardlink", "Multiply linked files are not accepted: %s" % item)
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            _reject("special-file", "Special filesystem objects are not accepted.")
    return info


def _checked_tree(path, visit=None):
    if visit:
        visit(path)
    info = _checked_path(path)
    if stat.S_ISDIR(info.st_mode):
        # Stream entries: os.walk materializes an unbounded sibling list before
        # a caller can enforce its aggregate inspection budget.
        try:
            with os.scandir(str(path)) as entries:
                for entry in entries:
                    _checked_tree(Path(entry.path), visit)
        except OSError as exc:
            raise UnsafeScope("tree-inspection-failed", str(exc)) from exc


def validate_request(request, *, _visit=None):
    """Validate exact existing workspace-relative files/directories read-only.

    Returns a normalized immutable request. This says nothing about whether
    launch is supported, and never copies files or edits their permissions.
    """
    if not isinstance(request, ScopeRequest):
        _reject("request-type", "Expected ScopeRequest.")
    if type(request.network) is not bool or type(request.credentials) is not bool:
        _reject("capability-type", "Network and credentials must be explicit bools.")
    if (not isinstance(request.workspace, str) or not request.workspace
            or "\x00" in request.workspace):
        _reject("workspace-type", "Workspace must be a nonempty string.")
    root = Path(request.workspace)
    if not stat.S_ISDIR(_checked_path(root).st_mode):
        _reject("workspace-directory-required", "Workspace must be a directory.")
    if not isinstance(request.writable, tuple) or not request.writable:
        _reject("exact-scope-required", "Writable scope must be a nonempty tuple.")
    checked = []
    for value in request.writable:
        if not isinstance(value, str) or not value or "\x00" in value:
            _reject("scope-type", "Each scope entry must be a nonempty string.")
        # Reject ADS, drive-relative, wildcard, UNC and platform-dependent slash
        # semantics even on POSIX; these must not become portable escape paths.
        if any(char in value for char in "*?[]:") or "\\" in value:
            _reject("unsupported-path-semantics", "Use exact relative slash paths.")
        part = Path(value)
        for segment in value.split("/"):
            basename = segment.split(".", 1)[0].upper()
            reserved = ("CON", "PRN", "AUX", "NUL") + tuple(
                prefix + str(number) for prefix in ("COM", "LPT")
                for number in range(1, 10))
            if segment.endswith((" ", ".")) or basename in reserved:
                _reject("windows-path-alias", "Device/trailing-dot aliases are unsupported.")
        if part.is_absolute() or ".." in part.parts or value in (".", "./"):
            _reject("outside-or-traversal", "Scope must name paths inside workspace.")
        candidate = root / part
        _checked_tree(candidate, _visit)
        normalized = part.as_posix()
        if normalized in checked:
            _reject("duplicate-scope", "Duplicate scope entries are not accepted.")
        checked.append(normalized)
    return ScopeRequest(str(root), tuple(checked), request.network,
                        request.credentials)


def prepare(request):
    """Fail before security/profile/source mutation when enforcement unavailable."""
    validate_request(request)
    evidence = capabilities()
    raise EnforcementUnavailable("enforced-backend-unavailable",
                                 "No reviewed enforced launch backend is available.",
                                 evidence)


def launch(request, argv, **kwargs):
    """Never accept an unrestricted launcher, forged plan or fallback callback."""
    # argv/kwargs are intentionally never evaluated or forwarded to a launcher.
    # A future implementation must replace this boundary, not add a fallback.
    return prepare(request)
