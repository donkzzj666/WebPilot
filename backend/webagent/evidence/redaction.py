"""Bounded, deterministic text filtering and a fail-closed raster boundary.

Text rules recognize labelled credentials and common credential formats; they
are not an assertion that arbitrary text contains no secret. Callers can add
known secrets. Raw screenshots remain BLOCKED: DOM masking cannot inspect
canvas, CSS backgrounds, image pixels or PDF content. An optional whole-image
neutral derivative has no visual information and cannot substantiate a visual
business claim. Its canonical bytes can be independently verified before send.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
import struct
import unicodedata
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit
import zlib

from ..errors import BusinessError


REDACTED = "[REDACTED]"
MAX_TEXT_CHARS = 1_000_000
MAX_MODEL_TOTAL_CHARS = 2_000_000
MAX_MODEL_TOTAL_BYTES = 4_000_000
MAX_MODEL_NODES = 20_000
MAX_MODEL_DEPTH = 32
MAX_PNG_BYTES = 16 * 1024 * 1024
MAX_PNG_DIMENSION = 8192
MAX_PNG_PIXELS = 16_777_216
MAX_PNG_DECODED_BYTES = 64 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _blocked() -> BusinessError:
    # Never interpolate the input, secret, field name or raw parser error.
    return BusinessError("INPUT_BLOCKED", "Content cannot cross the model boundary", status=409)


def _normalized(text: str) -> str:
    # Compatibility characters and format characters must not hide a label or
    # token from the detector. Safe input is returned unchanged by filter().
    characters = []
    for char in text:
        category, codepoint = unicodedata.category(char), ord(char)
        if category == "Cs":
            raise _blocked()
        if (category == "Cf" or (category == "Cc" and char not in "\r\n\t")
                or codepoint == 0x034f or 0x180b <= codepoint <= 0x180f
                or 0xfe00 <= codepoint <= 0xfe0f or 0xe0100 <= codepoint <= 0xe01ef):
            continue
        characters.append(char)
    return unicodedata.normalize("NFKC", "".join(characters))


_LABEL = (
    r"password|passwd|pwd|api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
    r"id[ _-]?token|token|authorization|proxy[ _-]?authorization|"
    r"cookies?|set[ _-]?cookie|client[ _-]?secret|secret(?:[ _-]?key)?|"
    r"aws[ _-]?(?:secret[ _-]?access[ _-]?key|access[ _-]?key[ _-]?id|session[ _-]?token)|"
    r"session[ _-]?(?:id|token)|credential|otp|one[ _-]?time[ _-]?(?:password|passcode)|"
    r"验证码|校验码|动态口令|密码|口令|密钥|令牌"
)
_LABEL_KEY = re.compile(r"^(?:" + _LABEL + r")$", re.IGNORECASE)
_PEM = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
    r"[\s\S]*?(?:-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|$)"
)
_HEADERS = re.compile(
    r"(?im)^(?P<label>[ \t]*(?:Cookie|Set-Cookie|Authorization|Proxy-Authorization)[ \t]*:)"
    r"[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*"
)
_BEARER = re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/=\-]+", re.IGNORECASE)
_TOKENS = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"sk-(?:proj-)?[A-Za-z0-9_\-]{8,}|"
    r"gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|"
    r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
    r")"
)
# A delimiter is required for generic 'token': ordinary prose such as 'token
# budget 2000' and DTO names like max_model_tokens are not credentials.
_PAIRS = re.compile(
    r"(?P<label>(?<![A-Za-z0-9_])(?:[\"']?(?:" + _LABEL + r")[\"']?))"
    r"(?P<sep>[ \t]*(?:[:=]|\bis\b|为|是)[ \t]*)"
    r"(?P<value>\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|[^\r\n]+)",
    re.IGNORECASE,
)
_CHINESE_PAIRS = re.compile(
    r"(?P<label>验证码|校验码|动态口令|密码|口令|密钥|令牌)"
    r"(?P<sep>[ \t]*(?:[:=]|为|是|[ \t]+)[ \t]*)"
    r"(?P<value>[^\r\n]+)"
)
_FIELD_LINES = re.compile(
    r"(?im)^(?P<label>[ \t]*(?:password|passwd|pwd|api[ _-]?key|client[ _-]?secret|"
    r"secret[ _-]?key|otp|one[ _-]?time[ _-]?(?:password|passcode)))"
    r"(?P<sep>[ \t]+|\r?\n[ \t]*)(?P<value>[^\r\n]+)"
)
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,31}://[^\s<>\"']+")
_QUERY_SECRET = re.compile(
    r"^(?:" + _LABEL + r"|key|signature|sig|signed[ _-]?token|"
    r"x[ _-]?amz[ _-]?(?:credential|signature|security[ _-]?token)|"
    r"x[ _-]?goog[ _-]?(?:credential|signature)|code)$", re.IGNORECASE
)


def _redacted_value(value: object) -> bool:
    return isinstance(value, str) and value.strip().strip("\"'") == REDACTED


def _secret_key(key: str) -> bool:
    return bool(_LABEL_KEY.fullmatch(_normalized(key).strip()))


class TextRedactor:
    """Recognize bounded textual credentials; remove their values deterministically."""

    def __init__(self, known_secrets: tuple[str, ...] = ()):
        if not isinstance(known_secrets, tuple) or len(known_secrets) > 128:
            raise _blocked()
        normalized = []
        size = 0
        for secret in known_secrets:
            if type(secret) is not str or not secret or len(secret) > 65536:
                raise _blocked()
            value = _normalized(secret)
            size += len(value)
            if not value or value == REDACTED or size > 131072:
                raise _blocked()
            normalized.append(value)
        self._secrets = tuple(sorted(set(normalized), key=len, reverse=True))

    def filter(self, text: str) -> str:
        if type(text) is not str or len(text) > MAX_TEXT_CHARS:
            raise _blocked()
        normalized = _normalized(text)
        if len(normalized) > MAX_TEXT_CHARS:
            raise _blocked()
        filtered = self._filter_normalized(normalized)
        # Keep safe numbers, IDs, Unicode facts and ordinary prose byte-exact.
        return text if filtered == normalized else filtered

    def contains_sensitive(self, text: str) -> bool:
        if type(text) is not str or len(text) > MAX_TEXT_CHARS:
            raise _blocked()
        normalized = _normalized(text)
        if len(normalized) > MAX_TEXT_CHARS:
            raise _blocked()
        return self._filter_normalized(normalized) != normalized

    def _filter_normalized(self, text: str) -> str:
        filtered = self._replace_known(text)
        filtered = _PEM.sub(REDACTED, filtered)
        filtered = _HEADERS.sub(lambda match: match.group("label") + " " + REDACTED, filtered)
        filtered = _BEARER.sub("Bearer " + REDACTED, filtered)
        filtered = _TOKENS.sub(REDACTED, filtered)
        # URL handling precedes pair handling: userinfo and percent-encoded query
        # values need parsing, not a substring credential guess.
        filtered = _URL.sub(lambda match: self._url(match.group(0)), filtered)
        # JSON credential properties can hold nested objects/arrays. Remove the
        # whole value, not just its first lexical token.
        stripped = filtered.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                value = json.loads(filtered)
                scrubbed, changed = self._scrub_json(value)
                if changed:
                    return json.dumps(scrubbed, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                return filtered
            except (ValueError, TypeError, RecursionError):
                # Malformed JSON still receives lexical filtering below.
                pass
        filtered = _PAIRS.sub(self._pair, filtered)
        filtered = _CHINESE_PAIRS.sub(self._pair, filtered)
        filtered = _FIELD_LINES.sub(self._pair, filtered)
        return filtered

    def _replace_known(self, text: str) -> str:
        # Never interpret the fixed placeholder as a fresh credential, including
        # when a caller's known secret is a substring of the word REDACTED.
        for secret in self._secrets:
            text = REDACTED.join(segment.replace(secret, REDACTED) for segment in text.split(REDACTED))
        return text

    @staticmethod
    def _pair(match: re.Match) -> str:
        value = match.group("value")
        if _redacted_value(value):
            return match.group(0)
        # Keep JSON string quoting when the original was quoted.
        if value.startswith(('"', "'")) and value[-1:] == value[:1]:
            replacement = value[0] + REDACTED + value[0]
        else:
            replacement = REDACTED
        return match.group("label") + match.group("sep") + replacement

    def _scrub_json(self, value: object) -> tuple[object, bool]:
        count = [0]

        def walk(current: object, depth: int) -> tuple[object, bool]:
            count[0] += 1
            if depth > MAX_MODEL_DEPTH or count[0] > MAX_MODEL_NODES:
                raise _blocked()
            if isinstance(current, dict):
                output, changed = {}, False
                for key, item in current.items():
                    safe_key = self._filter_prose(key)
                    changed |= safe_key != key
                    if _secret_key(key) and not _redacted_value(item):
                        output[safe_key], changed = REDACTED, True
                    else:
                        result, was_changed = walk(item, depth + 1)
                        output[safe_key] = result
                        changed |= was_changed
                return output, changed
            if isinstance(current, list):
                output, changed = [], False
                for item in current:
                    result, was_changed = walk(item, depth + 1)
                    output.append(result)
                    changed |= was_changed
                return output, changed
            if isinstance(current, str):
                filtered = self._filter_prose(current)
                return filtered, filtered != current
            return current, False

        return walk(value, 0)

    def _url(self, url: str) -> str:
        if len(url) > 16384:
            return REDACTED
        try:
            parsed = urlsplit(url)
            # Query parsing is bounded independently of the surrounding text.
            pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=512)
            changed = parsed.username is not None or parsed.password is not None
            netloc = parsed.netloc.rsplit("@", 1)[-1] if changed else parsed.netloc
            safe_pairs = []
            for key, value in pairs:
                if _QUERY_SECRET.fullmatch(_normalized(key).strip()) or self._sensitive_url_value(value):
                    changed = True
                else:
                    safe_pairs.append((key, value))
            fragment = parsed.fragment
            if fragment and self._sensitive_url_value(unquote(fragment)):
                fragment, changed = "", True
            path = parsed.path
            # Path credentials are filtered too, but do not remap ordinary paths.
            decoded_path = unquote(path)
            safe_path = self._filter_url_literal(decoded_path)
            if safe_path != decoded_path:
                path, changed = quote(safe_path, safe="/[]:@!$&'()*+,;=-._~"), True
            if not changed:
                return url
            return urlunsplit((parsed.scheme, netloc, path, urlencode(safe_pairs), fragment))
        except (ValueError, UnicodeError):
            return REDACTED

    def _filter_prose(self, value: str) -> str:
        normalized = _normalized(value)
        result = self._replace_known(normalized)
        result = _PEM.sub(REDACTED, result)
        result = _HEADERS.sub(lambda match: match.group("label") + " " + REDACTED, result)
        result = _BEARER.sub("Bearer " + REDACTED, result)
        result = _TOKENS.sub(REDACTED, result)
        result = _URL.sub(lambda match: self._url(match.group(0)), result)
        result = _PAIRS.sub(self._pair, result)
        result = _CHINESE_PAIRS.sub(self._pair, result)
        result = _FIELD_LINES.sub(self._pair, result)
        return value if result == normalized else result

    def _filter_url_literal(self, value: str) -> str:
        result = self._replace_known(_normalized(value))
        result = _TOKENS.sub(REDACTED, result)
        result = _BEARER.sub("Bearer " + REDACTED, result)
        result = _PAIRS.sub(self._pair, result)
        result = _CHINESE_PAIRS.sub(self._pair, result)
        return result

    def _sensitive_url_value(self, value: str) -> bool:
        normalized = _normalized(value)
        return self._filter_url_literal(normalized) != normalized


def safe_url(url: str, *, known_secrets: tuple[str, ...] = ()) -> str:
    """Display URL only: strip userinfo, credential queries and all fragments.

    This intentionally changes the address. Binding/permission URLs in a model
    input must instead be rejected when sensitive, never silently remapped.
    """
    if type(url) is not str or len(url) > 16384:
        raise _blocked()
    redactor = TextRedactor(known_secrets)
    sanitized = redactor._url(_normalized(url))
    if sanitized == REDACTED:
        return sanitized
    try:
        parsed = urlsplit(sanitized)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            return REDACTED
        # Accessing .port also validates malformed/out-of-range authority ports.
        parsed.port
        result = urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1],
                             parsed.path, parsed.query, ""))
        return redactor.filter(result)
    except (ValueError, UnicodeError):
        return REDACTED


def safe_metadata(value: object, *, known_secrets: tuple[str, ...] = ()) -> str:
    """A safe display string for JSON metadata; opaque objects get a placeholder."""
    redactor = TextRedactor(known_secrets)
    try:
        if type(value) is str:
            return redactor.filter(value)
        count, size = 0, 0

        def bounded(current: object, depth: int) -> object:
            nonlocal count, size
            count += 1
            if depth > MAX_MODEL_DEPTH or count > MAX_MODEL_NODES:
                raise _blocked()
            if type(current) is str:
                size += len(current)
                if size > MAX_MODEL_TOTAL_CHARS:
                    raise _blocked()
                return redactor.filter(current)
            if type(current) is dict:
                result = {}
                for key, item in current.items():
                    if type(key) is not str or len(key) > 1024:
                        raise _blocked()
                    size += len(key)
                    if size > MAX_MODEL_TOTAL_CHARS:
                        raise _blocked()
                    safe_key = redactor.filter(key)
                    if _secret_key(key):
                        result[safe_key] = REDACTED
                    else:
                        result[safe_key] = bounded(item, depth + 1)
                return result
            if type(current) is list:
                return [bounded(item, depth + 1) for item in current]
            if current is None or type(current) in (bool, int):
                return current
            if type(current) is float and math.isfinite(current):
                return current
            raise _blocked()

        serialized = json.dumps(bounded(value, 0), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return redactor.filter(serialized)
    except (ValueError, TypeError, RecursionError, BusinessError):
        return REDACTED


_FREE_OBSERVATION_FIELDS = frozenset({
    "title", "visible_excerpt", "visible_text", "text", "description", "caption", "metadata",
})


def filter_model_text(payload: dict, *, known_secrets: tuple[str, ...] = ()) -> dict:
    """Copy and filter observation prose; reject sensitive binding semantics.

    Contracts, checkpoints, IDs, URLs and all other semantic bindings stay
    exact. A secret there causes a safe error before a provider call or model
    reservation. Only observation prose may be replaced with [REDACTED].
    """
    return _filter_payload(payload, known_secrets, frozenset(
        ("observation", field) for field in _FREE_OBSERVATION_FIELDS
    ))


def filter_compilation_text(payload: dict, *, known_secrets: tuple[str, ...] = ()) -> dict:
    """Filter compiler prose, preserving explicit parameters and scenario.

    This accepts the already-restricted extraction payload. ``instruction``
    and ``web_context`` carry prose only; sensitive explicit semantic values
    are blocked, never rewritten into a different task or source URL.
    """
    return _filter_payload(payload, known_secrets, frozenset({("instruction",), ("web_context",)}))


def _filter_payload(payload: dict, known_secrets: tuple[str, ...], free_paths: frozenset) -> dict:
    if type(payload) is not dict:
        raise _blocked()
    redactor = TextRedactor(known_secrets)
    count = size = encoded_size = 0

    def charge(text: str) -> None:
        nonlocal size, encoded_size
        if len(text) > MAX_TEXT_CHARS:
            raise _blocked()
        try:
            byte_size = len(text.encode("utf-8"))
        except UnicodeError:
            raise _blocked() from None
        size += len(text)
        encoded_size += byte_size
        if size > MAX_MODEL_TOTAL_CHARS or encoded_size > MAX_MODEL_TOTAL_BYTES:
            raise _blocked()

    def walk(current: object, path: tuple[str, ...], depth: int, free: bool = False) -> object:
        nonlocal count
        count += 1
        if depth > MAX_MODEL_DEPTH or count > MAX_MODEL_NODES:
            raise _blocked()
        if type(current) is str:
            charge(current)
            if free:
                return redactor.filter(current)
            if redactor.contains_sensitive(current):
                raise _blocked()
            return current
        if type(current) is dict:
            output = {}
            for key, item in current.items():
                if type(key) is not str or len(key) > 1024 or redactor.contains_sensitive(key):
                    raise _blocked()
                charge(key)
                child_free = free or path + (key,) in free_paths
                if _secret_key(key) and not _redacted_value(item):
                    if not child_free:
                        raise _blocked()
                    output[key] = REDACTED
                else:
                    output[key] = walk(item, path + (key,), depth + 1, child_free)
            return output
        if type(current) is list:
            return [walk(item, path + ("[]",), depth + 1, free) for item in current]
        if current is None or type(current) in (bool, int):
            return current
        if type(current) is float and math.isfinite(current):
            return current
        raise _blocked()

    return walk(payload, (), 0)


@dataclass(frozen=True)
class ScreenshotOutcome:
    redaction_status: str
    reason: str
    data: bytes | None = None
    model_eligible: bool = False
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class _PNG:
    width: int
    height: int
    bit_depth: int
    color_type: int
    interlace: int
    chunks: tuple[tuple[bytes, bytes], ...]


def _parse_png(data: bytes) -> _PNG:
    if type(data) is not bytes or not 45 <= len(data) <= MAX_PNG_BYTES or not data.startswith(_PNG_SIGNATURE):
        raise _blocked()
    offset, chunks, total_data = 8, [], 0
    width = height = bit_depth = color_type = interlace = 0
    seen_data = seen_end = data_ended = seen_palette = False
    while offset < len(data):
        if len(chunks) > 4096 or offset + 12 > len(data):
            raise _blocked()
        length, kind = struct.unpack_from(">I4s", data, offset)
        end = offset + 12 + length
        if (end > len(data) or length > MAX_PNG_BYTES or not all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in kind)
                or not 65 <= kind[2] <= 90):
            raise _blocked()
        body = data[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        if zlib.crc32(kind + body) & 0xffffffff != crc:
            raise _blocked()
        if not chunks:
            if kind != b"IHDR" or length != 13:
                raise _blocked()
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
            supported = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if (not 1 <= width <= MAX_PNG_DIMENSION or not 1 <= height <= MAX_PNG_DIMENSION
                    or width * height > MAX_PNG_PIXELS or color_type not in supported
                    or bit_depth not in supported[color_type] or compression != 0 or filtering != 0 or interlace not in (0, 1)):
                raise _blocked()
        elif kind == b"IHDR" or seen_end:
            raise _blocked()
        if kind == b"IDAT":
            if data_ended:
                raise _blocked()
            seen_data = True
            total_data += length
            if total_data > MAX_PNG_BYTES:
                raise _blocked()
        elif seen_data:
            data_ended = True
        if kind == b"PLTE":
            if (seen_palette or seen_data or color_type in (0, 4) or not length or length % 3
                    or length > 768 or (color_type == 3 and length // 3 > 2 ** bit_depth)):
                raise _blocked()
            seen_palette = True
        if kind == b"IEND":
            if length or not seen_data or end != len(data):
                raise _blocked()
            seen_end = True
        elif kind not in (b"IHDR", b"IDAT", b"PLTE") and kind[:1].isupper():
            # An unsupported critical chunk must not be interpreted as an image.
            raise _blocked()
        chunks.append((kind, body))
        offset = end
    if not seen_end or (color_type == 3 and not seen_palette):
        raise _blocked()
    return _PNG(width, height, bit_depth, color_type, interlace, tuple(chunks))


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)


def _decoded(data: bytes, bound: int) -> bytes:
    try:
        decoder = zlib.decompressobj()
        result = decoder.decompress(data, bound + 1)
        if len(result) > bound or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
            raise _blocked()
        return result
    except zlib.error:
        raise _blocked() from None


def _validate_encoding(png: _PNG) -> None:
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[png.color_type]
    if png.interlace:
        passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
                  (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    else:
        passes = ((0, 0, 1, 1),)
    rows = []
    total = 0
    for left, top, stride_x, stride_y in passes:
        width = max(0, (png.width - left + stride_x - 1) // stride_x)
        height = max(0, (png.height - top + stride_y - 1) // stride_y)
        if width and height:
            length = (width * channels * png.bit_depth + 7) // 8 + 1
            total += length * height
            rows.append((length, height))
    if total > MAX_PNG_DECODED_BYTES:
        raise _blocked()
    compressed = b"".join(body for kind, body in png.chunks if kind == b"IDAT")
    raw = _decoded(compressed, total)
    if len(raw) != total:
        raise _blocked()
    offset = 0
    for length, height in rows:
        for _ in range(height):
            if raw[offset] > 4:
                raise _blocked()
            offset += length


def is_neutral_png(data: bytes) -> bool:
    """Prove the canonical all-neutral derivative independently of metadata."""
    try:
        png = _parse_png(data)
        if (png.bit_depth, png.color_type, png.interlace) != (8, 2, 0):
            return False
        if tuple(kind for kind, _ in png.chunks) != (b"IHDR", b"IDAT", b"IEND"):
            return False
        expected_row = b"\0" + b"\x80\x80\x80" * png.width
        size = len(expected_row) * png.height
        raw = _decoded(png.chunks[1][1], size)
        return len(raw) == size and all(
            raw[offset:offset + len(expected_row)] == expected_row
            for offset in range(0, size, len(expected_row))
        )
    except BusinessError:
        return False


class ScreenshotRedactor:
    """Raw pixel payloads never gain permission through DOM-only masking."""

    def filter(self, data: bytes) -> ScreenshotOutcome:
        png = _parse_png(data)
        # Validate basic image encoding with bounded decompression. Pixel masks
        # are not inferred from DOM text, selector boxes or credential regexes.
        _validate_encoding(png)
        return ScreenshotOutcome("BLOCKED", "opaque_pixels_require_trusted_redaction", width=png.width, height=png.height)

    def mask_all_png(self, data: bytes) -> ScreenshotOutcome:
        png = _parse_png(data)
        # Ignore every original pixel and ancillary chunk, including textual
        # metadata; output contains only dimensions and a fixed solid colour.
        self.filter(data)
        row = b"\0" + b"\x80\x80\x80" * png.width
        encoder = zlib.compressobj(level=9)
        parts = [encoder.compress(row) for _ in range(png.height)]
        parts.append(encoder.flush())
        encoded = (_PNG_SIGNATURE
                   + _chunk(b"IHDR", struct.pack(">IIBBBBB", png.width, png.height, 8, 2, 0, 0, 0))
                   + _chunk(b"IDAT", b"".join(parts)) + _chunk(b"IEND", b""))
        if not is_neutral_png(encoded):
            raise _blocked()
        return ScreenshotOutcome("FILTERED", "whole_image_mask_loses_visual_context", encoded, True,
                                 png.width, png.height)
