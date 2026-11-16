#!/usr/bin/env python3
"""YiDun icon-click captcha pure-Python protocol replay.

This script intentionally does not use browser runtime bridging.  It implements
the core-optimi 2.28.5 ``cb`` and click ``data`` crypto locally, then talks to
``/api/v3/get`` and ``/api/v3/check`` with ordinary HTTP requests.

The default ``--auto`` flow also obtains a fresh IR token, prepares a Zhihu
fingerprint value, downloads the captcha image, solves the icon order with
local OpenCV/NCC/SSIM matching, and submits ``/api/v3/check``.  Browser or MCP
tooling is useful for analysis only; this CLI does not call a browser.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import string
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, quote, unquote, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.yidun_crypto import build_click_data, format_validate, make_cb  # noqa: E402
from src.yidun_ir import build_ir_body  # noqa: E402


DEFAULT_CAPTCHA_ID = "9d74cae759784af382ac31ecf94a10a5"
DEFAULT_DT = "7empYrxxJ+lAAhFUBVKSvcsaa9/0bpAR"
DEFAULT_ZONE_ID = "CN31"
DEFAULT_PAGE_REFERER = (
    "https://www.zhihu.com/account/unhuman?type=unhuman&message="
    "%E7%B3%BB%E7%BB%9F%E7%9B%91%E6%B5%8B%E5%88%B0%E6%82%A8%E7%9A%84%E7%BD%91%E7%BB%9C%E7%8E%AF%E5%A2%83"
    "%E5%AD%98%E5%9C%A8%E5%BC%82%E5%B8%B8%EF%BC%8C%E4%B8%BA%E4%BF%9D%E8%AF%81%E6%82%A8%E7%9A%84"
    "%E6%AD%A3%E5%B8%B8%E8%AE%BF%E9%97%AE%EF%BC%8C%E8%AF%B7%E7%82%B9%E5%87%BB%E4%B8%8B%E6%96%B9"
    "%E9%AA%8C%E8%AF%81%E6%8C%89%E9%92%AE%E8%BF%9B%E8%A1%8C%E9%AA%8C%E8%AF%81%E3%80%82"
    "%E5%9C%A8%E6%82%A8%E9%AA%8C%E8%AF%81%E5%AE%8C%E6%88%90%E5%89%8D%EF%BC%8C%E8%AF%A5%E6%8F%90"
    "%E7%A4%BA%E5%B0%86%E5%A4%9A%E6%AC%A1%E5%87%BA%E7%8E%B0%E3%80%82&next="
    "https%3A%2F%2Fwww.zhihu.com%2Fsignin%3Fnext%3D%252F"
)
DEFAULT_QUERY_REFERER = "https://www.zhihu.com/account/unhuman"
DEFAULT_FP_CACHE = ROOT / "results" / "fp_current.txt"
LEGACY_FP_CACHE = ROOT / "results" / "fp.txt"

# Observed Zhihu ``fp`` values in this challenge are a 160-char custom-base64
# body plus a millisecond timestamp.  Reusing a stable body is preferable for
# server-side risk scoring; if no prior value exists we generate a same-shaped
# value and keep it on disk for subsequent runs.
FP_BODY_ALPHABET = string.ascii_letters + string.digits + "+/\\"
FP_RE = re.compile(r"^[A-Za-z0-9+/\\]{120,220}:\d{10,16}$")
DEBUG_FILE_RE = re.compile(
    r"^(?:debug_auto_|tmp_debug_|auto_failure_attempt_).*(?:\.png|\.jpg|\.jpeg|\.json)$",
    re.I,
)


def js_quote(value: Any) -> str:
    return quote(str(value), safe="-_.!~*'()")


def build_qs(pairs: Sequence[tuple[str, Any]]) -> str:
    return "&".join(f"{js_quote(k)}={js_quote(v)}" for k, v in pairs)


def callback_name() -> str:
    chars = string.ascii_lowercase + string.digits
    return "__JSONP_" + "".join(random.choice(chars) for _ in range(7)) + "_0"


def parse_jsonp(text: str) -> dict[str, Any]:
    m = re.match(r"^[\w.$]+\((.*)\);?\s*$", text, re.S)
    body = m.group(1) if m else text
    return json.loads(body)


def parse_points(raw: str | None) -> list[tuple[int, int] | tuple[int, int, int]]:
    if not raw:
        return []
    points: list[tuple[int, int] | tuple[int, int, int]] = []
    for part in re.split(r"[;|]", raw.strip()):
        if not part:
            continue
        nums = [int(round(float(x))) for x in part.split(",")]
        if len(nums) == 2:
            points.append((nums[0], nums[1]))
        elif len(nums) == 3:
            points.append((nums[0], nums[1], nums[2]))
        else:
            raise ValueError(f"bad point {part!r}; expected x,y or x,y,dt")
    return points


def with_click_timeline(points: Sequence[tuple[int, int]], base_delay: int = 450) -> list[tuple[int, int, int]]:
    """Attach plausible per-click deltas for ``build_click_data``."""

    # Captured SDK traces for this challenge used a near-zero first click and
    # roughly 0.4s between subsequent clicks.  Keep the first three values close
    # to the validated manual sample: 0, 520, 900.
    preset = [0, 520, 900]
    return [
        (int(x), int(y), preset[idx] if idx < len(preset) else preset[-1] + (idx - len(preset) + 1) * base_delay)
        for idx, (x, y) in enumerate(points)
    ]


def headers(page_referer: str) -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Referer": page_referer,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
    }


def post_headers(page_referer: str) -> dict[str, str]:
    h = headers(page_referer)
    h.update({"Origin": "https://www.zhihu.com", "Content-Type": "text/plain"})
    return h


def merge_url_query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlparse(url).query, keep_blank_values=True))


def now_ms() -> int:
    return int(time.time() * 1000)


def looks_like_fp(fp: str | None) -> bool:
    return bool(fp and FP_RE.match(fp.strip()))


def refresh_fp_timestamp(fp: str, ts: int | None = None) -> str:
    """Preserve the stable fingerprint body and update the trailing ms time."""

    body = fp.strip().rsplit(":", 1)[0] if ":" in fp else fp.strip()
    body = body[:160] if len(body) > 160 else body
    if len(body) < 120:
        body = (body + "".join(secrets.choice(FP_BODY_ALPHABET) for _ in range(160)))[:160]
    return f"{body}:{ts or now_ms()}"


def generate_fp(ts: int | None = None) -> str:
    body = "".join(secrets.choice(FP_BODY_ALPHABET) for _ in range(160))
    return f"{body}:{ts or now_ms()}"


def _extract_fp_from_get_url(text: str) -> str:
    m = re.search(r"[?&]fp=([^&\"\s]+)", text)
    return unquote(m.group(1)) if m else ""


def load_cached_fp(cache_path: str | Path) -> tuple[str, str]:
    """Return ``(fp, source)`` from explicit cache, legacy cache, or results JSON."""

    candidates: list[tuple[Path, str]] = []
    primary = Path(cache_path)
    candidates.append((primary, "cache"))
    if primary.resolve() != LEGACY_FP_CACHE.resolve():
        candidates.append((LEGACY_FP_CACHE, "legacy-cache"))
    for path, source in candidates:
        try:
            fp = path.read_text(encoding="utf-8").strip()
            if looks_like_fp(fp):
                return refresh_fp_timestamp(fp), f"{source}:{path}"
        except FileNotFoundError:
            continue
        except Exception:
            continue

    results = ROOT / "results"
    if results.exists():
        for path in sorted(results.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for value in (_extract_fp_from_get_url(text),):
                if looks_like_fp(value):
                    return refresh_fp_timestamp(value), f"result-json:{path}"
    return "", ""


def store_fp_cache(fp: str, cache_path: str | Path) -> None:
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fp, encoding="utf-8")


def ensure_fp(args: argparse.Namespace, *, source_hint: str = "") -> str:
    """Ensure ``args.fp`` is non-empty and same-shaped as Zhihu's fp.

    Preference order:
    1. Explicit/environment/captured fp.
    2. Reused local fp cache, with timestamp refreshed.
    3. Newly generated same-shaped fp, persisted for later reuse.
    """

    fp_source = source_hint
    if looks_like_fp(args.fp):
        args.fp = refresh_fp_timestamp(args.fp)
        fp_source = fp_source or "explicit"
    elif args.fp:
        # A malformed value is worse than an empty value for this flow.  Keep
        # it out of the request and fall back to cache/generation.
        args.fp = ""

    if not args.fp and not args.no_fp_cache:
        args.fp, fp_source = load_cached_fp(args.fp_cache)

    if not args.fp:
        args.fp = generate_fp()
        fp_source = "generated"

    if not args.no_fp_cache:
        try:
            store_fp_cache(args.fp, args.fp_cache)
        except Exception as e:
            args.fp_cache_error = str(e)
    args.fp_source = fp_source or "generated"
    return args.fp


def cleanup_debug_files(
    roots: Sequence[str | Path],
    *,
    max_age_seconds: int = 6 * 3600,
    max_files: int = 40,
) -> dict[str, Any]:
    """Remove stale debug artifacts created by this solver only."""

    removed: list[str] = []
    errors: list[str] = []
    now = time.time()
    for root in {Path(r) for r in roots if r}:
        if not root.exists() or not root.is_dir():
            continue
        files = [p for p in root.iterdir() if p.is_file() and DEBUG_FILE_RE.match(p.name)]
        stale = [p for p in files if max_age_seconds >= 0 and now - p.stat().st_mtime > max_age_seconds]
        keep_sorted = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
        overflow = keep_sorted[max_files:] if max_files >= 0 else []
        for path in sorted(set(stale + overflow)):
            try:
                path.unlink()
                removed.append(str(path))
            except Exception as e:
                errors.append(f"{path}: {e}")
    return {"removed": removed, "errors": errors}


def compact_vision(vision: dict[str, Any], *, keep_details: bool = False) -> dict[str, Any]:
    if keep_details:
        return vision
    scores = vision.get("scores") or []
    candidates = vision.get("candidates") or []
    compact_candidates = [
        {"box": c.get("box"), "center": c.get("center"), "source": c.get("source")} for c in candidates
    ]
    top_scores: list[list[dict[str, Any]]] = []
    for row in scores:
        order = sorted(range(len(row)), key=lambda idx: row[idx], reverse=True)[:5]
        top_scores.append(
            [
                {
                    "candidate": idx,
                    "score": round(float(row[idx]), 6),
                    "center": candidates[idx].get("center") if idx < len(candidates) else None,
                    "source": candidates[idx].get("source") if idx < len(candidates) else None,
                }
                for idx in order
            ]
        )
    return {
        "image": vision.get("image"),
        "points": vision.get("points"),
        "assignment": vision.get("assignment"),
        "ranked_assignments": (vision.get("ranked_assignments") or [])[:10],
        "total_score": vision.get("total_score"),
        "min_score": vision.get("min_score"),
        "score_gaps": vision.get("score_gaps"),
        "debug_image": vision.get("debug_image"),
        "candidate_count": len(candidates),
        "candidates": compact_candidates,
        "top_scores": top_scores,
        "formula": "0.5*ncc + 0.2*chamfer + 0.15*shape + 0.1*template_direct + 0.05*sift",
    }


def useful_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep only the values normally needed after a run.

    The internal result keeps full get/check URLs, ranked candidates and debug
    matrices so auto mode can retry and be inspected.  The default JSON output
    should be small: the page-side bypass value is ``validate``; the other
    fields below are the short-lived token/click/runtime values used to produce
    it.
    """

    runtime = result.get("runtime") or {}
    check = result.get("check") or {}
    check_data = check.get("data") or {}
    get_res = result.get("get") or {}
    get_data = get_res.get("data") or {}
    token = check_data.get("token") or get_data.get("token") or result.get("token")

    attempts = []
    success: dict[str, Any] | None = None
    for attempt in result.get("attempts") or []:
        files = attempt.get("files") or {}
        item = {
            "attempt": attempt.get("attempt"),
            "ok": bool(attempt.get("ok")),
            "token": (((attempt.get("check") or {}).get("data") or {}).get("token"))
            or ((attempt.get("get") or {}).get("data") or {}).get("token"),
            "points": attempt.get("points"),
            "check_rank": attempt.get("check_rank"),
            "checks": len(attempt.get("check_attempts") or []),
            "image": Path(files.get("image", "")).name if files.get("image") else "",
            "debug_image": attempt.get("debug_image", ""),
            "boundary": attempt.get("boundary"),
        }
        attempts.append(item)
        if item["ok"] and success is None:
            success = {
                "attempt": item["attempt"],
                "check_rank": item["check_rank"],
                "checks": item["checks"],
                "image": item["image"],
            }

    out = {
        "ok": bool(result.get("ok")),
        "result": check_data.get("result"),
        "validate": result.get("validate", ""),
        "raw_validate": result.get("raw_validate", ""),
        "token": token,
        "points": result.get("points"),
        "zoneId": runtime.get("zoneId") or check_data.get("zoneId") or get_data.get("zoneId"),
        "dt": runtime.get("dt"),
        "irToken": runtime.get("irToken"),
        "fp": runtime.get("fp"),
        "success": success,
        "boundary": result.get("boundary"),
    }
    # Successful runs only need the final values above.  For get-only/failure,
    # keep a tiny status summary so the reason is still visible without dumping
    # full retry matrices.
    if not out["ok"] and attempts:
        out["attempts"] = attempts
    if not out["ok"] and get_res:
        out["get"] = {
            "error": get_res.get("error"),
            "msg": get_res.get("msg"),
            "token": get_data.get("token"),
            "type": get_data.get("type"),
            "waitTime": get_data.get("waitTime"),
            "zoneId": get_data.get("zoneId"),
        }
    if not out["ok"] and check:
        out["check"] = {
            "error": check.get("error"),
            "msg": check.get("msg"),
            "result": check_data.get("result"),
            "token": check_data.get("token"),
            "zoneId": check_data.get("zoneId"),
        }
    return {k: v for k, v in out.items() if v is not None}


