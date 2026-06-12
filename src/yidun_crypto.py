"""Pure-Python reimplementation of NetEase YiDun core-optimi 2.28.5 crypto.

The names follow the deobfuscated SDK:

* ``xor_encode``  == module export ``xorEncode``
* ``aes_encrypt`` == module export ``aes`` (custom 64-byte block cipher, not AES)
* ``make_cb``     == /api/v3 get/check ``cb`` generator

No browser, Node, or JS runtime is required.
"""

from __future__ import annotations

import binascii
import os
import random
import string
from typing import Iterable, List, Sequence


SBOX_HEX = (
    "a7be3f3933fa8c5fcf86c4b6908b569ba1e26c1a6d7cfbf60ae4b00e074a194dac"
    "4b73e7f898541159a39d08183b76eedee3ed341e6685d2357440158394b1ff03a9004"
    "cbbb5ca7dcb7f41489a16e03dcc9c71eb3c9796685b1d01b4d56193a6e1f1a24704"
    "45c191ae49c5d82765dc82c350f263387a24a502fcbf442e2dddaad0e936d9ea22"
    "b89275307b42518fbc3a626ba806d4ecd6d725f50cc8c72fefa4551ccd6fc9b2b"
    "7ab954f815c7264c6e51f4eaf99885a79892b1b60a0b3526e57ba5d178d3709588"
    "47eb9fd28f9ce0bc023f4148a2adfe632126769057043d3bd8eda0df7872629f38"
    "09ef05310e83113216afe202c460fc23e789f77d1addb5e"
)
ROUND_KEY = "037606da0296055c"
SEED_KEY = "fd6a43ae25f74398b61c03c83be37449"

BASE64_ALPHABET = "i/x1XgU0z7k8N+lCpOnPrv6\\qu2Gj9HRcwTYZ4bfSJBhaWstAeoMIEQ5mDdVFLKy"
BASE64_PADDING = "3"
BASE64_PRIVATE_ALPHABET = "MB.CfHUzEeJpsuGkgNwhqiSaI4Fd9L6jYKZAxn1/Vml0c5rbXRP+8tD3QTO2vWyo"
BASE64_PRIVATE_PADDING = "7"

UUID_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def to_byte(n: int) -> int:
    """Match the SDK's recursive signed-byte normalizer (-128..127)."""

    n = int(n)
    while n < -0x80:
        n += 0x100
    while n > 0x7F:
        n -= 0x100
    return n


def u8(n: int) -> int:
    return int(n) & 0xFF


def padding_array_zero(length: int) -> List[int]:
    return [0] * length


def shift(a: int, b: int) -> int:
    return to_byte(a + b)


def shifts(values: Sequence[int], key: Sequence[int]) -> List[int]:
    if not key:
        return []
    return [shift(v, key[i % len(key)]) for i, v in enumerate(values)]


def xor(a: int, b: int) -> int:
    return to_byte(to_byte(a) ^ to_byte(b))


def xors(values: Sequence[int], key: Sequence[int]) -> List[int]:
    if not key:
        return []
    return [xor(v, key[i % len(key)]) for i, v in enumerate(values)]


def int_to_bytes(n: int) -> List[int]:
    n &= 0xFFFFFFFF
    return [
        to_byte((n >> 24) & 0xFF),
        to_byte((n >> 16) & 0xFF),
        to_byte((n >> 8) & 0xFF),
        to_byte(n & 0xFF),
    ]


def hex_to_byte(hex2: str) -> int:
    return to_byte(int(str(hex2)[:2], 16))


def hexs_to_bytes(hexs: str) -> List[int]:
    s = str(hexs)
    return [hex_to_byte(s[i : i + 2]) for i in range(0, len(s), 2)]


def byte_to_hex(b: int) -> str:
    return f"{u8(b):02x}"


def bytes_to_hex(bs: Sequence[int]) -> str:
    return "".join(byte_to_hex(b) for b in bs)


def string_to_bytes(s: str) -> List[int]:
    # Equivalent to encodeURIComponent + percent-byte parsing for valid strings.
    return [to_byte(b) for b in str(s).encode("utf-8")]


