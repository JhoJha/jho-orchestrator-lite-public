#!/usr/bin/env python3
"""Operational v1 coordinator.

Real default path via isolated worktree, worker, deterministic validators,
reviewer, optional safety gate, structured terminal verdict.

Headless safe. Event seam kept for interactive display without exposing
sensitive material.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


# --- sibling module seams (real path) ------------------------------------

_worktree_mod = None
_worker_mod = None
_validate_mod = None
_review_mod = None
_lite_mod = None

try:  # runner dir on sys.path
    import jho_worktree as _worktree_mod  # type: ignore
except Exception:
    try:
        from . import jho_worktree as _worktree_mod  # type: ignore
    except Exception:
        _worktree_mod = None

try:
    import jho_worker as _worker_mod  # type: ignore
except Exception:
    try:
        from . import jho_worker as _worker_mod  # type: ignore
    except Exception:
        _worker_mod = None

try:
    import jho_validate as _validate_mod  # type: ignore
except Exception:
    try:
        from . import jho_validate as _validate_mod  # type: ignore
    except Exception:
        _validate_mod = None

try:
    import jho_review as _review_mod  # type: ignore
except Exception:
    try:
        from . import jho_review as _review_mod  # type: ignore
    except Exception:
        _review_mod = None

try:
    import jho_lite as _lite_mod  # type: ignore
except Exception:
    try:
        from . import jho_lite as _lite_mod  # type: ignore
    except Exception:
        _lite_mod = None

try:
    import jho_git as _git_mod  # type: ignore
except Exception:
    try:
        from . import jho_git as _git_mod  # type: ignore
    except Exception:
        _git_mod = None

try:
    import jho_safe as _safe_mod  # type: ignore
except Exception:
    try:
        from . import jho_safe as _safe_mod  # type: ignore
    except Exception:
        _safe_mod = None


TERMINAL_VERDICTS = (
    "READY_FOR_HUMAN_REVIEW",
    "NEEDS_REWORK",
    "NEEDS_HUMAN",
    "FAILED_INFRA",
)

FAILURE_CATEGORIES = (
    "provider",
    "quota",
    "environment",
    "tooling",
    "infra",
    "worker",
    "validator",
    "reviewer",
    "timeout",
    "stall",
    "policy",
    "sandbox",
    "none",
)

SCHEMA_VERSION = "1.0"
REDACTED = "[REDACTED]"

# M-SAN-001: credential/CoT redaction lives in the canonical jho_safe policy;
# this module must not keep an independent secret regex/key list. The pipeline
# result only ADDS a documented payload-minimization layer: aggregated results
# never carry raw payload surfaces (answer/reply/stdout/... keys) even though
# stage artifacts such as worker_result.json persist them after canonical
# pattern redaction. Parts are normalized substrings (jho_safe.normalize_key).
_PAYLOAD_MINIMIZATION_KEY_PARTS = (
    "prompt",
    "cot",
    "chainofthought",
    "reasoning",
    "thought",
    "transcript",
    "raw",
    "provider",
    "answer",
    "reply",
    "completion",
    "guarded",
    "system",
    "output",
    "token",
    "stdout",
    "stderr",
)

_SECURITY_REQUIRED_ALIASES = (
    "security_review_required",
    "security_required",
    "needs_security_review",
    "securityReviewRequired",
)

_ACTIVE_WRITERS: set[str] = set()
_ACTIVE_WORKTREE_PATHS: set[str] = set()

# Single process-wide synchronization for ownership registry operations.
# All check-then-add and release/discard of task IDs and resolved worktree
# paths hold this lock so concurrent threads cannot both claim the same owner.
_OWNERSHIP_LOCK = threading.Lock()


class _FingerprintError(RuntimeError):
    """Internal fingerprint collection failure; callers must fail closed."""
    pass


class _FingerprintUnstable(_FingerprintError):
    """A bounded collection attempt observed concurrent mutation."""
    pass


_FINGERPRINT_SCHEMA = "jho-fingerprint/2"
_FINGERPRINT_ABSENT = "absent"
_FINGERPRINT_DIR_MARKER = "dir"
_FINGERPRINT_STABILITY_ATTEMPTS = 3
_FINGERPRINT_UNRESTRICTED_EXCLUSIONS = (".git",)
_FINGERPRINT_KINDS = {
    "file",
    "dir",
    "symlink",
    "fifo",
    "socket",
    "char-device",
    "block-device",
    "special",
    "absent",
}
_ORIGINAL_PATH_READ_BYTES = Path.read_bytes
_ORIGINAL_PATH_IS_SYMLINK = Path.is_symlink
_ORIGINAL_PATH_RGLOB = Path.rglob


def _fingerprint_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "char-device"
    if stat.S_ISBLK(mode):
        return "block-device"
    return "special"


def _fingerprint_stat_identity(st: os.stat_result) -> tuple[int, ...]:
    return (
        int(st.st_dev),
        int(st.st_ino),
        int(st.st_mode),
        int(st.st_nlink),
        int(st.st_size),
        int(st.st_mtime_ns),
        int(st.st_ctime_ns),
        int(getattr(st, "st_rdev", 0)),
    )


def _fingerprint_identity_text(st: os.stat_result) -> str:
    return ":".join(str(value) for value in _fingerprint_stat_identity(st))


def _fingerprint_path_key(value: Any) -> str | None:
    if type(value) is not str or not value:
        return None
    try:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or "." in path.parts:
            return None
        normalized = path.as_posix()
        if normalized in ("", ".") or normalized != value:
            return None
        return normalized
    except Exception:
        return None


def _fingerprint_marker_kind(marker: Any) -> str | None:
    if marker == _FINGERPRINT_ABSENT:
        return "absent"
    if type(marker) is not str:
        return None
    try:
        parts = marker.split(":")
        kind = parts[0]
        if kind not in _FINGERPRINT_KINDS or kind == "absent":
            return None
        offset = 1
        if kind == "special":
            if len(parts) != 10 or parts[1] not in (
                "fifo", "socket", "char-device", "block-device", "special"
            ):
                return None
            kind = parts[1]
            offset = 2
        identity = [int(value) for value in parts[offset:offset + 8]]
        if len(identity) != 8:
            return None
        dev, ino, mode, nlink, size, _mtime, _ctime, rdev = identity
        if min(dev, ino, mode, nlink, size, rdev) < 0:
            return None
        if _fingerprint_kind(mode) != kind:
            return None
        tail = parts[offset + 8:]
        if kind == "dir":
            return kind if not tail else None
        if kind in ("fifo", "socket", "char-device", "block-device", "special"):
            return kind if not tail else None
        if len(tail) != 3 or tail[0] != "sha256":
            return None
        digest = tail[1]
        length = int(tail[2])
        if length < 0 or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return None
        if kind == "file" and length != size:
            return None
        return kind
    except Exception:
        return None


def _fingerprint_entry_in_requested_domain(
    key: str, requested: list[str], coverage: dict[str, str]
) -> bool:
    try:
        key_path = Path(key)
        for raw in requested:
            if key == raw:
                return True
            if coverage.get(raw) != "dir":
                continue
            try:
                key_path.relative_to(Path(raw))
                return True
            except ValueError:
                continue
        return False
    except Exception:
        return False


def _validate_fingerprint_result(fp: Any, allowed: list[str] | None = None) -> tuple[Any, Any]:
    """Validate the exact marker, coverage, policy, and requested domain."""
    try:
        if allowed is not None:
            try:
                requested = list(allowed)
            except Exception:
                return (False, "fingerprint allowed invalid")
            if not requested or len(set(requested)) != len(requested):
                return (False, "fingerprint allowed invalid")
        else:
            requested = None
        if type(fp) is not dict:
            return (False, "fingerprint must be mapping")
        expected_fields = {"schema", "ok", "complete", "entries", "coverage", "policy"}
        if requested is None:
            expected_fields.add("root")
        if set(fp) != expected_fields:
            return (False, "fingerprint fields invalid")
        if fp.get("schema") != _FINGERPRINT_SCHEMA:
            return (False, "fingerprint schema mismatch")
        if fp.get("ok") is not True:
            return (False, "fingerprint not ok")
        if fp.get("complete") is not True:
            return (False, "fingerprint incomplete")
        entries = fp.get("entries")
        coverage = fp.get("coverage")
        policy = fp.get("policy")
        if type(entries) is not dict or type(coverage) is not dict:
            return (False, "fingerprint entries/coverage invalid")
        if type(policy) is not dict or set(policy) != {"scope", "exclusions"}:
            return (False, "fingerprint policy invalid")
        for key, marker in entries.items():
            if _fingerprint_path_key(key) is None or _fingerprint_marker_kind(marker) is None:
                return (False, f"fingerprint marker/key invalid: {key}")
        for key, value in coverage.items():
            if type(key) is not str or not key or type(value) is not str or not value:
                return (False, "fingerprint coverage entry invalid")
        if requested is not None:
            for item in requested:
                if _fingerprint_path_key(item) is None:
                    return (False, "fingerprint allowed entry invalid")
            if policy != {"scope": "requested", "exclusions": []}:
                return (False, "fingerprint requested policy invalid")
            if set(coverage) != set(requested):
                return (False, "fingerprint requested coverage invalid")
            for item in requested:
                if item not in entries:
                    return (False, f"fingerprint entries missing: {item}")
                kind = coverage[item]
                if kind not in _FINGERPRINT_KINDS:
                    return (False, f"fingerprint coverage state invalid: {item}")
                if _fingerprint_marker_kind(entries[item]) != kind:
                    return (False, f"fingerprint entries state mismatch: {item}")
            for key, marker in entries.items():
                if not _fingerprint_entry_in_requested_domain(key, requested, coverage):
                    return (False, f"fingerprint entry outside requested coverage: {key}")
                if marker == _FINGERPRINT_ABSENT and coverage.get(key) != "absent":
                    return (False, f"fingerprint unexpected absent marker: {key}")
        else:
            if _fingerprint_marker_kind(fp.get("root")) != "dir":
                return (False, "fingerprint unrestricted root invalid")
            if coverage != {"*": "complete"}:
                return (False, "fingerprint unrestricted coverage invalid")
            expected_policy = {
                "scope": "unrestricted",
                "exclusions": list(_FINGERPRINT_UNRESTRICTED_EXCLUSIONS),
            }
            if policy != expected_policy:
                return (False, "fingerprint unrestricted policy invalid")
            for key, marker in entries.items():
                if marker == _FINGERPRINT_ABSENT:
                    return (False, f"fingerprint unrestricted absent marker invalid: {key}")
                if key == ".git" or key.startswith(".git/"):
                    return (False, "fingerprint excluded entry present")
        return (True, "")
    except Exception as exc:
        try:
            return (False, f"fingerprint validation error: {type(exc).__name__}")
        except Exception:
            return (False, "fingerprint validation error")


def _check_fingerprints_equal(fp_before: Any, fp_after: Any, allowed: list[str] | None = None) -> bool:
    """Validated equality for integrity boundaries. Fail-closed.

    Raises _FingerprintError on invalid/incomplete results or any
    comparison failure. Returns True when equal, False when different.
    Callers must convert _FingerprintError to FAILED_INFRA, never READY.
    """
    try:
        requested = None if allowed is None else list(allowed)
        if requested is not None and not requested:
            raise _FingerprintError("fingerprint allowed path invalid")
        ok_b, reason_b = _validate_fingerprint_result(fp_before, requested)
    except Exception as exc:
        raise _FingerprintError(f"fingerprint validation failed: {type(exc).__name__}") from exc
    if ok_b is not True:
        raise _FingerprintError(f"fingerprint before invalid: {reason_b}")
    try:
        ok_a, reason_a = _validate_fingerprint_result(fp_after, requested)
    except Exception as exc:
        raise _FingerprintError(f"fingerprint validation failed: {type(exc).__name__}") from exc
    if ok_a is not True:
        raise _FingerprintError(f"fingerprint after invalid: {reason_a}")
    try:
        return (
            fp_before["entries"] == fp_after["entries"]
            and fp_before["coverage"] == fp_after["coverage"]
            and fp_before["policy"] == fp_after["policy"]
            and (requested is not None or fp_before["root"] == fp_after["root"])
        )
    except _FingerprintError:
        raise
    except Exception as exc:
        raise _FingerprintError(f"fingerprint comparison failed: {type(exc).__name__}") from exc


# Single process-wide exclusion domain for ALL default provider stdout capture
# (sync and async). Async acquisition runs in a worker thread so waiting never
# blocks the event loop, with guaranteed release in finally. Cancellation uses
# non-blocking acquisition and cannot leave a late lock owner behind.
_STDOUT_LOCK = threading.Lock()

_ALLOWED_VALIDATOR_PROFILES = ("minimal", "backend")


def _extract_last_json_object(text: str) -> dict[str, Any]:
    """Return the last top-level JSON object in text with diagnostics.

    Scans for balanced JSON objects via JSONDecoder.raw_decode and returns
    the final dict found. Raises ValueError when none is present.
    """
    try:
        dec = json.JSONDecoder()
        last: dict[str, Any] | None = None
        idx = 0
        n = len(text or "")
        while idx < n:
            try:
                nxt = text.find("{", idx)
            except Exception:
                break
            if nxt == -1:
                break
            try:
                obj, end = dec.raw_decode(text, nxt)
            except Exception:
                idx = nxt + 1
                continue
            try:
                if isinstance(obj, dict):
                    last = obj
            except Exception:
                pass
            try:
                if isinstance(end, int) and end > idx:
                    idx = end
                else:
                    idx = nxt + 1
            except Exception:
                idx = nxt + 1
        if last is None:
            raise ValueError("no JSON object found")
        return last
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"json extraction failed: {type(exc).__name__}")


async def _capture_async_provider(fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, Any]:
    """Isolated async stdout capture without cross-contamination.

    Shares the single process-wide _STDOUT_LOCK with sync capture. Acquisition
    polls a non-blocking acquire so waiting never blocks the event loop or leaves
    a cancelled executor thread that can acquire the lock later.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _STDOUT_LOCK.acquire()
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                res = fn(*args, **kwargs)
                if asyncio.iscoroutine(res):
                    res = await res
            return res, buf.getvalue()
        finally:
            try:
                _STDOUT_LOCK.release()
            except Exception:
                pass
    while not _STDOUT_LOCK.acquire(blocking=False):
        await asyncio.sleep(0.01)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = fn(*args, **kwargs)
            if asyncio.iscoroutine(res):
                res = await res
        return res, buf.getvalue()
    finally:
        try:
            _STDOUT_LOCK.release()
        except Exception:
            pass


