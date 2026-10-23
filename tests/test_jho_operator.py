from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RUNNER = Path(__file__).resolve().parents[1] / "runner"
sys.path.insert(0, str(RUNNER))

_IMPORT_CWD = Path.cwd()
import jho_operator


def ready():
    return {
        "verdict": "READY_FOR_HUMAN_REVIEW",
        "failure_category": "none",
        "changed_paths": ["a.py"],
    }


def noop():
    return {
        "verdict": "NEEDS_REWORK",
        "failure_category": "worker",
        "changed_paths": [],
    }


class TestJhoOperator(unittest.TestCase):
    def base(self):
        return {
            "task_id": "OP-1",
            "repo_path": "/tmp/repo",
            "allowed_paths": ["a.py"],
            "task": "bounded task",
            "worker_candidate": "codex:gpt-5.6-sol@high",
            "reviewer_candidate": "codex:gpt-5.6-luna@max",
            "validator_profile": "minimal",
        }

    def test_operator_import_preserves_caller_cwd(self):
        self.assertEqual(Path.cwd(), _IMPORT_CWD)

    def test_runtime_composition_is_bound(self):
        self.assertIsNotNone(jho_operator.jho_pipeline._worktree_mod)
        self.assertIsNotNone(jho_operator.jho_pipeline._worker_mod)
        self.assertIsNotNone(jho_operator.jho_pipeline._validate_mod)
        self.assertIsNotNone(jho_operator.jho_pipeline._review_mod)
        self.assertTrue(
            Path(jho_operator.jho_pipeline.__file__).resolve()
            == RUNNER / "jho_pipeline.py"
        )
        # Provider-free tests remain importable without protoAgent. Real default
        # execution still requires the explicit pin before invoking ACP seams.

    def test_ready_stops_first_attempt(self):
        with mock.patch.object(jho_operator.jho_pipeline, "run", return_value=ready()) as run:
            final, history = jho_operator.run_attempts(self.base(), fallbacks=[], max_attempts=3)
        self.assertEqual(final["verdict"], "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(history[-1]["next_action"], "HUMAN_GATE")

    def test_one_worker_noop_retries_same_candidate(self):
        with mock.patch.object(jho_operator.jho_pipeline, "run", side_effect=[noop(), ready()]) as run:
            final, history = jho_operator.run_attempts(self.base(), fallbacks=[], max_attempts=2)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(history[0]["next_action"], "RETRY_SAME")
        self.assertEqual(history[0]["candidate"], history[1]["candidate"])
        self.assertEqual(final["verdict"], "READY_FOR_HUMAN_REVIEW")

    def test_second_noop_escalates_when_fallback_exists(self):
        fallback = "opencode-go:muse-spark-1.3-contributor@xhigh"
        with mock.patch.object(
            jho_operator.jho_pipeline, "run", side_effect=[noop(), noop(), ready()]
        ) as run:
            final, history = jho_operator.run_attempts(
                self.base(), fallbacks=[fallback], max_attempts=3
            )
        self.assertEqual(run.call_count, 3)
        self.assertEqual(history[1]["next_action"], "ESCALATE")
        self.assertEqual(history[2]["candidate"], fallback)
        self.assertEqual(final["verdict"], "READY_FOR_HUMAN_REVIEW")

    def test_quota_escalates_without_retrying_same(self):
        quota = {
            "verdict": "FAILED_INFRA",
            "failure_category": "quota",
            "failure_detail": "provider quota",
            "changed_paths": [],
        }
        fallback = "codex:gpt-5.6-luna@high"
        with mock.patch.object(jho_operator.jho_pipeline, "run", side_effect=[quota, ready()]):
            final, history = jho_operator.run_attempts(
                self.base(), fallbacks=[fallback], max_attempts=2
            )
        self.assertEqual(history[0]["next_action"], "ESCALATE")
        self.assertEqual(history[1]["candidate"], fallback)
        self.assertEqual(final["verdict"], "READY_FOR_HUMAN_REVIEW")

    def test_validator_rework_does_not_auto_retry(self):
        bad = {
            "verdict": "NEEDS_REWORK",
            "failure_category": "validator",
            "changed_paths": ["a.py"],
        }
        with mock.patch.object(jho_operator.jho_pipeline, "run", return_value=bad) as run:
            final, history = jho_operator.run_attempts(self.base(), fallbacks=[], max_attempts=3)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(history[-1]["next_action"], "REWORK_CONTRACT")
        self.assertEqual(final["failure_category"], "validator")

    def test_infra_does_not_consume_blind_retry(self):
        bad = {
            "verdict": "FAILED_INFRA",
            "failure_category": "infra",
            "changed_paths": [],
        }
        with mock.patch.object(jho_operator.jho_pipeline, "run", return_value=bad) as run:
            _, history = jho_operator.run_attempts(self.base(), fallbacks=[], max_attempts=3)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(history[-1]["next_action"], "STOP_INFRA")

    def test_notification_mapping(self):
        self.assertEqual(
            jho_operator._notification_status({"verdict": "READY_FOR_HUMAN_REVIEW"}),
            ("completed", True),
        )
        self.assertEqual(
            jho_operator._notification_status({"verdict": "NEEDS_HUMAN"}),
            ("waiting_input", True),
        )
        self.assertEqual(
            jho_operator._notification_status({
                "verdict": "FAILED_INFRA",
                "failure_detail": "integrity mismatch",
            }),
            ("integrity_breach", False),
        )

    def test_export_contains_only_safe_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            task_dir = fake_home / "agent-runs/jho-worktrees/OP-1"
            task_dir.mkdir(parents=True)
            (task_dir / "manifest.json").write_text('{"task_id":"OP-1"}\n', encoding="utf-8")
            (task_dir / "secret.txt").write_text("do not export", encoding="utf-8")
            with mock.patch.object(jho_operator.Path, "home", return_value=fake_home):
                out = jho_operator.export_evidence(
                    logical_task_id="OP-1",
                    attempts=[{"task_id":"OP-1","attempt":1,"candidate":"x"}],
                    final_result=ready(),
                    notification={"success":True},
                    export_dir=Path(tmp)/"out",
                )
            import zipfile
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
            self.assertIn("attempts/OP-1/manifest.json", names)
            self.assertNotIn("attempts/OP-1/secret.txt", names)


if __name__ == "__main__":
    unittest.main()
