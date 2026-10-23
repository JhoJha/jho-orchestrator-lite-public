from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "runner"
sys.path.insert(0, str(RUNNER))

import jho_git
import jho_operator
import jho_pipeline
import jho_safe
import jho_tui
import jho_validate
import jho_worker


WORKER = "opencode-go:deepseek-v4.1-flash@high"
REVIEWER = "codex:gpt-5.6-luna@max"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc


def _repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="jho-hotfix-git-"))
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "allowed").mkdir()
    (root / "allowed" / "one.txt").write_text("one\n", encoding="utf-8")
    (root / "allowed" / "two.txt").write_text("two\n", encoding="utf-8")
    (root / "outside.txt").write_text("outside\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    return root


def _contract(source: Path, task_id: str) -> dict:
    return {
        "task_id": task_id,
        "repo_path": str(source),
        "allowed_paths": ["allowed"],
        "task": "bounded hotfix regression",
        "worker_candidate": WORKER,
        "reviewer_candidate": REVIEWER,
        "validator_profile": "minimal",
        "security_review_required": False,
    }


def _pipeline_kwargs(worktree: Path, diff: list[str]):
    return {
        "create_worktree_fn": lambda _task, _repo: {
            "worktree": str(worktree),
            "source_head": "head",
        },
        "get_source_state_fn": lambda _repo: {"head": "head", "status": ""},
        "get_worktree_state_fn": lambda _repo: {
            "head": "head",
            "status": "M allowed/file.py" if diff else "",
        },
        "get_actual_diff_fn": lambda _repo: list(diff),
    }


async def _worker_completed(_args: dict):
    return {"exit_code": 0, "status": "completed", "changed_paths": []}


def _validator_pass(_args: dict):
    return {"exit_code": 0, "status": "PASS", "results": []}


async def _reviewer_approve(_args: dict):
    return {"exit_code": 0, "status": "completed", "verdict": "APPROVE"}


class TestB001MachineSafeGitStatus(unittest.TestCase):
    def _rename(self, origin: str, destination: str) -> tuple[list[str], tuple[bool, list[str]]]:
        repo = _repo()
        target = repo / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "mv", origin, destination)
        records = jho_git.status_records(repo)
        paths = jho_git.changed_paths(records)
        return paths, jho_pipeline._fit_allowlist(paths, ["allowed"])

    def test_staged_rename_outside_to_allowed_rejected_with_both_paths(self):
        paths, policy = self._rename("outside.txt", "allowed/inside.txt")
        self.assertEqual(set(paths), {"outside.txt", "allowed/inside.txt"})
        self.assertFalse(policy[0])

    def test_real_pipeline_rejects_outside_to_allowed_rename(self):
        worktree = _repo()
        _git(worktree, "mv", "outside.txt", "allowed/inside.txt")
        source = Path(tempfile.mkdtemp(prefix="jho-source-"))
        result = jho_pipeline.run(
            _contract(source, "HOTFIX-RENAME"),
            create_worktree_fn=lambda _task, _repo: {
                "worktree": str(worktree),
                "source_head": "head",
            },
            get_source_state_fn=lambda _repo: {"head": "head", "status": ""},
            get_worktree_state_fn=lambda _repo: {
                "head": "head",
                "status": "R outside.txt allowed/inside.txt",
            },
            worker_runner=_worker_completed,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(result["verdict"], "NEEDS_REWORK")
        self.assertEqual(result["failure_category"], "policy")
        self.assertEqual(set(result["changed_paths"]), {"outside.txt", "allowed/inside.txt"})

    def test_staged_rename_allowed_to_outside_rejected_with_both_paths(self):
        paths, policy = self._rename("allowed/one.txt", "moved.txt")
        self.assertEqual(set(paths), {"allowed/one.txt", "moved.txt"})
        self.assertFalse(policy[0])

    def test_staged_rename_allowed_to_allowed_is_permitted(self):
        paths, policy = self._rename("allowed/one.txt", "allowed/renamed.txt")
        self.assertEqual(set(paths), {"allowed/one.txt", "allowed/renamed.txt"})
        self.assertTrue(policy[0], policy[1])

    def test_literal_arrow_spaces_and_unicode_are_not_rename_syntax(self):
        repo = _repo()
        literal = "allowed/name -> spaced ü.txt"
        (repo / literal).write_text("new\n", encoding="utf-8")
        records = jho_git.status_records(repo)
        selected = [record for record in records if record["path"] == literal]
        self.assertEqual(len(selected), 1)
        self.assertIsNone(selected[0]["origin"])
        self.assertIn(literal, jho_worker.git_status_paths(repo))

    def test_modified_untracked_and_deleted_paths_still_work(self):
        repo = _repo()
        (repo / "allowed" / "one.txt").write_text("changed\n", encoding="utf-8")
        (repo / "allowed" / "two.txt").unlink()
        (repo / "allowed" / "new file ñ.txt").write_text("new\n", encoding="utf-8")
        paths = set(jho_git.changed_paths(jho_git.status_records(repo)))
        self.assertEqual(
            paths,
            {"allowed/one.txt", "allowed/two.txt", "allowed/new file ñ.txt"},
        )


class TestB002WorkerNoop(unittest.TestCase):
    def test_real_pipeline_classifies_completed_empty_diff_before_validator(self):
        source = Path(tempfile.mkdtemp(prefix="jho-source-"))
        worktree = Path(tempfile.mkdtemp(prefix="jho-worktree-"))
        called = {"validator": False, "reviewer": False}

        def validator(args):
            called["validator"] = True
            return _validator_pass(args)

        async def reviewer(args):
            called["reviewer"] = True
            return await _reviewer_approve(args)

        result = jho_pipeline.run(
            _contract(source, "HOTFIX-NOOP"),
            **_pipeline_kwargs(worktree, []),
            worker_runner=_worker_completed,
            validator_runner=validator,
            reviewer_runner=reviewer,
        )
        self.assertEqual(result["verdict"], "NEEDS_REWORK")
        self.assertEqual(result["failure_category"], "worker")
        self.assertIn("WORKER_NOOP", result["failure_detail"])
        self.assertEqual(result["changed_paths"], [])
        self.assertEqual(called, {"validator": False, "reviewer": False})
        self.assertEqual(
            jho_operator._next_action(result, noop_count=1, fallback_remaining=True),
            "RETRY_SAME",
        )
        self.assertEqual(
            jho_operator._next_action(result, noop_count=2, fallback_remaining=True),
            "ESCALATE",
        )
        self.assertEqual(
            jho_operator._next_action(result, noop_count=2, fallback_remaining=False),
            "HUMAN",
        )


class TestB003ValidatorAuthority(unittest.TestCase):
    def test_synthetic_token_is_not_in_validator_environment(self):
        with mock.patch.dict(
            os.environ,
            {"AUDIT_FAKE_TOKEN": "AUDIT_SYNTHETIC_ENV_TOKEN_123456789"},
            clear=False,
        ):
            result = jho_validate._run(
                [
                    sys.executable,
                    "-c",
                    "import os; print('AUDIT_FAKE_TOKEN' in os.environ)",
                ],
                cwd=ROOT,
                timeout=30,
            )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["stdout_tail"].strip(), "False")

    def test_validator_code_mutating_ignored_source_sentinel_is_detected(self):
        source = _repo()
        (source / ".gitignore").write_text("audit-sentinel.txt\n", encoding="utf-8")
        _git(source, "add", ".gitignore")
        _git(source, "commit", "-qm", "ignore sentinel")
        sentinel = source / "audit-sentinel.txt"
        sentinel.write_text("before\n", encoding="utf-8")
        before = jho_validate._ignored_source_fingerprint(source)
        result = jho_validate._run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path(r'%s').write_text('after\\n')" % sentinel,
            ],
            cwd=source,
            timeout=30,
        )
        after = jho_validate._ignored_source_fingerprint(source)
        self.assertEqual(result["status"], "PASS")
        self.assertNotEqual(before, after)

    def test_normal_runtime_command_works_with_minimal_environment(self):
        result = jho_validate._run(
            [sys.executable, "-c", "import pathlib,sys; print(pathlib.Path.cwd().name); sys.exit(0)"],
            cwd=ROOT,
            timeout=30,
        )
        self.assertEqual(result["status"], "PASS")
        self.assertIn(ROOT.name, result["stdout_tail"])
        self.assertIn(result["confinement"], {"bubblewrap-ro-host", "process-only"})

    def test_bubblewrap_prevents_write_outside_candidate_when_available(self):
        if not jho_validate._bwrap_usable():
            self.skipTest("bubblewrap unavailable")
        worktree = Path(tempfile.mkdtemp(prefix="jho-validator-wt-"))
        protected = ROOT / "AGENTS.md"
        before = protected.read_bytes()
        result = jho_validate._run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path(r'%s').write_text('mutated')" % protected,
            ],
            cwd=worktree,
            timeout=30,
        )
        self.assertEqual(result["confinement"], "bubblewrap-ro-host")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(protected.read_bytes(), before)


