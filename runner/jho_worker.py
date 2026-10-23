#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

try:
    import jho_lite as jl
    import jho_worktree as jw
    import jho_git
    import jho_safe
except Exception as exc:
    jl = None  # type: ignore
    try:
        import jho_worktree as jw
        import jho_git
        import jho_safe
    except Exception:
        raise ImportError("Jho worker support modules unavailable") from exc

try:
    from plugins.coding_agent import _make_permission
except Exception:
    def _make_permission(_spec: Any) -> Any:
        raise WorkerError("pinned protoAgent permission seam unavailable")


class WorkerError(RuntimeError):
    pass


def git_status_paths(repo: Path) -> list[str]:
    try:
        return jho_git.changed_paths(jho_git.status_records(repo, timeout=20))
    except jho_git.GitStatusError as exc:
        raise WorkerError(str(exc)) from exc


def ensure_managed_task(task_id: str) -> tuple[dict[str, Any], Path, Path]:
    manifest = jw.load_manifest(task_id)
    source = Path(manifest["source_repo"]).resolve()
    worktree = Path(manifest["worktree"]).resolve()
    expected = jw.worktree_path(task_id).resolve()
    if worktree != expected:
        raise WorkerError(f"manifest worktree escaped managed root: {worktree}")
    if not worktree.is_dir():
        raise WorkerError(f"managed worktree missing: {worktree}")

    src = jw.repo_state(source)
    wt = jw.repo_state(worktree)
    if src["status"]:
        raise WorkerError("source checkout is dirty; refusing controlled worker")
    if src["head"] != manifest["source_head"]:
        raise WorkerError("source HEAD drifted since worktree creation")
    if wt["head"] != manifest["source_head"]:
        raise WorkerError("worktree HEAD does not match task baseline")
    if wt["status"]:
        raise WorkerError("worktree must be clean before worker starts")
    return manifest, source, worktree


def path_allowed(path: str, allowed: list[str]) -> bool:
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
            pass
    return False


