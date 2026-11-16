"""Local captcha icon recognition helpers (no network /check)."""

from __future__ import annotations

import base64
import binascii
import tempfile
from pathlib import Path
from typing import Any

from src.yidun_vision import solve_image_points


def recognize_image_file(
    image_path: str | Path,
    *,
    debug_image_path: str | Path | None = None,
) -> dict[str, Any]:
    points, vision = solve_image_points(image_path, debug_image_path=debug_image_path)
    return {
        "ok": True,
        "points": [list(p) for p in points],
        "vision": {
            "ranked_count": len(vision.get("ranked_assignments") or []),
            "assignment": vision.get("assignment"),
            "debug_image": vision.get("debug_image"),
        },
    }


def recognize_image_base64(
    image_b64: str,
    *,
    suffix: str = ".jpg",
    debug_dir: str | Path | None = None,
) -> dict[str, Any]:
    raw = image_b64.strip()
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid image_base64: {exc}") from exc
    if not data:
        raise ValueError("empty image_base64")

    debug_root = Path(debug_dir) if debug_dir else Path(tempfile.gettempdir())
    debug_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="yidun_rec_", suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        debug_path = debug_root / f"{tmp_path.stem}_debug.png"
        return recognize_image_file(tmp_path, debug_image_path=debug_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
