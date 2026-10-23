#!/usr/bin/env python3
"""Jho Control Center (read-only TUI).

Observability only. Never mutates task state. Standard library only.
Standalone: must not import jho_lite, jho_worktree, or protoAgent code.
Redaction is delegated to the shared canonical jho_safe policy (M-SAN-001);
the TUI only adds display normalization (ANSI/control/whitespace/length).
"""
from __future__ import annotations

import argparse
import json
import re
import select
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import jho_safe as _safe_mod
except Exception:  # keep the TUI launchable from any working directory
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import jho_safe as _safe_mod
    except Exception:
        _safe_mod = None

ALLOWED_FILES = (
    "manifest.json",
    "worker_result.json",
    "validator_result.json",
    "review_result.json",
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")

MAX_STR_LEN = 200
MAX_CHANGED_SHOWN = 5


def redact_text(text: str) -> str:
    # Canonical shared policy; fail closed if it is unavailable.
    if _safe_mod is None:
        return "[REDACTED]"
    return _safe_mod.redact_text(text, max_length=None)


def sanitize(value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    text = ANSI_RE.sub("", text)
    text = CONTROL_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_STR_LEN:
        text = text[:MAX_STR_LEN]
    return redact_text(text)


def load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except (PermissionError, OSError, UnicodeDecodeError):
        return None, "malformed"
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None, "malformed"
    except Exception:
        return None, "malformed"
    if not isinstance(data, dict):
        return None, "malformed"
    return data, None


def _as_str(data: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    return value if isinstance(value, str) else None


def _as_bool(data: dict[str, Any] | None, key: str) -> bool | None:
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    return value if isinstance(value, bool) else None


def _as_float(data: dict[str, Any] | None, key: str) -> float | None:
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _as_str_list(data: dict[str, Any] | None, key: str) -> list[str] | None:
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    if not isinstance(value, list):
        return None
    return [x for x in value if isinstance(x, str)]


@dataclass
class TaskView:
    task_id: str
    dir_label: str
    stage: str = "empty"
    manifest_created_at: str | None = None
    worker_candidate: str | None = None
    worker_model: str | None = None
    worker_effort: str | None = None
    worker_status: str | None = None
    worker_duration: float | None = None
    changed_paths: list[str] | None = None
    disallowed_paths: list[str] | None = None
    worker_source_integrity: bool | None = None
    thought_chunks: int | None = None
    tool_events: int | None = None
    validator_status: str | None = None
    validator_profile: str | None = None
    validator_source_integrity: bool | None = None
    reviewer_candidate: str | None = None
    reviewer_model: str | None = None
    reviewer_effort: str | None = None
    reviewer_verdict: str | None = None
    reviewer_status: str | None = None
    reviewer_duration: float | None = None
    reviewer_source_integrity: bool | None = None
    reviewer_readonly_integrity: bool | None = None
    missing: tuple[str, ...] = ()
    malformed: tuple[str, ...] = ()
    diagnostic: str | None = None


def build_task_view(task_dir: Path, runs_root: Path) -> TaskView:
    if task_dir == runs_root:
        dir_label = "."
        fallback_id = runs_root.name or "."
    else:
        dir_label = task_dir.name
        fallback_id = task_dir.name

    missing: list[str] = []
    malformed: list[str] = []

    manifest_data: dict[str, Any] | None = None
    worker_data: dict[str, Any] | None = None
    validator_data: dict[str, Any] | None = None
    review_data: dict[str, Any] | None = None

    for fname in ALLOWED_FILES:
        data, err = load_json(task_dir / fname)
        if err == "missing":
            missing.append(fname)
        elif err == "malformed":
            malformed.append(fname)
        if fname == "manifest.json":
            manifest_data = data
        elif fname == "worker_result.json":
            worker_data = data
        elif fname == "validator_result.json":
            validator_data = data
        elif fname == "review_result.json":
            review_data = data

    task_id = _as_str(manifest_data, "task_id") if manifest_data else None
    if not task_id:
        task_id = fallback_id

    if review_data is not None:
        stage = "review"
    elif validator_data is not None:
        stage = "validation"
    elif worker_data is not None:
        stage = "worker"
    elif manifest_data is not None:
        stage = "created"
    else:
        stage = "empty"

    manifest_created_at = _as_str(manifest_data, "created_at")

    worker_candidate = _as_str(worker_data, "candidate")
    worker_status = _as_str(worker_data, "status")
    worker_model = _as_str(worker_data, "model")
    worker_effort = _as_str(worker_data, "effort")
    worker_duration = _as_float(worker_data, "duration_seconds")
    changed_paths = _as_str_list(worker_data, "changed_paths")
    disallowed_paths = _as_str_list(worker_data, "disallowed_paths")
    worker_source_integrity = _as_bool(worker_data, "source_integrity")

    thought_chunks: int | None = None
    tool_events: int | None = None
    if isinstance(worker_data, dict):
        counters = worker_data.get("stream_counters")
        if isinstance(counters, dict):
            thought_chunks = _as_int(counters.get("thought_chunks"))
            tool_events = _as_int(counters.get("tool_events"))

    validator_status = _as_str(validator_data, "status")
    validator_profile = _as_str(validator_data, "profile")
    validator_source_integrity = _as_bool(validator_data, "source_integrity")

    reviewer_candidate = _as_str(review_data, "candidate")
    reviewer_model = _as_str(review_data, "model")
    reviewer_effort = _as_str(review_data, "effort")
    reviewer_verdict = _as_str(review_data, "verdict")
    reviewer_status = _as_str(review_data, "status")
    reviewer_duration = _as_float(review_data, "duration_seconds")
    reviewer_source_integrity = _as_bool(review_data, "source_integrity")
    reviewer_readonly_integrity = _as_bool(review_data, "reviewer_readonly_integrity")

    return TaskView(
        task_id=task_id,
        dir_label=dir_label,
        stage=stage,
        manifest_created_at=manifest_created_at,
        worker_candidate=worker_candidate,
        worker_model=worker_model,
        worker_effort=worker_effort,
        worker_status=worker_status,
        worker_duration=worker_duration,
        changed_paths=changed_paths,
        disallowed_paths=disallowed_paths,
        worker_source_integrity=worker_source_integrity,
        thought_chunks=thought_chunks,
        tool_events=tool_events,
        validator_status=validator_status,
        validator_profile=validator_profile,
        validator_source_integrity=validator_source_integrity,
        reviewer_candidate=reviewer_candidate,
        reviewer_model=reviewer_model,
        reviewer_effort=reviewer_effort,
        reviewer_verdict=reviewer_verdict,
        reviewer_status=reviewer_status,
        reviewer_duration=reviewer_duration,
        reviewer_source_integrity=reviewer_source_integrity,
        reviewer_readonly_integrity=reviewer_readonly_integrity,
        missing=tuple(missing),
        malformed=tuple(malformed),
    )


def build_snapshot(runs_root: Path) -> tuple[list[TaskView], list[str]]:
    runs_root = Path(runs_root).expanduser()
    try:
        if not runs_root.exists() or not runs_root.is_dir():
            return [], [f"runs_root missing or not a directory: {runs_root}"]
    except OSError as exc:
        return [], [f"runs_root unreadable: {type(exc).__name__}"]

    try:
        children = list(runs_root.iterdir())
    except OSError as exc:
        return [], [f"runs_root unreadable: {type(exc).__name__}"]

    child_dirs = sorted(
        (p for p in children if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name,
    )

    task_dirs: list[Path] = list(child_dirs)
    root_has_artifact = False
    for fname in ALLOWED_FILES:
        try:
            if (runs_root / fname).is_file():
                root_has_artifact = True
                break
        except OSError:
            continue
    if root_has_artifact and runs_root not in task_dirs:
        task_dirs.append(runs_root)

    views: list[TaskView] = []
    diagnostics: list[str] = []
    for task_dir in sorted(task_dirs, key=lambda p: p.name):
        try:
            views.append(build_task_view(task_dir, runs_root))
        except Exception as exc:
            label = task_dir.name or str(task_dir)
            diagnostics.append(f"{label}: {type(exc).__name__}")
            views.append(
                TaskView(
                    task_id=label,
                    dir_label=label,
                    stage="empty",
                    missing=tuple(ALLOWED_FILES),
                    diagnostic=type(exc).__name__,
                )
            )

    views.sort(key=lambda v: (v.task_id, v.dir_label))
    return views, diagnostics


def _integrity_label(value: bool | None) -> str | None:
    if value is True:
        return "PASS"
    if value is False:
        return "FAIL"
    return None


def _task_block_lines(tv: TaskView) -> list[str]:
    lines = [f"TASK {sanitize(tv.task_id)}"]
    lines.append(f"  path: {sanitize(tv.dir_label)}")
    lines.append(f"  stage: {sanitize(tv.stage)}")
    if tv.worker_candidate:
        lines.append(f"  worker.candidate: {sanitize(tv.worker_candidate)}")
    if tv.worker_model:
        lines.append(f"  worker.model: {sanitize(tv.worker_model)}")
    if tv.worker_effort:
        lines.append(f"  worker.effort: {sanitize(tv.worker_effort)}")
    if tv.worker_status:
        lines.append(f"  worker.status: {sanitize(tv.worker_status)}")
    if tv.worker_duration is not None:
        lines.append(f"  worker.duration_seconds: {tv.worker_duration:.3f}")
    if tv.changed_paths is not None:
        lines.append(f"  worker.changed_files: {len(tv.changed_paths)}")
        if tv.changed_paths:
            shown = [sanitize(p) for p in tv.changed_paths[:MAX_CHANGED_SHOWN]]
            lines.append(f"  worker.changed_paths: {', '.join(shown)}")
    if tv.disallowed_paths is not None:
        lines.append(f"  worker.disallowed_files: {len(tv.disallowed_paths)}")
    label = _integrity_label(tv.worker_source_integrity)
    if label:
        lines.append(f"  worker.source_integrity: {label}")
    if tv.thought_chunks is not None:
        lines.append(f"  worker.activity.thought_chunks: {tv.thought_chunks}")
    if tv.tool_events is not None:
        lines.append(f"  worker.activity.tool_events: {tv.tool_events}")
    if tv.validator_status:
        lines.append(f"  validator.status: {sanitize(tv.validator_status)}")
    if tv.validator_profile:
        lines.append(f"  validator.profile: {sanitize(tv.validator_profile)}")
    label = _integrity_label(tv.validator_source_integrity)
    if label:
        lines.append(f"  validator.source_integrity: {label}")
    if tv.reviewer_candidate:
        lines.append(f"  reviewer.candidate: {sanitize(tv.reviewer_candidate)}")
    if tv.reviewer_model:
        lines.append(f"  reviewer.model: {sanitize(tv.reviewer_model)}")
    if tv.reviewer_effort:
        lines.append(f"  reviewer.effort: {sanitize(tv.reviewer_effort)}")
    if tv.reviewer_verdict:
        lines.append(f"  reviewer.verdict: {sanitize(tv.reviewer_verdict)}")
    if tv.reviewer_status:
        lines.append(f"  reviewer.status: {sanitize(tv.reviewer_status)}")
    if tv.reviewer_duration is not None:
        lines.append(f"  reviewer.duration_seconds: {tv.reviewer_duration:.3f}")
    label = _integrity_label(tv.reviewer_source_integrity)
    if label:
        lines.append(f"  reviewer.source_integrity: {label}")
    label = _integrity_label(tv.reviewer_readonly_integrity)
    if label:
        lines.append(f"  reviewer.readonly_integrity: {label}")
    if tv.manifest_created_at:
        lines.append(f"  manifest.created_at: {sanitize(tv.manifest_created_at)}")
    if tv.missing:
        lines.append(f"  missing: {', '.join(tv.missing)}")
    if tv.malformed:
        lines.append(f"  malformed: {', '.join(tv.malformed)}")
    if tv.diagnostic:
        lines.append(f"  diagnostic: {sanitize(tv.diagnostic)}")
    return lines


def render_plain(
    tasks: list[TaskView], runs_root: Path, diagnostics: list[str]
) -> str:
    lines = [
        "JHO CONTROL CENTER (read-only) - one-shot",
        f"runs_root: {runs_root}",
        f"tasks: {len(tasks)}",
    ]
    if diagnostics:
        joined = "; ".join(sanitize(d) for d in diagnostics)
        lines.append(f"diagnostics: {joined}")
    else:
        lines.append("diagnostics: none")
    lines.append("")
    for tv in sorted(tasks, key=lambda v: (v.task_id, v.dir_label)):
        lines.extend(_task_block_lines(tv))
        lines.append("")
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    return text


def render_dashboard(
    tasks: list[TaskView],
    runs_root: Path,
    diagnostics: list[str],
    width: int | None = None,
) -> str:
    if width is None:
        try:
            width = shutil.get_terminal_size().columns
        except Exception:
            width = 80
    try:
        width = max(20, int(width))
    except Exception:
        width = 80
    lines = [
        "JHO CONTROL CENTER (read-only)",
        f"runs_root: {runs_root}  tasks: {len(tasks)}",
        "stages: created > worker > validation > review | empty = no artifacts",
    ]
    if diagnostics:
        joined = "; ".join(sanitize(d) for d in diagnostics)
        lines.append(f"diagnostics: {joined}")
    lines.append("")
    for tv in sorted(tasks, key=lambda v: (v.task_id, v.dir_label)):
        lines.extend(_task_block_lines(tv))
        lines.append("")
    lines.append("q quit  |  Ctrl-C interrupt  |  read-only observability")
    truncated = [line[:width] for line in lines]
    text = "\n".join(truncated)
    if not text.endswith("\n"):
        text += "\n"
    return text


def run_interactive(runs_root: Path, refresh: float) -> int:
    try:
        refresh = float(refresh)
    except Exception:
        refresh = 1.0
    if refresh <= 0:
        refresh = 1.0

    try:
        import termios
        import tty
    except Exception:
        termios = None  # type: ignore[assignment]
        tty = None  # type: ignore[assignment]

    is_tty = False
    try:
        is_tty = sys.stdin.isatty()
    except Exception:
        is_tty = False

    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    sys.stdout.flush()

    fd: int | None = None
    old_attrs: Any = None
    use_raw = False
    if is_tty and termios is not None and tty is not None:
        try:
            fd = sys.stdin.fileno()
            old_attrs = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            use_raw = True
        except Exception:
            use_raw = False
            fd = None
            old_attrs = None

    try:
        while True:
            tasks, diagnostics = build_snapshot(runs_root)
            frame = render_dashboard(tasks, runs_root, diagnostics)
            sys.stdout.write("\x1b[2J\x1b[H" + frame)
            sys.stdout.flush()
            try:
                ready, _, _ = select.select([sys.stdin], [], [], refresh)
            except (OSError, ValueError):
                try:
                    time.sleep(refresh)
                except KeyboardInterrupt:
                    return 130
                continue
            if not ready:
                continue
            try:
                ch = sys.stdin.read(1)
            except Exception:
                continue
            if not ch:
                try:
                    time.sleep(0.05)
                except KeyboardInterrupt:
                    return 130
                continue
            if ch.lower() == "q":
                return 0
            if not use_raw:
                try:
                    if ch != "\n":
                        sys.stdin.readline()
                except Exception:
                    pass
    except KeyboardInterrupt:
        return 130
    finally:
        try:
            if use_raw and fd is not None and old_attrs is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
        except Exception:
            pass
        try:
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
        except Exception:
            pass


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="jho-lite tui",
        description="Jho Control Center (read-only observability)",
    )
    parser.add_argument("--once", action="store_true", help="one-shot plain-text output")
    parser.add_argument("--runs-root", default=str(Path.home() / "agent-runs" / "jho-worktrees"))
    parser.add_argument("--refresh", type=float, default=1.0, help="interactive refresh seconds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runs_root = Path(args.runs_root).expanduser()
    if args.once:
        tasks, diagnostics = build_snapshot(runs_root)
        sys.stdout.write(render_plain(tasks, runs_root, diagnostics))
        return 0
    try:
        return run_interactive(runs_root, float(args.refresh))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
