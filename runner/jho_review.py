#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
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
    import jho_git
    import jho_safe
except Exception as exc:
    jl = None  # type: ignore
    try:
        import jho_worktree as jw
        import jho_git
        import jho_safe
    except Exception:
        raise ImportError("Jho reviewer support modules unavailable") from exc

try:
    from plugins.coding_agent import _make_permission
except Exception:
    def _make_permission(_spec: Any) -> Any:
        raise ReviewError("pinned protoAgent permission seam unavailable")


class ReviewError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, timeout=timeout)


def _patch_context(worktree: Path, max_chars: int = 180_000) -> str:
    tracked = _git(["diff", "--no-ext-diff", "--unified=80", "HEAD", "--"], worktree)
    chunks = [tracked.stdout or ""]
    for record in jho_git.status_records(worktree):
        if record.get("status") != "??":
            continue
        rel = str(record["path"])
        p = (worktree / rel).resolve()
        try:
            p.relative_to(worktree.resolve())
        except ValueError:
            continue
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            text = "<binary or unreadable file>"
        chunks.append(f"\n--- /dev/null\n+++ b/{rel}\n@@ new file @@\n{text}\n")
    joined = "\n".join(chunks)
    if len(joined) > max_chars:
        return joined[:max_chars] + "\n\n[PATCH CONTEXT TRUNCATED BY HOST]\n"
    return joined


async def execute(args: argparse.Namespace) -> int:
    if jl is None:
        raise ReviewError("pinned jho_lite/protoAgent runtime unavailable")
    jl.ensure_pinned_upstream()
    manifest = jw.load_manifest(args.task_id)
    source = Path(manifest["source_repo"]).resolve()
    worktree = Path(manifest["worktree"]).resolve()
    if worktree != jw.worktree_path(args.task_id).resolve():
        raise ReviewError("manifest worktree escaped managed task root")
    if not worktree.is_dir():
        raise ReviewError(f"worktree missing: {worktree}")

    validator_path = jw.task_dir(args.task_id) / "validator_result.json"
    if not validator_path.is_file():
        raise ReviewError("validator_result.json missing; reviewer must run after deterministic validators")
    validators = json.loads(validator_path.read_text(encoding="utf-8"))

    task_text = (args.task or "").strip()
    if args.task_file:
        task_text = Path(args.task_file).expanduser().read_text(encoding="utf-8").strip()
    if not task_text:
        raise ReviewError("task is empty")

    reg = jl.load_registry()
    pool, model, effort = jl.parse_candidate(args.candidate)
    jl.find_registry_candidate(reg, pool, model, effort)
    jl.enforce_hard_rules(pool, model, "interactive")
    if pool in {"agy", "nvidia"}:
        raise ReviewError("Slice 4 reviewer uses ACP-backed Codex/OpenCode only")

    command, cmd_args, wire_model, access_path = jl.adapter_for(pool, model)
    exe = shutil.which(command)
    if not exe:
        raise ReviewError(f"reviewer harness missing: {command}")

    patch = _patch_context(worktree)
    changed = _git(["status", "--porcelain=v1"], worktree).stdout or ""

    prompt = f"""JHO INDEPENDENT READ-ONLY REVIEW — mandatory policy.

You are an independent reviewer. Do not edit, create, delete, execute shell commands,
install packages, commit, push, merge or deploy anything.

Review the requested task against the actual patch context and deterministic validator evidence.
Focus on correctness, scope, regressions, security, and whether acceptance criteria are met.

TASK:
{task_text}

GIT STATUS:
{changed}

PATCH / NEW-FILE CONTEXT:
{patch}

DETERMINISTIC VALIDATORS:
{json.dumps(validators, indent=2, ensure_ascii=False)}

Return a concise review with findings if any.
Your FINAL non-empty line MUST be exactly one of:
JHO_REVIEW_VERDICT=APPROVE
JHO_REVIEW_VERDICT=REWORK
JHO_REVIEW_VERDICT=BLOCK
"""

    spec = {
        "name": f"jho-review-{pool}-{model}-{effort}",
        "command": exe,
        "args": cmd_args,
        "workdir": str(worktree),
        "env": None,
        "env_remove": [],
        "permissions": "readonly",
        "allow_kinds": [],
        "deny_kinds": [],
        "permissions_ceiling": "readonly",
        "conversation_key": "",
        "timeout_s": float(args.timeout),
    }

    counters = {"tool_events": 0, "thought_chunks": 0, "text_chunks": 0}

    async def on_tool(_event: dict) -> None:
        counters["tool_events"] += 1

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

    wt_before = jw.repo_state(worktree)
    src_before = jw.repo_state(source)
    started = time.monotonic()
    answer = ""
    error = None
    status = "failed"
    tapped = None
    try:
        reply = await client.prompt(
            prompt,
            tool_callback=on_tool,
            thought_callback=on_thought,
            text_callback=on_text,
            timeout=float(args.timeout),
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

    wt_after = jw.repo_state(worktree)
    src_after = jw.repo_state(source)
    reviewer_readonly_integrity = wt_before["head"] == wt_after["head"] and wt_before["status"] == wt_after["status"]
    source_integrity = src_before["head"] == src_after["head"] and src_before["status"] == src_after["status"] == ""

    verdicts = re.findall(r"(?m)^JHO_REVIEW_VERDICT=(APPROVE|REWORK|BLOCK)\s*$", answer)
    verdict = verdicts[-1] if verdicts else "UNPARSEABLE"
    if status != "completed":
        verdict = "ERROR"
    if not reviewer_readonly_integrity or not source_integrity:
        verdict = "BLOCK"
        status = "integrity_breach"
        error = error or "reviewer or source repository mutated during read-only review"

    payload = {
        "schema_version": "1.0",
        "task_id": args.task_id,
        "run_id": getattr(args, "run_id", "") or "",
        "candidate": args.candidate,
        "model": model,
        "effort": effort,
        "access_path": access_path,
        "status": status,
        "verdict": verdict,
        "duration_seconds": round(time.monotonic() - started, 3),
        "reviewer_readonly_integrity": reviewer_readonly_integrity,
        "source_integrity": source_integrity,
        "applied_config": client.applied_config,
        "signals": jl.serialize_tapped(tapped) if tapped else None,
        "stream_counters": counters,
        "answer": answer,
        "error": error,
    }
    out = jw.task_dir(args.task_id) / "review_result.json"
    payload = jho_safe.write_json(out, payload)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if status == "completed" and verdict == "APPROVE" else 6


def main() -> int:
    p = argparse.ArgumentParser(prog="jho-lite review-task", description="Jho v0.3 independent read-only reviewer")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--task-id", required=True)
    r.add_argument("--candidate", required=True)
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--task")
    g.add_argument("--task-file")
    r.add_argument("--timeout", default="300")
    r.add_argument("--run-id", default="")
    args = p.parse_args()
    try:
        if args.cmd == "run":
            return asyncio.run(execute(args))
        return 2
    except (
        ReviewError,
        getattr(jl, "PolicyError", ReviewError),
        getattr(jl, "CapabilityError", ReviewError),
    ) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
