"""M-SAN-001 regression tests: one canonical sanitization policy.

Verifies that jho_safe is the shared primitive and that no relevant surface
(persisted artifacts, evidence ZIP, pipeline results, TUI, notifications)
leaks credential values under decorated key names such as secretKey,
my_secret or api_key2.

All secrets used here are synthetic audit sentinels (AUDIT_SYNTHETIC_*).
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "runner"
sys.path.insert(0, str(RUNNER))

import jho_operator
import jho_pipeline
import jho_safe
import jho_tui

def _load_repo_jho_lite():
    """Load THIS repo's jho_lite.py explicitly.

    A plain ``import jho_lite`` may resolve to the pinned active runtime copy
    registered in sys.modules by jho_operator's bootstrap; M-SAN-001 covers
    the preserved repo source, which additionally requires the protoAgent
    plugins seam and is therefore optional in bare test environments.
    """
    import importlib.util

    try:
        spec = importlib.util.spec_from_file_location(
            "jho_lite_repo_under_test", RUNNER / "jho_lite.py"
        )
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


jho_lite = _load_repo_jho_lite()


SENTINEL = "AUDIT_SYNTHETIC_SECRET_123"

# Keys reproduced by the independent audit plus the full required name list.
CREDENTIAL_KEYS = (
    "password",
    "passwd",
    "secret",
    "secret_key",
    "secret-key",
    "secretKey",
    "client_secret",
    "client-secret",
    "clientSecret",
    "api_key",
    "api-key",
    "apiKey",
    "api_key2",
    "access_token",
    "access-token",
    "accessToken",
    "auth_token",
    "authorization",
    "Authorization",
    "token",
    "cookie",
    "x-api-key",
    "PRIVATE_KEY",
    "credential",
    "My_Secret_Value",
)

SECRET_STRINGS = (
    f"password={SENTINEL}",
    f"password: {SENTINEL}",
    f"secretKey={SENTINEL}",
    f"secret_key: {SENTINEL}",
    f"my_secret: {SENTINEL}",
    f"api-key={SENTINEL}",
    f"api_key2={SENTINEL}",
    f"accessToken={SENTINEL}",
    f"clientSecret={SENTINEL}",
    f"Authorization: Bearer {SENTINEL}",
    f"Authorization: Basic {SENTINEL}",
    f"Bearer {SENTINEL}",
    f'{{"secretKey": "{SENTINEL}"}}',
    f'{{"my_secret": "{SENTINEL}"}}',
    f'{{"api_key2": "{SENTINEL}"}}',
)

# Names that merely contain "key"/"token" fragments must NOT be redacted.
FALSE_POSITIVE_KEYS = (
    "keyboard_layout",
    "monkey_patch",
    "primary_key_name",
    "key_count",
    "max_tokens",
    "token_count",
    "task_id",
    "changed_paths",
    "duration_seconds",
    "stdout_tail",
)


class TestCanonicalKeyPolicy(unittest.TestCase):
    def test_required_credential_key_names_are_redacted(self):
        for key in CREDENTIAL_KEYS:
            with self.subTest(key=key):
                out = jho_safe.sanitize({key: SENTINEL})
                self.assertEqual(out[key], "[REDACTED]")
                self.assertNotIn(SENTINEL, json.dumps(out))

    def test_case_variants_are_redacted(self):
        for key in ("SECRETKEY", "SecretKey", "API_KEY2", "Api-Key", "ACCESSTOKEN"):
            with self.subTest(key=key):
                out = jho_safe.sanitize({key: SENTINEL})
                self.assertEqual(out[key], "[REDACTED]")

    def test_nested_dict_list_dict_is_redacted(self):
        sample = {
            "outer": {
                "items": [
                    {"secretKey": SENTINEL},
                    [{"clientSecret": SENTINEL}, {"safe": "keep-me"}],
                ],
                "diagnostic": f"api_key2={SENTINEL}",
            },
        }
        out = jho_safe.sanitize(sample)
        dumped = json.dumps(out)
        self.assertNotIn(SENTINEL, dumped)
        self.assertEqual(out["outer"]["items"][0]["secretKey"], "[REDACTED]")
        self.assertEqual(out["outer"]["items"][1][0]["clientSecret"], "[REDACTED]")
        self.assertEqual(out["outer"]["items"][1][1]["safe"], "keep-me")
        self.assertIn("[REDACTED]", dumped)

    def test_false_positive_keys_are_preserved(self):
        sample = {key: "benign-value" for key in FALSE_POSITIVE_KEYS}
        out = jho_safe.sanitize(sample)
        for key in FALSE_POSITIVE_KEYS:
            with self.subTest(key=key):
                self.assertEqual(out[key], "benign-value")

    def test_structure_survives_and_depth_is_bounded(self):
        deep: dict = {"secretKey": SENTINEL}
        node = deep
        for _ in range(40):
            node["next"] = {"level": 1}
            node = node["next"]
        out = jho_safe.sanitize(deep)
        dumped = json.dumps(out)
        self.assertNotIn(SENTINEL, dumped)
        self.assertEqual(out["secretKey"], "[REDACTED]")

    def test_unexpected_types_never_raise(self):
        class Boom:
            def __str__(self):
                raise RuntimeError("boom")

        for value in (object(), Boom(), {1, 2, 3}, b"bytes", lambda: None, float("nan")):
            with self.subTest(value=type(value).__name__):
                jho_safe.sanitize({"payload": value, "call": lambda: 1})

    def test_container_size_is_bounded(self):
        out = jho_safe.sanitize({"big": list(range(jho_safe.MAX_ITEMS + 100))})
        self.assertLessEqual(len(out["big"]), jho_safe.MAX_ITEMS + 1)

    def test_redaction_is_idempotent(self):
        sample = {"secretKey": SENTINEL, "log": f"Authorization: Bearer {SENTINEL}"}
        once = jho_safe.sanitize(sample)
        twice = jho_safe.sanitize(once)
        self.assertEqual(json.dumps(once, sort_keys=True), json.dumps(twice, sort_keys=True))


class TestCanonicalTextPolicy(unittest.TestCase):
    def test_required_secret_strings_are_redacted(self):
        for text in SECRET_STRINGS:
            with self.subTest(text=text):
                out = jho_safe.redact_text(text)
                self.assertNotIn(SENTINEL, out)
                self.assertIn("[REDACTED]", out)

    def test_diagnostic_structure_is_preserved(self):
        out = jho_safe.redact_text(f"secretKey={SENTINEL}")
        self.assertTrue(out.startswith("secretKey="))
        out = jho_safe.redact_text(f"Authorization: Bearer {SENTINEL}")
        self.assertIn("Bearer", out)

    def test_false_positive_text_is_untouched(self):
        text = "keyboard_layout=es monkey_patch primary_key_name key_count=3"
        self.assertEqual(jho_safe.redact_text(text), text)


class TestPipelineConvergence(unittest.TestCase):
    def test_pipeline_uses_canonical_policy_for_credential_keys(self):
        for key in CREDENTIAL_KEYS:
            with self.subTest(key=key):
                out = jho_pipeline.sanitize_value({key: SENTINEL})
                self.assertEqual(out[key], "[REDACTED]")

    def test_pipeline_uses_canonical_policy_for_secret_strings(self):
        for text in SECRET_STRINGS:
            with self.subTest(text=text):
                dumped = json.dumps(jho_pipeline.sanitize_value({"diagnostic": text}))
                self.assertNotIn(SENTINEL, dumped)

    def test_pipeline_payload_minimization_still_applies(self):
        out = jho_pipeline.sanitize_value(
            {"answer": "plain summary", "stdout": "plain", "output": "plain", "status": "ok"}
        )
        self.assertEqual(out["answer"], "[REDACTED]")
        self.assertEqual(out["stdout"], "[REDACTED]")
        self.assertEqual(out["output"], "[REDACTED]")
        self.assertEqual(out["status"], "ok")

    def test_canonical_policy_keeps_stage_artifact_answer(self):
        out = jho_safe.sanitize({"answer": "plain worker summary", "status": "completed"})
        self.assertEqual(out["answer"], "plain worker summary")


class TestTuiSurface(unittest.TestCase):
    def test_tui_redacts_secret_forms(self):
        for text in SECRET_STRINGS:
            with self.subTest(text=text):
                out = jho_tui.sanitize(text)
                self.assertNotIn(SENTINEL, out)
                self.assertIn("[REDACTED]", out)

    def test_tui_rendered_task_output_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task-a"
            task.mkdir()
            (task / "manifest.json").write_text(
                json.dumps({"task_id": "SAN-001"}), encoding="utf-8"
            )
            (task / "worker_result.json").write_text(
                json.dumps(
                    {
                        "task_id": "SAN-001",
                        "status": f"failed secretKey={SENTINEL}",
                        "diagnostic": f"Authorization: Basic {SENTINEL}",
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                jho_tui.main(["--once", "--runs-root", str(root)])
            out = buf.getvalue()
        self.assertNotIn(SENTINEL, out)
        self.assertIn("[REDACTED]", out)


class TestPersistedArtifactsAndZip(unittest.TestCase):
    def test_write_json_persistence_is_redacted(self):
        payload = {
            "schema_version": "1.0",
            "task_id": "SAN-002",
            "status": "completed",
            "secretKey": SENTINEL,
            "my_secret": SENTINEL,
            "api_key2": SENTINEL,
            "error": f"Authorization: Bearer {SENTINEL}",
            "nested": {"items": [{"clientSecret": SENTINEL}]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "worker_result.json"
            clean = jho_safe.write_json(path, payload)
            text = path.read_text(encoding="utf-8")
        self.assertNotIn(SENTINEL, text)
        self.assertIn("[REDACTED]", text)
        self.assertEqual(clean["secretKey"], "[REDACTED]")
        self.assertEqual(clean["status"], "completed")

    def test_evidence_zip_resanitizes_unsanitized_producer_artifacts(self):
        sentinels = {
            "secretKey": "AUDIT_SYNTHETIC_ZIP_KEY_111",
            "my_secret": "AUDIT_SYNTHETIC_ZIP_SECRET_222",
            "api_key2": "AUDIT_SYNTHETIC_ZIP_API_333",
            "clientSecret": "AUDIT_SYNTHETIC_ZIP_CLIENT_444",
            "accessToken": "AUDIT_SYNTHETIC_ZIP_TOKEN_555",
            "Authorization": "AUDIT_SYNTHETIC_ZIP_AUTH_666",
        }
        raw = {
            "status": "completed",
            "diagnostic": (
                "secretKey=AUDIT_SYNTHETIC_ZIP_STR_777 and "
                "Authorization: Basic AUDIT_SYNTHETIC_ZIP_BASIC_888"
            ),
            "nested": [{"api-key": "AUDIT_SYNTHETIC_ZIP_NESTED_999"}],
            **sentinels,
        }
        all_sentinels = list(sentinels.values()) + [
            "AUDIT_SYNTHETIC_ZIP_STR_777",
            "AUDIT_SYNTHETIC_ZIP_BASIC_888",
            "AUDIT_SYNTHETIC_ZIP_NESTED_999",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            task_id = "SAN-001-ZIP-A01"
            run_id = "RUN-SAN-001"
            task = home / "agent-runs" / "jho-worktrees" / task_id
            task.mkdir(parents=True)
            for name in ("worker_result.json", "validator_result.json", "review_result.json"):
                (task / name).write_text(
                    json.dumps({"task_id": task_id, "run_id": run_id, **raw}),
                    encoding="utf-8",
                )
            (task / "manifest.json").write_text(
                json.dumps({"task_id": task_id}), encoding="utf-8"
            )
            with mock.patch.object(jho_operator.Path, "home", return_value=home):
                output = jho_operator.export_evidence(
                    logical_task_id="SAN-001-ZIP",
                    attempts=[{"task_id": task_id, "run_id": run_id, "attempt": 1}],
                    final_result={"task_id": task_id, "run_id": run_id, **raw},
                    notification={"reason": f"Authorization: Bearer {SENTINEL}"},
                    export_dir=Path(tmp) / "exports",
                )
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                content = "\n".join(
                    archive.read(name).decode("utf-8") for name in names
                )
        self.assertIn("operator_summary.json", names)
        self.assertIn("pipeline_result.json", names)
        self.assertIn(f"attempts/{task_id}/worker_result.json", names)
        for value in all_sentinels + [SENTINEL]:
            self.assertNotIn(value, content)
        self.assertIn("[REDACTED]", content)

    def test_notification_text_is_redacted_at_operator_boundary(self):
        captured: dict = {}

        class FakeNotify:
            @staticmethod
            def notify_windows_best_effort(**kwargs):
                captured.update(kwargs)
                return {"attempted": True, "success": True}

        with mock.patch.object(
            jho_operator, "_load_notify_module", return_value=(FakeNotify, "fake")
        ):
            out = jho_operator.notify_result(
                {"verdict": "READY_FOR_HUMAN_REVIEW"},
                logical_task_id=f"TASK secretKey={SENTINEL}",
                candidate=f"cand api_key2={SENTINEL}",
                duration_seconds=1.0,
                enabled=True,
            )
        self.assertTrue(out["attempted"])
        self.assertNotIn(SENTINEL, captured["candidate"])
        self.assertIn("[REDACTED]", captured["candidate"])
        self.assertNotIn(SENTINEL, json.dumps(out))


class TestSurfaceWiring(unittest.TestCase):
    """The shared primitive must be consumed; divergent local lists must not
    reappear on the reviewed surfaces."""

    def test_surfaces_consume_jho_safe(self):
        for name in ("jho_pipeline.py", "jho_tui.py", "jho_lite.py", "jho_operator.py"):
            with self.subTest(module=name):
                text = (RUNNER / name).read_text(encoding="utf-8")
                self.assertIn("jho_safe", text)

    def test_persistent_writers_use_shared_boundary(self):
        for name in ("jho_worker.py", "jho_validate.py", "jho_review.py"):
            with self.subTest(module=name):
                text = (RUNNER / name).read_text(encoding="utf-8")
                self.assertIn("jho_safe.write_json", text)

    def test_no_independent_secret_regex_lists_remain(self):
        pipeline_text = (RUNNER / "jho_pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn("_SECRET_RES", pipeline_text)
        lite_text = (RUNNER / "jho_lite.py").read_text(encoding="utf-8")
        self.assertNotIn("pats = [", lite_text)
        tui_text = (RUNNER / "jho_tui.py").read_text(encoding="utf-8")
        self.assertNotIn("_ASSIGNMENT_RE", tui_text)

    @unittest.skipIf(jho_lite is None, "pinned protoAgent plugins unavailable")
    def test_jho_lite_redact_text_delegates(self):
        for text in SECRET_STRINGS:
            with self.subTest(text=text):
                out = jho_lite.redact_text(text)
                self.assertNotIn(SENTINEL, out)


if __name__ == "__main__":
    unittest.main()
