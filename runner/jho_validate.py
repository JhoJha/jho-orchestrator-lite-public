#!/usr/bin/env python3
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    import jho_lite as jl
    import jho_worktree as jw
    import jho_safe
except Exception as exc:
    jl = None  # type: ignore
    try:
        import jho_worktree as jw
        import jho_safe
    except Exception:
        raise ImportError("Jho validator support modules unavailable") from exc


class ValidatorError(RuntimeError):
    pass


_SAFE_ENV_KEYS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SYSTEMROOT",
    "WINDIR",
    "PATHEXT",
)
_IGNORED_FINGERPRINT_EXCLUSIONS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".tox",
    ".nox",
    "dist",
    "build",
}
_BWRAP = shutil.which("bwrap") if sys.platform.startswith("linux") else None


@functools.lru_cache(maxsize=1)
def _bwrap_usable() -> bool:
    if not _BWRAP:
        return False
    try:
        probe = subprocess.run(
            [_BWRAP, "--die-with-parent", "--ro-bind", "/", "/", "--", "/bin/true"],
            capture_output=True,
            timeout=5,
            env={"PATH": os.environ.get("PATH", os.defpath)},
        )
        return probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _validator_home(cwd: Path) -> str:
    digest = hashlib.sha256(str(cwd.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"/tmp/jho-validator-home-{digest}"


def _validator_command(cmd: list[str], cwd: Path) -> tuple[list[str], str]:
    """Use the host's maintained Bubblewrap primitive when available.

    The host filesystem is read-only, the candidate worktree is the sole writable
    host bind, and /tmp is ephemeral. Network isolation is not requested here
    because it is unavailable on some supported kernels; this remains documented
    residual risk rather than a silent validator break.
    """
    if not _bwrap_usable():
        return cmd, "process-only"
    root = str(cwd.resolve())
    private_home = _validator_home(cwd)
    return (
        [
            _BWRAP,
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/",
            "/",
            "--dev-bind",
            "/dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--dir",
            private_home,
            "--bind",
            root,
            root,
            "--chdir",
            root,
            "--",
            *cmd,
        ],
        "bubblewrap-ro-host",
    )


def validator_environment(cwd: Path) -> dict[str, str]:
    """Return the minimal non-credential environment for candidate execution."""
    env = {key: os.environ[key] for key in _SAFE_ENV_KEYS if os.environ.get(key)}
    env["HOME"] = _validator_home(cwd)
    env["TMPDIR"] = "/tmp"
    env["TEMP"] = "/tmp"
    env["TMP"] = "/tmp"
    env["PYTHONPATH"] = str(cwd)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _ignored_source_fingerprint(source: Path) -> dict[str, str]:
    """Fingerprint ignored source files except bounded cache/dependency trees.

    Git status already covers tracked and ordinary untracked paths. This closes
    its ignored-file blind spot without hashing virtualenvs and generated caches.
    """
    proc = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        cwd=source,
        capture_output=True,
        text=False,
        timeout=60,
    )
    if proc.returncode != 0:
        raise ValidatorError("cannot enumerate ignored source files")
    result: dict[str, str] = {}
    for raw in (proc.stdout or b"").split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", "surrogateescape")
        parts = Path(rel).parts
        if any(part in _IGNORED_FINGERPRINT_EXCLUSIONS for part in parts):
            continue
        path = source / rel
        try:
            if path.is_symlink():
                marker = "symlink:" + os.readlink(path)
            elif path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                marker = "file:" + digest.hexdigest()
            else:
                marker = "special"
        except OSError as exc:
            raise ValidatorError(f"cannot fingerprint ignored source path: {rel}") from exc
        result[rel] = marker
    return result


def _python_for(source_repo: Path) -> str:
    candidates = [
        source_repo / ".venv" / "bin" / "python",
        source_repo / "venv" / "bin" / "python",
    ]
    for p in candidates:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    raise ValidatorError("no Python interpreter available")


def _run(cmd: list[str], *, cwd: Path, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    execution_cmd, confinement = _validator_command(cmd, cwd)
    try:
        p = subprocess.run(
            execution_cmd,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=validator_environment(cwd),
        )
        missing_tool = bool(
            p.returncode != 0
            and re.search(
                r"(?i)(no module named|command not found|no such file or directory)",
                (p.stderr or "") + "\n" + (p.stdout or ""),
            )
        )
        status = "PASS" if p.returncode == 0 else ("TOOLING_ERROR" if missing_tool else "FAIL")
        return {
            "command": cmd,
            "confinement": confinement,
            "status": status,
            "returncode": p.returncode,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout_tail": (p.stdout or "")[-8000:],
            "stderr_tail": (p.stderr or "")[-8000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": cmd,
            "confinement": confinement,
            "status": "TIMEOUT",
            "returncode": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout_tail": (exc.stdout or "")[-8000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-8000:] if isinstance(exc.stderr, str) else "",
        }
    except FileNotFoundError as exc:
        return {
            "command": cmd,
            "confinement": confinement,
            "status": "TOOLING_ERROR",
            "returncode": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout_tail": "",
            "stderr_tail": f"{type(exc).__name__}: {exc}",
        }
    except (OSError, ValueError) as exc:
        return {
            "command": cmd,
            "confinement": confinement,
            "status": "ERROR",
            "returncode": None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout_tail": "",
            "stderr_tail": f"{type(exc).__name__}: {exc}",
        }


def execute(args: argparse.Namespace) -> int:
    if jl is None:
        raise ValidatorError("pinned jho_lite/protoAgent runtime unavailable")
    jl.ensure_pinned_upstream()
    manifest = jw.load_manifest(args.task_id)
    source = Path(manifest["source_repo"]).resolve()
    worktree = Path(manifest["worktree"]).resolve()
    if worktree != jw.worktree_path(args.task_id).resolve():
        raise ValidatorError("manifest worktree escaped managed task root")
    if not worktree.is_dir():
        raise ValidatorError(f"worktree missing: {worktree}")

    src_before = jw.repo_state(source)
    ignored_before = _ignored_source_fingerprint(source)
    if src_before["status"]:
        raise ValidatorError("source checkout is dirty before validation")
    if src_before["head"] != manifest["source_head"]:
        raise ValidatorError("source HEAD drifted since task creation")

    profile = args.profile
    timeout = float(args.timeout)
    results: list[dict[str, Any]] = []

    results.append(_run(["git", "diff", "--check"], cwd=worktree, timeout=min(timeout, 60.0)))

    if profile == "backend":
        py = _python_for(source)
        ruff_targets = args.ruff_target or ["app", "tests"]
        results.append(_run([py, "-m", "ruff", "check", *ruff_targets], cwd=worktree, timeout=timeout))
        pytest_targets = args.pytest_target or ["tests"]
        results.append(_run([py, "-m", "pytest", *pytest_targets], cwd=worktree, timeout=timeout))
    elif profile != "minimal":
        raise ValidatorError(f"unsupported validator profile: {profile}")

    src_after = jw.repo_state(source)
    ignored_after = _ignored_source_fingerprint(source)
    source_integrity = (
        src_before["head"] == src_after["head"]
        and src_before["status"] == src_after["status"]
        and src_after["status"] == ""
        and ignored_before == ignored_after
    )

    statuses = {str(item.get("status")) for item in results}
    if not source_integrity or "ERROR" in statuses:
        overall = "INFRA_ERROR"
    elif "TOOLING_ERROR" in statuses:
        overall = "TOOLING_ERROR"
    elif "TIMEOUT" in statuses:
        overall = "TIMEOUT"
    elif results and statuses == {"PASS"}:
        overall = "PASS"
    else:
        overall = "FAIL"
    payload = {
        "schema_version": "1.0",
        "task_id": args.task_id,
        "run_id": getattr(args, "run_id", "") or "",
        "profile": profile,
        "status": overall,
        "source_integrity": source_integrity,
        "python": _python_for(source) if profile == "backend" else None,
        "results": results,
    }
    out = jw.task_dir(args.task_id) / "validator_result.json"
    payload = jho_safe.write_json(out, payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if overall == "PASS" else (
        7 if overall == "TIMEOUT" else (
            4 if overall in {"INFRA_ERROR", "TOOLING_ERROR"} else 6
        )
    )


def main() -> int:
    p = argparse.ArgumentParser(prog="jho-lite validate", description="Jho v0.3 deterministic validators")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--task-id", required=True)
    r.add_argument("--profile", choices=("minimal", "backend"), default="minimal")
    r.add_argument("--pytest-target", action="append", default=[])
    r.add_argument("--ruff-target", action="append", default=[])
    r.add_argument("--timeout", default="300")
    r.add_argument("--run-id", default="")
    args = p.parse_args()
    try:
        if args.cmd == "run":
            return execute(args)
        return 2
    except (ValidatorError, getattr(jl, "PolicyError", ValidatorError)) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
