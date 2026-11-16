#!/usr/bin/env python3
from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.recognize import recognize_image_base64, recognize_image_file  # noqa: E402


class RecognizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        images = sorted((ROOT / "captures").glob("captcha_*.jpg"))
        if not images:
            raise unittest.SkipTest("no captcha_*.jpg under captures/")
        cls.image = images[0]

    def test_recognize_image_file(self) -> None:
        out = recognize_image_file(self.image)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["points"]), 3)

    def test_recognize_image_base64(self) -> None:
        b64 = base64.b64encode(self.image.read_bytes()).decode("ascii")
        out = recognize_image_base64(b64, debug_dir=ROOT / "results")
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["points"]), 3)


if __name__ == "__main__":
    unittest.main()
