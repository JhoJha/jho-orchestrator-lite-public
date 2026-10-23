#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import jho_lite as jl
except Exception:
    jl = None  # type: ignore

DEFAULT_REPO = jl.DEFAULT_PRODUCT if jl is not None else (Path.home() / "repos" / "client-product")
WORKTREE_ROOT = Path.home() / "agent-runs" / "jho-worktrees"

class WorktreeError(RuntimeError):
    pass

def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

def run_git(args: list[str], *, cwd: Path, timeout: float = 30.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git {' '.join(args)} timed out after {timeout}s") from exc

def require_ok(p: subprocess.CompletedProcess, what: str) -> str:
    if p.returncode != 0:
        detail = (p.stderr or p.stdout or "").strip()
        raise WorktreeError(f"{what} failed: {detail[-1200:]}")
    return (p.stdout or "").strip()

def safe_task_id(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise WorktreeError("task id is empty")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")[:96]
    if not safe:
        raise WorktreeError("task id contains no safe characters")
    return safe

def repo_state(repo: Path) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        raise WorktreeError(f"repository does not exist: {repo}")
    inside = run_git(["rev-parse", "--is-inside-work-tree"], cwd=repo)
    if require_ok(inside, "git repository check") != "true":
        raise WorktreeError(f"not a git worktree: {repo}")
    head = require_ok(run_git(["rev-parse", "HEAD"], cwd=repo), "git HEAD")
    status = require_ok(run_git(["status", "--porcelain=v1"], cwd=repo), "git status")
    branch = require_ok(run_git(["branch", "--show-current"], cwd=repo), "git branch")
    return {"repo": str(repo), "head": head, "status": status, "branch": branch}

def task_dir(task_id: str) -> Path:
    return WORKTREE_ROOT / safe_task_id(task_id)

def worktree_path(task_id: str) -> Path:
    return task_dir(task_id) / "worktree"

def manifest_path(task_id: str) -> Path:
    return task_dir(task_id) / "manifest.json"

def create_worktree(*, task_id: str, repo: Path) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    before = repo_state(repo)
    if before["status"]:
        raise WorktreeError(
            "source repository is dirty; refusing to create an execution worktree because baseline evidence would be ambiguous"
        )

    tid = safe_task_id(task_id)
    tdir = task_dir(tid)
    wt = worktree_path(tid)
    if tdir.exists():
        raise WorktreeError(f"task directory already exists: {tdir}")
    tdir.mkdir(parents=True, exist_ok=False)

    try:
        add = run_git(["worktree", "add", "--detach", str(wt), before["head"]], cwd=repo, timeout=60.0)
        require_ok(add, "git worktree add")

        wt_state = repo_state(wt)
        after = repo_state(repo)
        if wt_state["head"] != before["head"]:
            raise WorktreeError("created worktree HEAD does not match source baseline")
        if after["head"] != before["head"] or after["status"] != before["status"]:
            raise WorktreeError("source repository changed while creating isolated worktree")

        manifest = {
            "schema_version": "1.0",
            "task_id": tid,
            "created_at": now_utc(),
            "source_repo": str(repo),
            "source_branch": before["branch"],
            "source_head": before["head"],
            "source_status": before["status"],
            "worktree": str(wt),
            "worktree_head": wt_state["head"],
            "mode": "detached",
            "automatic_commit": False,
            "automatic_push": False,
            "automatic_merge": False,
            "automatic_deploy": False,
        }
        manifest_path(tid).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return manifest
    except Exception:
        try:
            if wt.exists():
                run_git(["worktree", "remove", "--force", str(wt)], cwd=repo, timeout=30.0)
        except Exception:
            pass
        if tdir.exists():
            shutil.rmtree(tdir, ignore_errors=True)
        raise

def load_manifest(task_id: str) -> dict[str, Any]:
    path = manifest_path(task_id)
    if not path.is_file():
        raise WorktreeError(f"manifest not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data

def status_worktree(task_id: str) -> dict[str, Any]:
    data = load_manifest(task_id)
    repo = Path(data["source_repo"]).resolve()
    wt = Path(data["worktree"]).resolve()
    expected = worktree_path(task_id).resolve()
    if wt != expected:
        raise WorktreeError(f"manifest worktree escaped managed root: {wt}")
    out = {"manifest": data, "source": repo_state(repo), "worktree_exists": wt.is_dir()}
    if wt.is_dir():
        out["worktree"] = repo_state(wt)
    return out

def remove_worktree(*, task_id: str, force: bool) -> dict[str, Any]:
    data = load_manifest(task_id)
    repo = Path(data["source_repo"]).resolve()
    wt = Path(data["worktree"]).resolve()
    expected = worktree_path(task_id).resolve()
    if wt != expected:
        raise WorktreeError(f"refusing cleanup outside managed worktree root: {wt}")

    if wt.is_dir():
        wt_state = repo_state(wt)
        if wt_state["status"] and not force:
            raise WorktreeError("worktree is dirty; refusing removal without --force")
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(wt))
        require_ok(run_git(args, cwd=repo, timeout=60.0), "git worktree remove")
    require_ok(run_git(["worktree", "prune"], cwd=repo), "git worktree prune")

    tdir = task_dir(task_id)
    if tdir.exists():
        shutil.rmtree(tdir)
    return {"task_id": safe_task_id(task_id), "removed": True, "source": repo_state(repo)}

def main() -> int:
    ap = argparse.ArgumentParser(prog="jho-lite worktree", description="Jho v0.3 isolated Git worktree manager")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("--task-id", required=True)
    c.add_argument("--repo", default=str(DEFAULT_REPO))

    s = sub.add_parser("status")
    s.add_argument("--task-id", required=True)

    r = sub.add_parser("remove")
    r.add_argument("--task-id", required=True)
    r.add_argument("--force", action="store_true")

    args = ap.parse_args()
    if jl is None:
        print("FAIL: pinned jho_lite/protoAgent runtime unavailable", file=sys.stderr)
        return 4
    jl.ensure_pinned_upstream()
    try:
        if args.cmd == "create":
            result = create_worktree(task_id=args.task_id, repo=Path(args.repo))
        elif args.cmd == "status":
            result = status_worktree(args.task_id)
        elif args.cmd == "remove":
            result = remove_worktree(task_id=args.task_id, force=args.force)
        else:
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except WorktreeError as exc:
        print(f"FAIL: WorktreeError: {exc}", file=sys.stderr)
        return 4

if __name__ == "__main__":
    raise SystemExit(main())
