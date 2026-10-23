#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import secrets
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

def _bootstrap_active_jho_lite() -> None:
    """Preserve the validated Operational-v1 module composition.

    jho_lite + protoAgent/plugins come from the pinned active runtime;
    jho_pipeline and split Jho modules come from this preservation runner.
    """
    candidates: list[Path] = []
    env = os.environ.get("JHO_ACTIVE_RUNTIME")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(Path.home() / "tools" / "jho-orchestrator-lite")
    for runtime in candidates:
        jl = runtime / "local_jho" / "runner" / "jho_lite.py"
        if not jl.is_file():
            continue
        if str(runtime) not in sys.path:
            sys.path.insert(0, str(runtime))
        active = jl.parent
        if str(active) not in sys.path:
            sys.path.insert(0, str(active))
        spec = importlib.util.spec_from_file_location("jho_lite", jl)
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        sys.modules["jho_lite"] = mod
        previous_cwd = Path.cwd()
        try:
            spec.loader.exec_module(mod)
        finally:
            os.chdir(previous_cwd)
        return

_bootstrap_active_jho_lite()
if str(HERE) in sys.path:
    sys.path.remove(str(HERE))
sys.path.insert(0, str(HERE))

import jho_pipeline
import jho_safe

SAFE_ARTIFACTS = (
    "manifest.json",
    "worker_result.json",
    "validator_result.json",
    "review_result.json",
    "pipeline_result.json",
    "operator_result.json",
)

def _safe_id(value: str) -> str:
    out = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    if not out:
        raise ValueError("task id becomes empty after sanitization")
    return out[:96]

def _active_runtime_candidates() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get("JHO_ACTIVE_RUNTIME")
    if env:
        out.append(Path(env).expanduser() / "local_jho" / "runner" / "jho_lite.py")
    out.append(Path.home() / "tools" / "jho-orchestrator-lite" / "local_jho" / "runner" / "jho_lite.py")
    sibling = HERE / "jho_lite.py"
    out.append(sibling)
    return out