async def execute(args: argparse.Namespace) -> int:
    if jl is None:
        raise WorkerError("pinned jho_lite/protoAgent runtime unavailable")
    jl.ensure_pinned_upstream()
    manifest, source, worktree = ensure_managed_task(args.task_id)

    reg = jl.load_registry()
    pool, model, effort = jl.parse_candidate(args.candidate)
    jl.find_registry_candidate(reg, pool, model, effort)
    jl.enforce_hard_rules(pool, model, "interactive")

    # Slice 3 intentionally supports ACP-backed editors only.
    if pool == "agy":
        raise WorkerError("Slice 3 does not enable AGY editing yet; AGY remains read-only")
    if pool == "nvidia":
        raise WorkerError("Slice 3 does not route NVIDIA on the interactive edit path")

    command, cmd_args, wire_model, access_path = jl.adapter_for(pool, model)
    exe = shutil.which(command)
    if not exe:
        raise WorkerError(f"required harness binary missing: {command}")

    task = (args.task or "").strip()
    if args.task_file:
        task = Path(args.task_file).expanduser().read_text(encoding="utf-8").strip()
    if not task:
        raise WorkerError("task is empty")

    allowed = [x.strip().strip("/") for x in args.allowed_path if x.strip()]
    if not allowed:
        raise WorkerError("at least one --allowed-path is required")
    for a in allowed:
        pa = Path(a)
        if pa.is_absolute() or ".." in pa.parts:
            raise WorkerError(f"unsafe allowed path: {a}")

    timeout = float(args.timeout)
    guarded = f"""JHO CONTROLLED EDIT WORKER POLICY — mandatory.

ACTIVE_WORKTREE={worktree}
SOURCE_CHECKOUT={source}

You are operating only inside ACTIVE_WORKTREE, a disposable Git worktree.

Rules:
- You MAY read and edit files only inside ACTIVE_WORKTREE.
- You MAY modify only these repository-relative paths/prefixes:
{chr(10).join(f'  - {x}' for x in allowed)}
- Do NOT run shell/terminal commands.
- Do NOT install packages.
- Do NOT delete files.
- Do NOT commit, push, merge, deploy, migrate databases, or change credentials.
- Do NOT inspect parent directories, the user's home directory, or SOURCE_CHECKOUT.
- Make the smallest change necessary for the requested task.
- When finished, summarize what you changed. Do not claim tests ran because deterministic validators run separately.

TASK:
{task}
"""

    spec = {
        "name": f"jho-edit-{pool}-{model}-{effort}",
        "command": exe,
        "args": cmd_args,
        "workdir": str(worktree),
        "env": None,
        "env_remove": [],
        # allowlist allows read/write/edit kinds while host-side permission policy
        # denies execute/delete. Deterministic validators run outside the model.
        "permissions": "allowlist",
        "allow_kinds": [],
        "deny_kinds": ["execute", "delete"],
        "permissions_ceiling": "",
        "conversation_key": "",
        "timeout_s": timeout,
    }

    counters = {"tool_events": 0, "thought_chunks": 0, "text_chunks": 0}
    tool_summaries: list[dict[str, Any]] = []

    async def on_tool(event: dict) -> None:
        counters["tool_events"] += 1
        summary = {}
        for key in ("type", "kind", "title", "status", "toolCallId", "tool_call_id"):
            val = event.get(key)
            if isinstance(val, (str, int, float, bool)):
                summary[key] = str(val)[:300]
        if summary:
            tool_summaries.append(summary)

    async def on_thought(_delta: str) -> None:
        counters["thought_chunks"] += 1

    async def on_text(_delta: str) -> None:
        counters["text_chunks"] += 1

    client = jl.ConfiguredAcpClient(
        exe,
        cmd_args,
        cwd=str(worktree),
        name=spec["name"],
        permission=_make_permission(spec),
        session_id_path=None,
        record_runs=True,
        desired_model=wire_model,
        desired_effort=effort,
        status_hook=None,
    )

    source_head_before = jl.git_head(source)
    source_status_before = jl.git_status(source)
    started = time.monotonic()
    status = "failed"
    answer = ""
    error = None
    tapped = None

    try:
        reply = await client.prompt(
            guarded,
            tool_callback=on_tool,
            thought_callback=on_thought,
            text_callback=on_text,
            timeout=timeout,
        )
        tapped = client.tapped_result(reply)
        answer = reply
        status = "completed"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await client.close()
        except Exception:
            pass

    duration = time.monotonic() - started
    changed = git_status_paths(worktree)
    disallowed = [p for p in changed if not path_allowed(p, allowed)]

    source_head_after = jl.git_head(source)
    source_status_after = jl.git_status(source)
    source_integrity = (
        source_head_before == source_head_after
        and source_status_before == source_status_after
        and source_status_after == ""
    )

    if disallowed:
        status = "policy_breach"
        error = f"worker changed disallowed paths: {disallowed}"
    if not source_integrity:
        status = "integrity_breach"
        error = "source checkout changed during isolated worker run"

    result = {
        "schema_version": "1.0",
        "task_id": args.task_id,
        "run_id": getattr(args, "run_id", "") or "",
        "candidate": args.candidate,
        "pool": pool,
        "model": model,
        "wire_model": wire_model,
        "effort": effort,
        "access_path": access_path,
        "worktree": str(worktree),
        "source_repo": str(source),
        "source_head": manifest["source_head"],
        "permissions": {
            "policy": "allowlist",
            "denied_kinds": ["execute", "delete"],
            "allowed_paths": allowed,
        },
        "status": status,
        "duration_seconds": round(duration, 3),
        "changed_paths": changed,
        "disallowed_paths": disallowed,
        "source_integrity": source_integrity,
        "applied_config": client.applied_config,
        "signals": jl.serialize_tapped(tapped) if tapped else None,
        "stream_counters": counters,
        "tool_summaries": tool_summaries[:100],
        "answer": answer,
        "error": error,
    }

    out = jw.task_dir(args.task_id) / "worker_result.json"
    result = jho_safe.write_json(out, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if status == "completed" else 6


def main() -> int:
    p = argparse.ArgumentParser(prog="jho-lite worker", description="Jho v0.3 controlled edit worker")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--task-id", required=True)
    r.add_argument("--candidate", required=True)
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--task")
    g.add_argument("--task-file")
    r.add_argument("--allowed-path", action="append", default=[], required=True)
    r.add_argument("--timeout", default="240")
    r.add_argument("--run-id", default="")

    args = p.parse_args()
    try:
        if args.cmd == "run":
            return asyncio.run(execute(args))
        return 2
    except (
        WorkerError,
        getattr(jl, "PolicyError", WorkerError),
        getattr(jl, "CapabilityError", WorkerError),
    ) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