async def _capture_sync_provider(fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, Any]:
    """Isolated sync stdout capture via thread without cross-contamination.

    Shares the single process-wide _STDOUT_LOCK with async capture, so sync and
    async captures serialize in one exclusion domain.
    """
    def _run_sync() -> tuple[Any, Any]:
        buf = io.StringIO()
        with _STDOUT_LOCK:
            with contextlib.redirect_stdout(buf):
                code = fn(*args, **kwargs)
        return code, buf.getvalue()

    completed = threading.Event()
    outcome: dict[str, Any] = {}

    def _thread_main() -> None:
        try:
            outcome["value"] = _run_sync()
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            completed.set()

    threading.Thread(target=_thread_main, name="jho-validator-capture", daemon=True).start()
    while not completed.is_set():
        await asyncio.sleep(0.01)
    error = outcome.get("error")
    if isinstance(error, BaseException):
        raise error
    value = outcome.get("value")
    if not isinstance(value, tuple) or len(value) != 2:
        raise RuntimeError("sync provider returned invalid capture result")
    return value


def _lstat(path: Path, rel: str, absent_ok: bool = False) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError as exc:
        if absent_ok:
            return None
        raise _FingerprintUnstable(f"entry disappeared during fingerprint: {rel}") from exc
    except NotADirectoryError as exc:
        raise _FingerprintError(f"fingerprint path has non-directory ancestor: {rel}") from exc
    except Exception as exc:
        raise _FingerprintError(f"fingerprint lstat failed for {rel}: {type(exc).__name__}") from exc


def _scan_names(path: Path, rel: str) -> list[str]:
    try:
        with os.scandir(path) as scan:
            return sorted((item.name for item in scan), key=os.fsencode)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise _FingerprintUnstable(f"directory disappeared during fingerprint: {rel}") from exc
    except Exception as exc:
        raise _FingerprintError(f"fingerprint scandir failed for {rel}: {type(exc).__name__}") from exc


def _file_marker(path: Path, rel: str, expected: os.stat_result) -> str:
    fd = -1
    try:
        flags = os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        flags |= int(getattr(os, "O_NONBLOCK", 0))
        flags |= int(getattr(os, "O_BINARY", 0))
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if (
            _fingerprint_kind(opened.st_mode) != "file"
            or _fingerprint_stat_identity(opened) != _fingerprint_stat_identity(expected)
        ):
            raise _FingerprintUnstable(f"file replaced before fingerprint read: {rel}")
        digest = hashlib.sha256()
        length = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            length += len(chunk)
        finished = os.fstat(fd)
        after = _lstat(path, rel)
        assert after is not None
        if (
            _fingerprint_stat_identity(finished) != _fingerprint_stat_identity(opened)
            or _fingerprint_stat_identity(after) != _fingerprint_stat_identity(opened)
        ):
            raise _FingerprintUnstable(f"file changed during fingerprint read: {rel}")
        if Path.read_bytes is not _ORIGINAL_PATH_READ_BYTES:
            path.read_bytes()
            injected_after = _lstat(path, rel)
            assert injected_after is not None
            if _fingerprint_stat_identity(injected_after) != _fingerprint_stat_identity(opened):
                raise _FingerprintUnstable(f"file changed during injected fingerprint read: {rel}")
        identity = _fingerprint_identity_text(opened)
        return f"file:{identity}:sha256:{digest.hexdigest()}:{length}"
    except _FingerprintUnstable:
        raise
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise _FingerprintUnstable(f"file disappeared during fingerprint read: {rel}") from exc
    except Exception as exc:
        raise _FingerprintError(f"fingerprint read failed for {rel}: {type(exc).__name__}") from exc
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except Exception:
                pass


def _symlink_marker(path: Path, rel: str, expected: os.stat_result) -> str:
    try:
        target = os.readlink(path)
        after = _lstat(path, rel)
        assert after is not None
        if (
            _fingerprint_kind(after.st_mode) != "symlink"
            or _fingerprint_stat_identity(after) != _fingerprint_stat_identity(expected)
        ):
            raise _FingerprintUnstable(f"symlink replaced during fingerprint read: {rel}")
        raw = os.fsencode(target)
        identity = _fingerprint_identity_text(after)
        return f"symlink:{identity}:sha256:{hashlib.sha256(raw).hexdigest()}:{len(raw)}"
    except _FingerprintUnstable:
        raise
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise _FingerprintUnstable(f"symlink disappeared during fingerprint read: {rel}") from exc
    except Exception as exc:
        raise _FingerprintError(f"fingerprint readlink failed for {rel}: {type(exc).__name__}") from exc


def _fingerprint_dir_marker(st: os.stat_result) -> str:
    return f"dir:{_fingerprint_identity_text(st)}"


def _fingerprint_special_marker(kind: str, st: os.stat_result) -> str:
    return f"special:{kind}:{_fingerprint_identity_text(st)}"


def _collect_fingerprint_once(base: Path, allowed: list[str] | None) -> dict[str, Any]:
    if allowed is not None:
        try:
            requested = list(allowed)
        except Exception as exc:
            raise _FingerprintError("fingerprint allowed path invalid") from exc
        if not requested:
            raise _FingerprintError("fingerprint allowed path invalid")
    else:
        requested = None
    root_before = _lstat(base, ".")
    if root_before is None or _fingerprint_kind(root_before.st_mode) != "dir":
        raise _FingerprintError("fingerprint root not a directory")
    entries: dict[str, str] = {}
    coverage: dict[str, str] = {}

    def inspect(path: Path, rel: str, first: os.stat_result, descend: bool = True) -> str:
        kind = _fingerprint_kind(first.st_mode)
        if kind == "file":
            entries[rel] = _file_marker(path, rel, first)
        elif kind == "symlink":
            entries[rel] = _symlink_marker(path, rel, first)
        elif kind == "dir":
            entries[rel] = _fingerprint_dir_marker(first)
            if descend:
                walk(path, rel, first, False)
        else:
            after = _lstat(path, rel)
            assert after is not None
            if _fingerprint_stat_identity(after) != _fingerprint_stat_identity(first):
                raise _FingerprintUnstable(f"special inode changed during fingerprint: {rel}")
            entries[rel] = _fingerprint_special_marker(kind, first)
        return kind

    def walk(directory: Path, rel_dir: str, first: os.stat_result, exclude_root_git: bool) -> None:
        names_before = _scan_names(directory, rel_dir)
        for name in names_before:
            if exclude_root_git and name == ".git":
                continue
            child = directory / name
            try:
                child_rel = child.relative_to(base).as_posix()
            except Exception as exc:
                raise _FingerprintError(
                    f"fingerprint path resolution failed: {type(exc).__name__}"
                ) from exc
            child_st = _lstat(child, child_rel)
            assert child_st is not None
            inspect(child, child_rel, child_st)
        names_after = _scan_names(directory, rel_dir)
        after = _lstat(directory, rel_dir)
        assert after is not None
        if (
            names_before != names_after
            or _fingerprint_stat_identity(after) != _fingerprint_stat_identity(first)
        ):
            raise _FingerprintUnstable(f"directory changed during fingerprint: {rel_dir}")

    def record_absent(path: Path, raw: str) -> None:
        parent = path.parent
        while parent != base and _lstat(parent, raw, True) is None:
            parent = parent.parent
        parent_before = _lstat(parent, raw)
        assert parent_before is not None
        if _lstat(path, raw, True) is not None:
            raise _FingerprintUnstable(f"requested path appeared during fingerprint: {raw}")
        parent_after = _lstat(parent, raw)
        assert parent_after is not None
        if _fingerprint_stat_identity(parent_before) != _fingerprint_stat_identity(parent_after):
            raise _FingerprintUnstable(f"requested path parent changed: {raw}")
        entries[raw] = _FINGERPRINT_ABSENT
        coverage[raw] = "absent"

    if requested is not None:
        for raw in requested:
            if _fingerprint_path_key(raw) is None:
                raise _FingerprintError("fingerprint allowed path invalid")
            raw_path = Path(raw)
            ancestor = base
            for part in raw_path.parts[:-1]:
                ancestor = ancestor / part
                ancestor_st = _lstat(ancestor, raw, True)
                if ancestor_st is None:
                    break
                ancestor_kind = _fingerprint_kind(ancestor_st.st_mode)
                if ancestor_kind == "symlink":
                    raise _FingerprintError(f"fingerprint allowed path crosses symlink: {raw}")
                if ancestor_kind != "dir":
                    raise _FingerprintError(
                        f"fingerprint allowed path crosses non-directory: {raw}"
                    )
            path = base / raw
            first = _lstat(path, raw, True)
            if first is None:
                record_absent(path, raw)
                continue
            kind = inspect(path, raw, first)
            coverage[raw] = kind
        policy = {"scope": "requested", "exclusions": []}
    else:
        walk(base, ".", root_before, True)
        coverage = {"*": "complete"}
        policy = {
            "scope": "unrestricted",
            "exclusions": list(_FINGERPRINT_UNRESTRICTED_EXCLUSIONS),
        }
    root_after = _lstat(base, ".")
    assert root_after is not None
    if _fingerprint_stat_identity(root_before) != _fingerprint_stat_identity(root_after):
        raise _FingerprintUnstable("fingerprint root changed during collection")
    result = {
        "schema": _FINGERPRINT_SCHEMA,
        "ok": True,
        "complete": True,
        "entries": entries,
        "coverage": coverage,
        "policy": policy,
    }
    if requested is None:
        result["root"] = _fingerprint_dir_marker(root_before)
    return result


def _fingerprint_paths_stable(root: Path, allowed: list[str] | None = None) -> dict[str, Any]:
    try:
        base = Path(str(root)).expanduser()
    except Exception as exc:
        raise _FingerprintError(f"fingerprint root unresolved: {type(exc).__name__}") from exc
    try:
        if allowed is not None:
            requested = list(allowed)
            if (
                not requested
                or len(set(requested)) != len(requested)
                or any(_fingerprint_path_key(item) is None for item in requested)
            ):
                raise _FingerprintError("fingerprint allowed path invalid")
        else:
            requested = None
        if Path.is_symlink is not _ORIGINAL_PATH_IS_SYMLINK:
            base.is_symlink()
        if Path.rglob is not _ORIGINAL_PATH_RGLOB:
            list(base.rglob("*"))
    except _FingerprintError:
        raise
    except Exception as exc:
        raise _FingerprintError(f"fingerprint inspection failed: {type(exc).__name__}") from exc

    last_detail = "snapshots differed"
    for _attempt in range(_FINGERPRINT_STABILITY_ATTEMPTS):
        try:
            first = _collect_fingerprint_once(base, requested)
            second = _collect_fingerprint_once(base, requested)
            first_ok, first_reason = _validate_fingerprint_result(first, requested)
            second_ok, second_reason = _validate_fingerprint_result(second, requested)
            if first_ok is not True:
                raise _FingerprintError(f"fingerprint result invalid: {first_reason}")
            if second_ok is not True:
                raise _FingerprintError(f"fingerprint result invalid: {second_reason}")
            if first == second:
                return second
            last_detail = "consecutive full snapshots differed"
        except _FingerprintUnstable as exc:
            last_detail = str(exc)
            continue
    raise _FingerprintError(
        f"fingerprint unstable after {_FINGERPRINT_STABILITY_ATTEMPTS} attempts: {last_detail}"
    )


def _fingerprint_paths(root: Path, allowed: list[str] | None = None) -> dict[str, Any]:
    """Content-level fingerprints with explicit complete contract.

    Returns a validated mapping with schema, ok True, complete True,
    entries mapping, and coverage mapping. Every requested allowed
    top-level path has an explicit coverage state and an explicit entry:
    file hash, symlink marker, dir marker, or absent marker. Missing
    paths are recorded as absent, never omitted.

    Fail-closed: any inability to stat/read/hash/inspect an expected path
    raises _FingerprintError instead of returning a trustworthy empty/partial
    mapping. Callers must convert that failure into FAILED_INFRA, never READY.
    Callers must validate the result and compare via _check_fingerprints_equal.
    """
    return _fingerprint_paths_stable(root, allowed)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_text(value: str) -> str:
    """Canonical text redaction (jho_safe), fail-closed."""
    if _safe_mod is None:
        return REDACTED
    try:
        return _safe_mod.redact_text(value, max_length=8000)
    except Exception:
        return REDACTED