def bytes_to_string(bs: Sequence[int]) -> str:
    return bytes(u8(b) for b in bs).decode("utf-8")


def copy_to_bytes(src: Sequence[int], src_pos: int, dst: List[int], dst_pos: int, length: int) -> List[int]:
    for i in range(length):
        if src_pos + i < len(src):
            idx = dst_pos + i
            if idx >= len(dst):
                dst.extend([0] * (idx + 1 - len(dst)))
            dst[idx] = src[src_pos + i]
    return dst


def gen_crc32_hex(bs: Sequence[int]) -> str:
    return f"{binascii.crc32(bytes(u8(b) for b in bs)) & 0xFFFFFFFF:08x}"


def _b64_chunk_encode(chunk: Sequence[int], alphabet: str, padding: str) -> str:
    out: List[str] = []
    if len(chunk) == 1:
        a = chunk[0]
        b = c = 0
        out.extend(
            [
                alphabet[(u8(a) >> 2) & 0x3F],
                alphabet[((a << 4) & 0x30) + ((u8(b) >> 4) & 0x0F)],
                padding,
                padding,
            ]
        )
    elif len(chunk) == 2:
        a, b = chunk[0], chunk[1]
        c = 0
        out.extend(
            [
                alphabet[(u8(a) >> 2) & 0x3F],
                alphabet[((a << 4) & 0x30) + ((u8(b) >> 4) & 0x0F)],
                alphabet[((b << 2) & 0x3C) + ((u8(c) >> 6) & 0x03)],
                padding,
            ]
        )
    elif len(chunk) == 3:
        a, b, c = chunk[0], chunk[1], chunk[2]
        out.extend(
            [
                alphabet[(u8(a) >> 2) & 0x3F],
                alphabet[((a << 4) & 0x30) + ((u8(b) >> 4) & 0x0F)],
                alphabet[((b << 2) & 0x3C) + ((u8(c) >> 6) & 0x03)],
                alphabet[c & 0x3F],
            ]
        )
    return "".join(out)


def base64_encode_bytes(bs: Sequence[int], alphabet: str = BASE64_ALPHABET, padding: str = BASE64_PADDING) -> str:
    if not bs:
        return ""
    return "".join(_b64_chunk_encode(bs[i : i + 3], alphabet, padding) for i in range(0, len(bs), 3))


def _b64_chunk_decode(indices: Sequence[int]) -> List[int]:
    out: List[int] = []
    if len(indices) == 2:
        out.append(to_byte(((indices[0] << 2) & 0xFF) + ((u8(indices[1]) >> 4) & 0x03)))
    elif len(indices) == 3:
        out.append(to_byte(((indices[0] << 2) & 0xFF) + ((u8(indices[1]) >> 4) & 0x03)))
        out.append(to_byte(((indices[1] << 4) & 0xFF) + ((u8(indices[2]) >> 2) & 0x0F)))
    elif len(indices) == 4:
        out.append(to_byte(((indices[0] << 2) & 0xFF) + ((u8(indices[1]) >> 4) & 0x03)))
        out.append(to_byte(((indices[1] << 4) & 0xFF) + ((u8(indices[2]) >> 2) & 0x0F)))
        out.append(to_byte(((indices[2] << 6) & 0xFF) + (indices[3] & 0x3F)))
    return out


def base64_decode_bytes(s: str, alphabet: str = BASE64_ALPHABET, padding: str = BASE64_PADDING) -> List[int]:
    if not s:
        return []
    raw = s.split(padding, 1)[0]
    out: List[int] = []
    for i in range(0, len(raw), 4):
        chunk = raw[i : i + 4]
        idxs = [alphabet.index(ch) for ch in chunk]
        out.extend(_b64_chunk_decode(idxs))
    return out


def base64_encode_private(bs: Sequence[int]) -> str:
    return base64_encode_bytes(bs, BASE64_PRIVATE_ALPHABET, BASE64_PRIVATE_PADDING)


def base64_decode_private(s: str) -> List[int]:
    return base64_decode_bytes(s, BASE64_PRIVATE_ALPHABET, BASE64_PRIVATE_PADDING)