class TestB004ArtifactSanitization(unittest.TestCase):
    def test_persistent_result_writers_use_shared_safe_boundary(self):
        for module in (jho_worker, jho_validate):
            text = Path(module.__file__).read_text(encoding="utf-8")
            self.assertIn("jho_safe.write_json", text)
        review_text = (RUNNER / "jho_review.py").read_text(encoding="utf-8")
        self.assertIn("jho_safe.write_json", review_text)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"
            sentinel = "AUDIT_SYNTHETIC_PERSISTED_SECRET_123456"
            jho_safe.write_json(path, {"answer": f"secret={sentinel}"})
            self.assertNotIn(sentinel, path.read_text(encoding="utf-8"))

    def test_all_raw_artifact_secret_variants_are_absent_from_zip(self):
        sentinels = {
            "password": "AUDIT_SYNTHETIC_PASSWORD_123456",
            "passwd": "AUDIT_SYNTHETIC_PASSWD_123456",
            "secret": "AUDIT_SYNTHETIC_SECRET_123456",
            "api_key": "AUDIT_SYNTHETIC_API_KEY_123456",
            "api-key": "AUDIT_SYNTHETIC_API_DASH_KEY_123456",
            "apikey": "AUDIT_SYNTHETIC_APIKEY_123456",
            "client_secret": "AUDIT_SYNTHETIC_CLIENT_SECRET_123456",
            "client-secret": "AUDIT_SYNTHETIC_CLIENT_DASH_SECRET_123456",
            "access_token": "AUDIT_SYNTHETIC_ACCESS_TOKEN_123456",
            "access-token": "AUDIT_SYNTHETIC_ACCESS_DASH_TOKEN_123456",
        }
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            task_id = "HOTFIX-EVIDENCE-RUN-A01"
            run_id = "RUN-CURRENT"
            task = home / "agent-runs" / "jho-worktrees" / task_id
            task.mkdir(parents=True)
            raw = {
                "task_id": task_id,
                "run_id": run_id,
                "answer": sentinels,
                "stderr": "Authorization: Bearer AUDIT_SYNTHETIC_BEARER_123456",
                "stdout": "authorization: basic AUDIT_SYNTHETIC_BASIC_123456",
                "error": "PaSsWoRd=AUDIT_SYNTHETIC_CASE_PASSWORD_123456",
                "nested": [{"API-KEY": "AUDIT_SYNTHETIC_UPPER_API_KEY_123456"}],
            }
            for name in ("worker_result.json", "validator_result.json", "review_result.json"):
                (task / name).write_text(json.dumps(raw), encoding="utf-8")
            (task / "manifest.json").write_text(json.dumps({"task_id": task_id}), encoding="utf-8")
            with mock.patch.object(jho_operator.Path, "home", return_value=home):
                output = jho_operator.export_evidence(
                    logical_task_id="HOTFIX-EVIDENCE",
                    attempts=[{"task_id": task_id, "run_id": run_id, "attempt": 1}],
                    final_result={"task_id": task_id, "run_id": run_id, **raw},
                    notification={"reason": raw["stderr"]},
                    export_dir=Path(temp) / "exports",
                )
            with zipfile.ZipFile(output) as archive:
                content = "\n".join(
                    archive.read(name).decode("utf-8") for name in archive.namelist()
                )
            for sentinel in list(sentinels.values()) + [
                "AUDIT_SYNTHETIC_BEARER_123456",
                "AUDIT_SYNTHETIC_BASIC_123456",
                "AUDIT_SYNTHETIC_CASE_PASSWORD_123456",
                "AUDIT_SYNTHETIC_UPPER_API_KEY_123456",
            ]:
                self.assertNotIn(sentinel, content)
            self.assertIn("[REDACTED]", content)