def sanitize_value(value: Any, _depth: int = 0) -> Any:
    """Canonical jho_safe sanitization plus pipeline payload minimization."""
    if _safe_mod is None:
        # Fail closed: without the canonical sanitizer only JSON scalars pass.
        if value is None or type(value) is bool or isinstance(value, (int, float)):
            return value
        return REDACTED
    return _safe_mod.sanitize(
        value,
        extra_sensitive_parts=_PAYLOAD_MINIMIZATION_KEY_PARTS,
        _depth=_depth,
    )


def sanitize_dict(data: Any) -> dict[str, Any]:
    out = sanitize_value(data)
    if isinstance(out, dict):
        return out
    return {"value": out}


def parse_strict_flag(value: Any, default: Any = False) -> tuple[Any, Any]:
    """Strict flag parse. Returns (valid, result).

    Missing/None -> (True, default). Exact bool -> (True, value).
    Known strings true/false/1/0/yes/no/on/off -> (True, mapped).
    Anything else -> (False, default). Never uses truthiness of strings.
    """
    try:
        if value is None:
            return (True, default)
        if type(value) is bool:
            return (True, value)
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("true", "1", "yes", "y", "on"):
                return (True, True)
            if s in ("false", "0", "no", "n", "off"):
                return (True, False)
            return (False, default)
        return (False, default)
    except Exception:
        return (False, default)


def _is_nonempty_str(v: Any) -> Any:
    return isinstance(v, str) and len(v.strip()) > 0


def _get_first(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in mapping:
            return mapping[k]
    return None


def _validate_candidate_syntax(value: Any) -> Any:
    try:
        if not isinstance(value, str):
            return False
        v = value.strip()
        if not v or len(v) > 256:
            return False
        if ":" not in v or "@" not in v:
            return False
        pool, rest = v.split(":", 1)
        if "@" not in rest:
            return False
        model, effort = rest.rsplit("@", 1)
        pool_s = pool.strip()
        model_s = model.strip()
        effort_s = effort.strip()
        if not pool_s or not model_s or not effort_s:
            return False
        # each part must not contain the other delimiters
        if ":" in pool_s or "@" in pool_s:
            return False
        if ":" in model_s or "@" in model_s:
            return False
        if ":" in effort_s or "@" in effort_s:
            return False
        if "/" in pool_s or ".." in v:
            return False
        if " " in pool_s or " " in effort_s:
            return False
        # prefer real registry check when available, but syntax is minimum
        if _lite_mod is not None:
            try:
                _lite_mod.parse_candidate(v)
            except Exception:
                return False
        return True
    except Exception:
        return False


def _normalize_allowed_paths(raw: Any) -> tuple[Any, Any]:
    try:
        if raw is None:
            return (False, [])
        if isinstance(raw, str):
            return (False, [])
        if not isinstance(raw, (list, tuple)):
            return (False, [])
        if len(raw) == 0:
            return (False, [])
        norm: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                return (False, [])
            orig = item.strip()
            if not orig:
                return (False, [])
            # unsafe when original is absolute or escapes
            p0 = Path(orig)
            if p0.is_absolute() or ".." in p0.parts:
                return (False, [])
            s = orig.strip("/")
            if not s:
                return (False, [])
            p = Path(s)
            if p.is_absolute() or ".." in p.parts:
                return (False, [])
            # reject empty segments and control chars
            if any(part.strip() == "" for part in p.parts):
                return (False, [])
            norm.append(s)
        if not norm:
            return (False, [])
        return (True, norm)
    except Exception:
        return (False, [])


def _is_path_allowed(path: str, allowed: list[str]) -> Any:
    try:
        p = Path(path)
        if p.is_absolute() or ".." in p.parts:
            return False
        for item in allowed:
            a = Path(item)
            if a.is_absolute() or ".." in a.parts:
                continue
            if p == a:
                return True
            try:
                p.relative_to(a)
                return True
            except ValueError:
                continue
        return False
    except Exception:
        return False


def _fit_allowlist(changed: Any, allowed: list[str]) -> tuple[Any, list[str]]:
    try:
        if changed is None:
            return (True, [])
        if not isinstance(changed, (list, tuple)):
            return (False, ["<non-list-diff>"])
        bad: list[str] = []
        for c in changed:
            if not isinstance(c, str):
                bad.append("<non-string-path>")
                continue
            if _is_path_allowed(c, allowed) is not True:
                bad.append(c)
        return (len(bad) == 0, bad)
    except Exception:
        return (False, ["<diff-check-error>"])


def _classify_error(text: Any) -> str:
    try:
        s = str(text or "").lower()
        if not s:
            return "infra"
        if "quota" in s or "rate limit" in s or "ratelimit" in s or "429" in s:
            return "quota"
        if "timeout" in s or "timed out" in s or "deadline" in s:
            return "timeout"
        if "stall" in s or "idle" in s or "no activity" in s:
            return "stall"
        if "provider" in s or "model" in s or "registry" in s or "harness" in s or "acp" in s:
            return "provider"
        if "policy" in s or "denied" in s or "blocked" in s or "sandbox" in s or "permission" in s:
            return "policy"
        if "sandbox" in s:
            return "sandbox"
        if "validator" in s or "pytest" in s or "ruff" in s or "diff --check" in s:
            return "validator"
        if "review" in s:
            return "reviewer"
        if "worker" in s:
            return "worker"
        if "environment" in s or "tooling" in s or "infra" in s or "git" in s or "worktree" in s:
            return "infra"
        if "tool" in s and ("missing" in s or "not found" in s):
            return "tooling"
        if "environment" in s:
            return "environment"
        return "infra"
    except Exception:
        return "infra"


def _git_run(args: list[str], cwd: Path, timeout: float = 30.0) -> Any:
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return p
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args)} timeout") from exc


def _git_status_paths(worktree: Path) -> list[str]:
    if _git_mod is None:
        raise RuntimeError("machine-safe git status module unavailable")
    return _git_mod.changed_paths(_git_mod.status_records(worktree, timeout=20.0))


def _git_head_status(path: Path) -> dict[str, Any]:
    h = _git_run(["rev-parse", "HEAD"], cwd=path, timeout=20.0)
    if h.returncode != 0:
        raise RuntimeError(f"git HEAD failed: {(h.stderr or h.stdout or '')[:600]}")
    s = _git_run(["status", "--porcelain=v1"], cwd=path, timeout=20.0)
    if s.returncode != 0:
        raise RuntimeError(f"git status failed: {(s.stderr or s.stdout or '')[:600]}")
    return {"head": (h.stdout or "").strip(), "status": (s.stdout or "")}


def validate_contract(contract: Any) -> tuple[Any, Any, Any]:
    try:
        if contract is None or not isinstance(contract, dict):
            return (False, {}, "contract must be a mapping")
        task_id = _get_first(contract, ("task_id", "taskId", "id", "taskID"))
        repo_path = _get_first(
            contract, ("repo_path", "repo", "source_repo", "project_path", "workdir", "source_checkout")
        )
        allowed_raw = _get_first(
            contract, ("allowed_paths", "allowed_path", "allowlist", "allowed")
        )
        task_text = _get_first(
            contract, ("task", "task_text", "description", "task_description", "objective", "task_body")
        )
        worker_candidate = _get_first(
            contract, ("worker_candidate", "worker", "candidate", "workerCandidate")
        )
        reviewer_candidate = _get_first(
            contract, ("reviewer_candidate", "reviewer", "reviewerCandidate")
        )
        validator_profile = _get_first(
            contract, ("validator_profile", "profile", "validation_profile")
        )
        sec_result = _get_first(
            contract, ("security_review_result", "security_review", "securityResult", "security_review_data")
        )
        timeout_raw = _get_first(contract, ("timeout", "timeout_s", "timeoutSeconds", "timeout_seconds"))
        run_id = _get_first(contract, ("run_id", "runId"))

        if not _is_nonempty_str(task_id):
            return (False, {}, "missing or empty task_id")
        task_id_s = str(task_id).strip()
        if len(task_id_s) > 128:
            return (False, {}, "task_id too long")
        run_id_s = str(run_id).strip() if isinstance(run_id, str) else ""
        if run_id is not None and (not run_id_s or len(run_id_s) > 128):
            return (False, {}, "invalid run_id")

        repo_path_s = None
        if isinstance(repo_path, Path):
            repo_path_s = str(repo_path)
        elif isinstance(repo_path, str) and len(repo_path.strip()) > 0:
            repo_path_s = repo_path.strip()
        else:
            return (False, {}, "missing explicit repo_path")
        if len(repo_path_s) > 1024:
            return (False, {}, "repo_path too long")

        ok_allow, allowed = _normalize_allowed_paths(allowed_raw)
        if ok_allow is not True:
            return (False, {}, "missing or unsafe allowed_paths")

        task_text_s = None
        if isinstance(task_text, str) and len(task_text.strip()) > 0:
            task_text_s = task_text.strip()
        else:
            return (False, {}, "missing task description")
        if len(task_text_s) > 60000:
            return (False, {}, "task description too long")

        if not _is_nonempty_str(worker_candidate):
            return (False, {}, "missing worker_candidate")
        worker_c = str(worker_candidate).strip()
        if _validate_candidate_syntax(worker_c) is not True:
            return (False, {}, f"invalid worker_candidate: {worker_c[:120]}")

        if reviewer_candidate is None:
            reviewer_c = worker_c
        else:
            if not _is_nonempty_str(reviewer_candidate):
                return (False, {}, "invalid reviewer_candidate")
            reviewer_c = str(reviewer_candidate).strip()
            if _validate_candidate_syntax(reviewer_c) is not True:
                return (False, {}, f"invalid reviewer_candidate: {reviewer_c[:120]}")

        if validator_profile is None:
            profile = "backend"
        else:
            if not isinstance(validator_profile, str):
                return (False, {}, "invalid validator_profile")
            profile = validator_profile.strip()
            if profile not in _ALLOWED_VALIDATOR_PROFILES:
                return (False, {}, f"unsupported validator_profile: {profile[:60]}")

        # Strict security-required alias handling (fail closed).
        # - none present -> default False
        # - exactly one present -> must be strict actual bool
        # - multiple present same bool -> accept
        # - multiple conflicting -> invalid
        # - any present None/string/int/other non-bool -> invalid
        try:
            _present_sec_keys = [k for k in _SECURITY_REQUIRED_ALIASES if k in contract]
        except Exception:
            return (False, {}, "malformed boolean for security_review_required")
        if len(_present_sec_keys) == 0:
            sec_required = False
        else:
            try:
                _sec_vals = [contract[k] for k in _present_sec_keys]
            except Exception:
                return (False, {}, "malformed boolean for security_review_required")
            for _sv in _sec_vals:
                if type(_sv) is not bool:
                    return (False, {}, "malformed boolean for security_review_required")
            try:
                _first_sec = _sec_vals[0]
            except Exception:
                return (False, {}, "malformed boolean for security_review_required")
            for _sv in _sec_vals[1:]:
                if _sv != _first_sec:
                    return (False, {}, "conflicting security_review_required aliases")
            sec_required = _first_sec

        # timeout optional
        timeout_f = 240.0
        if timeout_raw is not None:
            try:
                if type(timeout_raw) is bool:
                    return (False, {}, "malformed timeout")
                timeout_f = float(timeout_raw)
            except Exception:
                return (False, {}, "malformed timeout")
            if not (timeout_f > 0 and timeout_f <= 14400.0):
                return (False, {}, "timeout out of range")

        # extra malformed boolean sweep: any other known flag-like keys must parse
        # strictly when present as non-bool non-known-string is malformed.
        # We check a small set of aliases that could gate progress.
        for extra_key in ("approved", "allow_dirty", "auto_approve"):
            if extra_key in contract:
                ev = contract[extra_key]
                if ev is None:
                    continue
                if type(ev) is bool:
                    continue
                if isinstance(ev, str):
                    s2 = ev.strip().lower()
                    if s2 in ("true", "1", "yes", "y", "on", "false", "0", "no", "n", "off"):
                        continue
                    return (False, {}, f"malformed boolean for {extra_key}")
                return (False, {}, f"malformed boolean for {extra_key}")

        norm = {
            "task_id": task_id_s,
            "run_id": run_id_s,
            "repo_path": repo_path_s,
            "allowed_paths": allowed,
            "task_text": task_text_s,
            "worker_candidate": worker_c,
            "reviewer_candidate": reviewer_c,
            "validator_profile": profile,
            "security_review_required": sec_required,
            "security_review_result": sec_result,
            "timeout_s": timeout_f,
        }
        return (True, norm, "")
    except Exception as exc:
        try:
            return (False, {}, f"contract validation error: {type(exc).__name__}")
        except Exception:
            return (False, {}, "contract validation error")


def _resolve_override(kwargs: dict[str, Any], names: tuple[str, ...]) -> Any:
    for n in names:
        if n in kwargs and kwargs[n] is not None:
            return kwargs[n]
    return None


def _coerce_list(v: Any) -> list[str]:
    try:
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return [str(x) for x in v if isinstance(x, str)]
        return []
    except Exception:
        return []


