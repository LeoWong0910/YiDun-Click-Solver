"""Small CV helper for YiDun icon-click captcha images.

The protocol signer is pure Python in :mod:`src.yidun_crypto`; this module is
only responsible for selecting the three click coordinates from the downloaded
captcha image.  It does not call a browser or a remote recognition service.

The current Zhihu/YiDun type=7 image layout is:

* 320x160 top area: candidate icons over a background painting
* y=161..180, x=0..80: the rendered three-icon prompt row

The primary solver is local OpenCV ``TM_CCOEFF_NORMED`` template matching plus
NCC/chamfer/mask-SSIM scoring.  A local ddddocr detector is used only as an
optional candidate supplement when installed; SIFT is kept as a tiny optional
compatibility feature and is no longer a hard dependency or decisive signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations
from pathlib import Path
from typing import Any, Sequence


@dataclass
class Candidate:
    box: tuple[int, int, int, int]
    source: str
    # JSON-friendly side-channel used by the matcher.  For example
    # ``{"template_scores": {"0": 0.34}}`` means this candidate was also
    # proposed by prompt-icon template search for prompt #0.
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.box
        return (int(round((x1 + x2) / 2)), int(round((y1 + y2) / 2)))


PROMPT_SPLITS = ((0, 25), (25, 53), (53, 80))


def _require_cv():
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as e:  # pragma: no cover - environment boundary
        raise RuntimeError("auto vision needs opencv-python and numpy installed") from e
    return cv2, np


def _imread_unicode(image_path: str | Path) -> Any:
    """cv2.imread cannot reliably open non-ASCII Windows paths; use imdecode."""

    cv2, np = _require_cv()
    data = np.fromfile(str(image_path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise FileNotFoundError(str(image_path))
    return image


def _iou(a: Sequence[int], b: Sequence[int]) -> float:
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    aa = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    bb = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    return inter / (aa + bb - inter + 1e-9)


def _overlap_min_ratio(a: Sequence[int], b: Sequence[int]) -> float:
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    aa = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    bb = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    return inter / (min(aa, bb) + 1e-9)


def _center_close(a: Sequence[int], b: Sequence[int], threshold: int = 10) -> bool:
    acx = (a[0] + a[2]) / 2
    acy = (a[1] + a[3]) / 2
    bcx = (b[0] + b[2]) / 2
    bcy = (b[1] + b[3]) / 2
    return abs(acx - bcx) < threshold and abs(acy - bcy) < threshold


def _append_source(dst: Candidate, source: str) -> None:
    parts = [p for p in dst.source.split("+") if p]
    for part in source.split("+"):
        if part and part not in parts:
            parts.append(part)
    dst.source = "+".join(parts)


def _merge_meta(dst: Candidate, src: Candidate) -> None:
    if not src.meta:
        return
    for key, value in src.meta.items():
        if key == "template_scores":
            bucket = dst.meta.setdefault("template_scores", {})
            for prompt_idx, score in value.items():
                skey = str(prompt_idx)
                bucket[skey] = max(float(bucket.get(skey, 0.0)), float(score))
        elif key == "template_direct":
            bucket = dst.meta.setdefault("template_direct", {})
            for prompt_idx, info in value.items():
                skey = str(prompt_idx)
                score = float((info or {}).get("score", 0.0))
                old_score = float((bucket.get(skey) or {}).get("score", 0.0))
                if score >= old_score:
                    bucket[skey] = dict(info)
        else:
            dst.meta[key] = value


def _merge_candidate(
    candidates: list[Candidate],
    cand: Candidate,
    *,
    iou_threshold: float = 0.16,
    center_threshold: int = 14,
) -> None:
    """Add ``cand`` unless it is the same visual object as an existing one.

    ddddocr boxes are usually better centered than template-search boxes, so a
    merge keeps the existing geometry and only carries over source/meta hints.
    """

    for existing in candidates:
        if (
            _iou(cand.box, existing.box) > iou_threshold
            or _center_close(cand.box, existing.box, center_threshold)
            or _overlap_min_ratio(cand.box, existing.box) > 0.18
        ):
            # When a broad dark component overlaps a more prompt-like template
            # box, keep the template geometry.  Dark components often capture
            # only half of a pale icon and then steal another prompt.
            if "tmpl" in cand.source.split("+") and "dddd" not in existing.source.split("+"):
                ca = max(1, (cand.box[2] - cand.box[0]) * (cand.box[3] - cand.box[1]))
                ea = max(1, (existing.box[2] - existing.box[0]) * (existing.box[3] - existing.box[1]))
                if ca >= 0.45 * ea:
                    existing.box = cand.box
            _append_source(existing, cand.source)
            _merge_meta(existing, cand)
            return
    candidates.append(cand)


def _valid_top_box(box: Sequence[int], width: int = 320, *, allow_border: bool = True) -> bool:
    x1, y1, x2, y2 = map(int, box)
    w = x2 - x1
    h = y2 - y1
    if not (14 <= w <= 85 and 14 <= h <= 85):
        return False
    if y1 < 0 or y2 > 155 or x1 < 0 or x2 > width:
        return False
    if not allow_border and (x1 <= 1 or x2 >= width - 1 or y1 <= 1):
        return False
    return True


def _crop_nonwhite_prompt(cv2: Any, np: Any, crop: Any) -> Any:
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    mask = gray < 245
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return crop
    x1 = max(0, int(xs.min()) - 1)
    x2 = min(crop.shape[1], int(xs.max()) + 2)
    y1 = max(0, int(ys.min()) - 1)
    y2 = min(crop.shape[0], int(ys.max()) + 2)
    return crop[y1:y2, x1:x2]


def extract_prompt_icons(image: Any) -> list[Any]:
    cv2, np = _require_cv()
    if image.shape[0] < 180 or image.shape[1] < 80:
        raise ValueError(f"unexpected captcha image size: {image.shape[1]}x{image.shape[0]}")
    prompt = image[161:180, 0:80]
    icons: list[Any] = []
    for x1, x2 in PROMPT_SPLITS:
        icons.append(_crop_nonwhite_prompt(cv2, np, prompt[:, x1:x2]))
    return icons


def _dddd_candidates(image_path: str | Path) -> list[Candidate]:
    try:
        import ddddocr  # type: ignore
    except Exception:  # pragma: no cover - optional local detector
        return []

    detector = ddddocr.DdddOcr(det=True, show_ad=False)
    raw_boxes = detector.detection(Path(image_path).read_bytes())
    out: list[Candidate] = []
    for b in raw_boxes:
        x1, y1, x2, y2 = map(int, b)
        if y2 <= 160 and x2 - x1 >= 15 and y2 - y1 >= 15:
            out.append(Candidate((x1, y1, x2, y2), "dddd"))
    return out


def _dark_component_candidates(image: Any) -> list[Candidate]:
    """Find dark icons that ddddocr can miss.

    This deliberately only supplements with medium/large dark components.  A
    broader edge/brightness detector produced many false positives on watercolor
    backgrounds and hurt the final global assignment.
    """

    cv2, np = _require_cv()
    top = image[:160]
    hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2]
    out: list[Candidate] = []
    for thr in (70, 90):
        mask = (v < thr).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=1)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            area = cv2.contourArea(c)
            box = (int(x), int(y), int(x + w), int(y + h))
            # Border-touching dark components are almost always painting
            # shadows/frames (for example the left cliff in some samples), not
            # click icons.  Keeping them lets feature matchers steal a prompt
            # from a real ddddocr candidate.
            if not _valid_top_box(box, image.shape[1], allow_border=False):
                continue
            if area > 180 and y < 140:
                edge = cv2.Canny(v[y : y + h, x : x + w], 50, 150)
                edge_density = float((edge > 0).mean()) if edge.size else 0.0
                if 0.03 <= edge_density <= 0.45:
                    out.append(Candidate(box, "dark", {"edge_density": edge_density}))
    return out


def detect_candidates(image_path: str | Path) -> list[Candidate]:
    image = _imread_unicode(image_path)

    dddd = _dddd_candidates(image_path)
    candidates: list[Candidate] = list(dddd)
    for cand in _dark_component_candidates(image):
        _merge_candidate(candidates, cand, iou_threshold=0.15, center_threshold=10)
    return candidates


def _safe_norm01(arr: Any) -> Any:
    _, np = _require_cv()
    data = arr.astype(np.float32)
    lo = float(np.percentile(data, 1))
    hi = float(np.percentile(data, 99))
    if hi - lo < 1e-6:
        return np.zeros_like(data, dtype=np.float32)
    return np.clip((data - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)


def _prompt_foreground_mask(prompt_icon: Any) -> Any:
    cv2, np = _require_cv()
    gray = cv2.cvtColor(prompt_icon, cv2.COLOR_BGR2GRAY) if len(prompt_icon.shape) == 3 else prompt_icon
    # Prompt icons are rendered on a white strip.  Keep the actual glyph,
    # including small holes/inner strokes, and trim whitespace before scaling.
    mask = (gray < 245).astype(np.uint8) * 255
    if int((mask > 0).sum()) < 8:
        _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    n, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    if n > 1:
        total = int((mask > 0).sum())
        min_area = max(4, int(total * 0.018))
        cleaned = np.zeros_like(mask)
        for idx in range(1, n):
            if int(stats[idx, cv2.CC_STAT_AREA]) >= min_area:
                cleaned[labels == idx] = 255
        if int((cleaned > 0).sum()) >= 8:
            mask = cleaned
    return mask


def _square_resize_foreground(mask: Any, size: int, *, interpolation: int | None = None) -> Any | None:
    cv2, np = _require_cv()
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return None
    x1 = max(0, int(xs.min()) - 1)
    x2 = min(mask.shape[1], int(xs.max()) + 2)
    y1 = max(0, int(ys.min()) - 1)
    y2 = min(mask.shape[0], int(ys.max()) + 2)
    cropped = (mask[y1:y2, x1:x2] > 0).astype(np.uint8) * 255
    h, w = cropped.shape[:2]
    pad = max(h, w)
    canvas = np.zeros((pad, pad), np.uint8)
    y = (pad - h) // 2
    x = (pad - w) // 2
    canvas[y : y + h, x : x + w] = cropped
    interp = interpolation if interpolation is not None else cv2.INTER_NEAREST
    return cv2.resize(canvas, (size, size), interpolation=interp)


def _prompt_direct_templates(prompt_icon: Any, size: int) -> list[tuple[str, Any]]:
    cv2, np = _require_cv()
    mask = _square_resize_foreground(_prompt_foreground_mask(prompt_icon), size, interpolation=cv2.INTER_NEAREST)
    if mask is None or int((mask > 0).sum()) < 12:
        return []
    mask_f = (mask.astype(np.float32) / 255.0)
    edge = cv2.Canny(mask, 50, 150)
    edge = cv2.dilate(edge, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)), iterations=1)
    edge_f = edge.astype(np.float32) / 255.0
    # The same silhouette is matched against dark, light and color-saliency
    # search maps; this is more stable than assuming a fixed fill color.
    out = [("mask", mask_f)]
    if int((edge > 0).sum()) >= 8:
        out.append(("edge", edge_f))
    return out


def _top_search_maps(image: Any) -> dict[str, Any]:
    cv2, np = _require_cv()
    top = image[:160]
    hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY)
    h, s, v = cv2.split(hsv)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    edge = cv2.Canny(clahe, 40, 120)
    edge = cv2.dilate(edge, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)), iterations=1)
    # Separate render hypotheses:
    # - dark glyphs: low value / dark strokes
    # - light glyphs: high value + low saturation, common for white icons
    # - color glyphs: saturation channel, common for non-gray icon fills
    light = v.astype(np.float32) * (1.0 - np.clip(s.astype(np.float32) / 255.0, 0.0, 1.0))
    maps = {
        "dark": _safe_norm01(255 - v),
        "light": _safe_norm01(light),
        "gray_dark": _safe_norm01(255 - clahe),
        "gray_light": _safe_norm01(clahe),
        "color": _safe_norm01(s),
        "edge": edge.astype(np.float32) / 255.0,
    }
    return maps


def _template_candidates(image: Any, prompts: Sequence[Any]) -> list[Candidate]:
    """Propose candidates by searching each prompt icon over the top image.

    Prompt silhouettes are scaled to 20x20..50x50 and matched with
    ``TM_CCOEFF_NORMED`` over dark, light, gray and edge maps of the top
    region; color is retained as a histogram compatibility signal.  Hits are
    not trusted blindly; they add boxes and per-prompt direct template scores
    that the final assignment still has to win.
    """

    cv2, np = _require_cv()
    maps = _top_search_maps(image)
    width = image.shape[1]
    out: list[Candidate] = []
    for prompt_idx, prompt in enumerate(prompts):
        raw_hits: list[tuple[float, tuple[int, int, int, int], dict[str, Any]]] = []
        for size in range(20, 51, 2):
            templates = _prompt_direct_templates(prompt, size)
            if not templates:
                continue
            for templ_name, templ in templates:
                th, tw = templ.shape[:2]
                if th >= 160 or tw >= width:
                    continue
                # Saturation-only maps create many false hits on watercolor
                # backgrounds.  Keep color awareness in histogram scoring, but
                # localize by dark/light/gray render hypotheses and edges.
                search_names = ["edge"] if templ_name == "edge" else ["dark", "light", "gray_dark", "gray_light"]
                for map_name in search_names:
                    search = maps[map_name]
                    res = cv2.matchTemplate(search, templ.astype(np.float32), cv2.TM_CCOEFF_NORMED)
                    res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
                    # Keep a few local maxima per render hypothesis.  Final NMS
                    # across scales/render variants happens below.
                    for _ in range(3):
                        _, maxv, _, maxloc = cv2.minMaxLoc(res)
                        score = max(0.0, float(maxv))
                        if score < 0.28:
                            break
                        x, y = map(int, maxloc)
                        box = (x, y, x + tw, y + th)
                        if _valid_top_box(box, width, allow_border=False):
                            raw_hits.append(
                                (
                                    score,
                                    box,
                                    {
                                        "scale": size,
                                        "template": templ_name,
                                        "render": map_name,
                                        "rotation": "r0",
                                    },
                                )
                            )
                        sx1 = max(0, x - max(8, tw // 2))
                        sy1 = max(0, y - max(8, th // 2))
                        sx2 = min(res.shape[1], x + max(8, tw // 2))
                        sy2 = min(res.shape[0], y + max(8, th // 2))
                        res[sy1:sy2, sx1:sx2] = -1.0

        picked: list[tuple[float, tuple[int, int, int, int], dict[str, Any]]] = []
        for score, box, meta in sorted(raw_hits, key=lambda item: item[0], reverse=True):
            if any(_center_close(box, old_box, threshold=18) for _, old_box, _ in picked):
                continue
            picked.append((score, box, meta))
            if len(picked) >= 6:
                break
        for score, box, meta in picked:
            out.append(
                Candidate(
                    box,
                    "tmpl",
                    {
                        "template_scores": {str(prompt_idx): score},
                        "template_direct": {str(prompt_idx): {"score": score, **meta}},
                    },
                )
            )
    return out


def _update_existing_candidate_template_scores(image: Any, prompts: Sequence[Any], candidates: Sequence[Candidate]) -> None:
    """Attach rotation-tolerant top-area template scores to existing candidates.

    Rotated template search is valuable evidence for glyphs whose top rendering
    is rotated relative to the prompt (for example the tag/tower samples), but
    using all rotated maxima as new boxes creates many watercolor false
    positives.  Therefore rotated search only annotates already-detected or
    non-rotated-template candidates.
    """

    cv2, np = _require_cv()
    maps = _top_search_maps(image)
    width = image.shape[1]
    for prompt_idx, prompt in enumerate(prompts):
        for size in range(20, 51, 2):
            for templ_name, templ in _prompt_direct_templates(prompt, size):
                search_names = ["edge"] if templ_name == "edge" else ["dark", "light", "gray_dark", "gray_light"]
                templ_u8 = (templ * 255).astype(np.uint8) if templ.dtype.kind == "f" else templ
                for rot_name, rot_templ in _rotation_variants(templ_u8):
                    th, tw = rot_templ.shape[:2]
                    if th >= 160 or tw >= width:
                        continue
                    templ_f = rot_templ.astype(np.float32) / 255.0
                    for map_name in search_names:
                        res = cv2.matchTemplate(maps[map_name], templ_f, cv2.TM_CCOEFF_NORMED)
                        res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
                        for cand in candidates:
                            x1, y1, x2, y2 = cand.box
                            sx1 = max(0, x1 - tw // 2)
                            sy1 = max(0, y1 - th // 2)
                            sx2 = min(res.shape[1], x2)
                            sy2 = min(res.shape[0], y2)
                            if sx2 <= sx1 or sy2 <= sy1:
                                continue
                            _, maxv, _, maxloc = cv2.minMaxLoc(res[sy1:sy2, sx1:sx2])
                            score = max(0.0, float(maxv))
                            if score < 0.28:
                                continue
                            bucket = cand.meta.setdefault("template_scores", {})
                            direct = cand.meta.setdefault("template_direct", {})
                            key = str(prompt_idx)
                            old = float(bucket.get(key, 0.0) or 0.0)
                            if score > old:
                                bucket[key] = score
                                direct[key] = {
                                    "score": score,
                                    "scale": size,
                                    "template": templ_name,
                                    "render": map_name,
                                    "rotation": rot_name,
                                    "local_offset": [int(sx1 + maxloc[0]), int(sy1 + maxloc[1])],
                                }
                                _append_source(cand, "tmpl")


def _square_resize(gray: Any, size: int = 128) -> Any:
    cv2, np = _require_cv()
    h, w = gray.shape[:2]
    pad = max(h, w)
    canvas = np.full((pad, pad), 255, np.uint8)
    y = (pad - h) // 2
    x = (pad - w) // 2
    canvas[y : y + h, x : x + w] = gray
    return cv2.resize(canvas, (size, size), interpolation=cv2.INTER_CUBIC)


def _square_resize_array(arr: Any, size: int = 96, *, pad_value: float = 0.0) -> Any:
    cv2, np = _require_cv()
    h, w = arr.shape[:2]
    pad = max(h, w)
    canvas = np.full((pad, pad), pad_value, dtype=arr.dtype)
    y = (pad - h) // 2
    x = (pad - w) // 2
    canvas[y : y + h, x : x + w] = arr
    return cv2.resize(canvas, (size, size), interpolation=cv2.INTER_AREA)


def _rotation_variants(mask: Any) -> list[tuple[str, Any]]:
    """Return 0/90/180/270 variants for rotation-tolerant template matching."""

    cv2, np = _require_cv()
    variants = [
        ("r0", mask),
        ("r90", cv2.rotate(mask, cv2.ROTATE_90_CLOCKWISE)),
        ("r180", cv2.rotate(mask, cv2.ROTATE_180)),
        ("r270", cv2.rotate(mask, cv2.ROTATE_90_COUNTERCLOCKWISE)),
    ]
    out: list[tuple[str, Any]] = []
    seen: list[bytes] = []
    for name, value in variants:
        key = np.ascontiguousarray(value).tobytes()
        if key in seen:
            continue
        seen.append(key)
        out.append((name, value))
    return out


def _prep_gray(crop: Any) -> Any:
    cv2, _ = _require_cv()
    if len(crop.shape) == 3:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    else:
        gray = crop
    return _square_resize(gray)


def _sift_score(prompt_icon: Any, candidate_crop: Any) -> tuple[float, dict[str, int]]:
    cv2, _ = _require_cv()
    if not hasattr(cv2, "SIFT_create"):
        return 0.0, {"kp_prompt": 0, "kp_candidate": 0, "good": 0, "disabled": 1}
    sift = cv2.SIFT_create()
    a = _prep_gray(prompt_icon)
    b = _prep_gray(candidate_crop)
    best_score = 0.0
    best_info = {"kp_prompt": 0, "kp_candidate": 0, "good": 0}
    matcher = cv2.BFMatcher()
    for aa in (a, 255 - a):
        for bb in (b, 255 - b):
            kp1, des1 = sift.detectAndCompute(aa, None)
            kp2, des2 = sift.detectAndCompute(bb, None)
            if des1 is None or des2 is None or len(kp1) < 2 or len(kp2) < 2:
                continue
            good = 0
            for pair in matcher.knnMatch(des1, des2, k=2):
                if len(pair) < 2:
                    continue
                m, n = pair
                if m.distance < 0.75 * n.distance:
                    good += 1
            score = min(1.0, good / (min(len(kp1), len(kp2)) + 1e-6))
            if score > best_score:
                best_score = float(score)
                best_info = {"kp_prompt": len(kp1), "kp_candidate": len(kp2), "good": good}
    return best_score, best_info


def _edge_map(crop: Any, *, prompt: bool = False, size: int = 128) -> Any:
    cv2, np = _require_cv()
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop
    base = _square_resize(gray, size)
    if prompt:
        mask = (base < 245).astype(np.uint8) * 255
        edge = cv2.bitwise_or(cv2.Canny(base, 40, 120), cv2.Canny(mask, 50, 150))
    else:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(base)
        edge = cv2.Canny(clahe, 40, 120)
    return cv2.dilate(edge, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)), iterations=1)


def _chamfer_edge_score(prompt_icon: Any, candidate_crop: Any) -> tuple[float, dict[str, float]]:
    cv2, np = _require_cv()
    prompt_edge = _edge_map(prompt_icon, prompt=True)
    cand_edge = _edge_map(candidate_crop, prompt=False)

    def one_way(src_edge: Any, dst_edge: Any) -> tuple[float, float]:
        src_mask = src_edge > 0
        if int(src_mask.sum()) < 8:
            return 0.0, 99.0
        # cv2.distanceTransform expects non-zero free space and zero obstacles.
        dist = cv2.distanceTransform((dst_edge == 0).astype(np.uint8), cv2.DIST_L2, 3)
        # Percentile is more stable than mean when the candidate crop includes a
        # bit of watercolor background texture.
        d = float(np.percentile(dist[src_mask], 65))
        return float(np.exp(-d / 6.0)), d

    forward, d_forward = one_way(prompt_edge, cand_edge)
    backward, d_backward = one_way(cand_edge, prompt_edge)
    edge_density = float((cand_edge > 0).mean()) if cand_edge.size else 0.0
    score = 0.65 * forward + 0.35 * backward
    # Very dense edge maps usually indicate a textured background crop.  Keep a
    # soft penalty only; some real icons are dense after JPEG/anti-aliasing.
    if edge_density > 0.58:
        score *= 0.88
    return float(score), {
        "d_prompt_to_candidate": d_forward,
        "d_candidate_to_prompt": d_backward,
        "edge_density": edge_density,
    }


def _edge_ncc_score(prompt_icon: Any, candidate_crop: Any) -> float:
    cv2, _ = _require_cv()
    prompt_edge = _edge_map(prompt_icon, prompt=True).astype("float32") / 255.0
    cand_edge = _edge_map(candidate_crop, prompt=False).astype("float32") / 255.0
    res = cv2.matchTemplate(cand_edge, prompt_edge, cv2.TM_CCOEFF_NORMED)
    if res.size == 0:
        return 0.0
    return max(0.0, float(res[0, 0]))


def _same_size_ncc(a: Any, b: Any) -> float:
    cv2, np = _require_cv()
    aa = a.astype(np.float32)
    bb = b.astype(np.float32)
    if aa.shape != bb.shape:
        bb = cv2.resize(bb, (aa.shape[1], aa.shape[0]), interpolation=cv2.INTER_AREA)
    if float(aa.std()) < 1e-6 or float(bb.std()) < 1e-6:
        return 0.0
    res = cv2.matchTemplate(aa, bb, cv2.TM_CCOEFF_NORMED)
    return max(0.0, float(np.nan_to_num(res[0, 0], nan=0.0)))


def _candidate_saliency_maps(candidate_crop: Any, size: int = 96) -> dict[str, Any]:
    cv2, np = _require_cv()
    hsv = cv2.cvtColor(candidate_crop, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(candidate_crop, cv2.COLOR_BGR2GRAY)
    _, s, v = cv2.split(hsv)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4)).apply(gray)
    edge = cv2.Canny(clahe, 40, 120)
    edge = cv2.dilate(edge, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)), iterations=1)
    light = v.astype(np.float32) * (1.0 - np.clip(s.astype(np.float32) / 255.0, 0.0, 1.0))
    maps = {
        "dark": _safe_norm01(255 - v),
        "light": _safe_norm01(light),
        "gray_dark": _safe_norm01(255 - clahe),
        "gray_light": _safe_norm01(clahe),
        "color": _safe_norm01(s),
        "edge": edge.astype(np.float32) / 255.0,
    }
    return {name: _square_resize_array(value.astype(np.float32), size, pad_value=0.0) for name, value in maps.items()}


def _hist_intersection(a: Any, b: Any) -> float:
    _, np = _require_cv()
    aa = a.astype(np.float32).ravel()
    bb = b.astype(np.float32).ravel()
    if aa.size == 0 or bb.size == 0:
        return 0.0
    aa = aa / (float(aa.sum()) + 1e-9)
    bb = bb / (float(bb.sum()) + 1e-9)
    return float(np.minimum(aa, bb).sum())


def _histogram_similarity(prompt_icon: Any, candidate_crop: Any) -> tuple[float, dict[str, Any]]:
    """Gray/HSV histogram compatibility for dark and light render variants."""

    cv2, np = _require_cv()
    p_gray = cv2.cvtColor(prompt_icon, cv2.COLOR_BGR2GRAY) if len(prompt_icon.shape) == 3 else prompt_icon
    p_mask = _prompt_foreground_mask(prompt_icon) > 0
    if int(p_mask.sum()) < 8:
        return 0.0, {"reason": "empty_prompt"}

    c_gray = cv2.cvtColor(candidate_crop, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(candidate_crop, cv2.COLOR_BGR2HSV)
    _, c_s, c_v = cv2.split(hsv)
    c_masks = {
        "dark": (c_v < 120) & (c_s > 15),
        "light": (c_v > 145) & (c_s < 130),
        "color": (c_s > 60) & (c_v > 60),
    }

    p_dark = cv2.calcHist([p_gray], [0], p_mask.astype(np.uint8), [16], [0, 256]).ravel()
    p_light_values = (255 - p_gray).astype(np.uint8)
    p_light = cv2.calcHist([p_light_values], [0], p_mask.astype(np.uint8), [16], [0, 256]).ravel()

    best = 0.0
    best_info: dict[str, Any] = {"render": "", "gray": 0.0, "sat": 0.0}
    for render, mask in c_masks.items():
        if int(mask.sum()) < 8:
            continue
        mask_u8 = mask.astype(np.uint8)
        c_hist = cv2.calcHist([c_gray], [0], mask_u8, [16], [0, 256]).ravel()
        gray_score = max(_hist_intersection(p_dark, c_hist), _hist_intersection(p_light, c_hist))
        # A small HSV/color awareness term: dark/light glyphs should usually
        # have low saturation; colored glyphs should preserve a coherent
        # saturation distribution instead of random background texture.
        sat_hist = cv2.calcHist([c_s], [0], mask_u8, [12], [0, 256]).ravel()
        if render == "color":
            sat_score = min(1.0, float(mask.mean()) * 4.0) * (1.0 - float(np.std(sat_hist / (sat_hist.sum() + 1e-9))))
        else:
            sat_score = max(0.0, 1.0 - float(c_s[mask].mean()) / 255.0)
        score = 0.78 * gray_score + 0.22 * sat_score
        if score > best:
            best = float(score)
            best_info = {"render": render, "gray": float(gray_score), "sat": float(sat_score)}
    return best, best_info


def _local_template_components(prompt_icon: Any, candidate_crop: Any, size: int = 96) -> tuple[float, dict[str, Any]]:
    cv2, np = _require_cv()
    base_mask = _prompt_foreground_mask(prompt_icon)
    if int((base_mask > 0).sum()) < 8:
        return 0.0, {"reason": "empty_prompt"}

    maps = _candidate_saliency_maps(candidate_crop, size)
    scores: dict[str, float] = {name: 0.0 for name in ("dark", "light", "gray_dark", "gray_light", "color", "edge")}
    best_meta: dict[str, Any] = {}

    for templ_size in range(52, size + 1, 4):
        prompt_mask = _square_resize_foreground(base_mask, templ_size, interpolation=cv2.INTER_NEAREST)
        if prompt_mask is None:
            continue
        for rot_name, rot_mask in _rotation_variants(prompt_mask):
            prompt_mask_f = rot_mask.astype(np.float32) / 255.0
            prompt_edge = cv2.Canny(rot_mask, 50, 150)
            prompt_edge = cv2.dilate(
                prompt_edge, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)), iterations=1
            ).astype(np.float32) / 255.0

            for name in ("dark", "light", "gray_dark", "gray_light", "color"):
                res = cv2.matchTemplate(maps[name], prompt_mask_f, cv2.TM_CCOEFF_NORMED)
                val = max(0.0, float(np.nan_to_num(res.max(), nan=0.0))) if res.size else 0.0
                if val > scores[name]:
                    scores[name] = val
                    best_meta[name] = {"scale": templ_size, "rotation": rot_name}
            res = cv2.matchTemplate(maps["edge"], prompt_edge, cv2.TM_CCOEFF_NORMED)
            val = max(0.0, float(np.nan_to_num(res.max(), nan=0.0))) if res.size else 0.0
            if val > scores["edge"]:
                scores["edge"] = val
                best_meta["edge"] = {"scale": templ_size, "rotation": rot_name}

    # Do not let saturation alone dominate: colorful painting texture often
    # correlates with a prompt silhouette.  It remains visible in diagnostics
    # and in histogram similarity below.
    best_name = max(("dark", "light", "gray_dark", "gray_light", "edge"), key=lambda name: scores[name])
    hist, hist_info = _histogram_similarity(prompt_icon, candidate_crop)
    direct = 0.82 * float(scores[best_name]) + 0.18 * hist
    return float(direct), {
        "best_render": best_name,
        "render_scores": scores,
        "best_meta": best_meta.get(best_name, {}),
        "histogram": float(hist),
        "histogram_info": hist_info,
    }


def _template_direct_score(prompt_icon: Any, candidate_crop: Any, cand: Candidate, prompt_idx: int) -> tuple[float, dict[str, Any]]:
    local, info = _local_template_components(prompt_icon, candidate_crop)
    meta = cand.meta.get("template_direct", {}) if cand.meta else {}
    meta_info = meta.get(str(prompt_idx), meta.get(prompt_idx, {})) or {}
    meta_score = float(meta_info.get("score", 0.0) or 0.0)
    # Direct top-search evidence and local crop evidence are complementary:
    # top search localizes the object in the original image; local NCC/hist
    # filters out watercolor/background false positives.
    score = max(local, meta_score, 0.68 * local + 0.32 * meta_score)
    return float(score), {"local": info, "top_match": meta_info, "top_score": meta_score}


def _ncc_score(prompt_icon: Any, candidate_crop: Any) -> tuple[float, dict[str, Any]]:
    local, info = _local_template_components(prompt_icon, candidate_crop)
    edge = _edge_ncc_score(prompt_icon, candidate_crop)
    score = max(local, edge)
    return float(score), {"local_template": info, "edge_ncc": float(edge)}


def _binary_square_mask(mask: Any, *, size: int = 96) -> Any | None:
    cv2, np = _require_cv()
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return None
    x1 = max(0, int(xs.min()) - 1)
    x2 = min(mask.shape[1], int(xs.max()) + 2)
    y1 = max(0, int(ys.min()) - 1)
    y2 = min(mask.shape[0], int(ys.max()) + 2)
    cropped = (mask[y1:y2, x1:x2] > 0).astype(np.uint8) * 255
    h, w = cropped.shape[:2]
    pad = max(h, w)
    canvas = np.zeros((pad, pad), np.uint8)
    y = (pad - h) // 2
    x = (pad - w) // 2
    canvas[y : y + h, x : x + w] = cropped
    return cv2.resize(canvas, (size, size), interpolation=cv2.INTER_NEAREST)


def _largest_component(mask: Any, *, min_area: int = 20) -> Any | None:
    cv2, np = _require_cv()
    n, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    if n <= 1:
        return None
    best = max(range(1, n), key=lambda i: int(stats[i, cv2.CC_STAT_AREA]))
    if int(stats[best, cv2.CC_STAT_AREA]) < min_area:
        return None
    return (labels == best).astype(np.uint8) * 255


def _significant_components(mask: Any, *, min_area: int = 10, max_components: int = 8) -> Any | None:
    """Keep several foreground components instead of only the largest one.

    YiDun icon glyphs such as the "four squares" prompt are intentionally
    multi-component.  A largest-component-only mask destroys their holes and
    makes SSIM/shape scoring prefer unrelated single blobs.
    """

    cv2, np = _require_cv()
    n, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), 8)
    if n <= 1:
        return None
    comps = [
        (idx, int(stats[idx, cv2.CC_STAT_AREA]))
        for idx in range(1, n)
        if int(stats[idx, cv2.CC_STAT_AREA]) >= min_area
    ]
    if not comps:
        return None
    comps = sorted(comps, key=lambda item: item[1], reverse=True)[:max_components]
    out = np.zeros_like(mask, dtype=np.uint8)
    for idx, _ in comps:
        out[labels == idx] = 255
    return out


def _prompt_shape_mask(prompt_icon: Any) -> Any | None:
    cv2, np = _require_cv()
    gray = cv2.cvtColor(prompt_icon, cv2.COLOR_BGR2GRAY) if len(prompt_icon.shape) == 3 else prompt_icon
    return _binary_square_mask((gray < 245).astype(np.uint8) * 255)


def _candidate_shape_masks(candidate_crop: Any) -> list[tuple[str, Any]]:
    """Return normalized foreground-mask variants for shape comparison.

    YiDun renders the same prompt as black, white or colored glyphs depending
    on the background.  We therefore try dark and bright saliency masks and let
    the scorer pick the most compatible one.
    """

    cv2, np = _require_cv()
    hsv = cv2.cvtColor(candidate_crop, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(candidate_crop, cv2.COLOR_BGR2GRAY)
    _, s, v = cv2.split(hsv)
    mean = float(gray.mean())
    std = float(gray.std())
    raw_masks = [
        ("dark_abs", ((v < 105) & (s > 25)).astype(np.uint8) * 255),
        ("dark_local", (gray < max(80.0, mean - 0.45 * std)).astype(np.uint8) * 255),
        ("bright_low_s", ((s < 100) & (v > 145)).astype(np.uint8) * 255),
        ("bright_local", (gray > min(215.0, mean + 0.55 * std)).astype(np.uint8) * 255),
    ]

    out: list[tuple[str, Any]] = []
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    for name, mask in raw_masks:
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        component = _significant_components(mask, min_area=max(8, int(mask.size * 0.004)))
        if component is None:
            continue
        square = _binary_square_mask(component)
        if square is not None:
            out.append((name, square))
    return out


def _mask_features(mask: Any) -> dict[str, float]:
    cv2, np = _require_cv()
    m = (mask > 0).astype(np.uint8)
    contours, hierarchy = cv2.findContours(m * 255, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    holes = 0
    hole_area = 0.0
    components = 0
    if hierarchy is not None:
        for idx, h in enumerate(hierarchy[0]):
            area = float(cv2.contourArea(contours[idx]))
            if int(h[3]) < 0:
                components += 1
            elif area > 8:
                holes += 1
                hole_area += area
    return {
        "area_ratio": float(m.mean()),
        "holes": float(holes),
        "hole_area_ratio": float(hole_area / (m.sum() + 1e-9)),
        "components": float(components),
    }


def _ssim_score(a: Any, b: Any) -> float:
    """Small-image SSIM without an optional skimage dependency."""

    cv2, np = _require_cv()
    aa = a.astype(np.float32) / 255.0
    bb = b.astype(np.float32) / 255.0
    if aa.shape != bb.shape:
        bb = cv2.resize(bb, (aa.shape[1], aa.shape[0]), interpolation=cv2.INTER_AREA)
    c1 = 0.01**2
    c2 = 0.03**2
    mu_a = float(aa.mean())
    mu_b = float(bb.mean())
    var_a = float(((aa - mu_a) ** 2).mean())
    var_b = float(((bb - mu_b) ** 2).mean())
    cov = float(((aa - mu_a) * (bb - mu_b)).mean())
    den = (mu_a * mu_a + mu_b * mu_b + c1) * (var_a + var_b + c2)
    if den <= 1e-12:
        return 0.0
    val = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / den
    return float(max(0.0, min(1.0, val)))


def _single_shape_score(prompt_mask: Any, candidate_mask: Any) -> tuple[float, dict[str, float]]:
    cv2, np = _require_cv()
    p = prompt_mask > 0
    c = candidate_mask > 0
    inter = float(np.logical_and(p, c).sum())
    union = float(np.logical_or(p, c).sum()) + 1e-9
    iou = inter / union
    # Pearson on binary masks keeps interior holes/eyes/mouths visible, which
    # external contour matching misses.
    try:
        ncc = float(np.corrcoef(p.astype(float).ravel(), c.astype(float).ravel())[0, 1])
        if not np.isfinite(ncc):
            ncc = 0.0
    except Exception:
        ncc = 0.0
    ncc01 = max(0.0, (ncc + 1.0) / 2.0)
    ssim = _ssim_score(prompt_mask, candidate_mask)

    pc, _ = cv2.findContours(prompt_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cc, _ = cv2.findContours(candidate_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if pc and cc:
        shape_dist = float(cv2.matchShapes(max(pc, key=cv2.contourArea), max(cc, key=cv2.contourArea), cv2.CONTOURS_MATCH_I1, 0.0))
        contour_score = float(np.exp(-min(shape_dist, 5.0) / 0.8))
    else:
        shape_dist = 99.0
        contour_score = 0.0

    pf = _mask_features(prompt_mask)
    cf = _mask_features(candidate_mask)
    hole_diff = abs(pf["holes"] - cf["holes"])
    hole_score = float(np.exp(-hole_diff / 2.0))
    # Multi-hole prompts (faces/robot-like icons) are common; penalize masks
    # without comparable holes so a plain circle/animal body cannot win.
    structural_penalty = 1.0
    if pf["holes"] >= 9 and cf["holes"] < max(4.0, pf["holes"] * 0.40):
        hole_score *= 0.18
        structural_penalty = 0.52
    elif pf["holes"] >= 9 and cf["holes"] < max(5.0, pf["holes"] * 0.55):
        hole_score *= 0.35
        structural_penalty = 0.70
    elif 4 <= pf["holes"] <= 8 and cf["holes"] < max(3.0, pf["holes"] - 1.0):
        hole_score *= 0.25
        structural_penalty = 0.50
    elif pf["holes"] >= 4 and cf["holes"] < 3:
        hole_score *= 0.25
    elif pf["holes"] >= 3 and cf["holes"] < pf["holes"] - 1.0:
        hole_score *= 0.35
        structural_penalty = min(structural_penalty, 0.62)
    elif pf["holes"] >= 2 and abs(pf["holes"] - cf["holes"]) >= 2:
        hole_score *= 0.55
        structural_penalty = min(structural_penalty, 0.78)
    if pf["holes"] <= 2 and cf["holes"] >= 5:
        hole_score *= 0.45
    area_score = float(np.exp(-abs(pf["area_ratio"] - cf["area_ratio"]) / 0.35))

    score = (
        0.30 * iou
        + 0.20 * ncc01
        + 0.20 * ssim
        + 0.16 * contour_score
        + 0.10 * hole_score
        + 0.04 * area_score
    )
    score *= structural_penalty
    return float(score), {
        "iou": iou,
        "ncc": ncc,
        "ssim": ssim,
        "contour": contour_score,
        "shape_dist": shape_dist,
        "hole_score": hole_score,
        "area_score": area_score,
        "structural_penalty": structural_penalty,
        "prompt_holes": pf["holes"],
        "candidate_holes": cf["holes"],
    }


def _shape_mask_score(prompt_icon: Any, candidate_crop: Any) -> tuple[float, dict[str, Any]]:
    prompt_mask = _prompt_shape_mask(prompt_icon)
    if prompt_mask is None:
        return 0.0, {"mask": "", "reason": "empty_prompt"}
    best_score = 0.0
    best_info: dict[str, Any] = {"mask": "", "reason": "no_candidate_mask"}
    for rot_name, rot_prompt in _rotation_variants(prompt_mask):
        for name, candidate_mask in _candidate_shape_masks(candidate_crop):
            score, info = _single_shape_score(rot_prompt, candidate_mask)
            if score > best_score:
                best_score = score
                best_info = {"mask": name, "rotation": rot_name, **info}
    return float(best_score), best_info


def _combined_score(prompt_icon: Any, candidate_crop: Any, cand: Candidate, prompt_idx: int) -> tuple[float, dict[str, Any]]:
    sift, sift_info = _sift_score(prompt_icon, candidate_crop)
    chamfer, chamfer_info = _chamfer_edge_score(prompt_icon, candidate_crop)
    ncc, ncc_info = _ncc_score(prompt_icon, candidate_crop)
    shape, shape_info = _shape_mask_score(prompt_icon, candidate_crop)
    template_direct, template_info = _template_direct_score(prompt_icon, candidate_crop, cand, prompt_idx)
    top_ncc = float(template_info.get("top_score", 0.0) or 0.0)
    ncc = max(ncc, top_ncc)
    parts = set(cand.source.split("+"))
    source_multiplier = 1.0
    if "dddd" in parts:
        # ddddocr is not used as a recognizer here, but its detector boxes are
        # usually tighter than broad template/dark components.  A tiny bounded
        # confidence adjustment resolves near ties without changing the public
        # scoring formula or making detector output decisive.
        source_multiplier = 1.04
    if parts == {"dark"}:
        # The dark-component supplement is intentionally broad; it often
        # captures shadows or fragments near a real icon.  Keep it available as
        # a fallback, but make direct visual evidence overcome detector/template
        # candidates before it can win.
        source_multiplier = 0.76
    elif parts == {"tmpl"}:
        # A pure top-template box is useful when the detector misses a pale
        # glyph, but it is also the most common source of watercolor/background
        # false positives.  Keep it available, just slightly below detector or
        # dark-component backed boxes; border-adjacent template-only boxes are
        # penalized more because real click icons are normally fully visible.
        source_multiplier = 0.84
        if not (cand.meta.get("template_scores", {}) if cand.meta else {}).get(str(prompt_idx)):
            source_multiplier = 0.78
        x1, y1, x2, y2 = cand.box
        if x1 < 10 or x2 > 310 or y1 < 10 or y2 > 150:
            source_multiplier *= 0.84
        elif x1 < 16 or x2 > 304 or y2 > 145:
            source_multiplier *= 0.92
    ncc = min(1.0, ncc * source_multiplier)
    template_direct = min(1.0, template_direct * source_multiplier)
    chamfer = min(1.0, chamfer * (0.85 + 0.15 * source_multiplier))
    # Required weighting: NCC/direct-template matching is the main signal;
    # chamfer and SSIM-aware shape stabilize hole/contour structure; SIFT is a
    # small optional legacy term only.
    total = 0.5 * ncc + 0.2 * chamfer + 0.15 * shape + 0.1 * template_direct + 0.05 * sift
    return float(total), {
        "total": float(total),
        "ncc": float(ncc),
        "ncc_info": ncc_info,
        "chamfer": float(chamfer),
        "chamfer_info": chamfer_info,
        "shape": float(shape),
        "shape_info": shape_info,
        "template_direct": float(template_direct),
        "template_direct_info": template_info,
        "sift": float(sift),
        "sift_info": sift_info,
        "source_multiplier": float(source_multiplier),
        "formula": "0.5*ncc + 0.2*chamfer + 0.15*shape + 0.1*template_direct + 0.05*sift",
    }


def _candidate_crop(image: Any, cand: Candidate, margin: int = 1) -> Any:
    x1, y1, x2, y2 = cand.box
    return image[max(0, y1 - margin) : min(160, y2 + margin), max(0, x1 - margin) : min(image.shape[1], x2 + margin)]


def _best_assignment(scores: list[list[float]]) -> tuple[list[int], float]:
    if len(scores) != 3 or not scores[0]:
        raise ValueError("need scores for three prompt icons and at least one candidate")
    n = len(scores[0])
    if n < 3:
        raise ValueError(f"not enough candidate icons: {n}")
    best: tuple[list[int], float] | None = None
    # n is small (usually 3..8); brute force keeps the assignment globally
    # unique instead of greedily reusing one highly textured icon.
    for perm in permutations(range(n), 3):
        total = scores[0][perm[0]] + scores[1][perm[1]] + scores[2][perm[2]]
        if best is None or total > best[1]:
            best = (list(perm), float(total))
    assert best is not None
    return best


def _rank_assignments(scores: list[list[float]], *, limit: int = 40) -> list[dict[str, Any]]:
    """Return top unique prompt->candidate assignments by summed score.

    The vision scorer is intentionally conservative and keeps a broad candidate
    set.  The CLI now defaults to checking only the top-1 assignment and then
    refreshing the token/image, because live YiDun tokens in this challenge are
    effectively first-check decisive.  The longer ranked list is still useful
    for diagnostics or for explicitly requested ``--auto-alternatives`` probes.
    This remains pure algorithmic replay: the ranking is derived only from local
    NCC/chamfer/SSIM scores, not from a browser bridge.
    """

    if len(scores) != 3 or not scores[0]:
        return []
    n = len(scores[0])
    ranked: list[dict[str, Any]] = []
    for perm in permutations(range(n), 3):
        vals = [float(scores[i][perm[i]]) for i in range(3)]
        ranked.append(
            {
                "assignment": list(perm),
                "total_score": float(sum(vals)),
                "min_score": float(min(vals)),
                "scores": vals,
            }
        )
    ranked.sort(key=lambda item: (item["total_score"], item["min_score"]), reverse=True)
    return ranked[: max(1, int(limit))]


def _write_debug_image(
    image: Any,
    candidates: Sequence[Candidate],
    assignment: Sequence[int],
    details: dict[str, Any],
    debug_image_path: str | Path,
) -> None:
    cv2, _ = _require_cv()
    canvas = image.copy()
    assigned = {idx: rank for rank, idx in enumerate(assignment)}
    palette = {
        "dddd": (0, 180, 0),
        "tmpl": (255, 120, 0),
        "dark": (160, 0, 255),
    }
    for idx, cand in enumerate(candidates):
        x1, y1, x2, y2 = cand.box
        parts = cand.source.split("+")
        color = (120, 120, 120)
        for part in parts:
            if part in palette:
                color = palette[part]
                break
        thickness = 3 if idx in assigned else 1
        if idx in assigned:
            color = (0, 0, 255)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), color, thickness)
        label = f"{idx}:{cand.source}"
        if idx in assigned:
            rank = assigned[idx]
            label = f"{rank + 1}->{idx} {details['scores'][rank][idx]:.2f}"
            cv2.circle(canvas, cand.center, 4, (0, 0, 255), -1)
        cv2.putText(canvas, label, (max(0, x1), max(12, y1 - 3)), cv2.FONT_HERSHEY_SIMPLEX, 0.36, color, 1)
    path = Path(debug_image_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(path.suffix if path.suffix else ".png", canvas)
    if not ok:
        raise RuntimeError(f"failed to encode debug image: {path}")
    buf.tofile(str(path))
    details["debug_image"] = str(path)


def solve_image_points(
    image_path: str | Path,
    *,
    debug_image_path: str | Path | None = None,
) -> tuple[list[tuple[int, int]], dict[str, Any]]:
    """Return click points in prompt order plus a JSON-serializable debug blob."""

    image = _imread_unicode(image_path)

    prompts = extract_prompt_icons(image)
    candidates = detect_candidates(image_path)
    for cand in _template_candidates(image, prompts):
        _merge_candidate(candidates, cand, iou_threshold=0.16, center_threshold=18)
    _update_existing_candidate_template_scores(image, prompts, candidates)
    if len(candidates) < 3:
        raise RuntimeError(f"only detected {len(candidates)} candidate icons")

    matrix: list[list[float]] = []
    info_matrix: list[list[dict[str, Any]]] = []
    for prompt_idx, prompt in enumerate(prompts):
        row: list[float] = []
        info_row: list[dict[str, Any]] = []
        for cand in candidates:
            score, info = _combined_score(prompt, _candidate_crop(image, cand), cand, prompt_idx)
            row.append(score)
            info_row.append(info)
        matrix.append(row)
        info_matrix.append(info_row)

    assignment, total = _best_assignment(matrix)
    points = [candidates[idx].center for idx in assignment]
    ranked_assignments = _rank_assignments(matrix, limit=60)
    for item in ranked_assignments:
        item["points"] = [list(candidates[idx].center) for idx in item["assignment"]]

    # score gaps are useful when a retry is needed; low gaps usually mean an
    # ambiguous prompt/candidate pair rather than a protocol failure.
    gaps: list[float] = []
    for i, idx in enumerate(assignment):
        best = matrix[i][idx]
        others = [matrix[i][j] for j in range(len(matrix[i])) if j != idx]
        second = max(others) if others else 0.0
        gaps.append(float(best - second))

    details: dict[str, Any] = {
        "image": str(image_path),
        "points": points,
        "assignment": assignment,
        "total_score": total,
        "min_score": min(matrix[i][assignment[i]] for i in range(3)),
        "score_gaps": gaps,
        "ranked_assignments": ranked_assignments,
        "candidates": [
            {"box": list(c.box), "center": list(c.center), "source": c.source, "meta": c.meta} for c in candidates
        ],
        "scores": [[round(v, 6) for v in row] for row in matrix],
        "score_details": info_matrix,
    }
    if debug_image_path is not None:
        _write_debug_image(image, candidates, assignment, details, debug_image_path)
    return points, details


__all__ = ["Candidate", "detect_candidates", "extract_prompt_icons", "solve_image_points"]
