"""Tests for jho_tui (read-only control center)."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))

import jho_tui


def write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def run_once(runs_root: Path) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = jho_tui.main(["--once", "--runs-root", str(runs_root)])
    return code, buf.getvalue()


def make_full_task(task_dir: Path) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    write_json(task_dir / "manifest.json", {"task_id": "TUI-001", "created_at": "2026-09-12T00:00:00+00:00"})
    write_json(
        task_dir / "worker_result.json",
        {
            "task_id": "TUI-001",
            "candidate": "opencode-go:deepseek-v4.1-flash@high",
            "status": "completed",
            "model": "deepseek-v4.1-flash",
            "effort": "high",
            "duration_seconds": 12.345,
            "changed_paths": ["A.py", "B.py"],
            "disallowed_paths": [],
            "source_integrity": True,
            "stream_counters": {"thought_chunks": 7, "tool_events": 3},
        },
    )
    write_json(
        task_dir / "validator_result.json",
        {"task_id": "TUI-001", "status": "PASS", "profile": "minimal", "source_integrity": True},
    )
    write_json(
        task_dir / "review_result.json",
        {
            "task_id": "TUI-001",
            "candidate": "codex:gpt-5.6-luna@max",
            "model": "gpt-5.6-luna",
            "effort": "max",
            "verdict": "APPROVE",
            "status": "completed",
            "duration_seconds": 30.0,
            "source_integrity": True,
            "reviewer_readonly_integrity": True,
        },
    )


def snapshot_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class TestJhoTui(unittest.TestCase):
    def test_once_no_ansi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_full_task(root / "task-a")
            code, out = run_once(root)
            self.assertEqual(code, 0)
            self.assertNotIn("\x1b", out)
            self.assertIn("tasks: 1", out)

    def test_stage_inference_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_full_task(root / "task-a")
            _, out = run_once(root)
            self.assertIn("stage: review", out)
            self.assertIn("APPROVE", out)

    def test_stage_inference_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task-a"
            task.mkdir(parents=True)
            write_json(task / "manifest.json", {"task_id": "ONLY-MANIFEST"})
            _, out = run_once(root)
            self.assertIn("stage: created", out)

    def test_stage_progression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task-a"
            task.mkdir(parents=True)
            write_json(task / "manifest.json", {"task_id": "PROG"})
            write_json(task / "worker_result.json", {"status": "completed"})
            _, out = run_once(root)
            self.assertIn("stage: worker", out)
            write_json(task / "validator_result.json", {"status": "PASS"})
            _, out2 = run_once(root)
            self.assertIn("stage: validation", out2)

    def test_malformed_json_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task-a"
            task.mkdir(parents=True)
            write_json(task / "manifest.json", {"task_id": "BAD"})
            (task / "worker_result.json").write_bytes(b"{not valid json!!!")
            code, out = run_once(root)
            self.assertEqual(code, 0)
            self.assertIn("malformed: worker_result.json", out)
            self.assertNotIn("\x1b", out)

    def test_missing_runs_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"
            code, out = run_once(missing)
            self.assertEqual(code, 0)
            self.assertIn("tasks: 0", out)
            self.assertIn("diagnostics:", out)
            self.assertNotEqual(out.splitlines()[3].strip(), "diagnostics: none")

    def test_no_cot_or_secret_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task-a"
            task.mkdir(parents=True)
            cot_sentinel = "COT_SENTINEL_ZZ9QW42"
            prompt_sentinel = "PROMPT_SENTINEL_ZZ9QW43"
            bearer = "Bearer TESTBEARERTOKEN_ABCDEF1234567890"
            jwt_like = "eyJhbGciOiJIUzI1NiJ9.SENTINEL_PAYLOAD.SENTINEL_SIG"
            write_json(task / "manifest.json", {"task_id": "PRIV"})
            write_json(
                task / "worker_result.json",
                {
                    "status": "completed",
                    "answer": cot_sentinel,
                    "task": prompt_sentinel,
                    "tool_summaries": [{"title": cot_sentinel}],
                    "stream_counters": {"thought_chunks": 2},
                    "error": bearer,
                },
            )
            write_json(
                task / "review_result.json",
                {"verdict": "APPROVE", "status": "completed", "answer": jwt_like},
            )
            _, out = run_once(root)
            self.assertNotIn(cot_sentinel, out)
            self.assertNotIn(prompt_sentinel, out)
            self.assertNotIn("TESTBEARERTOKEN_ABCDEF1234567890", out)
            self.assertNotIn("SENTINEL_PAYLOAD", out)
            self.assertNotIn("\x1b", out)

    def test_partial_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task-a"
            task.mkdir(parents=True)
            write_json(task / "worker_result.json", {"status": "completed"})
            code, out = run_once(root)
            self.assertEqual(code, 0)
            self.assertIn("worker.status: completed", out)
            self.assertNotIn("worker.candidate", out)
            self.assertNotIn("reviewer.verdict", out)

    def test_deterministic_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_full_task(root / "task-b")
            make_full_task(root / "task-a")
            _, first = run_once(root)
            _, second = run_once(root)
            self.assertEqual(first, second)

    def test_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_full_task(root / "task-a")
            before = snapshot_hashes(root)
            jho_tui.build_snapshot(root)
            _, _ = run_once(root)
            after = snapshot_hashes(root)
            self.assertEqual(before, after)

    def test_reads_only_allowlisted_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task-a"
            task.mkdir(parents=True)
            write_json(task / "manifest.json", {"task_id": "ALLOW"})
            (task / "result.json").write_text("DECOY_RESULT_SENTINEL_9911", encoding="utf-8")
            (task / "answer.md").write_text("DECOY_ANSWER_SENTINEL_9912", encoding="utf-8")
            _, out = run_once(root)
            self.assertNotIn("DECOY_RESULT_SENTINEL_9911", out)
            self.assertNotIn("DECOY_ANSWER_SENTINEL_9912", out)
            self.assertIn("stage: created", out)

    def test_changed_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task-a"
            task.mkdir(parents=True)
            write_json(task / "manifest.json", {"task_id": "CHG"})
            write_json(
                task / "worker_result.json",
                {"status": "completed", "changed_paths": ["x.py", "y.py", "z.py"]},
            )
            _, out = run_once(root)
            self.assertIn("worker.changed_files: 3", out)
            self.assertIn("x.py", out)


if __name__ == "__main__":
    unittest.main()