def summarize_ir_response(res: dict[str, Any] | None) -> dict[str, Any] | None:
    if not res:
        return None
    data = res.get("data") or {}
    return {
        "ok": bool(res.get("ok")),
        "code": res.get("code"),
        "td_present": bool(data.get("td")),
        "ed_present": bool(data.get("ed")),
        "ed2_present": bool(data.get("ed2")),
        "ts": data.get("ts"),
    }


def getconf(session: requests.Session, args: argparse.Namespace) -> dict[str, Any]:
    cb = callback_name()
    pairs = [
        ("referer", args.query_referer),
        ("zoneId", ""),
        ("dt", args.dt),
        ("id", args.captcha_id),
        ("ipv6", "false"),
        ("runEnv", args.run_env),
        ("iv", "5"),
        ("loadVersion", args.load_version),
        ("callback", cb),
    ]
    url = "https://c.dun.163.com/api/v2/getconf?" + build_qs(pairs)
    r = session.get(url, headers=headers(args.page_referer), timeout=args.timeout)
    r.raise_for_status()
    res = parse_jsonp(r.text)
    if res.get("error") != 0:
        raise RuntimeError(f"getconf failed: {res}")
    return res


def fetch_ir_token(session: requests.Session, args: argparse.Namespace, conf: dict[str, Any] | None = None) -> str:
    """Get a fresh YiDun IR token without browser runtime.

    ``template`` mode replays a sanitized browser-like encrypted sensor blob
    with a fresh nonce, which produces server ``td`` data.  ``empty`` mode is
    retained as a boundary probe but often gives lower-quality tokens.
    """

    if conf is None:
        conf = getconf(session, args)
    data = conf.get("data", {})
    ir = data.get("ir") or {}
    args.dt = data.get("dt") or args.dt
    args.zone_id = data.get("zoneId") or args.zone_id
    pn = args.ir_pn or ir.get("pn") or "YD00192283058223"
    body = build_ir_body(pn, args.ir_sdk_version, args.ir_version_key, mode=args.ir_mode)
    api_servers = ir.get("apiServer") or ["ir-sdk.dun.163.com", "ir-sdk.dun.163yun.com"]
    last_error: Exception | None = None
    for host in api_servers:
        url = f"https://{host}/v4/j/up"
        try:
            r = session.post(
                url,
                headers=post_headers(args.page_referer),
                data=json.dumps(body, separators=(",", ":")),
                timeout=args.timeout,
            )
            r.raise_for_status()
            res = r.json()
            if res.get("ok") and res.get("data", {}).get("tk"):
                result = res["data"]["tk"]
                args.ir_token = result
                args.ir_last_response = res
                return result
            last_error = RuntimeError(f"{url}: {res}")
        except Exception as e:  # try backup host
            last_error = e
    raise RuntimeError(f"failed to fetch irToken: {last_error}")