class TestB005SecurityIdentity(unittest.TestCase):
    def setUp(self):
        self.expected = {
            "task_id": "TASK-1",
            "candidate": WORKER,
            "state_fingerprint": "a" * 64,
        }

    def test_bare_approval_fails(self):
        self.assertFalse(jho_pipeline._security_gate(True, {"approved": True}, None, self.expected)[0])

    def test_other_task_fails(self):
        identity = dict(self.expected, task_id="TASK-2")
        self.assertFalse(
            jho_pipeline._security_gate(True, {"approved": True, "identity": identity}, None, self.expected)[0]
        )

    def test_other_candidate_or_fingerprint_fails(self):
        for identity in (
            dict(self.expected, candidate=REVIEWER),
            dict(self.expected, state_fingerprint="b" * 64),
        ):
            self.assertFalse(
                jho_pipeline._security_gate(
                    True, {"approved": True, "identity": identity}, None, self.expected
                )[0]
            )

    def test_current_bound_approval_passes(self):
        approval = {"approved": True, "identity": dict(self.expected)}
        self.assertTrue(jho_pipeline._security_gate(True, approval, None, self.expected)[0])

    def test_current_hook_reject_has_precedence_over_stale_result(self):
        approval = {"approved": True, "identity": dict(self.expected)}
        accepted = jho_pipeline._security_gate(
            True, approval, lambda: {"approved": False}, self.expected
        )[0]
        self.assertFalse(accepted)