def xor_encode(token: str, plaintext: str) -> str:
    return base64_encode_bytes(xors(string_to_bytes(plaintext), string_to_bytes(token)))


def xor_decode(token: str, encoded: str) -> str:
    return bytes_to_string(xors(base64_decode_bytes(encoded), string_to_bytes(token)))


def _repeat_to_64(bs: List[int]) -> List[int]:
    if not bs:
        return padding_array_zero(0x40)
    if len(bs) >= 0x40:
        # JS splice(0, 0x40) returns the first 64 values.
        return list(bs[:0x40])
    return [bs[i % len(bs)] for i in range(0x40)]


def _pad_blocks(bs: List[int]) -> List[int]:
    if not bs:
        return padding_array_zero(0x40)
    n = len(bs)
    rem = n % 0x40
    zero_count = 0x40 - rem - 0x4 if rem <= 0x3C else 0x80 - rem - 0x4
    out = list(bs) + [0] * zero_count + int_to_bytes(n)
    return out


def _split_blocks(bs: Sequence[int]) -> List[List[int]]:
    if len(bs) % 0x40 != 0:
        return []
    return [list(bs[i : i + 0x40]) for i in range(0, len(bs), 0x40)]


_SBOX = hexs_to_bytes(SBOX_HEX)


def _sbox_sub(bs: Sequence[int]) -> List[int]:
    if not bs:
        return []
    return [_SBOX[0x10 * ((u8(b) >> 4) & 0x0F) + (b & 0x0F)] for b in bs]


def _rand_salt() -> List[int]:
    return [to_byte(b) for b in os.urandom(4)]


def _derive_key(salt: Sequence[int]) -> List[int]:
    seed = _repeat_to_64(string_to_bytes(SEED_KEY))
    seed = xors(seed, _repeat_to_64(list(salt)))
    return _repeat_to_64(seed)


def _round_identity(bs: Sequence[int], arg: int = 0) -> List[int]:
    return list(bs) if arg + 0x100 >= 0 else []


def _round_xor_const(bs: Sequence[int], arg: int) -> List[int]:
    arg = to_byte(arg)
    return [xor(v, arg) for v in bs]


def _round_shift_const(bs: Sequence[int], arg: int) -> List[int]:
    arg = to_byte(arg)
    return [shift(v, arg) for v in bs]


def _round_xor_inc(bs: Sequence[int], arg: int) -> List[int]:
    arg = to_byte(arg)
    out = []
    for v in bs:
        out.append(xor(v, arg))
        arg += 1
    return out


def _round_shift_inc(bs: Sequence[int], arg: int) -> List[int]:
    arg = to_byte(arg)
    out = []
    for v in bs:
        out.append(shift(v, arg))
        arg += 1
    return out


def _round_xor_dec(bs: Sequence[int], arg: int) -> List[int]:
    arg = to_byte(arg)
    out = []
    for v in bs:
        out.append(xor(v, arg))
        arg -= 1
    return out


def _round_shift_dec(bs: Sequence[int], arg: int) -> List[int]:
    arg = to_byte(arg)
    out = []
    for v in bs:
        out.append(shift(v, arg))
        arg -= 1
    return out


_ROUND_FUNCS = [
    _round_identity,
    _round_xor_const,
    _round_shift_const,
    _round_xor_inc,
    _round_shift_inc,
    _round_xor_dec,
    _round_shift_dec,
]


def _round_transform(block: Sequence[int]) -> List[int]:
    cur = list(block)
    for i in range(0, len(ROUND_KEY), 4):
        piece = ROUND_KEY[i : i + 4]
        idx = hex_to_byte(piece[:2])
        arg = hex_to_byte(piece[2:])
        cur = _ROUND_FUNCS[idx](cur, arg)
    return cur