def get_captcha(session: requests.Session, args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    conf: dict[str, Any] | None = None
    if args.get_url:
        q = merge_url_query(args.get_url)
        args.ir_token = args.ir_token or q.get("irToken", "")
        args.fp = args.fp or q.get("fp", "")
        args.dt = args.dt or q.get("dt", DEFAULT_DT)
        args.zone_id = args.zone_id or q.get("zoneId", DEFAULT_ZONE_ID)

    ensure_fp(args, source_hint="get-url" if args.get_url and args.fp else "")

    if not args.ir_token:
        conf = getconf(session, args)
        fetch_ir_token(session, args, conf)

    cb = callback_name()
    pairs = [
        ("referer", args.query_referer),
        ("zoneId", args.zone_id),
        ("dt", args.dt),
        ("irToken", args.ir_token),
        ("id", args.captcha_id),
        ("fp", args.fp),
        ("https", "true"),
        ("type", ""),
        ("version", args.version),
        ("dpr", args.dpr),
        ("dev", args.dev),
        ("cb", make_cb()),
        ("ipv6", "false"),
        ("runEnv", args.run_env),
        ("group", ""),
        ("scene", ""),
        ("lang", args.lang),
        ("sdkVersion", ""),
        ("loadVersion", args.load_version),
        ("iv", "4"),
        ("user", ""),
        ("width", args.width),
        ("audio", "false"),
        ("sizeType", args.size_type),
        ("smsVersion", "v3"),
        ("token", ""),
        ("callback", cb),
    ]
    url = "https://c.dun.163.com/api/v3/get?" + build_qs(pairs)
    r = session.get(url, headers=headers(args.page_referer), timeout=args.timeout)
    r.raise_for_status()
    res = parse_jsonp(r.text)
    return res, url


def save_image(session: requests.Session, bg: str, out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = Path(urlparse(bg).path).name or "captcha.jpg"
    img_path = out_dir / f"captcha_{name}"
    r = session.get(bg, timeout=15)
    r.raise_for_status()
    img_path.write_bytes(r.content)
    paths = {"image": str(img_path)}
    try:
        from PIL import Image

        im = Image.open(img_path)
        top = img_path.with_name(img_path.stem + "_top.png")
        prompt = img_path.with_name(img_path.stem + "_prompt_display.png")
        im.crop((0, 0, 320, 160)).save(top)
        im.crop((0, 161, 80, 180)).save(prompt)
        paths.update({"top": str(top), "prompt": str(prompt)})
    except Exception:
        pass
    return paths


def check_captcha(
    session: requests.Session,
    args: argparse.Namespace,
    token: str,
    points: Sequence[tuple[int, int] | tuple[int, int, int]],
) -> tuple[dict[str, Any], str, str]:
    data_obj = build_click_data(token, points, traces=[])
    data = json.dumps(data_obj, separators=(",", ":"))
    cb = callback_name()
    pairs = [
        ("referer", args.query_referer),
        ("zoneId", args.zone_id),
        ("dt", args.dt),
        ("id", args.captcha_id),
        ("token", token),
        ("data", data),
        ("width", args.width),
        ("type", args.captcha_type),
        ("version", args.version),
        ("cb", make_cb()),
        ("user", ""),
        ("extraData", ""),
        ("bf", args.bf),
        ("runEnv", args.run_env),
        ("sdkVersion", ""),
        ("loadVersion", args.load_version),
        ("iv", "4"),
        ("callback", cb),
    ]
    url = "https://c.dun.163.com/api/v3/check?" + build_qs(pairs)
    r = session.get(url, headers=headers(args.page_referer), timeout=args.timeout)
    r.raise_for_status()
    return parse_jsonp(r.text), url, data


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Pure-Python YiDun icon-click protocol solver")
    p.add_argument("--captcha-id", default=DEFAULT_CAPTCHA_ID)
    p.add_argument("--dt", default=os.environ.get("YIDUN_DT", DEFAULT_DT))
    p.add_argument("--zone-id", default=os.environ.get("YIDUN_ZONE_ID", DEFAULT_ZONE_ID))
    p.add_argument("--ir-token", default=os.environ.get("YIDUN_IR_TOKEN", ""))
    p.add_argument("--ir-pn", default=os.environ.get("YIDUN_IR_PN", ""), help="override IR product pn; normally read from getconf")
    p.add_argument("--ir-sdk-version", default="2.0.13_yanzhengma")
    p.add_argument("--ir-version-key", default="d44593ca")
    p.add_argument("--ir-mode", choices=["template", "empty"], default=os.environ.get("YIDUN_IR_MODE", "template"))
    p.add_argument("--fp", default=os.environ.get("YIDUN_FP", ""))
    p.add_argument("--fp-cache", default=os.environ.get("YIDUN_FP_CACHE", str(DEFAULT_FP_CACHE)))
    p.add_argument("--no-fp-cache", action="store_true", help="do not read/write the reusable Zhihu fp cache")
    p.add_argument("--get-url", help="captured /api/v3/get URL; irToken/fp/dt/zoneId are parsed from it")
    p.add_argument("--token", help="skip /get and check this token directly")
    p.add_argument("--points", help="click points, e.g. '51,67;181,88;112,42' or with dt")
    p.add_argument("--auto", action="store_true", default=True, help="fetch image, locally recognize three icons, then submit /check (default)")
    p.add_argument("--no-auto", dest="auto", action="store_false", help="disable local image solving and checking")
    p.add_argument("--max-attempts", type=int, default=10, help="auto mode: retry with a fresh token/image when check=false")
    p.add_argument(
        "--auto-alternatives",
        type=int,
        default=1,
        help=(
            "auto mode: submit up to N ranked local-vision assignments for the same token before fetching a new one; "
            "keep this at 1 for live runs because a wrong first /check commonly consumes the token"
        ),
    )
    p.add_argument(
        "--auto-alternative-delay-ms",
        type=int,
        default=180,
        help="auto mode: delay between ranked alternative /check requests for one token",
    )
    p.add_argument("--auto-save-failures", action="store_true", help="auto mode: keep per-attempt failure JSON under results/")
    p.add_argument("--debug-dir", default=str(ROOT / "results"), help="where auto failure debug images/json are written")
    p.add_argument("--debug-ttl-seconds", type=int, default=6 * 3600, help="delete old solver debug files older than this")
    p.add_argument("--debug-max-files", type=int, default=40, help="keep at most this many solver debug files per directory")
    p.add_argument("--vision-detail", action="store_true", help="include full per-candidate score_details in JSON output")
    p.add_argument("--getconf", action="store_true", help="call /api/v2/getconf first and print/save its result")
    p.add_argument("--get-only", action="store_true", help="only fetch captcha image/token; do not check")
    p.add_argument("--out-dir", default=str(ROOT / "captures"))
    p.add_argument("--json-out", default="", help="optional path to save structured result JSON")
    p.add_argument(
        "--output-detail",
        choices=["useful", "debug"],
        default="useful",
        help="JSON output shape: useful keeps only bypass parameters; debug keeps full retry/vision diagnostics",
    )
    p.add_argument("--timeout", type=float, default=12)
    p.add_argument(
        "--notify",
        action=argparse.BooleanOptionalAction,
        default=str(os.environ.get("YIDUN_NOTIFY", "")).strip().lower() in {"1", "true", "yes", "on"},
        help="send email/WeChat push after solve using NOTIFY_*/SMTP_*/SERVERCHAN_*/PUSHPLUS_* env",
    )
    p.add_argument("--page-referer", default=DEFAULT_PAGE_REFERER)
    p.add_argument("--query-referer", default=DEFAULT_QUERY_REFERER)
    p.add_argument("--version", default="2.28.5")
    p.add_argument("--load-version", default="2.5.4")
    p.add_argument("--run-env", default="10")
    p.add_argument("--width", default="320")
    p.add_argument("--captcha-type", default="7")
    p.add_argument("--size-type", default="10")
    p.add_argument("--dpr", default="1.5")
    p.add_argument("--dev", default="3")
    p.add_argument("--bf", default="0")
    p.add_argument("--lang", default="zh-CN")
    args = p.parse_args(argv)

    sess = requests.Session()
    result: dict[str, Any] = {
        "ok": False,
        "get": None,
        "check": None,
        "files": {},
        "boundary": None,
        "attempts": [],
        "runtime": {},
        "cleanup": None,
    }

    def emit(code: int) -> int:
        output = result if args.output_detail == "debug" else useful_result(result)
        if getattr(args, "notify", False):
            try:
                from src.notify import NotifyConfig, build_solve_message, notify_all

                useful = useful_result(result)
                title, body = build_solve_message(ok=bool(useful.get("ok")), result=useful)
                report = notify_all(
                    NotifyConfig.from_env(),
                    ok=bool(useful.get("ok")),
                    title=title,
                    body=body,
                    extra={"exit_code": code},
                )
                if args.output_detail == "debug":
                    output = dict(output)
                    output["notify"] = report
            except Exception as notify_exc:  # noqa: BLE001 — never fail the CLI on notify errors
                if args.output_detail == "debug":
                    output = dict(output)
                    output["notify_error"] = str(notify_exc)
        text = json.dumps(output, ensure_ascii=False, indent=2)
        print(text)
        if args.json_out:
            Path(args.json_out).write_text(text, encoding="utf-8")
        return code

    try:
        result["cleanup"] = cleanup_debug_files(
            [args.debug_dir, args.out_dir],
            max_age_seconds=args.debug_ttl_seconds,
            max_files=args.debug_max_files,
        )

        if args.getconf:
            result["getconf"] = getconf(sess, args)
            data = result["getconf"].get("data", {})
            args.dt = data.get("dt") or args.dt
            args.zone_id = data.get("zoneId") or args.zone_id

        manual_points = parse_points(args.points)
        auto_mode = bool(args.auto and not manual_points and not args.get_only)
        max_attempts = max(1, args.max_attempts if auto_mode else 1)
        if args.token and args.fp == "":
            ensure_fp(args)
            result["runtime"] = {
                "irToken": args.ir_token,
                "fp": args.fp,
                "fp_source": getattr(args, "fp_source", ""),
                "fp_cache": "" if args.no_fp_cache else str(args.fp_cache),
                "zoneId": args.zone_id,
                "dt": args.dt,
            }

        last_boundary = None
        for attempt_no in range(1, max_attempts + 1):
            if attempt_no > 1:
                cleanup_debug_files(
                    [args.debug_dir, args.out_dir],
                    max_age_seconds=args.debug_ttl_seconds,
                    max_files=args.debug_max_files,
                )
            attempt: dict[str, Any] = {"attempt": attempt_no, "get": None, "check": None, "files": {}, "vision": None}
            token = args.token if attempt_no == 1 else None
            if not token:
                # Force a fresh IR token for retries.  The endpoint accepts a
                # cheap IR upload, so there is no reason to reuse a stale one.
                if auto_mode and attempt_no > 1:
                    args.ir_token = ""
                get_res, get_url = get_captcha(sess, args)
                result["runtime"] = {
                    "irToken": args.ir_token,
                    "fp": args.fp,
                    "fp_source": getattr(args, "fp_source", ""),
                    "fp_cache": "" if args.no_fp_cache else str(args.fp_cache),
                    "zoneId": args.zone_id,
                    "dt": args.dt,
                }
                if getattr(args, "fp_cache_error", ""):
                    result["runtime"]["fp_cache_error"] = args.fp_cache_error
                ir_summary = summarize_ir_response(getattr(args, "ir_last_response", None))
                if ir_summary:
                    attempt["ir"] = ir_summary
                    result["ir"] = ir_summary
                result["get"] = get_res
                result["get_url"] = get_url
                attempt["get"] = get_res
                attempt["get_url"] = get_url
                if get_res.get("error") != 0:
                    last_boundary = f"get failed: {get_res}"
                    attempt["boundary"] = last_boundary
                    result["attempts"].append(attempt)
                    continue
                data = get_res["data"]
                token = data["token"]
                bg = data["bg"][0] if isinstance(data.get("bg"), list) else data.get("bg")
                if bg:
                    attempt["files"] = save_image(sess, bg, Path(args.out_dir))
                    result["files"] = attempt["files"]

            point_sets: list[dict[str, Any]] = []
            if manual_points:
                point_sets.append({"rank": 1, "points": [tuple(p) for p in manual_points], "source": "manual"})
            if auto_mode:
                from src.yidun_vision import solve_image_points  # imported lazily; optional CV deps

                image_file = attempt["files"].get("image") or result["files"].get("image")
                if not image_file:
                    raise RuntimeError("auto mode needs a downloaded captcha image")
                debug_path = Path(args.debug_dir) / f"debug_auto_attempt_{attempt_no}_{Path(image_file).stem}.png"
                try:
                    auto_points, vision = solve_image_points(image_file, debug_image_path=debug_path)
                except Exception as e:
                    # Even when assignment fails before scoring, keep a marked
                    # candidate image so the failed auto attempt is inspectable.
                    try:
                        from src.yidun_vision import _imread_unicode, _write_debug_image, detect_candidates

                        debug_details = {"scores": [[], [], []], "error": str(e)}
                        _write_debug_image(_imread_unicode(image_file), detect_candidates(image_file), [], debug_details, debug_path)
                        attempt["debug_image"] = debug_details.get("debug_image", str(debug_path))
                    except Exception as dbg_e:
                        attempt["debug_error"] = str(dbg_e)
                    last_boundary = f"auto vision failed: {e}"
                    attempt["boundary"] = last_boundary
                    result["attempts"].append(attempt)
                    if args.auto_save_failures:
                        out = ROOT / "results" / f"auto_failure_attempt_{attempt_no}.json"
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_text(json.dumps(attempt, ensure_ascii=False, indent=2), encoding="utf-8")
                    continue
                attempt["_pending_debug_image"] = vision.get("debug_image", str(debug_path))
                attempt["vision"] = compact_vision(vision, keep_details=args.vision_detail)
                result["vision"] = attempt["vision"]
                seen_point_keys: set[tuple[tuple[int, int], ...]] = set()
                ranked = vision.get("ranked_assignments") or []
                # The first item should match ``auto_points``.  If diagnostics
                # are missing for any reason, still submit the top-1 result.
                if not ranked:
                    ranked = [{"rank": 1, "assignment": vision.get("assignment"), "points": [list(p) for p in auto_points]}]
                for rank, item in enumerate(ranked[: max(1, args.auto_alternatives)], 1):
                    raw_points = item.get("points") or []
                    if len(raw_points) != 3:
                        continue
                    key = tuple((int(p[0]), int(p[1])) for p in raw_points)
                    if key in seen_point_keys:
                        continue
                    seen_point_keys.add(key)
                    point_sets.append(
                        {
                            "rank": rank,
                            "source": "auto-ranked",
                            "points": list(key),
                            "assignment": item.get("assignment"),
                            "score": item.get("total_score"),
                            "min_score": item.get("min_score"),
                        }
                    )

            if args.get_only or not point_sets:
                pending_debug = attempt.pop("_pending_debug_image", "")
                if pending_debug and args.get_only:
                    try:
                        Path(pending_debug).unlink(missing_ok=True)
                    except Exception:
                        pass
                result["attempts"].append(attempt)
                break

            check_attempts: list[dict[str, Any]] = []
            for check_idx, point_item in enumerate(point_sets, 1):
                if check_idx > 1 and args.auto_alternative_delay_ms > 0:
                    time.sleep(args.auto_alternative_delay_ms / 1000.0)
                base_points = [(int(p[0]), int(p[1])) for p in point_item["points"]]
                points = with_click_timeline(base_points) if auto_mode else point_item["points"]
                check_res, check_url, check_data = check_captcha(sess, args, token, points)
                check_data_obj = json.loads(check_data)
                ok = bool(check_res.get("data", {}).get("result"))
                check_entry = {
                    "rank": point_item.get("rank", check_idx),
                    "source": point_item.get("source", ""),
                    "assignment": point_item.get("assignment"),
                    "score": point_item.get("score"),
                    "min_score": point_item.get("min_score"),
                    "points": [list(p) for p in points],
                    "result": ok,
                    "error": check_res.get("error"),
                }
                check_attempts.append(check_entry)
                attempt.update(
                    {
                        "check": check_res,
                        "check_url": check_url,
                        "check_data": check_data_obj,
                        "points": [list(p) for p in points],
                        "check_rank": check_entry["rank"],
                    }
                )
                result.update({"check": check_res, "check_url": check_url, "check_data": check_data_obj})
                result["points"] = [list(p) for p in points]
                attempt["ok"] = ok
                if ok:
                    attempt["check_attempts"] = check_attempts
                    result["attempts"].append(attempt)
                    pending_debug = attempt.pop("_pending_debug_image", "")
                    if pending_debug:
                        try:
                            Path(pending_debug).unlink(missing_ok=True)
                        except Exception:
                            pass
                    result["ok"] = True
                    raw_validate = check_res["data"].get("validate", "")
                    result["raw_validate"] = raw_validate
                    result["validate"] = format_validate(raw_validate, args.fp, args.zone_id) if args.fp else raw_validate
                    result["boundary"] = None
                    return emit(0)

            last_boundary = "check returned result=false"
            attempt["check_attempts"] = check_attempts
            attempt["boundary"] = last_boundary
            if auto_mode:
                pending_debug = attempt.pop("_pending_debug_image", "")
                if pending_debug:
                    attempt["debug_image"] = pending_debug
            result["attempts"].append(attempt)
            if args.auto_save_failures:
                out = ROOT / "results" / f"auto_failure_attempt_{attempt_no}.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(attempt, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.get_only or not (manual_points or auto_mode):
            result["ok"] = True
            result["boundary"] = "captcha fetched; pass --points or --auto to submit /check"
            if "token" not in result and result.get("get", {}).get("data", {}).get("token"):
                result["token"] = result["get"]["data"]["token"]
            return emit(0)

        result["boundary"] = last_boundary or "check returned result=false"
        return emit(3)
    except Exception as e:
        result["boundary"] = str(e)
        return emit(1)


if __name__ == "__main__":
    raise SystemExit(main())