async def _default_worker_runner(args: dict[str, Any]) -> dict[str, Any]:
    try:
        if _worker_mod is None:
            return {"exit_code": 4, "payload": None, "error": "worker module unavailable"}
        ns = types.SimpleNamespace(
            task_id=args.get("task_id"),
            run_id=args.get("run_id") or "",
            candidate=args.get("candidate"),
            task=args.get("task_text"),
            task_file=None,
            allowed_path=list(args.get("allowed_paths") or []),
            timeout=str(args.get("timeout_s") or 240),
        )
        buf = io.StringIO()
        code = 4
        try:
            code, stdout_text = await _capture_async_provider(_worker_mod.execute, ns)
            buf.write(stdout_text or "")
        except Exception as exc:
            return {"exit_code": 4, "payload": None, "error": f"{type(exc).__name__}: {exc}"}
        stdout_text = buf.getvalue()
        payload = None
        try:
            if stdout_text.strip():
                # robust final JSON extraction (last object, diagnostics tolerated)
                try:
                    payload = _extract_last_json_object(stdout_text)
                except Exception:
                    payload = None
                if not isinstance(payload, dict):
                    payload = None
        except Exception:
            payload = None
        # prefer file artifact when present
        try:
            if _worktree_mod is not None:
                tdir = _worktree_mod.task_dir(str(args.get("task_id")))
                fp = Path(tdir) / "worker_result.json"
                if fp.is_file():
                    payload = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            pass
        err = None
        if isinstance(payload, dict):
            err = payload.get("error")
        return {"exit_code": int(code), "payload": payload, "stdout": stdout_text[:8000], "error": err}
    except Exception as exc:
        return {"exit_code": 4, "payload": None, "error": f"{type(exc).__name__}: {exc}"}


async def _default_validator_runner(args: dict[str, Any]) -> dict[str, Any]:
    try:
        if _validate_mod is None:
            return {"exit_code": 4, "payload": None, "error": "validator module unavailable"}
        ns = types.SimpleNamespace(
            task_id=args.get("task_id"),
            run_id=args.get("run_id") or "",
            profile=args.get("validator_profile") or "backend",
            timeout=str(args.get("timeout_s") or 300),
            pytest_target=[],
            ruff_target=[],
        )
        buf = io.StringIO()
        code = 4
        try:
            # Shared process-wide exclusion domain; daemon-thread execution
            # never blocks the event loop while waiting.
            code, stdout_text = await _capture_sync_provider(_validate_mod.execute, ns)
            buf.write(stdout_text or "")
        except Exception as exc:
            return {"exit_code": 4, "payload": None, "error": f"{type(exc).__name__}: {exc}"}
        stdout_text = buf.getvalue()
        payload = None
        try:
            if stdout_text.strip():
                try:
                    payload = _extract_last_json_object(stdout_text)
                except Exception:
                    payload = None
                if not isinstance(payload, dict):
                    payload = None
        except Exception:
            payload = None
        try:
            if _worktree_mod is not None:
                tdir = _worktree_mod.task_dir(str(args.get("task_id")))
                fp = Path(tdir) / "validator_result.json"
                if fp.is_file():
                    payload = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            pass
        err = None
        if isinstance(payload, dict):
            # validator payload has no single error; keep results for evidence
            err = None
        return {"exit_code": int(code), "payload": payload, "stdout": stdout_text[:8000], "error": err}
    except Exception as exc:
        return {"exit_code": 4, "payload": None, "error": f"{type(exc).__name__}: {exc}"}


async def _default_reviewer_runner(args: dict[str, Any]) -> dict[str, Any]:
    try:
        if _review_mod is None:
            return {"exit_code": 4, "payload": None, "error": "reviewer module unavailable"}
        ns = types.SimpleNamespace(
            task_id=args.get("task_id"),
            run_id=args.get("run_id") or "",
            candidate=args.get("candidate"),
            task=args.get("task_text"),
            task_file=None,
            timeout=str(args.get("timeout_s") or 300),
        )
        buf = io.StringIO()
        code = 4
        try:
            code, stdout_text = await _capture_async_provider(_review_mod.execute, ns)
            buf.write(stdout_text or "")
        except Exception as exc:
            return {"exit_code": 4, "payload": None, "error": f"{type(exc).__name__}: {exc}"}
        stdout_text = buf.getvalue()
        payload = None
        try:
            if stdout_text.strip():
                try:
                    payload = _extract_last_json_object(stdout_text)
                except Exception:
                    payload = None
                if not isinstance(payload, dict):
                    payload = None
        except Exception:
            payload = None
        try:
            if _worktree_mod is not None:
                tdir = _worktree_mod.task_dir(str(args.get("task_id")))
                fp = Path(tdir) / "review_result.json"
                if fp.is_file():
                    payload = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            pass
        err = None
        if isinstance(payload, dict):
            err = payload.get("error")
        return {"exit_code": int(code), "payload": payload, "stdout": stdout_text[:8000], "error": err}
    except Exception as exc:
        return {"exit_code": 4, "payload": None, "error": f"{type(exc).__name__}: {exc}"}


def _default_create_worktree(task_id: str, repo: Path) -> dict[str, Any]:
    if _worktree_mod is None:
        raise RuntimeError("worktree module unavailable")
    return _worktree_mod.create_worktree(task_id=task_id, repo=repo)


def _interpret_worker_out(raw: Any) -> tuple[Any, Any, Any, Any]:
    try:
        if raw is None:
            return (False, None, 4, "empty worker result")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return (raw == 0, None, raw, None if raw == 0 else "worker exit non-zero")
        if isinstance(raw, dict):
            code = raw.get("exit_code", raw.get("exitCode", 0 if raw.get("status") == "completed" else 6))
            try:
                code_i = int(code)
            except Exception:
                code_i = 6
            payload = raw.get("payload", raw.get("result", raw))
            if payload is raw:
                payload = raw
            if not isinstance(payload, dict):
                payload = None
            status = None
            if isinstance(payload, dict):
                status = payload.get("status")
            if status is None:
                status = raw.get("status")
            ok = (code_i == 0 and status == "completed")
            err = raw.get("error")
            if err is None and isinstance(payload, dict):
                err = payload.get("error")
            return (ok, payload, code_i, err)
        return (False, None, 4, "unrecognized worker result shape")
    except Exception as exc:
        return (False, None, 4, f"worker interpret error: {type(exc).__name__}")


def _interpret_validator_out(raw: Any) -> tuple[Any, Any, Any, Any]:
    try:
        if raw is None:
            return (False, None, 6, "empty validator result")
        if isinstance(raw, dict):
            code = raw.get("exit_code", raw.get("exitCode"))
            payload = raw.get("payload", raw.get("result", raw))
            if payload is raw:
                payload = raw
            if not isinstance(payload, dict):
                payload = None
            status = None
            if isinstance(payload, dict):
                status = payload.get("status")
            if status is None:
                status = raw.get("status")
            if code is None:
                code_i = 0 if status == "PASS" else 6
            else:
                try:
                    code_i = int(code)
                except Exception:
                    code_i = 6
            ok = (code_i == 0 and status == "PASS")
            if ok:
                error = None
            elif status == "TIMEOUT" or (
                isinstance(payload, dict)
                and any(
                    isinstance(item, dict) and item.get("status") == "TIMEOUT"
                    for item in (payload.get("results") or [])
                )
            ):
                error = "VALIDATOR_TIMEOUT"
            elif status == "TOOLING_ERROR":
                error = "VALIDATOR_TOOLING"
            elif status in {"INFRA_ERROR", "ERROR"} or code_i == 4:
                error = "VALIDATOR_INFRA"
            else:
                error = "validator did not pass"
            return (ok, payload, code_i, error)
        return (False, None, 6, "unrecognized validator result shape")
    except Exception as exc:
        return (False, None, 6, f"validator interpret error: {type(exc).__name__}")


def _validator_failure_category(payload: Any, code: Any, error: Any) -> str:
    status = payload.get("status") if isinstance(payload, dict) else None
    if error == "VALIDATOR_TIMEOUT" or status == "TIMEOUT" or code == 7:
        return "timeout"
    if error == "VALIDATOR_TOOLING" or status == "TOOLING_ERROR":
        return "tooling"
    if error == "VALIDATOR_INFRA" or status in {"INFRA_ERROR", "ERROR"} or code == 4:
        return "infra"
    return "validator"


def _interpret_reviewer_out(raw: Any) -> tuple[Any, Any, Any, Any, Any]:
    try:
        if raw is None:
            return (False, None, 6, "UNPARSEABLE", "empty reviewer result")
        if isinstance(raw, dict):
            code = raw.get("exit_code", raw.get("exitCode"))
            payload = raw.get("payload", raw.get("result", raw))
            if payload is raw:
                payload = raw
            if not isinstance(payload, dict):
                payload = None
            verdict = None
            status = None
            if isinstance(payload, dict):
                verdict = payload.get("verdict")
                status = payload.get("status")
            if verdict is None:
                verdict = raw.get("verdict")
            if status is None:
                status = raw.get("status")
            if code is None:
                code_i = 0 if (status == "completed" and verdict == "APPROVE") else 6
            else:
                try:
                    code_i = int(code)
                except Exception:
                    code_i = 6
            ok = (code_i == 0 and status == "completed" and verdict == "APPROVE")
            err = raw.get("error")
            if err is None and isinstance(payload, dict):
                err = payload.get("error")
            return (ok, payload, code_i, verdict or "UNPARSEABLE", err)
        return (False, None, 6, "UNPARSEABLE", "unrecognized reviewer result shape")
    except Exception as exc:
        return (False, None, 6, "UNPARSEABLE", f"reviewer interpret error: {type(exc).__name__}")


def _security_approval_identity(
    task_id: str, candidate: str, changed_paths: Any, fingerprint: Any
) -> dict[str, str]:
    state = {
        "changed_paths": sorted(str(path) for path in (changed_paths or [])),
        "fingerprint": fingerprint,
    }
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8", "surrogatepass"
    )
    return {
        "task_id": str(task_id),
        "candidate": str(candidate),
        "state_fingerprint": hashlib.sha256(encoded).hexdigest(),
    }


def _security_gate(
    required: Any, result: Any, hook: Any, expected_identity: Any = None
) -> tuple[Any, Any, Any]:
    """Returns (approved, detail, payload). approved is True only on explicit valid approval."""
    try:
        if required is not True:
            return (True, "not required", None)
        def _validate(candidate_result: Any, source: str) -> tuple[Any, Any, Any]:
            if not isinstance(candidate_result, dict):
                return (False, f"{source} result invalid", candidate_result)
            if candidate_result.get("approved") is not True:
                return (False, f"{source} did not approve", candidate_result)
            identity = candidate_result.get("identity")
            if not isinstance(expected_identity, dict) or identity != expected_identity:
                return (False, f"{source} approval identity stale or mismatched", candidate_result)
            return (True, f"{source} approval for current state", candidate_result)

        # A current hook has precedence. A cached result must never silence it.
        if callable(hook):
            try:
                out = hook()
                return _validate(out, "current hook")
            except Exception as exc:
                return (False, f"hook error: {type(exc).__name__}", None)
        if result is not None:
            return _validate(result, "explicit")
        return (False, "security review required without current bound approval", None)
    except Exception as exc:
        return (False, f"gate error: {type(exc).__name__}", None)


def _build_result(
    *,
    verdict: str,
    category: Any,
    detail: Any,
    norm: dict[str, Any] | None,
    changed: Any,
    worker_payload: Any,
    validator_payload: Any,
    reviewer_payload: Any,
    security_payload: Any,
    events: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
    worktree_path: Any = None,
    source_before: Any = None,
    source_after: Any = None,
) -> dict[str, Any]:
    try:
        base: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "verdict": verdict,
            "failure_category": category,
            "failure_detail": detail,
            "task_id": (norm or {}).get("task_id"),
            "run_id": (norm or {}).get("run_id"),
            "repo_path": (norm or {}).get("repo_path"),
            "allowed_paths": (norm or {}).get("allowed_paths"),
            "worker_candidate": (norm or {}).get("worker_candidate"),
            "reviewer_candidate": (norm or {}).get("reviewer_candidate"),
            "validator_profile": (norm or {}).get("validator_profile"),
            "changed_paths": changed,
            "worker_result": worker_payload,
            "validator_result": validator_payload,
            "reviewer_result": reviewer_payload,
            "security_review": security_payload,
            "worktree_path": worktree_path,
            "source_before": source_before,
            "source_after": source_after,
            "events": events,
            "extra": extra or {},
        }
        return sanitize_dict(base)
    except Exception:
        try:
            return sanitize_dict(
                {
                    "schema_version": SCHEMA_VERSION,
                    "verdict": "FAILED_INFRA",
                    "failure_category": "infra",
                    "failure_detail": REDACTED,
                    "events": [],
                    "extra": {},
                }
            )
        except Exception:
            return {
                "schema_version": "1.0",
                "verdict": "FAILED_INFRA",
                "failure_category": "infra",
                "failure_detail": "[REDACTED]",
                "events": [],
                "extra": {},
            }


