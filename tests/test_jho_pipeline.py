#!/usr/bin/env python3
"""Deterministic provider-free regression tests for Operational v1 coordinator."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[1]
_RUNNER = _REPO_ROOT / "runner"
for _p in (str(_RUNNER), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import jho_pipeline as pipeline
except Exception:
    from local_jho.runner import jho_pipeline as pipeline  # type: ignore


WORKER_CAND = "opencode-go:deepseek-v4.1-flash@high"
REVIEWER_CAND = "codex:gpt-5.6-luna@max"


def _mk_repo() -> str:
    d = tempfile.mkdtemp(prefix="jho-pipe-repo-")
    return d


def _mk_wt() -> str:
    d = tempfile.mkdtemp(prefix="jho-pipe-wt-")
    return d


def _mk_real_git_repo() -> str:
    d = tempfile.mkdtemp(prefix="jho-real-repo-")
    def _run(args: list[str]) -> None:
        p = subprocess.run(args, cwd=d, text=True, capture_output=True, timeout=30)
        if p.returncode != 0:
            raise RuntimeError(f"git setup failed {' '.join(args)}: {(p.stderr or p.stdout)[:800]}")
    _run(["git", "init"])
    _run(["git", "config", "user.email", "test@example.com"])
    _run(["git", "config", "user.name", "Test"])
    (Path(d) / "README.md").write_text("# test repo\n", encoding="utf-8")
    (Path(d) / "allowed").mkdir(parents=True, exist_ok=True)
    (Path(d) / "allowed" / "initial.py").write_text("x = 1\n", encoding="utf-8")
    _run(["git", "add", "."])
    _run(["git", "commit", "-m", "initial"])
    return d


def _base_contract(repo: str, task_id: str = "t-happy-001") -> dict:
    return {
        "task_id": task_id,
        "repo_path": repo,
        "allowed_paths": ["allowed"],
        "task": "Fix the widget under allowed/ with smallest change.",
        "worker_candidate": WORKER_CAND,
        "reviewer_candidate": REVIEWER_CAND,
        "validator_profile": "backend",
        "security_review_required": False,
        "timeout": 60,
    }


def _src_fn(head: str = "abc123head"):
    def _f(_repo: str):
        return {"head": head, "status": ""}
    return _f


def _wt_state_fn(head: str = "abc123head", status: str = ""):
    def _f(_wt: str):
        return {"head": head, "status": status}
    return _f


def _diff_fn(paths):
    state = {"n": 0}

    def _f(_wt: str):
        state["n"] += 1
        if callable(paths):
            return paths(state["n"])
        return list(paths)
    _f.calls = state  # type: ignore
    return _f


def _create_fn(wt_path: str):
    def _f(task_id: str, _repo):
        Path(wt_path).mkdir(parents=True, exist_ok=True)
        return {"worktree": wt_path, "source_head": "abc123head"}
    return _f


async def _worker_ok(args: dict):
    return {"exit_code": 0, "status": "completed", "changed_paths": ["allowed/file.py"], "error": None}


async def _worker_report_allowed_but_actual_differs(args: dict):
    return {"exit_code": 0, "status": "completed", "changed_paths": ["allowed/file.py"], "error": None}


def _validator_pass(args: dict):
    return {"exit_code": 0, "status": "PASS", "results": [{"command": ["pytest"], "status": "PASS"}]}


def _validator_fail(args: dict):
    return {"exit_code": 6, "status": "FAIL", "results": [{"command": ["pytest"], "status": "FAIL"}]}


async def _reviewer_approve(args: dict):
    return {"exit_code": 0, "status": "completed", "verdict": "APPROVE", "error": None}


async def _reviewer_rework(args: dict):
    return {"exit_code": 6, "status": "completed", "verdict": "REWORK", "error": "needs changes"}


def _security_approve_current(identity: dict):
    return {"approved": True, "identity": identity, "reviewer": "synthetic-security-test"}


def _run_sync(contract: dict, **kw):
    return pipeline.run(contract, **kw)


class TestHappyPath(unittest.TestCase):
    def test_happy_path_sequence(self):
        repo = _mk_repo()
        wt = _mk_wt()
        contract = _base_contract(repo, task_id="t-happy-seq-01")
        order: list[str] = []

        async def worker(args: dict):
            order.append("worker")
            return await _worker_ok(args)

        def validator(args: dict):
            order.append("validator")
            return _validator_pass(args)

        async def reviewer(args: dict):
            order.append("reviewer")
            return await _reviewer_approve(args)

        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=worker,
            validator_runner=validator,
            reviewer_runner=reviewer,
        )
        self.assertEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(order, ["worker", "validator", "reviewer"])
        self.assertIn("allowed/file.py", res.get("changed_paths") or [])
        self.assertTrue(isinstance(res.get("events"), list) and len(res["events"]) >= 4)
        self.assertEqual((res.get("validator_result") or {}).get("status"), "PASS")
        self.assertEqual((res.get("reviewer_result") or {}).get("verdict"), "APPROVE")

    def test_candidates_may_differ(self):
        repo = _mk_repo()
        wt = _mk_wt()
        contract = _base_contract(repo, task_id="t-diff-cand-01")
        contract["worker_candidate"] = WORKER_CAND
        contract["reviewer_candidate"] = REVIEWER_CAND
        self.assertNotEqual(contract["worker_candidate"], contract["reviewer_candidate"])
        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
            security_hook=_security_approve_current,
        )
        self.assertEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res.get("worker_candidate"), WORKER_CAND)
        self.assertEqual(res.get("reviewer_candidate"), REVIEWER_CAND)


class TestInvalidContracts(unittest.TestCase):
    def _base(self):
        return _base_contract(_mk_repo(), task_id="t-invalid-01")

    def test_none_nonmapping_missing(self):
        for bad in (None, "string", 123, ["list"], []):
            res = _run_sync(bad)  # type: ignore
            self.assertEqual(res.get("verdict"), "FAILED_INFRA", f"bad={bad!r}")
        res = _run_sync({})
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")
        for key in ("task_id", "repo_path", "allowed_paths", "task", "worker_candidate"):
            c = self._base()
            c.pop(key, None)
            res = _run_sync(c)
            self.assertEqual(res.get("verdict"), "FAILED_INFRA", f"missing {key}")

    def test_invalid_candidates(self):
        for bad_cand in ("", "nope", "poolonly:", "pool:model", "pool:model@", ":model@effort", "a:b@c:d@e", 123, None):
            c = self._base()
            c["worker_candidate"] = bad_cand  # type: ignore
            res = _run_sync(c)
            self.assertEqual(res.get("verdict"), "FAILED_INFRA", f"cand={bad_cand!r}")
        c = self._base()
        c["reviewer_candidate"] = "bad-candidate"
        res = _run_sync(c)
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")

    def test_unsafe_allowed_paths(self):
        for bad_allow in (["/abs/path"], ["../escape"], [["nested"]], "allowed", [], [""], ["/"], None):
            c = self._base()
            c["allowed_paths"] = bad_allow  # type: ignore
            res = _run_sync(c)
            self.assertEqual(res.get("verdict"), "FAILED_INFRA", f"allow={bad_allow!r}")
        c = self._base()
        c["allowed_paths"] = ["allowed", "/etc/passwd"]
        res = _run_sync(c)
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")

    def test_malformed_boolean_fails_closed(self):
        for bad_bool in ("maybe", "2", "yes-ish", 123, 1, 0, [], {}):
            c = self._base()
            c["security_review_required"] = bad_bool  # type: ignore
            res = _run_sync(c)
            self.assertEqual(res.get("verdict"), "FAILED_INFRA", f"bool={bad_bool!r}")

    def test_no_exception_escapes(self):
        try:
            res = pipeline.run(None)  # type: ignore
        except Exception as exc:  # pragma: no cover
            self.fail(f"exception escaped: {exc}")
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")


class TestBooleanGate(unittest.TestCase):
    def test_false_string_does_not_pass_gate(self):
        repo = _mk_repo()
        wt = _mk_wt()
        contract = _base_contract(repo, task_id="t-bool-false-01")
        contract["security_review_required"] = "false"  # type: ignore
        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )
        self.assertNotEqual(res.get("verdict"), "NEEDS_HUMAN")
        self.assertIn(res.get("verdict"), ("READY_FOR_HUMAN_REVIEW", "FAILED_INFRA"))

    def test_false_approval_does_not_pass(self):
        repo = _mk_repo()
        wt = _mk_wt()
        contract = _base_contract(repo, task_id="t-bool-approve-01")
        contract["security_review_required"] = True
        contract["security_review_result"] = {"approved": "false"}  # type: ignore
        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res.get("verdict"), "NEEDS_HUMAN")

    def test_true_string_does_not_fabricate_approval(self):
        repo = _mk_repo()
        wt = _mk_wt()
        contract = _base_contract(repo, task_id="t-bool-true-str-01")
        contract["security_review_required"] = True
        contract["security_review_result"] = {"approved": "true"}  # type: ignore
        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res.get("verdict"), "NEEDS_HUMAN")


class TestValidatorReviewerSecurity(unittest.TestCase):
    def test_validator_fail_needs_rework(self):
        repo = _mk_repo()
        wt = _mk_wt()
        contract = _base_contract(repo, task_id="t-val-fail-01")
        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_fail,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res.get("verdict"), "NEEDS_REWORK")
        self.assertEqual(res.get("failure_category"), "validator")

    def test_reviewer_rework_needs_rework(self):
        repo = _mk_repo()
        wt = _mk_wt()
        contract = _base_contract(repo, task_id="t-rev-rework-01")
        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_rework,
        )
        self.assertEqual(res.get("verdict"), "NEEDS_REWORK")
        self.assertEqual(res.get("failure_category"), "reviewer")

    def test_security_required_without_review_needs_human(self):
        repo = _mk_repo()
        wt = _mk_wt()
        contract = _base_contract(repo, task_id="t-sec-missing-01")
        contract["security_review_required"] = True
        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res.get("verdict"), "NEEDS_HUMAN")

    def test_bare_security_approved_true_fails_closed(self):
        repo = _mk_repo()
        wt = _mk_wt()
        contract = _base_contract(repo, task_id="t-sec-ok-01")
        contract["security_review_required"] = True
        contract["security_review_result"] = {"approved": True, "reviewer": "sec-team"}
        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res.get("verdict"), "NEEDS_HUMAN")


class TestNoAutoOpsNoHardcode(unittest.TestCase):
    def test_no_product_hardcode(self):
        text = Path(pipeline.__file__).read_text(encoding="utf-8")
        self.assertNotIn("client-product", text)

    def test_no_auto_history_ops(self):
        text = Path(pipeline.__file__).read_text(encoding="utf-8").lower()
        self.assertNotIn('"commit"', text)
        self.assertNotIn("'commit'", text)
        self.assertNotIn('"push"', text)
        self.assertNotIn("'push'", text)
        self.assertNotIn('"merge"', text)
        self.assertNotIn("'merge'", text)
        self.assertNotIn('"deploy"', text)
        self.assertNotIn("'deploy'", text)
        self.assertNotIn("git commit", text)
        self.assertNotIn("git push", text)
        self.assertNotIn("git merge", text)

    def test_explicit_repo_required(self):
        c = _base_contract(_mk_repo(), task_id="t-explicit-01")
        c.pop("repo_path", None)
        c.pop("repo", None)
        res = _run_sync(c)
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")


class TestRedaction(unittest.TestCase):
    def test_recursive_secret_prompt_redaction(self):
        repo = _mk_repo()
        wt = _mk_wt()
        sk = "sk-AUDIT_SYNTHETIC_1234567890abcdef1234567890"
        gh = "ghp_AUDIT_SYNTHETIC_1234567890abcdef1234567890ABCD"
        nv = "nvapi-AUDIT_SYNTHETIC_1234567890abcdef1234567890"
        bearer = "Bearer abcdef1234567890ABCDEF1234567890"
        contract = _base_contract(repo, task_id=f"t-redact-01 {sk}")
        contract["allowed_paths"] = ["allowed"]
        artifacts: list[dict] = []

        def writer(data: dict):
            artifacts.append(data)

        async def worker_with_secrets(args: dict):
            return {
                "exit_code": 0,
                "status": "completed",
                "changed_paths": ["allowed/file.py"],
                "error": f"worker note {gh}",
                "extra": {"nested": {"inner": f"key {nv} and {bearer}", "prompt": "should be hidden", "thought": "hidden too"}},
                "answer": f"raw answer with {sk}",
            }

        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=worker_with_secrets,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
            artifact_writer=writer,
        )
        dumped = json.dumps(res, ensure_ascii=False)
        for secret in (sk, gh, nv, "abcdef1234567890ABCDEF1234567890"):
            self.assertNotIn(secret, dumped, f"secret leaked: {secret[:8]}")
        self.assertIn("[REDACTED]", dumped)
        self.assertNotIn(sk, str(res.get("task_id")))
        wdump = json.dumps(res.get("worker_result") or {}, ensure_ascii=False)
        self.assertNotIn(gh, wdump)
        self.assertNotIn(nv, wdump)
        self.assertNotIn(sk, wdump)
        self.assertTrue(len(artifacts) >= 1)
        adump = json.dumps(artifacts[-1], ensure_ascii=False)
        for secret in (sk, gh, nv):
            self.assertNotIn(secret, adump)

    def test_sanitize_value_recursive(self):
        sample = {
            "task_id": "t1 sk-AUDIT_SYNTHETIC_AAAAAAAAAAAAAAAA1234567890",
            "nested": {"extra": {"token": "ghp_AUDIT_SYNTHETIC_BBBBBBBBBBBBBBBBBBBBBBBBBB1234", "prompt": "hello"}},
            "changed_paths": ["allowed/nvapi-AUDIT_SYNTHETIC_CCCCCCCCCCCCCCCCCCCCCCCCCCCCCC.py"],
            "events": [{"detail": "Bearer DDDDDDDDDDDDDDDDDDDDDDDDDDDDDD", "extra": {}}],
        }
        out = pipeline.sanitize_value(sample)
        dumped = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("sk-AUDIT_SYNTHETIC_AAAAA", dumped)
        self.assertNotIn("ghp_AUDIT_SYNTHETIC_BBBBB", dumped)
        self.assertNotIn("nvapi-AUDIT_SYNTHETIC_CCCC", dumped)
        self.assertNotIn("Bearer DDDDD", dumped)
        self.assertIn("[REDACTED]", dumped)
        self.assertEqual(out["nested"]["extra"]["prompt"], "[REDACTED]")


class TestRealDiffAuthoritative(unittest.TestCase):
    def test_worker_report_not_trusted(self):
        repo = _mk_repo()
        wt = _mk_wt()
        contract = _base_contract(repo, task_id="t-diff-auth-01")
        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M outside/evil.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py", "outside/evil.py"]),
            worker_runner=_worker_report_allowed_but_actual_differs,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
            security_hook=_security_approve_current,
        )
        self.assertIn(res.get("verdict"), ("NEEDS_REWORK", "FAILED_INFRA"))
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")

    def test_validator_mutation_rejected(self):
        repo = _mk_repo()
        wt = _mk_wt()
        target = Path(wt) / "allowed" / "file.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("before-validator\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-val-mut-01")

        def validator_mutate(args: dict):
            target.write_text("after-validator-inplace\n", encoding="utf-8")
            return _validator_pass(args)

        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=validator_mutate,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res.get("verdict"), "NEEDS_REWORK")
        self.assertEqual(res.get("failure_category"), "validator")

    def test_reviewer_mutation_rejected(self):
        repo = _mk_repo()
        wt = _mk_wt()
        target = Path(wt) / "allowed" / "file.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("before-reviewer\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-rev-mut-01")

        async def reviewer_mutate(args: dict):
            target.write_text("after-reviewer-inplace\n", encoding="utf-8")
            return await _reviewer_approve(args)

        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=reviewer_mutate,
        )
        self.assertEqual(res.get("verdict"), "NEEDS_REWORK")
        self.assertEqual(res.get("failure_category"), "reviewer")


class TestContentIntegrityF1(unittest.TestCase):
    def test_validator_inplace_edit_same_paths_detected(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("v1\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-f1-val-inplace-01")

        def validator_mutate(args: dict):
            (Path(wt) / "allowed" / "file.py").write_text("v2-inplace-edit\n", encoding="utf-8")
            return _validator_pass(args)

        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=validator_mutate,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res.get("verdict"), "NEEDS_REWORK")
        self.assertEqual(res.get("failure_category"), "validator")

    def test_reviewer_inplace_edit_same_status_detected(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("v1\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-f1-rev-inplace-01")

        async def reviewer_mutate(args: dict):
            (Path(wt) / "allowed" / "file.py").write_text("v3-reviewer-edit\n", encoding="utf-8")
            return await _reviewer_approve(args)

        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=reviewer_mutate,
        )
        self.assertEqual(res.get("verdict"), "NEEDS_REWORK")
        self.assertEqual(res.get("failure_category"), "reviewer")

    def test_validator_deletion_same_diff_list_detected(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("v1\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-f1-val-del-01")

        def validator_delete(args: dict):
            try:
                (Path(wt) / "allowed" / "file.py").unlink()
            except Exception:
                pass
            return _validator_pass(args)

        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=validator_delete,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res.get("verdict"), "NEEDS_REWORK")
        self.assertEqual(res.get("failure_category"), "validator")

    def test_symlink_swap_same_path_detected(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        target = Path(wt) / "allowed" / "file.py"
        target.write_text("v1\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-f1-symlink-01")

        def validator_symlink(args: dict):
            try:
                target.unlink()
            except Exception:
                pass
            try:
                os.symlink("/tmp/other-target", str(target))
            except Exception:
                target.write_text("symlink-fallback-different-content\n", encoding="utf-8")
            return _validator_pass(args)

        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=validator_symlink,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res.get("verdict"), "NEEDS_REWORK")
        self.assertEqual(res.get("failure_category"), "validator")

    def test_no_mutation_with_real_file_passes(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-f1-stable-01")
        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")

    def test_security_gate_final_source_recheck(self):
        repo = _mk_repo()
        wt = _mk_wt()
        contract = _base_contract(repo, task_id="t-f1-sec-final-01")
        contract["security_review_required"] = True
        contract["security_review_result"] = {"approved": True}
        src_calls = {"n": 0}
        base_src = _src_fn()

        def src_seq(_repo: str):
            src_calls["n"] += 1
            if src_calls["n"] <= 4:
                return base_src(_repo)
            return {"head": "different-head-after-gate", "status": ""}

        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=src_seq,
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
            security_hook=_security_approve_current,
        )
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")


class TestSanitizationF2(unittest.TestCase):
    def test_sensitive_keys_redacted_by_key_without_pattern(self):
        sample = {
            "token": "plain-value-no-equals",
            "access_token": "abc123",
            "api_key": "mykey",
            "apikey": "x",
            "secret": "s",
            "password": "p",
            "authorization": "authval",
            "cookie": "c",
            "stdout": "benign output",
            "stderr": "benign err",
            "provider": "prov-data",
            "raw": "rawdata",
            "prompt": "hello",
            "reasoning": "r",
            "thought": "t",
            "transcript": "tr",
            "completion": "c",
            "answer": "a",
            "reply": "r",
            "CoT": "chain",
        }
        out = pipeline.sanitize_value(sample)
        for k in sample.keys():
            self.assertEqual(out[k], "[REDACTED]", f"key {k} not redacted")

    def test_stdout_stderr_keys_redacted_in_result(self):
        repo = _mk_repo()
        wt = _mk_wt()
        contract = _base_contract(repo, task_id="t-f2-stdout-01")
        artifacts: list[dict] = []

        def writer(data: dict):
            artifacts.append(data)

        async def worker_with_stdout(args: dict):
            return {
                "exit_code": 0,
                "status": "completed",
                "changed_paths": ["allowed/file.py"],
                "error": None,
                "stdout": "raw provider stdout with secret sk-AUDIT_SYNTHETIC_1234567890abcdefgh",
                "stderr": "raw stderr",
                "provider": {"name": "x"},
                "raw": "rawdata",
            }

        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=worker_with_stdout,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
            artifact_writer=writer,
        )
        dumped = json.dumps(res, ensure_ascii=False)
        self.assertNotIn("sk-AUDIT_SYNTHETIC_1234567890abcdefgh", dumped)
        w = res.get("worker_result") or {}
        self.assertEqual(w.get("stdout"), "[REDACTED]")
        self.assertEqual(w.get("stderr"), "[REDACTED]")
        self.assertEqual(w.get("provider"), "[REDACTED]")
        self.assertEqual(w.get("raw"), "[REDACTED]")
        self.assertTrue(len(artifacts) >= 1)
        adump = json.dumps(artifacts[-1], ensure_ascii=False)
        self.assertNotIn("sk-AUDIT_SYNTHETIC_1234567890abcdefgh", adump)
        self.assertIn("[REDACTED]", adump)

    def test_error_and_artifact_writer_sanitized(self):
        repo = _mk_repo()
        wt = _mk_wt()
        contract = _base_contract(repo, task_id="t-f2-err-01")
        artifacts: list[dict] = []

        def writer(data: dict):
            artifacts.append(data)

        async def worker_err(args: dict):
            return {
                "exit_code": 0,
                "status": "completed",
                "changed_paths": ["allowed/file.py"],
                "error": None,
                "transcript": "should hide",
                "nested": {"password": "hunter2-secret-value"},
            }

        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=worker_err,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
            artifact_writer=writer,
        )
        w = res.get("worker_result") or {}
        self.assertEqual(w.get("transcript"), "[REDACTED]")
        self.assertEqual((w.get("nested") or {}).get("password"), "[REDACTED]")
        adump = json.dumps(artifacts[-1], ensure_ascii=False)
        self.assertNotIn("hunter2-secret-value", adump)


class TestConcurrencyF3(unittest.TestCase):
    def test_stdout_lock_exists_and_uses_thread(self):
        self.assertTrue(hasattr(pipeline, "_STDOUT_LOCK"))
        lock = getattr(pipeline, "_STDOUT_LOCK")
        self.assertTrue(isinstance(lock, type(threading.Lock())))
        text = Path(pipeline.__file__).read_text(encoding="utf-8")
        self.assertIn("threading.Lock", text)
        self.assertIn("threading.Thread", text)
        self.assertIn("redirect_stdout", text)
        self.assertTrue(hasattr(pipeline, "_capture_async_provider"))
        self.assertTrue(hasattr(pipeline, "_capture_sync_provider"))

    def test_isolated_stdout_capture_no_cross_contamination(self):
        async def scenario():
            async def _printer(token: str, delay: float):
                import sys as _sys
                print(f"START-{token}")
                await asyncio.sleep(delay)
                print(f"PAYLOAD-{token}")
                print(json.dumps({"marker": token}))
                return 0

            async def _run_one(token: str):
                code, out = await pipeline._capture_async_provider(_printer, token, 0.05)
                return code, out

            (c1, o1), (c2, o2) = await asyncio.gather(_run_one("AAA"), _run_one("BBB"))
            return (c1, o1, c2, o2)

        c1, o1, c2, o2 = asyncio.run(scenario())
        self.assertEqual(c1, 0)
        self.assertEqual(c2, 0)
        self.assertIn("AAA", o1)
        self.assertIn("BBB", o2)
        self.assertNotIn("BBB", o1)
        self.assertNotIn("AAA", o2)

    def test_sync_capture_isolated(self):
        async def scenario():
            def _sync_printer(token: str):
                print(f"SYNC-{token}")
                print(json.dumps({"m": token}))
                return 7

            async def _run_one(token: str):
                return await pipeline._capture_sync_provider(_sync_printer, token)

            (c1, o1), (c2, o2) = await asyncio.gather(_run_one("X1"), _run_one("X2"))
            return c1, o1, c2, o2

        c1, o1, c2, o2 = asyncio.run(scenario())
        self.assertEqual(c1, 7)
        self.assertEqual(c2, 7)
        self.assertIn("X1", o1)
        self.assertNotIn("X2", o1)
        self.assertIn("X2", o2)
        self.assertNotIn("X1", o2)

    def test_concurrent_same_task_second_fails(self):
        async def scenario():
            repo = _mk_repo()
            wt = _mk_wt()
            contract = _base_contract(repo, task_id="t-conc-same-99")
            entered = asyncio.Event()
            release = asyncio.Event()

            async def blocking_worker(args: dict):
                entered.set()
                await release.wait()
                return await _worker_ok(args)

            async def run_first():
                return await pipeline.run_pipeline(
                    dict(contract),
                    create_worktree_fn=_create_fn(wt),
                    get_source_state_fn=_src_fn(),
                    get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
                    get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
                    worker_runner=blocking_worker,
                    validator_runner=_validator_pass,
                    reviewer_runner=_reviewer_approve,
                )

            t1 = asyncio.create_task(run_first())
            await entered.wait()

            res2 = await pipeline.run_pipeline(
                dict(contract),
                create_worktree_fn=_create_fn(wt),
                get_source_state_fn=_src_fn(),
                get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
                get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
                worker_runner=_worker_ok,
                validator_runner=_validator_pass,
                reviewer_runner=_reviewer_approve,
            )
            release.set()
            res1 = await t1
            return res1, res2

        res1, res2 = asyncio.run(scenario())
        self.assertEqual(res1.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res2.get("verdict"), "FAILED_INFRA")

    def test_concurrent_same_resolved_worktree_different_tasks_fails(self):
        async def scenario():
            repo = _mk_repo()
            wt_shared = _mk_wt()
            cA = _base_contract(repo, task_id="t-conc-A-99")
            cB = _base_contract(repo, task_id="t-conc-B-99")
            entered = asyncio.Event()
            release = asyncio.Event()

            async def blocking_worker(args: dict):
                entered.set()
                await release.wait()
                return await _worker_ok(args)

            async def run_a():
                return await pipeline.run_pipeline(
                    cA,
                    create_worktree_fn=_create_fn(wt_shared),
                    get_source_state_fn=_src_fn(),
                    get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
                    get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
                    worker_runner=blocking_worker,
                    validator_runner=_validator_pass,
                    reviewer_runner=_reviewer_approve,
                )

            tA = asyncio.create_task(run_a())
            await entered.wait()
            resB = await pipeline.run_pipeline(
                cB,
                create_worktree_fn=_create_fn(wt_shared),
                get_source_state_fn=_src_fn(),
                get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
                get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
                worker_runner=_worker_ok,
                validator_runner=_validator_pass,
                reviewer_runner=_reviewer_approve,
            )
            release.set()
            resA = await tA
            return resA, resB

        resA, resB = asyncio.run(scenario())
        self.assertEqual(resA.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(resB.get("verdict"), "FAILED_INFRA")


class TestExplicitNoneF4(unittest.TestCase):
    def _happy_kwargs(self, wt: str):
        return dict(
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )

    def test_absent_defaults_false_ready(self):
        repo = _mk_repo()
        wt = _mk_wt()
        c = _base_contract(repo, task_id="t-f4-absent-01")
        c.pop("security_review_required", None)
        c.pop("security_required", None)
        res = _run_sync(c, **self._happy_kwargs(wt))
        self.assertEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")

    def test_present_bool_accept(self):
        repo = _mk_repo()
        wt = _mk_wt()
        c = _base_contract(repo, task_id="t-f4-false-01")
        c["security_review_required"] = False
        res = _run_sync(c, **self._happy_kwargs(wt))
        self.assertEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        c2 = _base_contract(repo, task_id="t-f4-true-01")
        c2["security_review_required"] = True
        res2 = _run_sync(
            c2, **self._happy_kwargs(_mk_wt()), security_hook=_security_approve_current
        )
        self.assertEqual(res2.get("verdict"), "READY_FOR_HUMAN_REVIEW")

    def test_present_none_is_malformed(self):
        repo = _mk_repo()
        c = _base_contract(repo, task_id="t-f4-none-01")
        c["security_review_required"] = None  # type: ignore
        res = _run_sync(c, **self._happy_kwargs(_mk_wt()))
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")

    def test_present_string_int_other_malformed(self):
        for bad in ("false", "true", "0", "1", 0, 1, 0.0, [], {}, b"false"):
            repo = _mk_repo()
            c = _base_contract(repo, task_id="t-f4-bad-01")
            c["security_review_required"] = bad  # type: ignore
            res = _run_sync(c, **self._happy_kwargs(_mk_wt()))
            self.assertEqual(res.get("verdict"), "FAILED_INFRA", f"bad={bad!r}")


class TestSeamPresence(unittest.TestCase):
    def test_default_path_uses_existing_seams(self):
        text = Path(pipeline.__file__).read_text(encoding="utf-8")
        for mod in ("jho_worktree", "jho_worker", "jho_validate", "jho_review", "jho_lite"):
            self.assertIn(mod, text, f"missing seam {mod}")
        self.assertTrue(hasattr(pipeline, "run_pipeline"))
        self.assertTrue(hasattr(pipeline, "run"))


class TestRealDefaultSeamsF5(unittest.TestCase):
    def test_real_git_worktree_with_stubbed_execute_boundary(self):
        try:
            import jho_worktree as jw
            import jho_worker as jworker
            import jho_validate as jvalidate
            import jho_review as jreview
        except Exception as exc:
            self.skipTest(f"runner modules unavailable: {exc}")
            return
        repo = _mk_real_git_repo()
        wt_root = tempfile.mkdtemp(prefix="jho-wt-root-")
        orig_root = jw.WORKTREE_ROOT
        orig_worker = jworker.execute
        orig_validator = jvalidate.execute
        orig_reviewer = jreview.execute
        called: dict[str, bool] = {}
        task_id = "t-f5-real-01"
        try:
            jw.WORKTREE_ROOT = Path(wt_root)  # type: ignore
            # also patch pipeline's cached reference if different object
            try:
                if pipeline._worktree_mod is not None:
                    pipeline._worktree_mod.WORKTREE_ROOT = Path(wt_root)  # type: ignore
            except Exception:
                pass

            async def fake_worker_execute(ns):
                called["worker"] = True
                self.assertEqual(getattr(ns, "task_id", None), task_id)
                manifest = jw.load_manifest(getattr(ns, "task_id"))
                wt = Path(manifest["worktree"])
                (wt / "allowed").mkdir(parents=True, exist_ok=True)
                (wt / "allowed" / "hello.py").write_text("x = 1\n", encoding="utf-8")
                result = {
                    "schema_version": "1.0",
                    "task_id": getattr(ns, "task_id"),
                    "candidate": getattr(ns, "candidate", ""),
                    "status": "completed",
                    "changed_paths": ["allowed/hello.py"],
                    "error": None,
                }
                out = jw.task_dir(getattr(ns, "task_id")) / "worker_result.json"
                out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                print("DIAG worker starting")
                print("DIAG harness notes")
                print(json.dumps(result, indent=2))
                return 0

            def fake_validator_execute(ns):
                called["validator"] = True
                self.assertEqual(getattr(ns, "task_id", None), task_id)
                manifest = jw.load_manifest(getattr(ns, "task_id"))
                wt = Path(manifest["worktree"])
                self.assertTrue((wt / "allowed" / "hello.py").is_file())
                payload = {
                    "schema_version": "1.0",
                    "task_id": getattr(ns, "task_id"),
                    "profile": getattr(ns, "profile", "backend"),
                    "status": "PASS",
                    "results": [{"command": ["git", "diff", "--check"], "status": "PASS"}],
                }
                out = jw.task_dir(getattr(ns, "task_id")) / "validator_result.json"
                out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
                print("DIAG validator running")
                print(json.dumps(payload, indent=2))
                return 0

            async def fake_review_execute(ns):
                called["reviewer"] = True
                self.assertEqual(getattr(ns, "task_id", None), task_id)
                vpath = jw.task_dir(getattr(ns, "task_id")) / "validator_result.json"
                self.assertTrue(vpath.is_file())
                payload = {
                    "schema_version": "1.0",
                    "task_id": getattr(ns, "task_id"),
                    "candidate": getattr(ns, "candidate", ""),
                    "status": "completed",
                    "verdict": "APPROVE",
                    "error": None,
                }
                out = jw.task_dir(getattr(ns, "task_id")) / "review_result.json"
                out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
                print("REVIEW DIAG line")
                print(json.dumps(payload, indent=2))
                return 0

            jworker.execute = fake_worker_execute  # type: ignore
            jvalidate.execute = fake_validator_execute  # type: ignore
            jreview.execute = fake_review_execute  # type: ignore

            contract = {
                "task_id": task_id,
                "repo_path": repo,
                "allowed_paths": ["allowed"],
                "task": "Create allowed/hello.py with smallest change.",
                "worker_candidate": WORKER_CAND,
                "reviewer_candidate": REVIEWER_CAND,
                "validator_profile": "minimal",
                "security_review_required": False,
                "timeout": 60,
            }
            res = pipeline.run(contract)
            self.assertTrue(called.get("worker") is True, "default adapter did not call jho_worker.execute")
            self.assertTrue(called.get("validator") is True, "default adapter did not call jho_validate.execute")
            self.assertTrue(called.get("reviewer") is True, "default adapter did not call jho_review.execute")
            self.assertEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
            self.assertIn("allowed/hello.py", res.get("changed_paths") or [])
            self.assertEqual((res.get("validator_result") or {}).get("status"), "PASS")
            self.assertEqual((res.get("reviewer_result") or {}).get("verdict"), "APPROVE")
            self.assertEqual((res.get("reviewer_result") or {}).get("status"), "completed")
        finally:
            try:
                jw.WORKTREE_ROOT = orig_root  # type: ignore
            except Exception:
                pass
            try:
                if pipeline._worktree_mod is not None:
                    pipeline._worktree_mod.WORKTREE_ROOT = orig_root  # type: ignore
            except Exception:
                pass
            try:
                jworker.execute = orig_worker  # type: ignore
            except Exception:
                pass
            try:
                jvalidate.execute = orig_validator  # type: ignore
            except Exception:
                pass
            try:
                jreview.execute = orig_reviewer  # type: ignore
            except Exception:
                pass
            try:
                # best-effort cleanup of created worktree
                import jho_worktree as _jw2
                try:
                    _jw2.WORKTREE_ROOT = Path(wt_root)  # type: ignore
                    _jw2.remove_worktree(task_id=task_id, force=True)
                except Exception:
                    pass
                finally:
                    try:
                        _jw2.WORKTREE_ROOT = orig_root  # type: ignore
                    except Exception:
                        pass
            except Exception:
                pass


class TestRobustJsonF6(unittest.TestCase):
    def test_extract_last_json_selects_final(self):
        out = pipeline._extract_last_json_object(
            'starting\n{"marker": "early", "status": "x"}\nmore logs\n{"status": "completed", "marker": "final"}'
        )
        self.assertIsInstance(out, dict)
        self.assertEqual(out.get("marker"), "final")

    def test_multiline_pretty_with_diagnostics(self):
        pretty = json.dumps({"status": "completed", "verdict": "APPROVE", "n": 1}, indent=2)
        text = "DIAG line 1\nDIAG line 2\n" + pretty + "\n"
        out = pipeline._extract_last_json_object(text)
        self.assertEqual(out.get("verdict"), "APPROVE")
        # whole loads would fail due to diagnostics
        with self.assertRaises(Exception):
            json.loads(text)
        self.assertIsNotNone(out)

    def test_whole_loads_fails_but_helper_succeeds(self):
        payload = {"status": "PASS", "results": []}
        text = "log line\n" + json.dumps({"noise": 1}) + "\nlog middle\n" + json.dumps(payload, indent=2) + "\ntrailing log"
        with self.assertRaises(Exception):
            json.loads(text)
        out = pipeline._extract_last_json_object(text)
        self.assertEqual(out.get("status"), "PASS")

    def test_no_bare_loads_on_stdout(self):
        text = Path(pipeline.__file__).read_text(encoding="utf-8")
        self.assertIn("_extract_last_json_object", text)
        # must not claim last-object while doing whole-output loads on provider stdout
        self.assertNotIn("json.loads(stdout_text)", text)
        self.assertNotIn("json.loads(stdout_text.strip", text)


class TestSecurityAliasConflictR2214(unittest.TestCase):
    """R2.2.14 defect A: conflicting security aliases must fail closed."""

    def _happy_kwargs(self, wt: str):
        return dict(
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )

    def test_conflicting_aliases_fail_closed(self):
        # canonical Luna example: False vs True must not silently choose first
        cases = [
            {"security_review_required": False, "security_required": True},
            {"security_review_required": True, "security_required": False},
            {"security_required": True, "needs_security_review": False},
            {"needs_security_review": True, "securityReviewRequired": False},
            {"security_review_required": True, "security_required": True, "needs_security_review": False},
        ]
        for i, extra in enumerate(cases):
            repo = _mk_repo()
            c = _base_contract(repo, task_id=f"t-r2214-conflict-{i}")
            c.pop("security_review_required", None)
            c.update(extra)
            ok, _, _ = pipeline.validate_contract(c)
            self.assertFalse(ok, f"conflict should be invalid: {extra!r}")
            res = _run_sync(c, **self._happy_kwargs(_mk_wt()))
            self.assertEqual(res.get("verdict"), "FAILED_INFRA", f"conflict {extra!r}")
            self.assertEqual(res.get("failure_category"), "policy")

    def test_identical_aliases_accepted(self):
        # same bool across aliases is accepted
        repo = _mk_repo()
        c = _base_contract(repo, task_id="t-r2214-ident-false")
        c.pop("security_review_required", None)
        c["security_review_required"] = False
        c["security_required"] = False
        c["needs_security_review"] = False
        res = _run_sync(c, **self._happy_kwargs(_mk_wt()))
        self.assertEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")

        repo2 = _mk_repo()
        c2 = _base_contract(repo2, task_id="t-r2214-ident-true")
        c2.pop("security_review_required", None)
        c2["security_review_required"] = True
        c2["security_required"] = True
        res2 = _run_sync(
            c2, **self._happy_kwargs(_mk_wt()), security_hook=_security_approve_current
        )
        self.assertEqual(res2.get("verdict"), "READY_FOR_HUMAN_REVIEW")

    def test_alias_none_string_int_fails(self):
        for alias in ("security_review_required", "security_required", "needs_security_review", "securityReviewRequired"):
            for bad in (None, "true", "false", "yes", "1", "0", 1, 0, 0.0, [], {}, b"true"):
                repo = _mk_repo()
                c = _base_contract(repo, task_id="t-r2214-badalias-01")
                c.pop("security_review_required", None)
                c.pop("security_required", None)
                c.pop("needs_security_review", None)
                c.pop("securityReviewRequired", None)
                c[alias] = bad  # type: ignore
                ok, _, _ = pipeline.validate_contract(c)
                self.assertFalse(ok, f"alias {alias} bad={bad!r} should be invalid")
                res = _run_sync(c, **self._happy_kwargs(_mk_wt()))
                self.assertEqual(res.get("verdict"), "FAILED_INFRA", f"alias {alias} bad={bad!r}")

    def test_absent_aliases_default_false(self):
        repo = _mk_repo()
        c = _base_contract(repo, task_id="t-r2214-absent-01")
        for k in ("security_review_required", "security_required", "needs_security_review", "securityReviewRequired"):
            c.pop(k, None)
        ok, norm, _ = pipeline.validate_contract(c)
        self.assertTrue(ok)
        self.assertEqual(norm.get("security_review_required"), False)
        res = _run_sync(c, **self._happy_kwargs(_mk_wt()))
        self.assertEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")


class TestSensitiveSeparatorsR2214(unittest.TestCase):
    """R2.2.14 defect B: separator/case variants must redact plain values."""

    def test_separator_variants_redacted_plain_values(self):
        sample = {
            "x-api-key": "plain123",
            "api-key": "hello-world",
            "private-key": "mykey123",
            "X-API-KEY": "upper-value",
            "X_API_KEY": "under-value",
            "x_api_key": "under2",
            "Api-Key": "mixed-case",
            "PRIVATE-KEY": "upper-priv",
            "private_key": "under-priv",
            "privatekey": "flat",
            "Private Key": "with-space",
            "x api key": "spaces",
            "API KEY": "spaces-upper",
            "PRIVATE KEY": "space-priv",
            "access-token": "tok-plain",
            "Access_Token": "tok-plain2",
        }
        out = pipeline.sanitize_value(sample)
        for k in sample.keys():
            self.assertEqual(out[k], "[REDACTED]", f"key {k!r} not redacted")

    def test_nested_separators_redacted(self):
        sample = {
            "outer": {
                "inner": {"x-api-key": "plain-value-no-equals", "nested_list": [{"private-key": "abc123"}]},
                "api-key": "still-plain",
            },
            "lst": [
                {"level": 1, "PRIVATE_KEY": "hidden"},
                {"X-Api-Key": "hidden2"},
            ],
            "deep": {"a": {"b": {"c": {"access-token": "plain"}}}},
        }
        out = pipeline.sanitize_value(sample)
        dumped = json.dumps(out, ensure_ascii=False)
        for plain in ("plain-value-no-equals", "abc123", "still-plain", "hidden", "hidden2", "plain"):
            self.assertNotIn(plain, dumped, f"plain value leaked: {plain!r}")
        self.assertEqual(out["outer"]["inner"]["x-api-key"], "[REDACTED]")
        self.assertEqual(out["outer"]["inner"]["nested_list"][0]["private-key"], "[REDACTED]")
        self.assertEqual(out["outer"]["api-key"], "[REDACTED]")
        self.assertEqual(out["lst"][0]["PRIVATE_KEY"], "[REDACTED]")
        self.assertEqual(out["lst"][1]["X-Api-Key"], "[REDACTED]")
        self.assertIn("[REDACTED]", dumped)

    def test_plain_values_redacted_by_key_in_result(self):
        repo = _mk_repo()
        wt = _mk_wt()
        contract = _base_contract(repo, task_id="t-r2214-sep-result-01")
        artifacts: list[dict] = []

        def writer(data: dict):
            artifacts.append(data)

        async def worker_with_sep_keys(args: dict):
            return {
                "exit_code": 0,
                "status": "completed",
                "changed_paths": ["allowed/file.py"],
                "error": None,
                "config": {"x-api-key": "plain123", "private-key": "plainkey"},
                "api-key": "plain-api",
            }

        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=worker_with_sep_keys,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
            artifact_writer=writer,
        )
        dumped = json.dumps(res, ensure_ascii=False)
        for plain in ("plain123", "plainkey", "plain-api"):
            self.assertNotIn(plain, dumped, f"separator key value leaked: {plain!r}")
        w = res.get("worker_result") or {}
        self.assertEqual((w.get("config") or {}).get("x-api-key"), "[REDACTED]")
        self.assertEqual((w.get("config") or {}).get("private-key"), "[REDACTED]")
        self.assertEqual(w.get("api-key"), "[REDACTED]")
        self.assertTrue(len(artifacts) >= 1)
        adump = json.dumps(artifacts[-1], ensure_ascii=False)
        for plain in ("plain123", "plainkey", "plain-api"):
            self.assertNotIn(plain, adump)


class TestSharedStdoutDomainR2215(unittest.TestCase):
    """R2.2.15 defect A: sync and async capture share one exclusion domain."""

    def test_single_exclusion_domain(self):
        text = Path(pipeline.__file__).read_text(encoding="utf-8")
        self.assertIn("_STDOUT_LOCK", text)
        self.assertIn("threading.Lock", text)
        self.assertIn("threading.Thread", text)
        self.assertIn("redirect_stdout", text)
        # separate async lock domain must be gone
        self.assertNotIn("asyncio.Lock", text)
        self.assertNotIn("_ASYNC_STDOUT_LOCK", text)
        # async acquisition must not block event loop and must release in finally
        self.assertIn("_STDOUT_LOCK.acquire", text)
        self.assertIn("finally", text)
        self.assertIn("_STDOUT_LOCK.release", text)

    def test_mixed_sync_async_capture_no_cross(self):
        async def scenario():
            async def _async_printer(token: str, delay: float):
                print(f"START-{token}")
                await asyncio.sleep(delay)
                print(f"PAYLOAD-{token}")
                print(json.dumps({"marker": token}))
                return 0

            def _sync_printer(token: str):
                print(f"START-{token}")
                time.sleep(0.05)
                print(f"PAYLOAD-{token}")
                print(json.dumps({"marker": token}))
                return 0

            async def _run_async():
                return await pipeline._capture_async_provider(_async_printer, "ALPHA2215", 0.05)

            async def _run_sync():
                return await pipeline._capture_sync_provider(_sync_printer, "BETA2215")

            (cA, oA), (cS, oS) = await asyncio.gather(_run_async(), _run_sync())
            return cA, oA, cS, oS

        cA, oA, cS, oS = asyncio.run(scenario())
        self.assertEqual(cA, 0)
        self.assertEqual(cS, 0)
        self.assertIn("ALPHA2215", oA)
        self.assertIn("BETA2215", oS)
        self.assertNotIn("BETA2215", oA)
        self.assertNotIn("ALPHA2215", oS)

    def test_mixed_reverse_order_no_cross(self):
        async def scenario():
            def _sync_printer(token: str):
                print(f"SYNC2-{token}")
                time.sleep(0.05)
                print(json.dumps({"m": token}))
                return 3

            async def _async_printer(token: str):
                print(f"ASYNC2-{token}")
                await asyncio.sleep(0.05)
                print(json.dumps({"m": token}))
                return 4

            async def _run_sync():
                return await pipeline._capture_sync_provider(_sync_printer, "R2215S")

            async def _run_async():
                return await pipeline._capture_async_provider(_async_printer, "R2215A")

            (cS, oS), (cA, oA) = await asyncio.gather(_run_sync(), _run_async())
            return cS, oS, cA, oA

        cS, oS, cA, oA = asyncio.run(scenario())
        self.assertEqual(cS, 3)
        self.assertEqual(cA, 4)
        self.assertIn("R2215S", oS)
        self.assertIn("R2215A", oA)
        self.assertNotIn("R2215A", oS)
        self.assertNotIn("R2215S", oA)


class TestWorktreeOwnershipReleaseR2215(unittest.TestCase):
    """R2.2.15 defect B: resolved worktree ownership must be released."""

    def _happy_kwargs(self, wt: str):
        return dict(
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )

    def test_concurrent_same_worktree_still_rejected(self):
        async def scenario():
            repo = _mk_repo()
            wt_shared = _mk_wt()
            cA = _base_contract(repo, task_id="t-r2215-conc-A")
            cB = _base_contract(repo, task_id="t-r2215-conc-B")
            entered = asyncio.Event()
            release = asyncio.Event()

            async def blocking_worker(args: dict):
                entered.set()
                await release.wait()
                return await _worker_ok(args)

            async def run_a():
                return await pipeline.run_pipeline(
                    cA,
                    create_worktree_fn=_create_fn(wt_shared),
                    get_source_state_fn=_src_fn(),
                    get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
                    get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
                    worker_runner=blocking_worker,
                    validator_runner=_validator_pass,
                    reviewer_runner=_reviewer_approve,
                )

            tA = asyncio.create_task(run_a())
            await entered.wait()
            resB = await pipeline.run_pipeline(
                cB,
                create_worktree_fn=_create_fn(wt_shared),
                get_source_state_fn=_src_fn(),
                get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
                get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
                worker_runner=_worker_ok,
                validator_runner=_validator_pass,
                reviewer_runner=_reviewer_approve,
            )
            release.set()
            resA = await tA
            return resA, resB

        resA, resB = asyncio.run(scenario())
        self.assertEqual(resA.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(resB.get("verdict"), "FAILED_INFRA")

    def test_sequential_reuse_after_success_allowed(self):
        repo = _mk_repo()
        wt = _mk_wt()
        c1 = _base_contract(repo, task_id="t-r2215-reuse-ok-1")
        c2 = _base_contract(repo, task_id="t-r2215-reuse-ok-2")
        res1 = _run_sync(c1, **self._happy_kwargs(wt))
        self.assertEqual(res1.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        res2 = _run_sync(c2, **self._happy_kwargs(wt))
        self.assertEqual(res2.get("verdict"), "READY_FOR_HUMAN_REVIEW")

    def test_sequential_reuse_after_rework_allowed(self):
        repo = _mk_repo()
        wt = _mk_wt()
        c1 = _base_contract(repo, task_id="t-r2215-reuse-rw-1")
        res1 = _run_sync(
            c1,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_fail,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res1.get("verdict"), "NEEDS_REWORK")
        c2 = _base_contract(repo, task_id="t-r2215-reuse-rw-2")
        res2 = _run_sync(c2, **self._happy_kwargs(wt))
        self.assertEqual(res2.get("verdict"), "READY_FOR_HUMAN_REVIEW")

    def test_sequential_reuse_after_human_gated_allowed(self):
        repo = _mk_repo()
        wt = _mk_wt()
        c1 = _base_contract(repo, task_id="t-r2215-reuse-hu-1")
        c1["security_review_required"] = True
        res1 = _run_sync(
            c1,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res1.get("verdict"), "NEEDS_HUMAN")
        c2 = _base_contract(repo, task_id="t-r2215-reuse-hu-2")
        c2["security_review_required"] = True
        # ensure worktree file exists stable for fingerprint path
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("stable\n", encoding="utf-8")
        res2 = _run_sync(
            c2,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
            security_hook=_security_approve_current,
        )
        self.assertEqual(res2.get("verdict"), "READY_FOR_HUMAN_REVIEW")

    def test_sequential_reuse_after_infra_allowed(self):
        repo = _mk_repo()
        wt = _mk_wt()

        async def _worker_fail_infra(args: dict):
            return {"exit_code": 4, "status": "error", "error": "infra timeout boom", "changed_paths": []}

        c1 = _base_contract(repo, task_id="t-r2215-reuse-infra-1")
        res1 = _run_sync(
            c1,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status=""),
            get_actual_diff_fn=_diff_fn([]),
            worker_runner=_worker_fail_infra,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res1.get("verdict"), "FAILED_INFRA")
        c2 = _base_contract(repo, task_id="t-r2215-reuse-infra-2")
        res2 = _run_sync(c2, **self._happy_kwargs(wt))
        self.assertEqual(res2.get("verdict"), "READY_FOR_HUMAN_REVIEW")


class TestSecurityHookIntegrityR2215(unittest.TestCase):
    """R2.2.15 defect C: integrity recheck after security hook."""

    def _base_kwargs(self, wt: str, src_fn, diff_fn):
        return dict(
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=src_fn,
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=diff_fn,
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )

    def test_hook_inplace_edit_blocks_ready(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        target = Path(wt) / "allowed" / "file.py"
        target.write_text("v1-stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-r2215-hook-inplace-01")
        contract["security_review_required"] = True

        def hook(identity):
            target.write_text("v2-hook-mutated-inplace\n", encoding="utf-8")
            return {"approved": True, "identity": identity}

        res = _run_sync(
            contract,
            **self._base_kwargs(wt, _src_fn(), _diff_fn(["allowed/file.py"])),
            security_hook=hook,
        )
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertIn(res.get("verdict"), ("NEEDS_REWORK", "FAILED_INFRA", "NEEDS_HUMAN"))

    def test_hook_unexpected_path_blocks_ready(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-r2215-hook-path-01")
        contract["security_review_required"] = True

        def diff_filesystem_aware(_wt: str):
            base = ["allowed/file.py"]
            try:
                if (Path(wt) / "unexpected_evil.py").exists():
                    return base + ["unexpected_evil.py"]
            except Exception:
                pass
            return base

        def hook(identity):
            (Path(wt) / "unexpected_evil.py").write_text("evil\n", encoding="utf-8")
            return {"approved": True, "identity": identity}

        res = _run_sync(
            contract,
            **self._base_kwargs(wt, _src_fn(), diff_filesystem_aware),
            security_hook=hook,
        )
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertIn(res.get("verdict"), ("NEEDS_REWORK", "FAILED_INFRA", "NEEDS_HUMAN"))

    def test_hook_source_mutation_blocks_ready(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-r2215-hook-src-01")
        contract["security_review_required"] = True
        src_state = {"head": "abc123head", "status": ""}

        def src_fn(_repo: str):
            return dict(src_state)

        def hook(identity):
            src_state["head"] = "mutated-head-by-hook"
            return {"approved": True, "identity": identity}

        res = _run_sync(
            contract,
            **self._base_kwargs(wt, src_fn, _diff_fn(["allowed/file.py"])),
            security_hook=hook,
        )
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")

    def test_nonmutating_hook_allows_ready(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-r2215-hook-clean-01")
        contract["security_review_required"] = True

        def clean_hook(identity):
            return {"approved": True, "identity": identity}

        res = _run_sync(
            contract,
            **self._base_kwargs(wt, _src_fn(), _diff_fn(["allowed/file.py"])),
            security_hook=clean_hook,
        )
        self.assertEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")


class TestCancellationSafeStdoutR2217(unittest.TestCase):
    """R2.2.17 defect A: cancellation-safe shared stdout lock."""

    def test_cancelled_waiter_does_not_strand_lock(self):
        async def scenario():
            self.assertFalse(pipeline._STDOUT_LOCK.locked())
            pipeline._STDOUT_LOCK.acquire()
            try:
                async def _printer():
                    print("hello-waiter")
                    return 0

                waiter = asyncio.create_task(pipeline._capture_async_provider(_printer))
                await asyncio.sleep(0.15)
                self.assertFalse(waiter.done())
                waiter.cancel()
                try:
                    await waiter
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            finally:
                try:
                    pipeline._STDOUT_LOCK.release()
                except Exception:
                    pass
            await asyncio.sleep(0.3)
            self.assertFalse(
                pipeline._STDOUT_LOCK.locked(),
                "cancelled waiter stranded the stdout lock",
            )

            async def _printer2():
                print("after-cancel-ok")
                return 0

            code, out = await pipeline._capture_async_provider(_printer2)
            self.assertEqual(code, 0)
            self.assertIn("after-cancel-ok", out)

            def _sync_printer():
                print("sync-after-cancel-ok")
                return 1

            c2, o2 = await pipeline._capture_sync_provider(_sync_printer)
            self.assertEqual(c2, 1)
            self.assertIn("sync-after-cancel-ok", o2)

        asyncio.run(scenario())

    def test_cancellation_while_holding_releases_lock(self):
        async def scenario():
            started = asyncio.Event()

            async def _blocking_printer():
                started.set()
                await asyncio.sleep(0.4)
                print("held-output-2217")
                return 0

            task = asyncio.create_task(pipeline._capture_async_provider(_blocking_printer))
            await started.wait()
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            await asyncio.sleep(0.1)
            self.assertFalse(pipeline._STDOUT_LOCK.locked())

            async def _next():
                print("next-ok-2217")
                return 0

            code, out = await pipeline._capture_async_provider(_next)
            self.assertEqual(code, 0)
            self.assertIn("next-ok-2217", out)

        asyncio.run(scenario())

    def test_event_loop_not_blocked_while_waiting(self):
        async def scenario():
            pipeline._STDOUT_LOCK.acquire()
            try:
                async def _waiter():
                    print("waiter-2217")
                    return 0

                t = asyncio.create_task(pipeline._capture_async_provider(_waiter))
                await asyncio.sleep(0.05)
                flag: list[int] = []

                async def _quick():
                    flag.append(1)

                await _quick()
                self.assertEqual(flag, [1])
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            finally:
                try:
                    pipeline._STDOUT_LOCK.release()
                except Exception:
                    pass
            await asyncio.sleep(0.3)
            self.assertFalse(pipeline._STDOUT_LOCK.locked())

        asyncio.run(scenario())

    def test_single_domain_shielded_acquisition(self):
        text = Path(pipeline.__file__).read_text(encoding="utf-8")
        self.assertIn("_STDOUT_LOCK", text)
        self.assertIn("threading.Lock", text)
        self.assertIn("redirect_stdout", text)
        self.assertNotIn("asyncio.Lock", text)
        self.assertNotIn("_ASYNC_STDOUT_LOCK", text)
        self.assertIn("blocking=False", text)
        self.assertIn("await asyncio.sleep", text)
        self.assertIn("_STDOUT_LOCK.acquire", text)
        self.assertIn("_STDOUT_LOCK.release", text)


class TestAtomicOwnershipR2217(unittest.TestCase):
    """R2.2.17 defect B: atomic writer/worktree ownership."""

    def test_ownership_lock_exists_and_guards_registries(self):
        self.assertTrue(hasattr(pipeline, "_OWNERSHIP_LOCK"))
        self.assertTrue(isinstance(pipeline._OWNERSHIP_LOCK, type(threading.Lock())))
        text = Path(pipeline.__file__).read_text(encoding="utf-8")
        self.assertIn("_OWNERSHIP_LOCK", text)
        self.assertIn("_ACTIVE_WRITERS", text)
        self.assertIn("_ACTIVE_WORKTREE_PATHS", text)
        # atomic check+add under the same lock and release under same lock
        self.assertIn("with _OWNERSHIP_LOCK", text)

    def test_threaded_same_task_only_one_proceeds(self):
        repo = _mk_repo()
        wt1 = _mk_wt()
        wt2 = _mk_wt()
        base = _base_contract(repo, task_id="t-r2217-race-task-01")
        entered = threading.Event()
        release = threading.Event()

        async def blocking_worker(args: dict):
            entered.set()
            deadline = asyncio.get_running_loop().time() + 10
            while not release.is_set() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            return await _worker_ok(args)

        results: dict[str, Any] = {}

        def run_first():
            c = dict(base)
            try:
                results["r1"] = pipeline.run(
                    c,
                    create_worktree_fn=_create_fn(wt1),
                    get_source_state_fn=_src_fn(),
                    get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
                    get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
                    worker_runner=blocking_worker,
                    validator_runner=_validator_pass,
                    reviewer_runner=_reviewer_approve,
                )
            except Exception as exc:  # pragma: no cover
                results["r1"] = {"verdict": f"EXC:{exc}"}

        t1 = threading.Thread(target=run_first, daemon=True)
        t1.start()
        self.assertTrue(entered.wait(timeout=10), "first worker did not start")
        c2 = dict(base)
        res2 = pipeline.run(
            c2,
            create_worktree_fn=_create_fn(wt2),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )
        release.set()
        t1.join(timeout=15)
        res1 = results.get("r1")
        self.assertIsNotNone(res1)
        self.assertEqual(res1.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res2.get("verdict"), "FAILED_INFRA")
        # sequential reuse after terminal path remains allowed
        wt3 = _mk_wt()
        c3 = dict(base)
        res3 = pipeline.run(
            c3,
            create_worktree_fn=_create_fn(wt3),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res3.get("verdict"), "READY_FOR_HUMAN_REVIEW")

    def test_threaded_same_worktree_only_one_proceeds(self):
        repo = _mk_repo()
        wt_shared = _mk_wt()
        cA = _base_contract(repo, task_id="t-r2217-race-wt-A01")
        cB = _base_contract(repo, task_id="t-r2217-race-wt-B01")
        entered = threading.Event()
        release = threading.Event()

        async def blocking_worker(args: dict):
            entered.set()
            deadline = asyncio.get_running_loop().time() + 10
            while not release.is_set() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            return await _worker_ok(args)

        results: dict[str, Any] = {}

        def run_a():
            try:
                results["rA"] = pipeline.run(
                    dict(cA),
                    create_worktree_fn=_create_fn(wt_shared),
                    get_source_state_fn=_src_fn(),
                    get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
                    get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
                    worker_runner=blocking_worker,
                    validator_runner=_validator_pass,
                    reviewer_runner=_reviewer_approve,
                )
            except Exception as exc:  # pragma: no cover
                results["rA"] = {"verdict": f"EXC:{exc}"}

        tA = threading.Thread(target=run_a, daemon=True)
        tA.start()
        self.assertTrue(entered.wait(timeout=10), "first worker did not start")
        resB = pipeline.run(
            dict(cB),
            create_worktree_fn=_create_fn(wt_shared),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )
        release.set()
        tA.join(timeout=15)
        resA = results.get("rA")
        self.assertIsNotNone(resA)
        self.assertEqual(resA.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(resB.get("verdict"), "FAILED_INFRA")

    def test_concurrent_claim_race_single_winner(self):
        task = "t-r2217-direct-race-01"
        with pipeline._OWNERSHIP_LOCK:
            pipeline._ACTIVE_WRITERS.discard(task)
        n = 16
        try:
            barrier = threading.Barrier(n)
        except Exception:
            self.skipTest("barrier unavailable")
            return
        winners: list[str] = []
        winners_lock = threading.Lock()

        def attempt():
            try:
                barrier.wait(timeout=10)
            except Exception:
                return
            won = False
            try:
                with pipeline._OWNERSHIP_LOCK:
                    if task not in pipeline._ACTIVE_WRITERS:
                        pipeline._ACTIVE_WRITERS.add(task)
                        won = True
            except Exception:
                won = False
            if won:
                with winners_lock:
                    winners.append(threading.current_thread().name)

        threads = [threading.Thread(target=attempt, daemon=True) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        try:
            self.assertEqual(len(winners), 1, f"expected exactly one owner, got {len(winners)}")
        finally:
            with pipeline._OWNERSHIP_LOCK:
                pipeline._ACTIVE_WRITERS.discard(task)


class TestFingerprintFailClosedR2217(unittest.TestCase):
    """R2.2.17 defect C: fingerprint collection must fail closed."""

    def _happy_kwargs(self, wt: str):
        return dict(
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )

    def test_fingerprint_failure_before_gate_blocks_ready(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-r2217-fp-pre-01")
        contract["security_review_required"] = True
        orig_fp = pipeline._fingerprint_paths
        calls = {"n": 0}

        def failing_fp(root, allowed=None):
            calls["n"] += 1
            # order: 1 before-validate, 2 after-validate, 3 before-review,
            # 4 after-review, 5 pre-gate, 6 post-gate
            if calls["n"] == 5:
                raise pipeline._FingerprintError("mocked pre-gate collection failure")
            return orig_fp(root, allowed)

        with mock.patch.object(pipeline, "_fingerprint_paths", side_effect=failing_fp):
            res = _run_sync(contract, **self._happy_kwargs(wt))
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")

    def test_fingerprint_failure_after_gate_blocks_ready(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-r2217-fp-post-01")
        contract["security_review_required"] = True
        orig_fp = pipeline._fingerprint_paths
        calls = {"n": 0}

        def failing_fp(root, allowed=None):
            calls["n"] += 1
            if calls["n"] == 6:
                raise pipeline._FingerprintError("mocked post-gate collection failure")
            return orig_fp(root, allowed)

        with mock.patch.object(pipeline, "_fingerprint_paths", side_effect=failing_fp):
            res = _run_sync(
                contract, **self._happy_kwargs(wt), security_hook=_security_approve_current
            )
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")

    def test_same_path_mutation_cannot_hide_behind_failed_fingerprints(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        target = Path(wt) / "allowed" / "file.py"
        target.write_text("v1-stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-r2217-fp-hide-01")
        contract["security_review_required"] = True
        orig_fp = pipeline._fingerprint_paths
        calls = {"n": 0}

        def failing_both_gate(root, allowed=None):
            calls["n"] += 1
            if calls["n"] in (5, 6):
                raise pipeline._FingerprintError("mocked gate collection failure")
            return orig_fp(root, allowed)

        def mutating_hook():
            target.write_text("v2-mutated-inplace\n", encoding="utf-8")
            return {"approved": True}

        with mock.patch.object(pipeline, "_fingerprint_paths", side_effect=failing_both_gate):
            res = _run_sync(
                contract,
                create_worktree_fn=_create_fn(wt),
                get_source_state_fn=_src_fn(),
                get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
                get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
                worker_runner=_worker_ok,
                validator_runner=_validator_pass,
                reviewer_runner=_reviewer_approve,
                security_hook=mutating_hook,
            )
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")

    def test_read_failure_fails_closed_not_empty(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-r2217-fp-read-01")
        orig_read = Path.read_bytes

        def failing_read(self, *a, **kw):
            try:
                s = str(self)
            except Exception:
                s = ""
            if s.endswith("allowed/file.py"):
                raise PermissionError("mocked read failure")
            return orig_read(self, *a, **kw)

        with mock.patch.object(Path, "read_bytes", failing_read):
            res = _run_sync(contract, **self._happy_kwargs(wt))
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")

    def test_fingerprint_helper_fail_closed_shape(self):
        self.assertTrue(hasattr(pipeline, "_FingerprintError"))
        text = Path(pipeline.__file__).read_text(encoding="utf-8")
        self.assertIn("_FingerprintError", text)
        self.assertIn("raise _FingerprintError", text)


class TestFingerprintExplicitContractR2218(unittest.TestCase):
    """R2.2.18: explicit fingerprint evidence, fail-closed at integrity boundaries."""

    def _happy_kwargs(self, wt: str):
        return dict(
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
            get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )

    def test_missing_allowed_file_explicit_absent(self):
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        fp = pipeline._fingerprint_paths(Path(wt), ["allowed/missing.py"])
        self.assertIsInstance(fp, dict)
        self.assertTrue(fp.get("ok") is True)
        self.assertTrue(fp.get("complete") is True)
        entries = fp.get("entries")
        coverage = fp.get("coverage")
        self.assertIsInstance(entries, dict)
        self.assertIsInstance(coverage, dict)
        self.assertIn("allowed/missing.py", entries)
        self.assertEqual(entries["allowed/missing.py"], "absent")
        self.assertIn("allowed/missing.py", coverage)
        self.assertEqual(coverage["allowed/missing.py"], "absent")
        ok, reason = pipeline._validate_fingerprint_result(fp, ["allowed/missing.py"])
        self.assertTrue(ok is True, reason)
        # bare empty map must never validate
        ok2, _ = pipeline._validate_fingerprint_result({}, ["allowed/missing.py"])
        self.assertFalse(ok2)

    def test_absent_before_absent_after_equal(self):
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        before = pipeline._fingerprint_paths(Path(wt), ["allowed/missing.py"])
        after = pipeline._fingerprint_paths(Path(wt), ["allowed/missing.py"])
        self.assertTrue(pipeline._check_fingerprints_equal(before, after, ["allowed/missing.py"]) is True)
        # Fingerprint equality remains valid, but a completed worker with no
        # authoritative diff is now correctly classified as WORKER_NOOP.
        repo = _mk_repo()
        contract = _base_contract(repo, task_id="t-r2218-absent-equal-01")
        contract["allowed_paths"] = ["allowed/missing.py"]
        res = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status=""),
            get_actual_diff_fn=_diff_fn([]),
            worker_runner=_worker_ok,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )
        # worker reports changed_paths outside allowlist? worker_ok reports allowed/file.py
        # so use a worker that reports no changes for this absent case
        async def _w_none(args: dict):
            return {"exit_code": 0, "status": "completed", "changed_paths": [], "error": None}
        res2 = _run_sync(
            contract,
            create_worktree_fn=_create_fn(wt),
            get_source_state_fn=_src_fn(),
            get_worktree_state_fn=_wt_state_fn(status=""),
            get_actual_diff_fn=_diff_fn([]),
            worker_runner=_w_none,
            validator_runner=_validator_pass,
            reviewer_runner=_reviewer_approve,
        )
        self.assertEqual(res.get("verdict"), "NEEDS_REWORK")
        self.assertEqual(res2.get("verdict"), "NEEDS_REWORK")
        self.assertEqual(res2.get("failure_category"), "worker")
        self.assertIn("WORKER_NOOP", res2.get("failure_detail") or "")

    def test_present_before_deleted_after_detected_changed(self):
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        target = Path(wt) / "allowed" / "file.py"
        target.write_text("v1\n", encoding="utf-8")
        before = pipeline._fingerprint_paths(Path(wt), ["allowed/file.py"])
        self.assertNotEqual(before["entries"]["allowed/file.py"], "absent")
        target.unlink()
        after = pipeline._fingerprint_paths(Path(wt), ["allowed/file.py"])
        self.assertEqual(after["entries"]["allowed/file.py"], "absent")
        self.assertEqual(after["coverage"]["allowed/file.py"], "absent")
        self.assertFalse(pipeline._check_fingerprints_equal(before, after, ["allowed/file.py"]))

    def test_unexpected_type_at_validator_boundary_fails_closed(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-r2218-val-type-01")
        orig_fp = pipeline._fingerprint_paths
        calls = {"n": 0}

        def bad_fp(root, allowed=None):
            calls["n"] += 1
            if calls["n"] == 2:
                return {}
            return orig_fp(root, allowed)

        with mock.patch.object(pipeline, "_fingerprint_paths", side_effect=bad_fp):
            res = _run_sync(contract, **self._happy_kwargs(wt))
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")

    def test_unexpected_type_at_reviewer_boundary_fails_closed(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-r2218-rev-type-01")
        orig_fp = pipeline._fingerprint_paths
        calls = {"n": 0}

        def bad_fp(root, allowed=None):
            calls["n"] += 1
            if calls["n"] == 4:
                return "unexpected-string-type"
            return orig_fp(root, allowed)

        with mock.patch.object(pipeline, "_fingerprint_paths", side_effect=bad_fp):
            res = _run_sync(contract, **self._happy_kwargs(wt))
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")

    def test_unexpected_type_at_security_boundary_fails_closed(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-r2218-sec-type-01")
        contract["security_review_required"] = True
        orig_fp = pipeline._fingerprint_paths
        calls = {"n": 0}

        def bad_fp(root, allowed=None):
            calls["n"] += 1
            if calls["n"] == 6:
                return {"entries": {}, "coverage": {}}
            return orig_fp(root, allowed)

        with mock.patch.object(pipeline, "_fingerprint_paths", side_effect=bad_fp):
            res = _run_sync(
                contract, **self._happy_kwargs(wt), security_hook=_security_approve_current
            )
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")

    def test_incomplete_coverage_missing_fails_closed(self):
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("stable\n", encoding="utf-8")
        good = pipeline._fingerprint_paths(Path(wt), ["allowed"])
        self.assertTrue(pipeline._validate_fingerprint_result(good, ["allowed"])[0] is True)
        bad = {"schema": good["schema"], "ok": True, "complete": True, "entries": dict(good["entries"]), "coverage": {}}
        ok, _ = pipeline._validate_fingerprint_result(bad, ["allowed"])
        self.assertFalse(ok)
        # pipeline with coverage-missing after-validate must fail closed
        repo = _mk_repo()
        contract = _base_contract(repo, task_id="t-r2218-coverage-01")
        orig_fp = pipeline._fingerprint_paths
        calls = {"n": 0}

        def incomplete_fp(root, allowed=None):
            calls["n"] += 1
            base = orig_fp(root, allowed)
            if calls["n"] == 2:
                trimmed = dict(base)
                trimmed["coverage"] = {}
                return trimmed
            return base

        with mock.patch.object(pipeline, "_fingerprint_paths", side_effect=incomplete_fp):
            res = _run_sync(contract, **self._happy_kwargs(wt))
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")

    def test_comparison_exception_cannot_be_swallowed(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-r2218-cmp-exc-01")
        with mock.patch.object(pipeline, "_check_fingerprints_equal", side_effect=RuntimeError("mocked comparison boom")):
            res = _run_sync(contract, **self._happy_kwargs(wt))
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")

    def test_stat_read_readlink_hash_walk_failures_fail_closed(self):
        # read failure
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        (Path(wt) / "allowed" / "file.py").write_text("stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-r2218-stat-read-01")
        orig_read = Path.read_bytes

        def failing_read(self, *a, **kw):
            try:
                s = str(self)
            except Exception:
                s = ""
            if s.endswith("allowed/file.py"):
                raise PermissionError("mocked read failure 2218")
            return orig_read(self, *a, **kw)

        with mock.patch.object(Path, "read_bytes", failing_read):
            res = _run_sync(contract, **self._happy_kwargs(wt))
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")
        # stat failure
        contract2 = _base_contract(repo, task_id="t-r2218-stat-02")
        with mock.patch.object(Path, "is_symlink", side_effect=PermissionError("mocked stat failure")):
            res2 = _run_sync(contract2, **self._happy_kwargs(wt))
        self.assertEqual(res2.get("verdict"), "FAILED_INFRA")
        # walk failure
        contract3 = _base_contract(repo, task_id="t-r2218-walk-03")
        with mock.patch.object(Path, "rglob", side_effect=OSError("mocked walk failure")):
            res3 = _run_sync(contract3, **self._happy_kwargs(wt))
        self.assertEqual(res3.get("verdict"), "FAILED_INFRA")
        # hash failure
        contract4 = _base_contract(repo, task_id="t-r2218-hash-04")
        import hashlib as _hl
        with mock.patch.object(_hl, "sha256", side_effect=RuntimeError("mocked hash failure")):
            res4 = _run_sync(contract4, **self._happy_kwargs(wt))
        self.assertEqual(res4.get("verdict"), "FAILED_INFRA")

    def test_same_path_mutation_cannot_hide_behind_invalid_empty(self):
        repo = _mk_repo()
        wt = _mk_wt()
        (Path(wt) / "allowed").mkdir(parents=True, exist_ok=True)
        target = Path(wt) / "allowed" / "file.py"
        target.write_text("v1-stable\n", encoding="utf-8")
        contract = _base_contract(repo, task_id="t-r2218-hide-empty-01")

        def empty_fp(root, allowed=None):
            return {}

        def validator_mutate(args: dict):
            target.write_text("v2-mutated\n", encoding="utf-8")
            return _validator_pass(args)

        with mock.patch.object(pipeline, "_fingerprint_paths", side_effect=empty_fp):
            res = _run_sync(
                contract,
                create_worktree_fn=_create_fn(wt),
                get_source_state_fn=_src_fn(),
                get_worktree_state_fn=_wt_state_fn(status="M allowed/file.py"),
                get_actual_diff_fn=_diff_fn(["allowed/file.py"]),
                worker_runner=_worker_ok,
                validator_runner=validator_mutate,
                reviewer_runner=_reviewer_approve,
            )
        self.assertNotEqual(res.get("verdict"), "READY_FOR_HUMAN_REVIEW")
        self.assertEqual(res.get("verdict"), "FAILED_INFRA")

    def test_explicit_contract_helpers_present(self):
        self.assertTrue(hasattr(pipeline, "_validate_fingerprint_result"))
        self.assertTrue(hasattr(pipeline, "_check_fingerprints_equal"))
        self.assertTrue(hasattr(pipeline, "_FINGERPRINT_SCHEMA") or hasattr(pipeline, "_validate_fingerprint_result"))
        text = Path(pipeline.__file__).read_text(encoding="utf-8")
        self.assertIn("_validate_fingerprint_result", text)
        self.assertIn("_check_fingerprints_equal", text)
        self.assertIn("coverage", text)
        self.assertIn("absent", text)
        # fail-closed: no swallowed comparison exceptions
        # integrity comparisons must use validated contract, not bare isinstance skip
        self.assertNotIn("if isinstance(_pre_gate_fp, dict) and isinstance(_post_gate_fp, dict)", text)


class TestFingerprintStabilityR2225(unittest.TestCase):
    """R2.2.25: accept only two equal consecutive inode-aware full snapshots."""

    def test_nested_mutation_after_local_pass_forces_new_full_pair(self):
        wt = Path(_mk_wt())
        nested = wt / "allowed" / "nested"
        nested.mkdir(parents=True)
        (nested / "seed.txt").write_text("seed\n", encoding="utf-8")
        original_lstat = pipeline._lstat
        original_collect = pipeline._collect_fingerprint_once
        local_checks = {"n": 0}
        collections = {"n": 0}

        def mutate_after_nested_final_lstat(path, rel, absent_ok=False):
            result = original_lstat(path, rel, absent_ok)
            if rel == "allowed/nested" and result is not None:
                local_checks["n"] += 1
                if local_checks["n"] == 2:
                    (nested / "late.txt").write_text("late\n", encoding="utf-8")
            return result

        def counted_collect(base, allowed):
            collections["n"] += 1
            return original_collect(base, allowed)

        with mock.patch.object(pipeline, "_lstat", side_effect=mutate_after_nested_final_lstat):
            with mock.patch.object(pipeline, "_collect_fingerprint_once", side_effect=counted_collect):
                result = pipeline._fingerprint_paths(wt, ["allowed"])
        self.assertIn("allowed/nested/late.txt", result["entries"])
        self.assertEqual(collections["n"], 4)

    def test_same_content_file_inode_replacement_between_passes_retries(self):
        wt = Path(_mk_wt())
        target = wt / "allowed" / "same.txt"
        target.parent.mkdir(parents=True)
        target.write_text("identical\n", encoding="utf-8")
        original_collect = pipeline._collect_fingerprint_once
        calls = {"n": 0}

        def replace_after_first(base, allowed):
            result = original_collect(base, allowed)
            calls["n"] += 1
            if calls["n"] == 1:
                replacement = target.with_name("same.replacement")
                replacement.write_text("identical\n", encoding="utf-8")
                os.replace(replacement, target)
            return result

        with mock.patch.object(pipeline, "_collect_fingerprint_once", side_effect=replace_after_first):
            result = pipeline._fingerprint_paths(wt, ["allowed/same.txt"])
        self.assertEqual(result["coverage"]["allowed/same.txt"], "file")
        self.assertEqual(calls["n"], 4)

    def test_same_target_symlink_inode_replacement_between_passes_retries(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        wt = Path(_mk_wt())
        allowed = wt / "allowed"
        allowed.mkdir(parents=True)
        link = allowed / "link"
        try:
            os.symlink("unchanged-target", link)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        original_collect = pipeline._collect_fingerprint_once
        calls = {"n": 0}

        def replace_after_first(base, requested):
            result = original_collect(base, requested)
            calls["n"] += 1
            if calls["n"] == 1:
                replacement = allowed / "replacement-link"
                os.symlink("unchanged-target", replacement)
                os.replace(replacement, link)
            return result

        with mock.patch.object(pipeline, "_collect_fingerprint_once", side_effect=replace_after_first):
            result = pipeline._fingerprint_paths(wt, ["allowed/link"])
        self.assertEqual(result["coverage"]["allowed/link"], "symlink")
        self.assertEqual(calls["n"], 4)

    def test_one_transient_snapshot_mismatch_succeeds_with_bounded_retry(self):
        wt = Path(_mk_wt())
        target = wt / "allowed" / "value.txt"
        target.parent.mkdir(parents=True)
        target.write_text("one\n", encoding="utf-8")
        original_collect = pipeline._collect_fingerprint_once
        calls = {"n": 0}

        def mutate_once(base, allowed):
            result = original_collect(base, allowed)
            calls["n"] += 1
            if calls["n"] == 1:
                target.write_text("two\n", encoding="utf-8")
            return result

        with mock.patch.object(pipeline, "_collect_fingerprint_once", side_effect=mutate_once):
            result = pipeline._fingerprint_paths(wt, ["allowed"])
        self.assertTrue(result["ok"] is True)
        self.assertEqual(calls["n"], 4)

    def test_repeated_snapshot_mismatch_exhausts_bounded_retry(self):
        wt = Path(_mk_wt())
        target = wt / "allowed" / "same.txt"
        target.parent.mkdir(parents=True)
        target.write_text("same\n", encoding="utf-8")
        original_collect = pipeline._collect_fingerprint_once
        calls = {"n": 0}

        def replace_after_every_snapshot(base, allowed):
            result = original_collect(base, allowed)
            calls["n"] += 1
            replacement = target.with_name(f"replacement-{calls['n']}")
            replacement.write_text("same\n", encoding="utf-8")
            os.replace(replacement, target)
            return result

        with mock.patch.object(
            pipeline, "_collect_fingerprint_once", side_effect=replace_after_every_snapshot
        ):
            with self.assertRaises(pipeline._FingerprintError):
                pipeline._fingerprint_paths(wt, ["allowed/same.txt"])
        self.assertEqual(calls["n"], pipeline._FINGERPRINT_STABILITY_ATTEMPTS * 2)


class TestFingerprintRequestedDomainR2225(unittest.TestCase):
    """R2.2.25: requested coverage authorizes only exact items or dir descendants."""

    def _tree(self):
        wt = Path(_mk_wt())
        allowed = wt / "allowed"
        allowed.mkdir(parents=True)
        (allowed / "file.txt").write_text("allowed\n", encoding="utf-8")
        outside = wt / "outside"
        outside.mkdir()
        (outside / "file.txt").write_text("outside\n", encoding="utf-8")
        return wt

    def _with_extra(self, base, key, marker):
        candidate = dict(base)
        candidate["entries"] = dict(base["entries"])
        candidate["entries"][key] = marker
        return candidate

    def test_arbitrary_extra_entry_rejected(self):
        wt = self._tree()
        base = pipeline._fingerprint_paths(wt, ["allowed/file.txt"])
        outside = pipeline._fingerprint_paths(wt, ["outside/file.txt"])
        candidate = self._with_extra(base, "outside/file.txt", outside["entries"]["outside/file.txt"])
        ok, _ = pipeline._validate_fingerprint_result(candidate, ["allowed/file.txt"])
        self.assertFalse(ok)

    def test_descendant_under_requested_directory_accepted(self):
        wt = self._tree()
        result = pipeline._fingerprint_paths(wt, ["allowed"])
        self.assertIn("allowed/file.txt", result["entries"])
        ok, reason = pipeline._validate_fingerprint_result(result, ["allowed"])
        self.assertTrue(ok, reason)

    def test_descendant_under_requested_file_rejected(self):
        wt = self._tree()
        base = pipeline._fingerprint_paths(wt, ["allowed/file.txt"])
        marker = base["entries"]["allowed/file.txt"]
        candidate = self._with_extra(base, "allowed/file.txt/child", marker)
        ok, _ = pipeline._validate_fingerprint_result(candidate, ["allowed/file.txt"])
        self.assertFalse(ok)

    def test_prefix_confusion_rejected(self):
        wt = self._tree()
        foo = wt / "foo"
        foobar = wt / "foobar"
        foo.mkdir()
        foobar.mkdir()
        (foobar / "child.txt").write_text("x\n", encoding="utf-8")
        base = pipeline._fingerprint_paths(wt, ["foo"])
        outside = pipeline._fingerprint_paths(wt, ["foobar/child.txt"])
        candidate = self._with_extra(
            base, "foobar/child.txt", outside["entries"]["foobar/child.txt"]
        )
        ok, _ = pipeline._validate_fingerprint_result(candidate, ["foo"])
        self.assertFalse(ok)

    def test_extra_absent_special_symlink_and_file_markers_rejected(self):
        wt = self._tree()
        base = pipeline._fingerprint_paths(wt, ["allowed"])
        markers = [("outside/missing", pipeline._FINGERPRINT_ABSENT)]
        outside_file = pipeline._fingerprint_paths(wt, ["outside/file.txt"])
        markers.append(("outside/file.txt", outside_file["entries"]["outside/file.txt"]))
        link = wt / "outside" / "link"
        if hasattr(os, "symlink"):
            try:
                os.symlink("target", link)
                link_fp = pipeline._fingerprint_paths(wt, ["outside/link"])
                markers.append(("outside/link", link_fp["entries"]["outside/link"]))
            except OSError:
                pass
        fifo = wt / "outside" / "pipe"
        if hasattr(os, "mkfifo"):
            try:
                os.mkfifo(fifo)
                fifo_fp = pipeline._fingerprint_paths(wt, ["outside/pipe"])
                markers.append(("outside/pipe", fifo_fp["entries"]["outside/pipe"]))
            except OSError:
                pass
        self.assertGreaterEqual(len(markers), 2)
        for key, marker in markers:
            candidate = self._with_extra(base, key, marker)
            ok, _ = pipeline._validate_fingerprint_result(candidate, ["allowed"])
            self.assertFalse(ok, key)

    def test_absolute_and_parent_entry_keys_rejected(self):
        wt = self._tree()
        base = pipeline._fingerprint_paths(wt, ["allowed"])
        marker = base["entries"]["allowed/file.txt"]
        for key in ("/absolute/file.txt", "allowed/../outside/file.txt"):
            candidate = self._with_extra(base, key, marker)
            ok, _ = pipeline._validate_fingerprint_result(candidate, ["allowed"])
            self.assertFalse(ok, key)


class TestFingerprintNonDirectoryAncestorsR2227(unittest.TestCase):
    """R2.2.27: only a genuinely missing requested path may be absent."""

    def test_child_beneath_regular_file_fails_closed(self):
        wt = Path(_mk_wt())
        (wt / "file").write_text("not a directory\n", encoding="utf-8")
        with self.assertRaises(pipeline._FingerprintError):
            pipeline._fingerprint_paths(wt, ["file/child"])

    def test_child_beneath_fifo_fails_closed_but_fifo_itself_is_fingerprinted(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFOs unavailable")
        wt = Path(_mk_wt())
        fifo = wt / "pipe"
        try:
            os.mkfifo(fifo)
        except OSError as exc:
            self.skipTest(f"FIFOs unavailable: {exc}")
        direct = pipeline._fingerprint_paths(wt, ["pipe"])
        self.assertEqual(direct["coverage"]["pipe"], "fifo")
        with self.assertRaises(pipeline._FingerprintError):
            pipeline._fingerprint_paths(wt, ["pipe/child"])

    def test_child_beneath_socket_fails_closed_but_socket_itself_is_fingerprinted(self):
        import socket

        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("Unix sockets unavailable")
        wt = Path(_mk_wt())
        socket_path = wt / "endpoint.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            try:
                sock.bind(str(socket_path))
            except OSError as exc:
                self.skipTest(f"Unix sockets not permitted: {exc}")
            direct = pipeline._fingerprint_paths(wt, ["endpoint.sock"])
            self.assertEqual(direct["coverage"]["endpoint.sock"], "socket")
            with self.assertRaises(pipeline._FingerprintError):
                pipeline._fingerprint_paths(wt, ["endpoint.sock/child"])
        finally:
            sock.close()

    def test_genuinely_missing_requested_path_remains_explicit_absent(self):
        wt = Path(_mk_wt())
        (wt / "existing").mkdir()
        result = pipeline._fingerprint_paths(wt, ["existing/missing/child"])
        self.assertEqual(result["entries"]["existing/missing/child"], "absent")
        self.assertEqual(result["coverage"]["existing/missing/child"], "absent")


class TestFingerprintUnrestrictedRootIdentityR2227(unittest.TestCase):
    """R2.2.27: unrestricted equality includes the root directory inode."""

    def test_unrestricted_schema_requires_valid_root_directory_identity(self):
        wt = Path(_mk_wt())
        (wt / "same.txt").write_text("identical\n", encoding="utf-8")
        result = pipeline._fingerprint_paths(wt)
        self.assertEqual(pipeline._fingerprint_marker_kind(result.get("root")), "dir")
        self.assertTrue(pipeline._validate_fingerprint_result(result)[0])

        missing = dict(result)
        missing.pop("root")
        self.assertFalse(pipeline._validate_fingerprint_result(missing)[0])

        invalid = dict(result)
        invalid["root"] = result["entries"]["same.txt"]
        self.assertFalse(pipeline._validate_fingerprint_result(invalid)[0])

    def test_identical_content_root_replacement_between_passes_retries(self):
        wt = Path(_mk_wt())
        (wt / "same.txt").write_text("identical\n", encoding="utf-8")

        # Build two individually valid unrestricted snapshots with identical visible
        # entries/coverage/policy but different real root-directory identity.
        first = pipeline._collect_fingerprint_once(wt, None)
        other = Path(_mk_wt())
        (other / "same.txt").write_text("identical\n", encoding="utf-8")
        other_snapshot = pipeline._collect_fingerprint_once(other, None)

        second = dict(first)
        second["root"] = other_snapshot["root"]

        self.assertEqual(first["entries"], second["entries"])
        self.assertEqual(first["coverage"], second["coverage"])
        self.assertEqual(first["policy"], second["policy"])
        self.assertNotEqual(first["root"], second["root"])
        self.assertTrue(pipeline._validate_fingerprint_result(first)[0])
        self.assertTrue(pipeline._validate_fingerprint_result(second)[0])
        self.assertFalse(pipeline._check_fingerprints_equal(first, second))

        # Simulate the exact between-pass condition deterministically: the first
        # pair differs only by root identity, so stability must retry; the second
        # pair is stable and must be accepted.
        sequence = [first, second, second, second]
        with mock.patch.object(
            pipeline, "_collect_fingerprint_once", side_effect=sequence
        ) as collect:
            result = pipeline._fingerprint_paths(wt)

        self.assertEqual(collect.call_count, 4)
        self.assertEqual(result, second)


class TestFingerprintEmptyRequestedScopeR2227(unittest.TestCase):
    """R2.2.27: an explicit empty requested scope never becomes unrestricted."""

    def test_empty_requested_iterables_fail_collection_closed(self):
        wt = Path(_mk_wt())
        (wt / "visible.txt").write_text("visible\n", encoding="utf-8")
        empty_values = ([], (), set(), (item for item in ()))
        for empty in empty_values:
            with self.assertRaises(pipeline._FingerprintError):
                pipeline._fingerprint_paths(wt, empty)

    def test_empty_requested_scope_fails_internal_collection_validation_and_comparison(self):
        wt = Path(_mk_wt())
        (wt / "visible.txt").write_text("visible\n", encoding="utf-8")
        unrestricted = pipeline._fingerprint_paths(wt, None)

        with self.assertRaises(pipeline._FingerprintError):
            pipeline._collect_fingerprint_once(wt, [])
        ok, _ = pipeline._validate_fingerprint_result(unrestricted, [])
        self.assertFalse(ok)
        with self.assertRaises(pipeline._FingerprintError):
            pipeline._check_fingerprints_equal(unrestricted, unrestricted, [])


if __name__ == "__main__":
    unittest.main()