class TestB006TerminalMutation(unittest.TestCase):
    def test_artifact_writer_mutation_cannot_keep_ready(self):
        source = Path(tempfile.mkdtemp(prefix="jho-source-"))
        worktree = Path(tempfile.mkdtemp(prefix="jho-worktree-"))
        target = worktree / "allowed" / "file.py"
        target.parent.mkdir()
        target.write_text("validated\n", encoding="utf-8")

        def writer(result):
            if result.get("verdict") == "READY_FOR_HUMAN_REVIEW":
                target.write_text("mutated by observer\n", encoding="utf-8")

        result = jho_pipeline.run(
            _contract(source, "HOTFIX-OBSERVER"),
            **_pipeline_kwargs(worktree, ["allowed/file.py"]),
            worker_runner=_worker_completed,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
            artifact_writer=writer,
        )
        self.assertNotEqual(result["verdict"], "READY_FOR_HUMAN_REVIEW")
        self.assertIn("observer mutated", result["failure_detail"])

    def test_done_event_sink_mutation_cannot_keep_ready(self):
        source = Path(tempfile.mkdtemp(prefix="jho-source-"))
        worktree = Path(tempfile.mkdtemp(prefix="jho-worktree-"))
        target = worktree / "allowed" / "file.py"
        target.parent.mkdir()
        target.write_text("validated\n", encoding="utf-8")

        def event_sink(event):
            if event.get("phase") == "done":
                target.write_text("mutated by event sink\n", encoding="utf-8")

        result = jho_pipeline.run(
            _contract(source, "HOTFIX-EVENT-SINK"),
            **_pipeline_kwargs(worktree, ["allowed/file.py"]),
            worker_runner=_worker_completed,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
            event_sink=event_sink,
        )
        self.assertNotEqual(result["verdict"], "READY_FOR_HUMAN_REVIEW")
        self.assertIn("observer mutated", result["failure_detail"])


