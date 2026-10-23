from __future__ import annotations
import ast
import hashlib
import unittest
from pathlib import Path

EXPECTED_SHA = "fe153523539ef54ae07248bbe13b5f6292ec9862b7585c92d243cc4314772290"

class TestClosureCleanup(unittest.TestCase):
    def test_pipeline_hash_and_no_unreachable_fingerprint_tail(self):
        path = Path(__file__).resolve().parents[1] / "runner" / "jho_pipeline.py"
        data = path.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), EXPECTED_SHA)
        tree = ast.parse(data.decode("utf-8"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_fingerprint_paths")
        non_doc = list(fn.body)
        if non_doc and isinstance(non_doc[0], ast.Expr) and isinstance(getattr(non_doc[0], "value", None), ast.Constant):
            non_doc = non_doc[1:]
        self.assertEqual(len(non_doc), 1)
        self.assertIsInstance(non_doc[0], ast.Return)

if __name__ == "__main__":
    unittest.main()
