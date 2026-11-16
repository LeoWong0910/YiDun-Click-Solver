#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_api():
    path = ROOT / "tools" / "nas_api_server.py"
    spec = importlib.util.spec_from_file_location("nas_api_server", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BuildArgvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.api = _load_api()

    def test_default_includes_auto(self) -> None:
        argv = self.api._build_argv({"max_attempts": 3})
        self.assertIn("--auto", argv)
        self.assertIn("--max-attempts", argv)
        self.assertIn("3", argv)
        self.assertIn("--output-detail", argv)
        self.assertIn("useful", argv)

    def test_get_only_uses_flag(self) -> None:
        argv = self.api._build_argv({"get_only": True})
        self.assertIn("--get-only", argv)


if __name__ == "__main__":
    unittest.main()