def _load_notify_module() -> tuple[Any | None, str | None]:
    for path in _active_runtime_candidates():
        try:
            if not path.is_file():
                continue
            spec = importlib.util.spec_from_file_location("jho_notify_runtime", path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            previous_cwd = Path.cwd()
            try:
                spec.loader.exec_module(mod)
            finally:
                os.chdir(previous_cwd)
            if callable(getattr(mod, "notify_windows_best_effort", None)):
                return mod, str(path)
        except Exception:
            continue
    return None, None

def notification_capability() -> dict[str, Any]:
    mod, origin = _load_notify_module()
    if mod is None:
        return {"available": False, "backend": None, "origin": None, "reason": "notify module unavailable"}
    try:
        cap = mod.windows_notification_capability()
        return jho_safe.sanitize_dict(
            {**(cap if isinstance(cap, dict) else {}), "origin": origin}
        )
    except Exception as exc:
        return jho_safe.sanitize_dict({
            "available": False,
            "backend": None,
            "origin": origin,
            "reason": f"{type(exc).__name__}: {exc}",
        })

def _notification_status(result: dict[str, Any]) -> tuple[str, bool]:
    verdict = str(result.get("verdict") or "")
    category = str(result.get("failure_category") or "")
    detail = str(result.get("failure_detail") or "").lower()
    if verdict == "READY_FOR_HUMAN_REVIEW":
        return "completed", True
    if "integrity" in detail and verdict != "READY_FOR_HUMAN_REVIEW":
        return "integrity_breach", False
    if verdict == "NEEDS_HUMAN":
        return "waiting_input", True
    if verdict == "NEEDS_REWORK":
        return "blocked_by_policy", category not in {"policy", "sandbox"}
    return "failed", False

def notify_result(
    result: dict[str, Any],
    *,
    logical_task_id: str,
    candidate: str,
    duration_seconds: float,
    enabled: bool,
) -> dict[str, Any]:
    if not enabled:
        return {"attempted": False, "success": False, "enabled": False, "reason": "disabled"}
    mod, origin = _load_notify_module()
    if mod is None:
        return {"attempted": False, "success": False, "enabled": enabled, "reason": "notify module unavailable"}
    status, integrity_ok = _notification_status(result)
    try:
        out = mod.notify_windows_best_effort(
            status=status,
            candidate=jho_safe.redact_text(f"{logical_task_id} | {candidate}"),
            duration_seconds=duration_seconds,
            integrity_ok=integrity_ok,
            enabled=enabled,
        )
        if isinstance(out, dict):
            out["origin"] = origin
            out["mapped_status"] = status
            return jho_safe.sanitize_dict(out)
        return {"attempted": True, "success": False, "enabled": enabled, "reason": "invalid notify result"}
    except Exception as exc:
        return jho_safe.sanitize_dict({
            "attempted": True,
            "success": False,
            "enabled": enabled,
            "origin": origin,
            "reason": f"{type(exc).__name__}: {exc}",
        })

def _is_noop(result: dict[str, Any]) -> bool:
    return (
        str(result.get("verdict")) == "NEEDS_REWORK"
        and str(result.get("failure_category")) == "worker"
        and not list(result.get("changed_paths") or [])
    )

def _next_action(
    result: dict[str, Any],
    *,
    noop_count: int,
    fallback_remaining: bool,
) -> str:
    verdict = str(result.get("verdict") or "")
    category = str(result.get("failure_category") or "")
    if verdict == "READY_FOR_HUMAN_REVIEW":
        return "HUMAN_GATE"
    if verdict == "NEEDS_HUMAN":
        return "HUMAN"
    if _is_noop(result):
        if noop_count <= 1:
            return "RETRY_SAME"
        return "ESCALATE" if fallback_remaining else "HUMAN"
    if category in {"quota", "provider"} and fallback_remaining:
        return "ESCALATE"
    if category in {"infra", "tooling", "environment", "timeout", "stall"}:
        return "STOP_INFRA"
    if category in {"validator", "reviewer"} or verdict == "NEEDS_REWORK":
        return "REWORK_CONTRACT"
    if category in {"policy", "sandbox", "security"}:
        return "HUMAN"
    return "HUMAN"

def _attempt_task_id(logical_id: str, attempt: int, run_id: str) -> str:
    suffix = f"-R{_safe_id(run_id)[:16]}-A{attempt:02d}"
    # jho_worktree.safe_task_id persists at 96 chars; keep the identity suffix
    # inside that domain so long logical IDs cannot collide after truncation.
    logical_safe = _safe_id(logical_id)
    return logical_safe[: max(1, 96 - len(suffix))] + suffix

def run_attempts(
    base_contract: dict[str, Any],
    *,
    fallbacks: list[str],
    max_attempts: int,
    run_id: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    logical_id = str(base_contract["task_id"])
    current_candidate = str(base_contract["worker_candidate"])
    fallbacks = list(fallbacks)
    history: list[dict[str, Any]] = []
    noop_count = 0
    attempt = 0
    current_run_id = run_id or (
        datetime.now().strftime("%Y%m%dT%H%M%S%f") + "-" + secrets.token_hex(4)
    )

    while attempt < max_attempts:
        attempt += 1
        contract = dict(base_contract)
        contract["task_id"] = _attempt_task_id(logical_id, attempt, current_run_id)
        contract["run_id"] = current_run_id
        contract["worker_candidate"] = current_candidate
        if not contract.get("reviewer_candidate"):
            contract["reviewer_candidate"] = current_candidate

        started = time.monotonic()
        result = jho_pipeline.run(contract)
        elapsed = time.monotonic() - started
        if not isinstance(result, dict):
            result = {
                "schema_version": "1.0",
                "verdict": "FAILED_INFRA",
                "failure_category": "infra",
                "failure_detail": "pipeline returned non-mapping",
                "changed_paths": [],
            }

        if _is_noop(result):
            noop_count += 1
        else:
            noop_count = 0

        action = _next_action(
            result,
            noop_count=noop_count,
            fallback_remaining=bool(fallbacks),
        )
        history.append({
            "attempt": attempt,
            "run_id": current_run_id,
            "task_id": contract["task_id"],
            "candidate": current_candidate,
            "duration_seconds": round(elapsed, 3),
            "verdict": result.get("verdict"),
            "failure_category": result.get("failure_category"),
            "changed_paths": list(result.get("changed_paths") or []),
            "next_action": action,
        })

        if action == "RETRY_SAME" and attempt < max_attempts:
            continue
        if action == "ESCALATE" and attempt < max_attempts and fallbacks:
            current_candidate = fallbacks.pop(0)
            noop_count = 0
            continue
        return result, history

    return result, history

def _default_export_dir() -> Path:
    configured = os.environ.get("JHO_EXPORT_DIR")
    if configured:
        return Path(configured).expanduser()
    users = Path("/mnt/c/Users")
    if users.is_dir():
        try:
            candidates = sorted(
                path / "Documents" / "Archivos de WSL" / "Exports" / "ZIP"
                for path in users.iterdir()
                if path.is_dir()
            )
            for candidate in candidates:
                if candidate.is_dir():
                    return candidate
        except OSError:
            pass
    return Path.home() / "agent-runs" / "jho-exports"

def _task_dir(task_id: str) -> Path:
    return Path.home() / "agent-runs" / "jho-worktrees" / task_id

def export_evidence(
    *,
    logical_task_id: str,
    attempts: list[dict[str, Any]],
    final_result: dict[str, Any],
    notification: dict[str, Any],
    export_dir: Path,
) -> Path:
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    safe = _safe_id(logical_task_id)
    out = export_dir / f"JHO_{safe}_EVIDENCE_{stamp}_{secrets.token_hex(3)}.zip"
    summary = {
        "schema_version": "1.0",
        "logical_task_id": logical_task_id,
        "attempts": attempts,
        "final_verdict": final_result.get("verdict"),
        "failure_category": final_result.get("failure_category"),
        "notification": notification,
        "automatic_commit": False,
        "automatic_push": False,
        "automatic_merge": False,
        "automatic_deploy": False,
    }
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        clean_summary = jho_safe.sanitize_dict(summary)
        clean_final = jho_safe.sanitize_dict(final_result)
        z.writestr("operator_summary.json", json.dumps(clean_summary, indent=2, ensure_ascii=False) + "\n")
        z.writestr("pipeline_result.json", json.dumps(clean_final, indent=2, ensure_ascii=False) + "\n")
        for row in attempts:
            tid = str(row["task_id"])
            expected_run_id = str(row.get("run_id") or "")
            tdir = _task_dir(tid)
            for name in SAFE_ARTIFACTS:
                src = tdir / name
                if not src.is_file():
                    continue
                try:
                    data = json.loads(src.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(data, dict) or str(data.get("task_id") or "") != tid:
                    continue
                if name != "manifest.json" and expected_run_id:
                    if str(data.get("run_id") or "") != expected_run_id:
                        continue
                clean = jho_safe.sanitize_dict(data)
                z.writestr(
                    f"attempts/{_safe_id(tid)}/{name}",
                    json.dumps(clean, indent=2, ensure_ascii=False) + "\n",
                )
    return out

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="jho run", description="Jho Operational v1 operator")
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--allowed-path", action="append", default=[], required=True)
    tg = ap.add_mutually_exclusive_group(required=True)
    tg.add_argument("--task")
    tg.add_argument("--task-file")
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--reviewer-candidate", default="")
    ap.add_argument("--fallback-candidate", action="append", default=[])
    ap.add_argument("--max-attempts", type=int, default=2)
    ap.add_argument("--profile", default="backend")
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--security-review-required", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--no-export", action="store_true")
    ap.add_argument("--export-dir", default="")
    ap.add_argument("--dry-run", action="store_true")
    return ap

def main(argv: list[str] | None = None) -> int:
    initial_cwd = Path.cwd()
    args = _build_parser().parse_args(argv)
    if args.max_attempts < 1 or args.max_attempts > 5:
        raise SystemExit("--max-attempts must be between 1 and 5")

    task_text = args.task
    if args.task_file:
        task_path = Path(args.task_file).expanduser()
        if not task_path.is_absolute():
            task_path = initial_cwd / task_path
        task_text = task_path.read_text(encoding="utf-8")

    repo_path = Path(args.repo).expanduser()
    if not repo_path.is_absolute():
        repo_path = initial_cwd / repo_path

    contract = {
        "task_id": args.task_id,
        "repo_path": str(repo_path.resolve()),
        "allowed_paths": list(args.allowed_path),
        "task": task_text,
        "worker_candidate": args.candidate,
        "reviewer_candidate": args.reviewer_candidate or args.candidate,
        "validator_profile": args.profile,
        "timeout_s": args.timeout,
        "security_review_required": bool(args.security_review_required),
    }

    if args.dry_run:
        print(json.dumps({
            "status": "DRY_RUN",
            "contract": contract,
            "fallback_candidates": args.fallback_candidate,
            "max_attempts": args.max_attempts,
            "notification": notification_capability(),
            "automatic_commit": False,
            "automatic_push": False,
            "automatic_merge": False,
            "automatic_deploy": False,
        }, indent=2, ensure_ascii=False))
        return 0

    started = time.monotonic()
    final, history = run_attempts(
        contract,
        fallbacks=list(args.fallback_candidate),
        max_attempts=args.max_attempts,
    )
    elapsed = time.monotonic() - started

    notification = notify_result(
        final,
        logical_task_id=args.task_id,
        candidate=str(history[-1]["candidate"] if history else args.candidate),
        duration_seconds=elapsed,
        enabled=not args.no_notify,
    )

    evidence = None
    if not args.no_export:
        export_dir = Path(args.export_dir).expanduser() if args.export_dir else _default_export_dir()
        if not export_dir.is_absolute():
            export_dir = initial_cwd / export_dir
        evidence = export_evidence(
            logical_task_id=args.task_id,
            attempts=history,
            final_result=final,
            notification=notification,
            export_dir=export_dir,
        )

    output = {
        "logical_task_id": args.task_id,
        "verdict": final.get("verdict"),
        "failure_category": final.get("failure_category"),
        "attempts": history,
        "notification": notification,
        "evidence_zip": str(evidence) if evidence else None,
        "automatic_commit": False,
        "automatic_push": False,
        "automatic_merge": False,
        "automatic_deploy": False,
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))
    return 0 if final.get("verdict") == "READY_FOR_HUMAN_REVIEW" else 6

if __name__ == "__main__":
    raise SystemExit(main())