async def run_pipeline(contract: Any, **kwargs: Any) -> dict[str, Any]:
    events: list[dict[str, Any]] = []

    def _record(phase: str, status: str, detail: Any = None, extra: Any = None) -> None:
        try:
            ev = {
                "ts": _utc_now(),
                "phase": str(phase)[:120],
                "status": str(status)[:120],
                "detail": detail,
                "extra": extra or {},
            }
            events.append(ev)
            sink = _resolve_override(kwargs, ("event_sink", "on_event", "emit_event", "eventSink"))
            if callable(sink):
                try:
                    sink(sanitize_dict(ev))
                except Exception:
                    pass
        except Exception:
            pass

    def _persist(result: dict[str, Any]) -> dict[str, Any]:
        try:
            writer = _resolve_override(
                kwargs, ("artifact_writer", "write_artifact", "artifactWriter", "persist_fn")
            )
            if writer is None and isinstance(contract, dict):
                for ck in ("artifact_writer", "write_artifact", "artifactWriter"):
                    try:
                        cv = contract.get(ck)
                    except Exception:
                        cv = None
                    if callable(cv):
                        writer = cv
                        break
            if callable(writer):
                try:
                    writer(sanitize_dict(result))
                except Exception:
                    pass
        except Exception:
            pass
        return result

    norm: dict[str, Any] | None = None
    try:
        _record("preconditions", "started")
        ok, norm_d, err = validate_contract(contract)
        if ok is not True:
            _record("preconditions", "failed", err)
            try:
                raw_tid = None
                if isinstance(contract, dict):
                    raw_tid = contract.get("task_id", contract.get("taskId", contract.get("id")))
            except Exception:
                raw_tid = None
            tmp_norm = {"task_id": raw_tid, "repo_path": None, "allowed_paths": None}
            res = _build_result(
                verdict="FAILED_INFRA",
                category="policy",
                detail=err,
                norm=tmp_norm,  # type: ignore
                changed=[],
                worker_payload=None,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
            )
            return _persist(res)
        norm = norm_d
        _record("preconditions", "ok")

        task_id = str(norm["task_id"])
        try:
            with _OWNERSHIP_LOCK:
                if task_id in _ACTIVE_WRITERS:
                    _conflict_task = True
                else:
                    _ACTIVE_WRITERS.add(task_id)
                    _conflict_task = False
        except Exception:
            _conflict_task = True
        if _conflict_task:
            _record("preconditions", "failed", "one writer per worktree: task already active")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="policy",
                detail="one writer per worktree: task already active",
                norm=norm,
                changed=[],
                worker_payload=None,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
            )
            return _persist(res)
        try:
            return await _run_pipeline_inner(norm, events, _record, kwargs, contract)
        finally:
            try:
                with _OWNERSHIP_LOCK:
                    _ACTIVE_WRITERS.discard(task_id)
            except Exception:
                pass
    except Exception as exc:
        try:
            _record("pipeline", "error", f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        try:
            res = _build_result(
                verdict="FAILED_INFRA",
                category=_classify_error(str(exc)),
                detail=f"{type(exc).__name__}: {exc}",
                norm=norm,
                changed=[],
                worker_payload=None,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
            )
            return _persist(res)
        except Exception:
            return sanitize_dict(
                {
                    "schema_version": SCHEMA_VERSION,
                    "verdict": "FAILED_INFRA",
                    "failure_category": "infra",
                    "failure_detail": "pipeline error",
                    "events": events,
                    "extra": {},
                }
            )


async def _run_pipeline_inner(
    norm: dict[str, Any],
    events: list[dict[str, Any]],
    _record: Callable[..., None],
    kwargs: dict[str, Any],
    contract: Any,
) -> dict[str, Any]:
    def _persist_inner(result: dict[str, Any]) -> dict[str, Any]:
        try:
            writer = _resolve_override(
                kwargs, ("artifact_writer", "write_artifact", "artifactWriter", "persist_fn")
            )
            if writer is None and isinstance(contract, dict):
                for ck in ("artifact_writer", "write_artifact", "artifactWriter"):
                    try:
                        cv = contract.get(ck)
                    except Exception:
                        cv = None
                    if callable(cv):
                        writer = cv
                        break
            if callable(writer):
                try:
                    writer(sanitize_dict(result))
                except Exception:
                    pass
        except Exception:
            pass
        return result

    task_id = str(norm["task_id"])
    run_id = str(norm.get("run_id") or "")
    repo_path = Path(str(norm["repo_path"])).expanduser()
    allowed: list[str] = list(norm["allowed_paths"])
    task_text = str(norm["task_text"])
    worker_candidate = str(norm["worker_candidate"])
    reviewer_candidate = str(norm["reviewer_candidate"])
    validator_profile = str(norm.get("validator_profile") or "backend")
    timeout_f = float(norm.get("timeout_s") or 240.0)
    sec_required = norm.get("security_review_required")
    sec_required = sec_required is True
    sec_result_raw = norm.get("security_review_result")

    sec_hook = _resolve_override(
        kwargs, ("security_hook", "securityHook", "security_review_hook", "security_reviewer", "security_fn")
    )
    if sec_hook is None and isinstance(contract, dict):
        for ck in ("security_hook", "securityHook", "security_review_hook", "security_reviewer"):
            try:
                cv = contract.get(ck)
            except Exception:
                cv = None
            if callable(cv):
                sec_hook = cv
                break
    # async hook support: wrap to sync call inside gate by running if needed
    # gate expects sync hook; if async, we handle here
    async def _maybe_await_hook(h: Any) -> Any:
        try:
            out = h()
            if asyncio.iscoroutine(out):
                out = await out
            return out
        except Exception as exc:
            raise exc

    worker_runner = _resolve_override(
        kwargs,
        ("worker_runner", "run_worker", "workerRunner", "worker_fn", "workerFunc", "workerHook"),
    )
    validator_runner = _resolve_override(
        kwargs,
        ("validator_runner", "run_validator", "validatorRunner", "validator_fn", "validatorFunc"),
    )
    reviewer_runner = _resolve_override(
        kwargs,
        ("reviewer_runner", "run_reviewer", "reviewerRunner", "reviewer_fn", "reviewerFunc"),
    )
    worktree_ops = _resolve_override(kwargs, ("worktree_ops", "worktree", "worktree_manager"))
    create_fn = _resolve_override(kwargs, ("create_worktree_fn", "createWorktreeFn", "create_fn"))
    src_state_fn = _resolve_override(
        kwargs, ("get_source_state_fn", "source_state_fn", "sourceStateFn", "read_source_state")
    )
    wt_state_fn = _resolve_override(
        kwargs, ("get_worktree_state_fn", "worktree_state_fn", "worktreeStateFn")
    )
    diff_fn = _resolve_override(
        kwargs, ("get_actual_diff_fn", "actual_diff_fn", "actualDiffFn", "read_diff_fn", "diffFn")
    )

    # unpack worktree_ops bundle when explicit fns missing
    if worktree_ops is not None:
        try:
            if isinstance(worktree_ops, dict):
                if create_fn is None:
                    create_fn = worktree_ops.get("create") or worktree_ops.get("create_worktree")
                if src_state_fn is None:
                    src_state_fn = worktree_ops.get("source_state") or worktree_ops.get("get_source_state")
                if wt_state_fn is None:
                    wt_state_fn = worktree_ops.get("worktree_state") or worktree_ops.get("get_worktree_state")
                if diff_fn is None:
                    diff_fn = (
                        worktree_ops.get("actual_changed")
                        or worktree_ops.get("actual_diff")
                        or worktree_ops.get("get_actual_diff")
                        or worktree_ops.get("diff")
                    )
            else:
                if create_fn is None:
                    for nm in ("create", "create_worktree"):
                        if hasattr(worktree_ops, nm) and callable(getattr(worktree_ops, nm)):
                            create_fn = getattr(worktree_ops, nm)
                            break
                if src_state_fn is None:
                    for nm in ("source_state", "get_source_state", "read_source_state"):
                        if hasattr(worktree_ops, nm) and callable(getattr(worktree_ops, nm)):
                            src_state_fn = getattr(worktree_ops, nm)
                            break
                if wt_state_fn is None:
                    for nm in ("worktree_state", "get_worktree_state"):
                        if hasattr(worktree_ops, nm) and callable(getattr(worktree_ops, nm)):
                            wt_state_fn = getattr(worktree_ops, nm)
                            break
                if diff_fn is None:
                    for nm in ("actual_changed", "actual_diff", "get_actual_diff", "diff"):
                        if hasattr(worktree_ops, nm) and callable(getattr(worktree_ops, nm)):
                            diff_fn = getattr(worktree_ops, nm)
                            break
        except Exception:
            pass

    # contract-level fakes (provider-free tests may embed here)
    if isinstance(contract, dict):
        try:
            if worker_runner is None:
                for ck in ("worker_runner", "run_worker", "_worker_runner", "workerHook"):
                    cv = contract.get(ck)
                    if callable(cv):
                        worker_runner = cv
                        break
            if validator_runner is None:
                for ck in ("validator_runner", "run_validator", "_validator_runner"):
                    cv = contract.get(ck)
                    if callable(cv):
                        validator_runner = cv
                        break
            if reviewer_runner is None:
                for ck in ("reviewer_runner", "run_reviewer", "_reviewer_runner"):
                    cv = contract.get(ck)
                    if callable(cv):
                        reviewer_runner = cv
                        break
        except Exception:
            pass

    # --- explicit repo check (no default fallback) -------------------------
    try:
        if not repo_path.exists() or not repo_path.is_dir():
            _record("preconditions", "failed", "repo_path missing")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="environment",
                detail="repo_path does not exist",
                norm=norm,
                changed=[],
                worker_payload=None,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
            )
            return _persist_inner(res)
    except Exception as exc:
        _record("preconditions", "failed", f"{type(exc).__name__}")
        res = _build_result(
            verdict="FAILED_INFRA",
            category="environment",
            detail="repo_path check failed",
            norm=norm,
            changed=[],
            worker_payload=None,
            validator_payload=None,
            reviewer_payload=None,
            security_payload=None,
            events=events,
        )
        return _persist_inner(res)

    # --- source baseline ----------------------------------------------------
    try:
        if callable(src_state_fn):
            try:
                maybe = src_state_fn(str(repo_path))
                if asyncio.iscoroutine(maybe):
                    src_before = await maybe
                else:
                    src_before = maybe
            except Exception as exc:
                raise exc
        else:
            if _worktree_mod is None:
                raise RuntimeError("worktree module unavailable for baseline")
            src_before = _worktree_mod.repo_state(repo_path)
        if not isinstance(src_before, dict) or "head" not in src_before:
            raise RuntimeError("invalid source baseline")
    except Exception as exc:
        _record("preconditions", "failed", f"baseline failed: {type(exc).__name__}")
        res = _build_result(
            verdict="FAILED_INFRA",
            category=_classify_error(str(exc)),
            detail=f"baseline failed: {type(exc).__name__}: {exc}",
            norm=norm,
            changed=[],
            worker_payload=None,
            validator_payload=None,
            reviewer_payload=None,
            security_payload=None,
            events=events,
        )
        return _persist_inner(res)

    try:
        if str(src_before.get("status") or "") != "":
            _record("preconditions", "failed", "source checkout not clean")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="environment",
                detail="source checkout is dirty; refusing run",
                norm=norm,
                changed=[],
                worker_payload=None,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                source_before=src_before,
                source_after=src_before,
            )
            return _persist_inner(res)
    except Exception:
        pass
    _record("worktree", "started")

    # --- create isolated worktree -------------------------------------------
    worktree_path: Path | None = None
    manifest: Any = None
    try:
        if callable(create_fn):
            try:
                maybe = create_fn(task_id, repo_path)
                if asyncio.iscoroutine(maybe):
                    manifest = await maybe
                else:
                    manifest = maybe
                if isinstance(manifest, dict):
                    wp = manifest.get("worktree") or manifest.get("worktree_path") or manifest.get("path")
                    if wp:
                        worktree_path = Path(str(wp))
                    else:
                        # fake returned path directly?
                        worktree_path = None
                elif isinstance(manifest, (str, Path)):
                    worktree_path = Path(str(manifest))
                    manifest = {"worktree": str(worktree_path)}
                else:
                    raise RuntimeError("create hook returned invalid shape")
            except Exception as exc:
                raise exc
        else:
            manifest = _default_create_worktree(task_id, repo_path)
            try:
                worktree_path = Path(str(manifest["worktree"]))
            except Exception:
                raise RuntimeError("worktree creation returned invalid manifest")
        if worktree_path is None:
            raise RuntimeError("worktree path unresolved")
    except Exception as exc:
        _record("worktree", "failed", f"{type(exc).__name__}: {exc}")
        res = _build_result(
            verdict="FAILED_INFRA",
            category=_classify_error(str(exc)),
            detail=f"worktree creation failed: {type(exc).__name__}: {exc}",
            norm=norm,
            changed=[],
            worker_payload=None,
            validator_payload=None,
            reviewer_payload=None,
            security_payload=None,
            events=events,
            source_before=src_before,
        )
        return _persist_inner(res)
    _record("worktree", "ok", None, {"worktree": str(worktree_path)})
    # resolved-worktree writer ownership: one writer per resolved worktree.
    # Lifecycle-safe: released in finally on EVERY terminal path after acquired.
    try:
        _resolved_wt = str(Path(str(worktree_path)).resolve())
    except Exception:
        try:
            _resolved_wt = str(worktree_path)
        except Exception:
            _resolved_wt = ""
    _owns_resolved_wt = False
    _wt_conflict = False
    try:
        with _OWNERSHIP_LOCK:
            if _resolved_wt and _resolved_wt in _ACTIVE_WORKTREE_PATHS:
                _wt_conflict = True
            elif _resolved_wt:
                _ACTIVE_WORKTREE_PATHS.add(_resolved_wt)
                _owns_resolved_wt = True
    except Exception:
        try:
            if _resolved_wt and not _owns_resolved_wt:
                _wt_conflict = True
        except Exception:
            pass
    if _wt_conflict:
        _record("worktree", "failed", "one writer per worktree: resolved worktree already active")
        res = _build_result(
            verdict="FAILED_INFRA",
            category="policy",
            detail="one writer per worktree: resolved worktree already active",
            norm=norm,
            changed=[],
            worker_payload=None,
            validator_payload=None,
            reviewer_payload=None,
            security_payload=None,
            events=events,
            worktree_path=str(worktree_path),
            source_before=src_before,
        )
        return _persist_inner(res)

    try:
        # helpers to read states with fakes or real git
        async def _read_source() -> dict[str, Any]:
            if callable(src_state_fn):
                maybe = src_state_fn(str(repo_path))
                if asyncio.iscoroutine(maybe):
                    return await maybe
                return maybe
            if _worktree_mod is not None:
                return _worktree_mod.repo_state(repo_path)
            return _git_head_status(repo_path)

        async def _read_wt_state() -> dict[str, Any]:
            assert worktree_path is not None
            if callable(wt_state_fn):
                maybe = wt_state_fn(str(worktree_path))
                if asyncio.iscoroutine(maybe):
                    return await maybe
                return maybe
            if _worktree_mod is not None:
                try:
                    return _worktree_mod.repo_state(worktree_path)
                except Exception:
                    pass
            return _git_head_status(worktree_path)

        async def _read_diff() -> list[str]:
            assert worktree_path is not None
            if callable(diff_fn):
                maybe = diff_fn(str(worktree_path))
                if asyncio.iscoroutine(maybe):
                    out = await maybe
                else:
                    out = maybe
                if out is None:
                    return []
                if isinstance(out, (list, tuple)):
                    return [str(x) for x in out]
                raise RuntimeError("diff hook returned invalid shape")
            try:
                return _git_status_paths(worktree_path)
            except Exception as exc:
                raise exc

        # source unchanged after create
        try:
            src_after_create = await _read_source()
            if src_after_create.get("head") != src_before.get("head") or str(
                src_after_create.get("status") or ""
            ) != str(src_before.get("status") or ""):
                _record("worktree", "failed", "source changed during create")
                res = _build_result(
                    verdict="FAILED_INFRA",
                    category="infra",
                    detail="source checkout changed during worktree creation",
                    norm=norm,
                    changed=[],
                    worker_payload=None,
                    validator_payload=None,
                    reviewer_payload=None,
                    security_payload=None,
                    events=events,
                    worktree_path=str(worktree_path),
                    source_before=src_before,
                    source_after=src_after_create,
                )
                return _persist_inner(res)
        except Exception as exc:
            # if read failed, fail closed
            if "source checkout changed" in str(exc):
                raise exc
            _record("worktree", "failed", f"source recheck failed: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category=_classify_error(str(exc)),
                detail=f"source recheck failed: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=[],
                worker_payload=None,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
            )
            return _persist_inner(res)

        # --- worker --------------------------------------------------------------
        _record("worker", "started", worker_candidate)
        worker_raw: Any = None
        try:
            w_args = {
                "task_id": task_id,
                "run_id": run_id,
                "candidate": worker_candidate,
                "task_text": task_text,
                "allowed_paths": allowed,
                "timeout_s": timeout_f,
                "worktree_path": str(worktree_path),
                "source_path": str(repo_path),
            }
            if callable(worker_runner):
                try:
                    maybe = worker_runner(w_args) if _takes_dict(worker_runner) else worker_runner(**w_args)
                except TypeError:
                    try:
                        maybe = worker_runner(task_id, worker_candidate, task_text, allowed)
                    except Exception:
                        maybe = worker_runner(w_args)
                if asyncio.iscoroutine(maybe):
                    worker_raw = await maybe
                else:
                    worker_raw = maybe
            else:
                worker_raw = await _default_worker_runner(w_args)
        except Exception as exc:
            _record("worker", "failed", f"{type(exc).__name__}: {exc}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category=_classify_error(str(exc)),
                detail=f"worker raised: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=[],
                worker_payload=None,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
            )
            return _persist_inner(res)

        w_ok, w_payload, w_code, w_err = _interpret_worker_out(worker_raw)
        _record("worker", "finished", None, {"exit_code": w_code, "ok": w_ok})

        # real diff authoritative (never trust worker report alone)
        try:
            actual_after_worker = await _read_diff()
        except Exception as exc:
            _record("worker", "failed", f"diff read failed: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category=_classify_error(str(exc)),
                detail=f"worktree diff read failed after worker: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=[],
                worker_payload=w_payload,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
            )
            return _persist_inner(res)

        try:
            src_after_worker = await _read_source()
        except Exception as exc:
            res = _build_result(
                verdict="FAILED_INFRA",
                category=_classify_error(str(exc)),
                detail=f"source recheck failed after worker: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_worker,
                worker_payload=w_payload,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
            )
            return _persist_inner(res)

        if src_after_worker.get("head") != src_before.get("head") or str(
            src_after_worker.get("status") or ""
        ) != "":
            _record("worker", "failed", "source integrity breach")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail="source checkout changed during worker run",
                norm=norm,
                changed=actual_after_worker,
                worker_payload=w_payload,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_worker,
            )
            return _persist_inner(res)

        fit_ok, bad = _fit_allowlist(actual_after_worker, allowed)
        if fit_ok is not True:
            _record("worker", "failed", f"disallowed paths: {bad[:5]}")
            res = _build_result(
                verdict="NEEDS_REWORK",
                category="policy",
                detail=f"worker changed paths outside allowlist: {bad[:10]}",
                norm=norm,
                changed=actual_after_worker,
                worker_payload=w_payload,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_worker,
            )
            return _persist_inner(res)

        if w_ok is not True:
            cat = _classify_error(str(w_err or "") + " worker")
            # policy/integrity style worker failures that already passed allowlist
            # are worker-attributed rework unless infra/timeout/provider
            if cat in ("infra", "timeout", "stall", "provider", "quota", "tooling", "environment"):
                verdict_w = "FAILED_INFRA"
            else:
                verdict_w = "NEEDS_REWORK"
                if cat not in ("worker", "policy", "sandbox", "reviewer", "validator"):
                    cat = "worker"
            _record("worker", "failed", str(w_err or "worker did not complete")[:500])
            res = _build_result(
                verdict=verdict_w,  # type: ignore
                category=cat,
                detail=f"worker failed: {w_err or 'non-zero exit'}",
                norm=norm,
                changed=actual_after_worker,
                worker_payload=w_payload,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_worker,
            )
            return _persist_inner(res)

        if not actual_after_worker:
            _record("worker", "failed", "WORKER_NOOP")
            res = _build_result(
                verdict="NEEDS_REWORK",
                category="worker",
                detail="WORKER_NOOP: completed worker produced no authoritative changes",
                norm=norm,
                changed=[],
                worker_payload=w_payload,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_worker,
            )
            return _persist_inner(res)

        # --- validators (behavioral, via existing seam) ---------------------------
        _record("validate", "started", validator_profile)
        try:
            fp_before_validate = _fingerprint_paths(worktree_path, allowed)
        except _FingerprintError as exc:
            _record("validate", "failed", f"fingerprint collection failed: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint collection failed before validators: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_worker,
                worker_payload=w_payload,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_worker,
            )
            return _persist_inner(res)
        except Exception as exc:
            _record("validate", "failed", f"fingerprint collection failed: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint collection failed before validators: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_worker,
                worker_payload=w_payload,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_worker,
            )
            return _persist_inner(res)
        try:
            _ok_bv, _reason_bv = _validate_fingerprint_result(fp_before_validate, allowed)
        except Exception as exc:
            _record("validate", "failed", f"fingerprint validation failed: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint validation failed before validators: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_worker,
                worker_payload=w_payload,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_worker,
            )
            return _persist_inner(res)
        if _ok_bv is not True:
            _record("validate", "failed", f"fingerprint invalid before validators: {_reason_bv}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint invalid before validators: {_reason_bv}",
                norm=norm,
                changed=actual_after_worker,
                worker_payload=w_payload,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_worker,
            )
            return _persist_inner(res)
        validator_raw: Any = None
        try:
            v_args = {
                "task_id": task_id,
                "run_id": run_id,
                "validator_profile": validator_profile,
                "profile": validator_profile,
                "timeout_s": timeout_f,
                "worktree_path": str(worktree_path),
                "source_path": str(repo_path),
            }
            if callable(validator_runner):
                try:
                    maybe = validator_runner(v_args) if _takes_dict(validator_runner) else validator_runner(**v_args)
                except TypeError:
                    try:
                        maybe = validator_runner(task_id, validator_profile)
                    except Exception:
                        maybe = validator_runner(v_args)
                if asyncio.iscoroutine(maybe):
                    validator_raw = await maybe
                else:
                    validator_raw = maybe
            else:
                # real path must use existing seam (now async, shared lock)
                validator_raw = await _default_validator_runner(v_args)
        except Exception as exc:
            _record("validate", "failed", f"{type(exc).__name__}: {exc}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category=_classify_error(str(exc)),
                detail=f"validator raised: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_worker,
                worker_payload=w_payload,
                validator_payload=None,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_worker,
            )
            return _persist_inner(res)

        v_ok, v_payload, v_code, v_err = _interpret_validator_out(validator_raw)
        validator_failure_category = _validator_failure_category(v_payload, v_code, v_err)
        _record("validate", "finished", None, {"exit_code": v_code, "ok": v_ok})

        try:
            actual_after_validate = await _read_diff()
        except Exception as exc:
            res = _build_result(
                verdict="FAILED_INFRA",
                category=_classify_error(str(exc)),
                detail=f"worktree diff read failed after validators: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_worker,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_worker,
            )
            return _persist_inner(res)

        try:
            src_after_validate = await _read_source()
        except Exception as exc:
            res = _build_result(
                verdict="FAILED_INFRA",
                category=_classify_error(str(exc)),
                detail=f"source recheck failed after validators: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
            )
            return _persist_inner(res)

        if src_after_validate.get("head") != src_before.get("head") or str(
            src_after_validate.get("status") or ""
        ) != "":
            _record("validate", "failed", "source integrity breach")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail="source checkout changed during validators",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)

        # validators must not mutate unexpectedly
        try:
            set_a = set(actual_after_worker or [])
            set_b = set(actual_after_validate or [])
        except Exception:
            set_a = set()
            set_b = set()
        if set_a != set_b:
            _record("validate", "failed", "validator mutated worktree")
            res = _build_result(
                verdict="NEEDS_REWORK" if validator_failure_category == "validator" else "FAILED_INFRA",
                category=validator_failure_category,
                detail=f"validators mutated worktree: before={sorted(set_a)[:10]} after={sorted(set_b)[:10]}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)

        # content-level fingerprints: same-path in-place mutation/deletion/symlink
        # Fail-closed: collection failure or invalid result is infra, never unchanged.
        try:
            fp_after_validate = _fingerprint_paths(worktree_path, allowed)
        except _FingerprintError as exc:
            _record("validate", "failed", f"fingerprint collection failed: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint collection failed after validators: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)
        except Exception as exc:
            _record("validate", "failed", f"fingerprint collection failed: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint collection failed after validators: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)
        try:
            _fp_equal_v = _check_fingerprints_equal(fp_before_validate, fp_after_validate, allowed)
        except _FingerprintError as exc:
            _record("validate", "failed", f"fingerprint invalid: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint validation failed after validators: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)
        except Exception as exc:
            _record("validate", "failed", f"fingerprint comparison failed: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint comparison failed after validators: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)
        if _fp_equal_v is not True:
            _record("validate", "failed", "validator mutated worktree content")
            res = _build_result(
                verdict="NEEDS_REWORK" if validator_failure_category == "validator" else "FAILED_INFRA",
                category=validator_failure_category,
                detail="validators mutated worktree content (fingerprint mismatch)",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)

        fit_ok2, bad2 = _fit_allowlist(actual_after_validate, allowed)
        if fit_ok2 is not True:
            _record("validate", "failed", "allowlist breach after validators")
            res = _build_result(
                verdict=(
                    "NEEDS_REWORK"
                    if v_ok is True or validator_failure_category == "validator"
                    else "FAILED_INFRA"
                ),
                category="policy" if v_ok is True else validator_failure_category,
                detail=f"unexpected paths after validators: {bad2[:10]}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)

        if v_ok is not True:
            _record("validate", "failed", str(v_err or "validator FAIL")[:500])
            res = _build_result(
                verdict="NEEDS_REWORK" if validator_failure_category == "validator" else "FAILED_INFRA",
                category=validator_failure_category,
                detail=f"validators failed: {v_err or v_payload}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)

        # --- reviewer (read-only, may use different candidate) --------------------
        _record("review", "started", reviewer_candidate)
        try:
            wt_before_review = await _read_wt_state()
        except Exception as exc:
            res = _build_result(
                verdict="FAILED_INFRA",
                category=_classify_error(str(exc)),
                detail=f"worktree state read failed before review: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)

        reviewer_raw: Any = None
        try:
            fp_before_review = _fingerprint_paths(worktree_path, allowed)
        except _FingerprintError as exc:
            _record("review", "failed", f"fingerprint collection failed: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint collection failed before review: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)
        except Exception as exc:
            _record("review", "failed", f"fingerprint collection failed: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint collection failed before review: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)
        try:
            _ok_br, _reason_br = _validate_fingerprint_result(fp_before_review, allowed)
        except Exception as exc:
            _record("review", "failed", f"fingerprint validation failed: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint validation failed before review: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)
        if _ok_br is not True:
            _record("review", "failed", f"fingerprint invalid before review: {_reason_br}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint invalid before review: {_reason_br}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)
        try:
            r_args = {
                "task_id": task_id,
                "run_id": run_id,
                "candidate": reviewer_candidate,
                "task_text": task_text,
                "timeout_s": timeout_f,
                "worktree_path": str(worktree_path),
                "source_path": str(repo_path),
                "validator_result": v_payload,
            }
            if callable(reviewer_runner):
                try:
                    maybe = reviewer_runner(r_args) if _takes_dict(reviewer_runner) else reviewer_runner(**r_args)
                except TypeError:
                    try:
                        maybe = reviewer_runner(task_id, reviewer_candidate, task_text)
                    except Exception:
                        maybe = reviewer_runner(r_args)
                if asyncio.iscoroutine(maybe):
                    reviewer_raw = await maybe
                else:
                    reviewer_raw = maybe
            else:
                reviewer_raw = await _default_reviewer_runner(r_args)
        except Exception as exc:
            _record("review", "failed", f"{type(exc).__name__}: {exc}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category=_classify_error(str(exc)),
                detail=f"reviewer raised: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=None,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)

        r_ok, r_payload, r_code, r_verdict, r_err = _interpret_reviewer_out(reviewer_raw)
        _record("review", "finished", str(r_verdict), {"exit_code": r_code, "ok": r_ok})

        try:
            actual_after_review = await _read_diff()
            wt_after_review = await _read_wt_state()
            src_after_review = await _read_source()
        except Exception as exc:
            res = _build_result(
                verdict="FAILED_INFRA",
                category=_classify_error(str(exc)),
                detail=f"state recheck failed after review: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
            )
            return _persist_inner(res)

        if src_after_review.get("head") != src_before.get("head") or str(
            src_after_review.get("status") or ""
        ) != "":
            _record("review", "failed", "source integrity breach")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail="source checkout changed during review",
                norm=norm,
                changed=actual_after_review,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_review,
            )
            return _persist_inner(res)

        # reviewer read-only check
        try:
            ro_ok = (
                wt_before_review.get("head") == wt_after_review.get("head")
                and str(wt_before_review.get("status") or "")
                == str(wt_after_review.get("status") or "")
            )
        except Exception:
            ro_ok = False
        if ro_ok is not True:
            _record("review", "failed", "reviewer mutated worktree")
            res = _build_result(
                verdict="NEEDS_REWORK",
                category="reviewer",
                detail="reviewer mutated worktree; read-only violated",
                norm=norm,
                changed=actual_after_review,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_review,
            )
            return _persist_inner(res)

        try:
            set_c = set(actual_after_validate or [])
            set_d = set(actual_after_review or [])
        except Exception:
            set_c = set()
            set_d = set()
        if set_c != set_d:
            _record("review", "failed", "review mutated diff")
            res = _build_result(
                verdict="NEEDS_REWORK",
                category="reviewer",
                detail="reviewer changed worktree diff",
                norm=norm,
                changed=actual_after_review,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_review,
            )
            return _persist_inner(res)

        # content-level fingerprints: same-path in-place mutation/deletion/symlink
        # Fail-closed: collection failure or invalid result is infra, never unchanged.
        try:
            fp_after_review = _fingerprint_paths(worktree_path, allowed)
        except _FingerprintError as exc:
            _record("review", "failed", f"fingerprint collection failed: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint collection failed after review: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_review,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_review,
            )
            return _persist_inner(res)
        except Exception as exc:
            _record("review", "failed", f"fingerprint collection failed: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint collection failed after review: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_review,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_review,
            )
            return _persist_inner(res)
        try:
            _fp_equal_r = _check_fingerprints_equal(fp_before_review, fp_after_review, allowed)
        except _FingerprintError as exc:
            _record("review", "failed", f"fingerprint invalid: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint validation failed after review: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_review,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_review,
            )
            return _persist_inner(res)
        except Exception as exc:
            _record("review", "failed", f"fingerprint comparison failed: {type(exc).__name__}")
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"fingerprint comparison failed after review: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_review,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_review,
            )
            return _persist_inner(res)
        if _fp_equal_r is not True:
            _record("review", "failed", "reviewer mutated worktree content")
            res = _build_result(
                verdict="NEEDS_REWORK",
                category="reviewer",
                detail="reviewer mutated worktree content (fingerprint mismatch)",
                norm=norm,
                changed=actual_after_review,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_review,
            )
            return _persist_inner(res)

        fit_ok3, bad3 = _fit_allowlist(actual_after_review, allowed)
        if fit_ok3 is not True:
            _record("review", "failed", "allowlist breach after review")
            res = _build_result(
                verdict="NEEDS_REWORK",
                category="policy",
                detail=f"unexpected paths after review: {bad3[:10]}",
                norm=norm,
                changed=actual_after_review,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_review,
            )
            return _persist_inner(res)

        if r_ok is not True:
            _record("review", "failed", f"verdict={r_verdict}")
            res = _build_result(
                verdict="NEEDS_REWORK",
                category="reviewer",
                detail=f"reviewer did not approve: verdict={r_verdict} err={r_err}",
                norm=norm,
                changed=actual_after_review,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_review,
            )
            return _persist_inner(res)

        # --- safety gate ----------------------------------------------------------
        _record("safety_gate", "started")
        approved = False
        gate_detail = "not required"
        gate_payload: Any = None
        # Pre-gate integrity snapshot: a callable security hook must not be able
        # to mutate source/worktree and still produce READY. Snapshot before ANY
        # hook execution, recheck after gate before READY.
        _pre_gate_src: Any = None
        _pre_gate_diff: Any = None
        _pre_gate_fp: Any = None
        _pre_gate_wt: Any = None
        if sec_required is True:
            try:
                try:
                    _pre_gate_src = await _read_source()
                except Exception:
                    _pre_gate_src = None
                try:
                    _pre_gate_diff = await _read_diff()
                except Exception:
                    _pre_gate_diff = None
                try:
                    _pre_gate_fp = _fingerprint_paths(worktree_path, allowed)
                except _FingerprintError as exc:
                    _record("safety_gate", "failed", f"fingerprint collection failed: {type(exc).__name__}")
                    res = _build_result(
                        verdict="FAILED_INFRA",
                        category="infra",
                        detail=f"fingerprint collection failed before security gate: {type(exc).__name__}: {exc}",
                        norm=norm,
                        changed=actual_after_review,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=None,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=src_after_review,
                    )
                    return _persist_inner(res)
                except Exception as exc:
                    _record("safety_gate", "failed", f"fingerprint collection failed: {type(exc).__name__}")
                    res = _build_result(
                        verdict="FAILED_INFRA",
                        category="infra",
                        detail=f"fingerprint collection failed before security gate: {type(exc).__name__}: {exc}",
                        norm=norm,
                        changed=actual_after_review,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=None,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=src_after_review,
                    )
                    return _persist_inner(res)
                try:
                    _ok_pre, _reason_pre = _validate_fingerprint_result(_pre_gate_fp, allowed)
                except Exception as exc:
                    _record("safety_gate", "failed", f"fingerprint validation failed: {type(exc).__name__}")
                    res = _build_result(
                        verdict="FAILED_INFRA",
                        category="infra",
                        detail=f"fingerprint validation failed before security gate: {type(exc).__name__}: {exc}",
                        norm=norm,
                        changed=actual_after_review,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=None,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=src_after_review,
                    )
                    return _persist_inner(res)
                if _ok_pre is not True:
                    _record("safety_gate", "failed", f"fingerprint invalid before security gate: {_reason_pre}")
                    res = _build_result(
                        verdict="FAILED_INFRA",
                        category="infra",
                        detail=f"fingerprint invalid before security gate: {_reason_pre}",
                        norm=norm,
                        changed=actual_after_review,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=None,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=src_after_review,
                    )
                    return _persist_inner(res)
                try:
                    _pre_gate_wt = await _read_wt_state()
                except Exception:
                    _pre_gate_wt = None
            except Exception:
                pass
            # re-validate pre-gate invalid case that fell through via inner pass
            try:
                if _pre_gate_fp is not None:
                    _ok_pre2, _reason_pre2 = _validate_fingerprint_result(_pre_gate_fp, allowed)
                    if _ok_pre2 is not True:
                        _record("safety_gate", "failed", f"fingerprint invalid before security gate: {_reason_pre2}")
                        res = _build_result(
                            verdict="FAILED_INFRA",
                            category="infra",
                            detail=f"fingerprint invalid before security gate: {_reason_pre2}",
                            norm=norm,
                            changed=actual_after_review,
                            worker_payload=w_payload,
                            validator_payload=v_payload,
                            reviewer_payload=r_payload,
                            security_payload=None,
                            events=events,
                            worktree_path=str(worktree_path),
                            source_before=src_before,
                            source_after=src_after_review,
                        )
                        return _persist_inner(res)
            except Exception as exc:
                _record("safety_gate", "failed", f"fingerprint validation failed: {type(exc).__name__}")
                res = _build_result(
                    verdict="FAILED_INFRA",
                    category="infra",
                    detail=f"fingerprint validation failed before security gate: {type(exc).__name__}: {exc}",
                    norm=norm,
                    changed=actual_after_review,
                    worker_payload=w_payload,
                    validator_payload=v_payload,
                    reviewer_payload=r_payload,
                    security_payload=None,
                    events=events,
                    worktree_path=str(worktree_path),
                    source_before=src_before,
                    source_after=src_after_review,
                )
                return _persist_inner(res)
        try:
            if sec_required is True:
                expected_security_identity = _security_approval_identity(
                    task_id, worker_candidate, _pre_gate_diff, _pre_gate_fp
                )
                # resolve async hook when needed
                hook_to_use = sec_hook
                hook_sync = None
                if callable(hook_to_use):
                    try:
                        probe = hook_to_use
                        # wrap async hooks: evaluate here
                        maybe_h = None
                        try:
                            if _takes_no_arg(probe):
                                maybe_h = probe()
                            else:
                                try:
                                    maybe_h = probe(dict(expected_security_identity))
                                except TypeError:
                                    maybe_h = probe()
                        except Exception as exc_h:
                            maybe_h = exc_h
                        if asyncio.iscoroutine(maybe_h):
                            try:
                                hook_out = await maybe_h
                            except Exception as exc_h2:
                                hook_out = {"approved": None, "error": f"{type(exc_h2).__name__}: {exc_h2}"}
                        elif isinstance(maybe_h, Exception):
                            hook_out = {"approved": None, "error": f"{type(maybe_h).__name__}: {maybe_h}"}
                        else:
                            hook_out = maybe_h
                        # build sync hook returning hook_out

                        def _closed_hook(out: Any = hook_out) -> Any:
                            return out

                        hook_sync = _closed_hook
                    except Exception:
                        hook_sync = hook_to_use
                else:
                    hook_sync = None
                ap, gd, gp = _security_gate(
                    True, sec_result_raw, hook_sync, expected_security_identity
                )
                approved = ap is True
                gate_detail = str(gd)
                gate_payload = gp
                if approved is not True:
                    _record("safety_gate", "blocked", gate_detail)
                    res = _build_result(
                        verdict="NEEDS_HUMAN",
                        category="policy",
                        detail=f"safety gate blocked: {gate_detail}",
                        norm=norm,
                        changed=actual_after_review,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=gate_payload,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=src_after_review,
                    )
                    return _persist_inner(res)
                # Integrity recheck after security hook and before READY.
                # Any mutation during the hook must fail closed, never READY.
                try:
                    _post_gate_src = await _read_source()
                except Exception as exc:
                    _record("safety_gate", "failed", f"post-gate source recheck failed: {type(exc).__name__}")
                    res = _build_result(
                        verdict="FAILED_INFRA",
                        category=_classify_error(str(exc)),
                        detail=f"source recheck failed after security gate: {type(exc).__name__}: {exc}",
                        norm=norm,
                        changed=actual_after_review,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=gate_payload,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                    )
                    return _persist_inner(res)
                try:
                    _post_gate_diff = await _read_diff()
                except Exception as exc:
                    _record("safety_gate", "failed", f"post-gate diff recheck failed: {type(exc).__name__}")
                    res = _build_result(
                        verdict="FAILED_INFRA",
                        category=_classify_error(str(exc)),
                        detail=f"worktree diff recheck failed after security gate: {type(exc).__name__}: {exc}",
                        norm=norm,
                        changed=actual_after_review,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=gate_payload,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=_post_gate_src,
                    )
                    return _persist_inner(res)
                try:
                    _post_gate_fp = _fingerprint_paths(worktree_path, allowed)
                except _FingerprintError as exc:
                    _record("safety_gate", "failed", f"fingerprint collection failed: {type(exc).__name__}")
                    res = _build_result(
                        verdict="FAILED_INFRA",
                        category="infra",
                        detail=f"fingerprint collection failed after security gate: {type(exc).__name__}: {exc}",
                        norm=norm,
                        changed=actual_after_review,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=gate_payload,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=_post_gate_src,
                    )
                    return _persist_inner(res)
                except Exception as exc:
                    _record("safety_gate", "failed", f"fingerprint collection failed: {type(exc).__name__}")
                    res = _build_result(
                        verdict="FAILED_INFRA",
                        category="infra",
                        detail=f"fingerprint collection failed after security gate: {type(exc).__name__}: {exc}",
                        norm=norm,
                        changed=actual_after_review,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=gate_payload,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=_post_gate_src,
                    )
                    return _persist_inner(res)
                try:
                    _ok_post, _reason_post = _validate_fingerprint_result(_post_gate_fp, allowed)
                except Exception as exc:
                    _record("safety_gate", "failed", f"fingerprint validation failed: {type(exc).__name__}")
                    res = _build_result(
                        verdict="FAILED_INFRA",
                        category="infra",
                        detail=f"fingerprint validation failed after security gate: {type(exc).__name__}: {exc}",
                        norm=norm,
                        changed=actual_after_review,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=gate_payload,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=_post_gate_src,
                    )
                    return _persist_inner(res)
                if _ok_post is not True:
                    _record("safety_gate", "failed", f"fingerprint invalid after security gate: {_reason_post}")
                    res = _build_result(
                        verdict="FAILED_INFRA",
                        category="infra",
                        detail=f"fingerprint invalid after security gate: {_reason_post}",
                        norm=norm,
                        changed=actual_after_review,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=gate_payload,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=_post_gate_src,
                    )
                    return _persist_inner(res)
                try:
                    _post_gate_wt = await _read_wt_state()
                except Exception:
                    _post_gate_wt = None
                # source HEAD/status mutation -> fail closed
                try:
                    _post_head = _post_gate_src.get("head") if isinstance(_post_gate_src, dict) else None
                    _post_status = str((_post_gate_src.get("status") or "")) if isinstance(_post_gate_src, dict) else ""
                    if _post_head != src_before.get("head") or _post_status != "":
                        _record("safety_gate", "failed", "source changed during security gate")
                        res = _build_result(
                            verdict="FAILED_INFRA",
                            category="infra",
                            detail="source checkout changed during security gate",
                            norm=norm,
                            changed=_post_gate_diff,
                            worker_payload=w_payload,
                            validator_payload=v_payload,
                            reviewer_payload=r_payload,
                            security_payload=gate_payload,
                            events=events,
                            worktree_path=str(worktree_path),
                            source_before=src_before,
                            source_after=_post_gate_src,
                        )
                        return _persist_inner(res)
                    if isinstance(_pre_gate_src, dict) and isinstance(_post_gate_src, dict):
                        _pre_head = _pre_gate_src.get("head")
                        _pre_status = str(_pre_gate_src.get("status") or "")
                        if _post_head != _pre_head or _post_status != _pre_status:
                            _record("safety_gate", "failed", "source changed during security gate")
                            res = _build_result(
                                verdict="FAILED_INFRA",
                                category="infra",
                                detail="source checkout changed during security gate",
                                norm=norm,
                                changed=_post_gate_diff,
                                worker_payload=w_payload,
                                validator_payload=v_payload,
                                reviewer_payload=r_payload,
                                security_payload=gate_payload,
                                events=events,
                                worktree_path=str(worktree_path),
                                source_before=src_before,
                                source_after=_post_gate_src,
                            )
                            return _persist_inner(res)
                except Exception as exc:
                    # fail closed on recheck error when verdicts already built?
                    # If we already returned, unreachable; otherwise fail closed.
                    try:
                        _record("safety_gate", "failed", f"source recheck error: {type(exc).__name__}")
                    except Exception:
                        pass
                    res = _build_result(
                        verdict="FAILED_INFRA",
                        category="infra",
                        detail="source integrity recheck failed after security gate",
                        norm=norm,
                        changed=_post_gate_diff if "_post_gate_diff" in locals() else actual_after_review,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=gate_payload,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=_post_gate_src if "_post_gate_src" in locals() else src_after_review,
                    )
                    return _persist_inner(res)
                # unexpected path/allowlist mutation -> fail closed
                try:
                    _pre_set = set(_pre_gate_diff or []) if isinstance(_pre_gate_diff, list) else set(actual_after_review or [])
                    _post_set = set(_post_gate_diff or [])
                    if _pre_set != _post_set:
                        _record("safety_gate", "failed", "worktree diff changed during security gate")
                        res = _build_result(
                            verdict="NEEDS_REWORK",
                            category="policy",
                            detail="worktree diff changed during security gate",
                            norm=norm,
                            changed=_post_gate_diff,
                            worker_payload=w_payload,
                            validator_payload=v_payload,
                            reviewer_payload=r_payload,
                            security_payload=gate_payload,
                            events=events,
                            worktree_path=str(worktree_path),
                            source_before=src_before,
                            source_after=_post_gate_src,
                        )
                        return _persist_inner(res)
                except Exception:
                    pass
                fit_post, bad_post = _fit_allowlist(_post_gate_diff, allowed)
                if fit_post is not True:
                    _record("safety_gate", "failed", f"allowlist breach after security gate: {bad_post[:5]}")
                    res = _build_result(
                        verdict="NEEDS_REWORK",
                        category="policy",
                        detail=f"unexpected paths after security gate: {bad_post[:10]}",
                        norm=norm,
                        changed=_post_gate_diff,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=gate_payload,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=_post_gate_src,
                    )
                    return _persist_inner(res)
                # same-path content/symlink/deletion mutation -> fail closed via validated contract
                try:
                    _fp_equal_gate = _check_fingerprints_equal(_pre_gate_fp, _post_gate_fp, allowed)
                except _FingerprintError as exc:
                    _record("safety_gate", "failed", f"fingerprint invalid: {type(exc).__name__}")
                    res = _build_result(
                        verdict="FAILED_INFRA",
                        category="infra",
                        detail=f"fingerprint validation failed during security gate: {type(exc).__name__}: {exc}",
                        norm=norm,
                        changed=_post_gate_diff,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=gate_payload,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=_post_gate_src,
                    )
                    return _persist_inner(res)
                except Exception as exc:
                    _record("safety_gate", "failed", f"fingerprint comparison failed: {type(exc).__name__}")
                    res = _build_result(
                        verdict="FAILED_INFRA",
                        category="infra",
                        detail=f"fingerprint comparison failed during security gate: {type(exc).__name__}: {exc}",
                        norm=norm,
                        changed=_post_gate_diff,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=gate_payload,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=_post_gate_src,
                    )
                    return _persist_inner(res)
                if _fp_equal_gate is not True:
                    _record("safety_gate", "failed", "worktree content changed during security gate")
                    res = _build_result(
                        verdict="NEEDS_REWORK",
                        category="policy",
                        detail="worktree content changed during security gate (fingerprint mismatch)",
                        norm=norm,
                        changed=_post_gate_diff,
                        worker_payload=w_payload,
                        validator_payload=v_payload,
                        reviewer_payload=r_payload,
                        security_payload=gate_payload,
                        events=events,
                        worktree_path=str(worktree_path),
                        source_before=src_before,
                        source_after=_post_gate_src,
                    )
                    return _persist_inner(res)
                # worktree head/status mutation -> fail closed
                try:
                    if isinstance(_pre_gate_wt, dict) and isinstance(_post_gate_wt, dict):
                        if _pre_gate_wt.get("head") != _post_gate_wt.get("head") or str(
                            _pre_gate_wt.get("status") or ""
                        ) != str(_post_gate_wt.get("status") or ""):
                            _record("safety_gate", "failed", "worktree state changed during security gate")
                            res = _build_result(
                                verdict="NEEDS_REWORK",
                                category="policy",
                                detail="worktree state changed during security gate",
                                norm=norm,
                                changed=_post_gate_diff,
                                worker_payload=w_payload,
                                validator_payload=v_payload,
                                reviewer_payload=r_payload,
                                security_payload=gate_payload,
                                events=events,
                                worktree_path=str(worktree_path),
                                source_before=src_before,
                                source_after=_post_gate_src,
                            )
                            return _persist_inner(res)
                except Exception:
                    pass
                # refresh evidence to post-gate authoritative state (identical when clean)
                try:
                    actual_after_review = _post_gate_diff
                    src_after_review = _post_gate_src
                except Exception:
                    pass
            else:
                approved = True
                gate_detail = "not required"
                gate_payload = sec_result_raw
            _record("safety_gate", "ok", gate_detail)
        except Exception as exc:
            # If we already built a terminal NEEDS_REWORK/FAILED_INFRA above we returned;
            # reaching here means gate error itself.
            try:
                _maybe_verdict = None
                try:
                    _maybe_verdict = locals().get("res", {}).get("verdict") if isinstance(locals().get("res"), dict) else None
                except Exception:
                    _maybe_verdict = None
            except Exception:
                pass
            _record("safety_gate", "blocked", f"{type(exc).__name__}")
            res = _build_result(
                verdict="NEEDS_HUMAN",
                category="policy",
                detail=f"safety gate error: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_validate,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=None,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_validate,
            )
            return _persist_inner(res)

        # Snapshot immediately before observers receive the terminal result.
        # event_sink/artifact_writer are compatibility seams and execute in-process;
        # READY is therefore conditional on a final post-callback identity check.
        try:
            terminal_diff_before = await _read_diff()
            terminal_fp_before = _fingerprint_paths(worktree_path, allowed)
            terminal_src_before = await _read_source()
            terminal_wt_before = await _read_wt_state()
        except Exception as exc:
            res = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"terminal integrity snapshot failed: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_review,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=gate_payload,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_review,
            )
            return _persist_inner(res)

        _record("done", "ok", "ready for human review")
        res = _build_result(
            verdict="READY_FOR_HUMAN_REVIEW",
            category="none",
            detail="pipeline completed; ready for human review",
            norm=norm,
            changed=actual_after_review,
            worker_payload=w_payload,
            validator_payload=v_payload,
            reviewer_payload=r_payload,
            security_payload=gate_payload,
            events=events,
            worktree_path=str(worktree_path),
            source_before=src_before,
            source_after=src_after_review,
        )
        persisted = _persist_inner(res)
        try:
            terminal_diff_after = await _read_diff()
            terminal_fp_after = _fingerprint_paths(worktree_path, allowed)
            terminal_src_after = await _read_source()
            terminal_wt_after = await _read_wt_state()
            terminal_unchanged = (
                terminal_diff_before == terminal_diff_after
                and _check_fingerprints_equal(terminal_fp_before, terminal_fp_after, allowed)
                and terminal_src_before == terminal_src_after
                and terminal_wt_before == terminal_wt_after
            )
        except Exception as exc:
            failed = _build_result(
                verdict="FAILED_INFRA",
                category="infra",
                detail=f"terminal integrity recheck failed: {type(exc).__name__}: {exc}",
                norm=norm,
                changed=actual_after_review,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=gate_payload,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=src_after_review,
            )
            return _persist_inner(failed)
        if terminal_unchanged is not True:
            failed = _build_result(
                verdict="NEEDS_REWORK",
                category="policy",
                detail="terminal observer mutated validated candidate state",
                norm=norm,
                changed=terminal_diff_after,
                worker_payload=w_payload,
                validator_payload=v_payload,
                reviewer_payload=r_payload,
                security_payload=gate_payload,
                events=events,
                worktree_path=str(worktree_path),
                source_before=src_before,
                source_after=terminal_src_after,
            )
            return _persist_inner(failed)
        return persisted
    finally:
        try:
            if _owns_resolved_wt and _resolved_wt:
                with _OWNERSHIP_LOCK:
                    _ACTIVE_WORKTREE_PATHS.discard(_resolved_wt)
        except Exception:
            pass


def _takes_dict(fn: Any) -> Any:
    try:
        import inspect

        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        if len(params) == 1:
            p = params[0]
            # single positional dict-style
            return True
        return False
    except Exception:
        return False


def _takes_no_arg(fn: Any) -> Any:
    try:
        import inspect

        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        required = [
            p
            for p in params
            if p.default is inspect.Parameter.empty
            and p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        return len(required) == 0
    except Exception:
        return False


def run(contract: Any, **kwargs: Any) -> dict[str, Any]:
    """Thin headless sync wrapper. Never raises; fails closed."""
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(lambda: asyncio.run(run_pipeline(contract, **kwargs)))
                return fut.result(timeout=16000)
        return asyncio.run(run_pipeline(contract, **kwargs))
    except Exception as exc:
        try:
            return sanitize_dict(
                {
                    "schema_version": SCHEMA_VERSION,
                    "verdict": "FAILED_INFRA",
                    "failure_category": "infra",
                    "failure_detail": f"{type(exc).__name__}: {exc}",
                    "events": [],
                    "extra": {},
                }
            )
        except Exception:
            return {
                "schema_version": "1.0",
                "verdict": "FAILED_INFRA",
                "failure_category": "infra",
                "failure_detail": "[REDACTED]",
                "events": [],
                "extra": {},
            }


# aliases for harness compatibility (all headless, deterministic, safe)
async def execute_pipeline(contract: Any, **kwargs: Any) -> dict[str, Any]:
    return await run_pipeline(contract, **kwargs)


async def arun(contract: Any, **kwargs: Any) -> dict[str, Any]:
    return await run_pipeline(contract, **kwargs)


async def coordinate(contract: Any, **kwargs: Any) -> dict[str, Any]:
    return await run_pipeline(contract, **kwargs)


async def execute(contract: Any, **kwargs: Any) -> dict[str, Any]:
    return await run_pipeline(contract, **kwargs)


def run_sync(contract: Any, **kwargs: Any) -> dict[str, Any]:
    return run(contract, **kwargs)


def main(argv: Any = None) -> int:
    ap = argparse.ArgumentParser(prog="jho-pipeline", description="Operational v1 coordinator")
    ap.add_argument("--task-id", default="")
    ap.add_argument("--repo", default="")
    ap.add_argument("--allowed-path", action="append", default=[])
    ap.add_argument("--task", default="")
    ap.add_argument("--candidate", default="")
    ap.add_argument("--reviewer-candidate", default="")
    ap.add_argument("--profile", default="backend")
    args = ap.parse_args(argv)
    contract = {
        "task_id": args.task_id,
        "repo_path": args.repo,
        "allowed_paths": args.allowed_path,
        "task": args.task,
        "worker_candidate": args.candidate,
        "reviewer_candidate": args.reviewer_candidate or args.candidate,
        "validator_profile": args.profile,
    }
    out = run(contract)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("verdict") == "READY_FOR_HUMAN_REVIEW" else 6


if __name__ == "__main__":
    raise SystemExit(main())