class TestM001ValidatorTimeout(unittest.TestCase):
    def test_pipeline_timeout_stops_as_infra_not_rework_contract(self):
        source = Path(tempfile.mkdtemp(prefix="jho-source-"))
        worktree = Path(tempfile.mkdtemp(prefix="jho-worktree-"))
        target = worktree / "allowed" / "file.py"
        target.parent.mkdir()
        target.write_text("candidate\n", encoding="utf-8")
        reviewer_called = {"value": False}

        async def reviewer(args):
            reviewer_called["value"] = True
            return await _reviewer_approve(args)

        def timeout(_args):
            return {
                "exit_code": 7,
                "status": "TIMEOUT",
                "results": [{"command": ["pytest"], "status": "TIMEOUT"}],
            }

        result = jho_pipeline.run(
            _contract(source, "HOTFIX-TIMEOUT"),
            **_pipeline_kwargs(worktree, ["allowed/file.py"]),
            worker_runner=_worker_completed,
            validator_runner=timeout,
            reviewer_runner=reviewer,
        )
        self.assertEqual(result["verdict"], "FAILED_INFRA")
        self.assertEqual(result["failure_category"], "timeout")
        self.assertEqual(
            jho_operator._next_action(result, noop_count=0, fallback_remaining=True),
            "STOP_INFRA",
        )
        self.assertFalse(reviewer_called["value"])

    def test_tooling_error_remains_distinct_from_functional_failure(self):
        raw = {"exit_code": 4, "status": "TOOLING_ERROR", "results": []}
        ok, payload, code, error = jho_pipeline._interpret_validator_out(raw)
        self.assertFalse(ok)
        self.assertEqual(error, "VALIDATOR_TOOLING")
        self.assertEqual(jho_pipeline._validator_failure_category(payload, code, error), "tooling")

    def test_timeout_with_partial_candidate_mutation_still_stops_infra(self):
        source = Path(tempfile.mkdtemp(prefix="jho-source-"))
        worktree = Path(tempfile.mkdtemp(prefix="jho-worktree-"))
        target = worktree / "allowed" / "file.py"
        target.parent.mkdir()
        target.write_text("candidate\n", encoding="utf-8")

        def timeout_after_mutation(_args):
            target.write_text("partial timeout mutation\n", encoding="utf-8")
            return {"exit_code": 7, "status": "TIMEOUT", "results": []}

        result = jho_pipeline.run(
            _contract(source, "HOTFIX-TIMEOUT-MUTATION"),
            **_pipeline_kwargs(worktree, ["allowed/file.py"]),
            worker_runner=_worker_completed,
            validator_runner=timeout_after_mutation,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(result["verdict"], "FAILED_INFRA")
        self.assertEqual(result["failure_category"], "timeout")
        self.assertEqual(
            jho_operator._next_action(result, noop_count=0, fallback_remaining=False),
            "STOP_INFRA",
        )


class TestM002Cwd(unittest.TestCase):
    def test_notification_loader_restores_caller_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            module = root / "jho_lite.py"
            changed = root / "changed"
            changed.mkdir()
            module.write_text(
                "import os\nos.chdir(%r)\ndef notify_windows_best_effort(**kw): return {}\n"
                % str(changed),
                encoding="utf-8",
            )
            before = Path.cwd()
            with mock.patch.object(jho_operator, "_active_runtime_candidates", return_value=[module]):
                loaded, _ = jho_operator._load_notify_module()
            self.assertIsNotNone(loaded)
            self.assertEqual(Path.cwd(), before)

    def test_no_notify_does_not_load_dynamic_module_or_change_cwd(self):
        before = Path.cwd()
        with mock.patch.object(jho_operator, "_load_notify_module", side_effect=AssertionError("loaded")):
            result = jho_operator.notify_result(
                {}, logical_task_id="TASK", candidate=WORKER, duration_seconds=0, enabled=False
            )
        self.assertFalse(result["attempted"])
        self.assertEqual(Path.cwd(), before)

    def test_relative_cli_paths_resolve_from_initial_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            initial = Path(temp)
            (initial / "repo").mkdir()
            (initial / "task.txt").write_text("relative task\n", encoding="utf-8")
            captured: dict[str, object] = {}

            def attempts(contract, **kwargs):
                captured["contract"] = contract
                return (
                    {"verdict": "READY_FOR_HUMAN_REVIEW", "failure_category": "none"},
                    [{"task_id": "attempt", "candidate": WORKER}],
                )

            def export(**kwargs):
                captured["export_dir"] = kwargs["export_dir"]
                return initial / "exports" / "evidence.zip"

            before = Path.cwd()
            try:
                os.chdir(initial)
                with mock.patch.object(jho_operator, "run_attempts", side_effect=attempts):
                    with mock.patch.object(jho_operator, "export_evidence", side_effect=export):
                        with contextlib.redirect_stdout(io.StringIO()):
                            code = jho_operator.main(
                                [
                                    "--task-id",
                                    "RELATIVE",
                                    "--repo",
                                    "repo",
                                    "--allowed-path",
                                    "allowed",
                                    "--task-file",
                                    "task.txt",
                                    "--candidate",
                                    WORKER,
                                    "--export-dir",
                                    "exports",
                                    "--no-notify",
                                ]
                            )
                self.assertEqual(code, 0)
                contract = captured["contract"]
                self.assertEqual(contract["repo_path"], str((initial / "repo").resolve()))
                self.assertEqual(contract["task"], "relative task\n")
                self.assertEqual(captured["export_dir"], initial / "exports")
                self.assertEqual(Path.cwd(), initial)
            finally:
                os.chdir(before)


class TestM003RunEvidenceIdentity(unittest.TestCase):
    def test_long_logical_ids_keep_unique_run_and_attempt_suffixes(self):
        logical = "L" * 128
        first = jho_operator._attempt_task_id(logical, 1, "RUN-ONE")
        second = jho_operator._attempt_task_id(logical, 2, "RUN-TWO")
        self.assertLessEqual(len(first), 96)
        self.assertNotEqual(first, second)
        self.assertIn("RUN-ONE", first)
        self.assertIn("RUN-TWO", second)
        self.assertNotIn(" ", jho_operator._attempt_task_id("task with spaces", 1, "RUN"))

    def test_old_run_artifact_is_not_exported_and_names_do_not_collide(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / "home"
            task_id = "TASK-RCURRENT-A01"
            task = home / "agent-runs" / "jho-worktrees" / task_id
            task.mkdir(parents=True)
            (task / "worker_result.json").write_text(
                json.dumps({"task_id": task_id, "run_id": "OLD_RUN", "answer": "OLD_RUN"}),
                encoding="utf-8",
            )
            args = dict(
                logical_task_id="TASK",
                attempts=[{"task_id": task_id, "run_id": "CURRENT_RUN", "attempt": 1}],
                final_result={"task_id": task_id, "run_id": "CURRENT_RUN", "verdict": "FAILED_INFRA"},
                notification={},
                export_dir=Path(temp) / "exports",
            )
            with mock.patch.object(jho_operator.Path, "home", return_value=home):
                first = jho_operator.export_evidence(**args)
                second = jho_operator.export_evidence(**args)
            self.assertNotEqual(first, second)
            with zipfile.ZipFile(first) as archive:
                self.assertNotIn(
                    f"attempts/{task_id}/worker_result.json", archive.namelist()
                )
                content = "\n".join(
                    archive.read(name).decode("utf-8") for name in archive.namelist()
                )
            self.assertNotIn("OLD_RUN", content)


class TestM004Harness(unittest.TestCase):
    def test_pipeline_import_is_current_hotfix_repo(self):
        self.assertEqual(Path(jho_pipeline.__file__).resolve(), ROOT / "runner" / "jho_pipeline.py")
        self.assertEqual(
            Path(jho_operator.jho_pipeline.__file__).resolve(), ROOT / "runner" / "jho_pipeline.py"
        )

    def test_every_defined_test_method_is_discovered_and_guard_is_last(self):
        defined = 0
        max_test_line = 0
        guard_lines: list[int] = []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                            defined += 1
                            max_test_line = max(max_test_line, item.lineno)
                if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test):
                    guard_lines.append(node.lineno)
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")

        def count(item):
            return sum(count(child) if isinstance(child, unittest.TestSuite) else 1 for child in item)

        self.assertEqual(count(suite), defined)
        pipeline_tree = ast.parse((ROOT / "tests" / "test_jho_pipeline.py").read_text(encoding="utf-8"))
        pipeline_guard = [
            node.lineno
            for node in pipeline_tree.body
            if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test)
        ]
        pipeline_tests = [
            item.lineno
            for node in pipeline_tree.body
            if isinstance(node, ast.ClassDef)
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test")
        ]
        self.assertEqual(len(pipeline_guard), 1)
        self.assertGreater(pipeline_guard[0], max(pipeline_tests))


class TestN001AndN002(unittest.TestCase):
    def test_tui_redacts_common_notification_forms(self):
        text = " ".join(
            [
                "password=AUDIT_SYNTHETIC_PASSWORD_999",
                "api-key: AUDIT_SYNTHETIC_API_KEY_999",
                "Authorization: Basic AUDIT_SYNTHETIC_BASIC_999",
                "Bearer AUDIT_SYNTHETIC_BEARER_999",
                "token=AUDIT_SYNTHETIC_TOKEN_999",
                "secret=AUDIT_SYNTHETIC_SECRET_999",
            ]
        )
        clean = jho_tui.sanitize(text)
        self.assertIn("[REDACTED]", clean)
        self.assertNotIn("AUDIT_SYNTHETIC_", clean)

    def test_export_default_honors_explicit_override(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(os.environ, {"JHO_EXPORT_DIR": temp}):
                self.assertEqual(jho_operator._default_export_dir(), Path(temp))


if __name__ == "__main__":
    unittest.main()