def aes_encrypt(plaintext: str, salt: Sequence[int] | None = None) -> str:
    """SDK ``aes`` output. ``salt`` is for tests; omit it for real requests."""

    msg = string_to_bytes(plaintext)
    if salt is None:
        salt_bytes = _rand_salt()
    else:
        if len(salt) != 4:
            raise ValueError("salt must contain exactly 4 bytes")
        salt_bytes = [to_byte(b) for b in salt]

    key = _derive_key(salt_bytes)
    crc_bytes = string_to_bytes(gen_crc32_hex(msg))
    blocks = _split_blocks(_pad_blocks(list(msg) + crc_bytes))
    out: List[int] = list(salt_bytes)
    feedback = list(key)
    for block in blocks:
        cur = xors(_round_transform(block), key)
        cur = shifts(cur, feedback)
        cur = xors(cur, feedback)
        feedback = _sbox_sub(_sbox_sub(cur))
        out.extend(feedback)
    return base64_encode_private(out)


def uuid(length: int = 32, radix: int | None = None) -> str:
    chars = UUID_CHARS
    radix = radix or len(chars)
    return "".join(chars[int(random.random() * radix)] for _ in range(length))


def make_cb(raw: str | None = None) -> str:
    """Generate /api/v3 ``cb`` exactly like core-optimi m25b40/vfnv46."""

    code = "vfnv46"
    pos = [0x1, 0xA, 0xC, 0xD, 0x1A, 0x1F]
    chars = list(raw if raw is not None else uuid(0x20))
    if len(chars) < 0x20:
        raise ValueError("raw cb seed must be at least 32 chars")
    for i, p in enumerate(pos):
        chars[p] = code[i]
    return aes_encrypt("".join(chars[:0x20]))


def safe_validate_hash(value: str) -> str:
    """Module 0x30: make the custom-base64 string URL/header friendly."""

    return str(value).replace("\\", "-").replace("/", "_").replace("+", "*")


def format_validate(validate: str, fp: str, zone_id: str = "") -> str:
    """SDK ``_0xab267f`` wrapper passed to the page callback."""

    h = safe_validate_hash(aes_encrypt(f"{validate}::{fp}"))
    return f"{zone_id + '_' if zone_id else ''}{h}_v_i_1"


def sample(values: Sequence[str], count: int) -> List[str]:
    n = len(values)
    if n <= count:
        return list(values)
    out: List[str] = []
    j = 0
    for i in range(n):
        if i >= (j * (n - 1)) / (count - 1):
            out.append(values[i])
            j += 1
    return out


def build_click_data(
    token: str,
    points: Sequence[tuple[int, int] | tuple[int, int, int]],
    *,
    traces: Sequence[tuple[int, int, int, int]] | None = None,
    click_count: int | None = None,
    sample_num: int = 0x32,
) -> dict[str, str]:
    """Build the JSON object used as ``data`` for type=7 icon point captcha.

    Points are ``(x, y)`` or ``(x, y, dt_ms)``.  When dt is omitted a plausible
    increasing click timeline is used.
    """

    enc_points: List[str] = []
    for idx, point in enumerate(points):
        if len(point) == 2:
            x, y = point  # type: ignore[misc]
            # The SDK sets beginTime immediately before the first point, so
            # the first click usually has dt≈0.  Subsequent values are only
            # sanity telemetry; the decisive data is the coordinate order.
            dt = idx * 450
        else:
            x, y, dt = point  # type: ignore[misc]
        enc_points.append(xor_encode(token, f"{round(x)},{round(y)},{round(dt)}"))

    enc_traces: List[str] = []
    for x, y, dt, trusted in traces or []:
        enc_traces.append(xor_encode(token, f"{round(x)},{round(y)},{round(dt)},{trusted}"))

    clicks = len(points) if click_count is None else click_count
    return {
        "d": "",
        "m": aes_encrypt(":".join(sample(enc_traces, sample_num))),
        "p": aes_encrypt(":".join(enc_points)),
        "ext": aes_encrypt(xor_encode(token, f"{clicks},{len(enc_traces)}")),
    }


__all__ = [
    "aes_encrypt",
    "base64_decode_bytes",
    "base64_decode_private",
    "base64_encode_bytes",
    "base64_encode_private",
    "build_click_data",
    "gen_crc32_hex",
    "format_validate",
    "make_cb",
    "sample",
    "string_to_bytes",
    "to_byte",
    "u8",
    "uuid",
    "xor_decode",
    "xor_encode",
]
