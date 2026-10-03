"""Credential boundaries, semantic binding preservation and opaque pixel denial."""
from copy import deepcopy
import json
import struct
import zlib

import pytest

from webagent.errors import BusinessError
from webagent.evidence.redaction import (
    MAX_MODEL_DEPTH, MAX_MODEL_NODES, MAX_MODEL_TOTAL_CHARS, MAX_MODEL_TOTAL_BYTES, MAX_PNG_BYTES,
    MAX_PNG_DIMENSION, MAX_TEXT_CHARS, REDACTED, ScreenshotRedactor,
    TextRedactor, filter_compilation_text, filter_model_text, is_neutral_png, safe_metadata, safe_url,
)


@pytest.mark.parametrize("text,secret", [
    ("API credential sk-proj-ABCdef0123456789", "ABCdef0123456789"),
    ("sk-ABCdef0123456789", "ABCdef0123456789"),
    ("ghp_AbcDEF0123456789", "AbcDEF0123456789"),
    ("github_pat_AbcDEF0123456789_abcdef", "AbcDEF0123456789"),
    ("gho_AbcDEF0123456789", "AbcDEF0123456789"),
    ("AKIA0123456789ABCDEF", "0123456789ABCDEF"),
    ("ASIA0123456789ABCDEF", "0123456789ABCDEF"),
    ("Bearer short-secret-token", "short-secret-token"),
    ("password=hunter2", "hunter2"),
    ("Passwd: hunter2", "hunter2"),
    ("Password hunter2", "hunter2"),
    ("Password\nhunter2", "hunter2"),
    ("OTP\n918273", "918273"),
    ("pwd is hunter2", "hunter2"),
    ("API_KEY = fixture-api-key", "fixture-api-key"),
    ("token: fixture-token", "fixture-token"),
    ("access_token: fixture-access", "fixture-access"),
    ("refresh-token=fixture-refresh", "fixture-refresh"),
    ("client_secret=fixture-secret", "fixture-secret"),
    ("AWS_SECRET_ACCESS_KEY=FixtureLongSecret", "FixtureLongSecret"),
    ("OTP: 918273", "918273"),
    ("one-time-passcode = 918273", "918273"),
    ("验证码：918273", "918273"),
    ("您的密码是 hunter2", "hunter2"),
    ("密钥 fixture-secret", "fixture-secret"),
    ("session_id=fixture-session", "fixture-session"),
    ("eyJabcdefghijk.abcdefghijk.abcdefghijk", "abcdefghijk"),
])
def test_common_credentials_are_removed_and_filter_is_idempotent(text, secret):
    redactor = TextRedactor()
    assert redactor.contains_sensitive(text)
    filtered = redactor.filter(text)
    assert secret not in filtered
    assert REDACTED in filtered
    assert redactor.filter(filtered) == filtered
    assert not redactor.contains_sensitive(filtered)


@pytest.mark.parametrize("text", [
    "Token budget 2000; Run run-123; Task task-123; epoch 1",
    "max_model_tokens=2000 max_actions=30 time_budget_seconds=100",
    "Report 2026-10-01: revenue 918273 CNY, growth 12.5%",
    "User password reset help is available", "AWS infrastructure report",
    "普通事实：营业收入９１８２７３万元；参考编号M1-14。",
    "flow-token-budget selected_flow_versions source-1 artifact-01",
    "https://example.test/report?id=123&year=2026#section-1",
    "[REDACTED]", "", "Unicode café e\u0301; 安全事实",
])
def test_ordinary_facts_ids_numbers_and_safe_unicode_stay_exact(text):
    redactor = TextRedactor()
    assert not redactor.contains_sensitive(text)
    assert redactor.filter(text) == text


@pytest.mark.parametrize("invisible", ["\u200b", "\u200d", "\ufeff", "\u00ad", "\u034f", "\ufe0f", "\x00"])
def test_invisible_characters_cannot_hide_credentials(invisible):
    text = "ｐａｓｓ" + invisible + "ｗｏｒｄ： hunter2\nｓｋ－" + invisible + "ＡＢＣｄｅｆ０１２３４５６７８９"
    redactor = TextRedactor()
    assert redactor.contains_sensitive(text)
    result = redactor.filter(text)
    assert "hunter2" not in result and "ABCdef0123456789" not in result
    assert redactor.filter(result) == result


def test_known_literal_secrets_are_normalized_and_placeholders_stay_stable():
    redactor = TextRedactor(("café", "fixture-secret-long", "fixture-secret", "RED"))
    result = redactor.filter("source contains cafe\u0301, fixture-secret-long and RED")
    assert "café" not in result and "fixture-secret" not in result
    assert result.count(REDACTED) == 3
    assert redactor.filter(result) == result
    assert not redactor.contains_sensitive(result)


@pytest.mark.parametrize("header", ["Cookie", "Set-Cookie", "Authorization", "Proxy-Authorization"])
def test_whole_folded_credential_header_is_removed(header):
    result = TextRedactor().filter(header + ": sid=fixture-secret; identity=private\n  extra=hidden\nSafe fact: 42")
    assert "fixture-secret" not in result and "identity" not in result and "hidden" not in result
    assert "Safe fact: 42" in result
    assert TextRedactor().filter(result) == result


@pytest.mark.parametrize("kind", ["PRIVATE", "RSA PRIVATE", "EC PRIVATE", "OPENSSH PRIVATE", "ENCRYPTED PRIVATE"])
def test_complete_or_truncated_pem_private_key_is_removed(kind):
    for ending in ("\n-----END " + kind + " KEY-----", ""):
        text = "before\n-----BEGIN " + kind + " KEY-----\nFixturePrivateKeyMaterial\n" + ending
        result = TextRedactor().filter(text)
        assert "FixturePrivateKeyMaterial" not in result
        assert "before" in result


def test_nested_json_secret_properties_and_escaped_secret_strings_are_filtered():
    original = {
        "facts": {"value": 918273, "period": "2026-10-01"},
        "password": {"nested": ["private-one", "private-two"]},
        "body": {"a": "sk-ABCdef0123456789", "b": "password: hidden", "c": "literal-known"},
    }
    escaped = json.dumps(original).replace("password", "pass\\u0077ord").replace("ABC", "\\u0041BC")
    filtered = TextRedactor(("literal-known",)).filter(escaped)
    value = json.loads(filtered)
    assert value["facts"] == original["facts"]
    assert value["password"] == REDACTED
    assert value["body"] == {"a": REDACTED, "b": "password: " + REDACTED, "c": REDACTED}
    assert TextRedactor(("literal-known",)).filter(filtered) == filtered


def test_labelled_unquoted_secret_value_with_punctuation_is_not_partially_exposed():
    filtered = TextRedactor().filter("password: secret,containing;punctuation\nsafe fact 42")
    assert "secret" not in filtered and "containing" not in filtered and "punctuation" not in filtered
    assert "safe fact 42" in filtered


@pytest.mark.parametrize("text", ["Bearer " + "a" * 9000, "sk-" + "a" * 5000, "ghp_" + "a" * 5000])
def test_very_long_recognized_tokens_are_not_only_partially_masked(text):
    assert "a" * 8 not in TextRedactor().filter(text)


@pytest.mark.parametrize("url", [
    "https://user:private@example.test/report?q=safe#private",
    "https://example.test/report?q=safe&password=private#private",
    "https://example.test/report?q=safe&%70%61%73%73%77%6f%72%64=private#private",
    "https://example.test/report?q=safe&token=private#private",
    "https://example.test/report?q=safe&x-amz-signature=private#private",
])
def test_display_urls_remove_userinfo_sensitive_queries_and_fragments(url):
    result = safe_url(url)
    assert "private" not in result and "user:" not in result and "#" not in result
    assert "q=safe" in result


def test_display_url_preserves_safe_parameters_and_known_secret_query_values_are_removed():
    url = "https://example.test/report?id=123&year=2026&custom=literal-known#section"
    assert safe_url(url, known_secrets=("literal-known",)) == "https://example.test/report?id=123&year=2026"
    assert TextRedactor().filter("https://u:private@example.test/report") == "https://example.test/report"
    assert TextRedactor().contains_sensitive("https://example.test/report?code=one-time-auth-code")


@pytest.mark.parametrize("url", ["file:///private/secret", "https://[invalid", "https://example.test:99999/x", "javascript:alert(1)"])
def test_invalid_and_nonweb_display_urls_do_not_leak_arbitrary_input(url):
    assert safe_url(url) == REDACTED


def test_percent_encoded_secret_path_is_filtered_for_display_and_detected_for_bindings():
    url = "https://example.test/path/sk-%41BCdef0123456789"
    assert TextRedactor().contains_sensitive(url)
    assert "ABCdef0123456789" not in safe_url(url)


def test_metadata_filters_whole_structured_secret_property_and_opaque_object():
    text = safe_metadata({"token": {"x": "private"}, "ordinary": 42})
    assert json.loads(text) == {"token": REDACTED, "ordinary": 42}
    assert safe_metadata(object()) == REDACTED
    assert safe_metadata(float("nan")) == REDACTED
    assert safe_metadata("token: private") == "token: " + REDACTED
    assert safe_metadata([0] * MAX_MODEL_NODES) == REDACTED
    assert safe_metadata({"safe": "x" * (MAX_TEXT_CHARS + 1)}) == REDACTED


def _payload():
    return {
        "run_id": "run-1", "contract": {"budget_profile": {"max_actions": 10}, "source": "source-1"},
        "observation": {"title": "ordinary title", "visible_excerpt": "ordinary fact 42", "snapshot_id": "snapshot-1",
                        "source_url": "https://example.test/report", "redaction_status": "FILTERED"},
        "verified_checkpoint": {"checkpoint_id": "cp-1", "action_sequence": 3},
        "image_evidence_ids": [], "selected_flow_versions": [],
    }


def test_model_payload_filters_observation_prose_but_never_mutates_original_or_bindings():
    payload = _payload()
    payload["observation"]["title"] = "password: hidden"
    payload["observation"]["visible_excerpt"] = "Revenue 42\nsk-ABCdef0123456789"
    before = deepcopy(payload)
    result = filter_model_text(payload)
    assert payload == before
    assert result["contract"] == payload["contract"] and result["verified_checkpoint"] == payload["verified_checkpoint"]
    assert result["observation"]["title"] == "password: " + REDACTED
    assert result["observation"]["visible_excerpt"] == "Revenue 42\n" + REDACTED
    assert filter_model_text(result) == result


def test_model_payload_recursively_filters_free_metadata_values_and_secret_properties():
    payload = _payload()
    payload["observation"]["metadata"] = {"ordinary": ["password: private", "safe fact"], "token": {"secret": "hidden"}}
    result = filter_model_text(payload)
    assert result["observation"]["metadata"] == {"ordinary": ["password: " + REDACTED, "safe fact"], "token": REDACTED}


@pytest.mark.parametrize("path,value", [
    (("run_id",), "sk-ABCdef0123456789"),
    (("contract", "source"), "password: private"),
    (("observation", "source_url"), "https://user:private@example.test/report"),
    (("observation", "source_url"), "https://example.test/report?api_key=private"),
    (("observation", "snapshot_id"), "github_pat_ABCdef0123456789"),
    (("verified_checkpoint", "checkpoint_id"), "literal-known"),
])
def test_secret_binding_field_is_blocked_without_remapping_or_sensitive_error(path, value):
    payload = _payload()
    parent = payload
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    before = deepcopy(payload)
    with pytest.raises(BusinessError) as error:
        filter_model_text(payload, known_secrets=("literal-known",))
    assert payload == before
    assert error.value.code == "INPUT_BLOCKED" and error.value.status == 409
    assert value not in str(error.value) and "private" not in str(error.value)


def test_secret_property_outside_free_observation_text_is_blocked_even_for_unrecognized_value():
    payload = _payload()
    payload["contract"]["token"] = "unrecognized-custom-value"
    with pytest.raises(BusinessError):
        filter_model_text(payload)


@pytest.mark.parametrize("value", [None, b"secret", [], object()])
def test_model_payload_rejects_nonjson_roots(value):
    with pytest.raises(BusinessError):
        filter_model_text(value)


def test_text_and_model_size_and_nesting_are_bounded_with_safe_errors():
    with pytest.raises(BusinessError):
        TextRedactor().filter("x" * (MAX_TEXT_CHARS + 1))
    with pytest.raises(BusinessError):
        TextRedactor().contains_sensitive("x" * (MAX_TEXT_CHARS + 1))
    with pytest.raises(BusinessError):
        filter_model_text({"many": [0] * MAX_MODEL_NODES})
    value = "leaf"
    for _ in range(MAX_MODEL_DEPTH + 1):
        value = [value]
    with pytest.raises(BusinessError):
        filter_model_text({"deep": value})
    with pytest.raises(BusinessError):
        filter_model_text({"strings": ["x" * MAX_TEXT_CHARS] * (MAX_MODEL_TOTAL_CHARS // MAX_TEXT_CHARS + 1)})
    cyclic = {}
    cyclic["same"] = cyclic
    with pytest.raises(BusinessError):
        filter_model_text(cyclic)
    with pytest.raises(BusinessError):
        filter_model_text({"s": ["😀" * (MAX_MODEL_TOTAL_BYTES // 8)] * 2})
    with pytest.raises(BusinessError):
        TextRedactor().filter("surrogate \ud800")


def test_compiler_filters_instruction_and_recursive_web_context_without_mutating_explicit_parameters():
    payload = {
        "instruction": "Find report 2026-10-01\npassword: private-password",
        "explicit_scenario": "finance",
        "explicit_parameters": {"entity": "Example Inc", "report_version": "2026-10-01", "count": 10},
        "web_context": ["Revenue 42\nsk-ABCdef0123456789", {"title": "literal-known", "token": "private"}],
    }
    before = deepcopy(payload)
    result = filter_compilation_text(payload, known_secrets=("literal-known",))
    assert payload == before
    assert result["explicit_parameters"] == payload["explicit_parameters"]
    assert result["explicit_scenario"] == "finance"
    assert result["instruction"] == "Find report 2026-10-01\npassword: " + REDACTED
    assert result["web_context"] == ["Revenue 42\n" + REDACTED, {"title": REDACTED, "token": REDACTED}]
    assert filter_compilation_text(result, known_secrets=("literal-known",)) == result


@pytest.mark.parametrize("key,value", [
    ("explicit_parameters", {"repository": "literal-known"}),
    ("explicit_parameters", {"source_url": "https://user:private@example.test/report"}),
    ("explicit_parameters", {"source_url": "https://example.test/report?token=private"}),
    ("parameters", {"branch": "sk-ABCdef0123456789"}),
    ("explicit_scenario", "password: private"),
    ("scenario", "literal-known"),
])
def test_compiler_binding_secrets_block_before_fake_provider_is_called(key, value):
    payload = {"instruction": "ordinary user request", "web_context": [], key: value}
    calls = []

    def fake_compiler(filtered):
        calls.append(filtered)

    with pytest.raises(BusinessError) as error:
        fake_compiler(filter_compilation_text(payload, known_secrets=("literal-known",)))
    assert calls == [] and error.value.code == "INPUT_BLOCKED"
    assert "private" not in str(error.value) and "literal-known" not in str(error.value)


def test_compiler_normal_safe_urls_and_unicode_parameters_stay_exact():
    payload = {"instruction": "查找2026-10-01的报告", "explicit_scenario": "research",
               "explicit_parameters": {"topic": "量子计算", "source": "https://example.test/report?year=2026"},
               "web_context": ["ordinary fact 918273"]}
    assert filter_compilation_text(payload) == payload


@pytest.mark.parametrize("secrets", [[], ("",), (REDACTED,), (b"secret",), ("x" * 65537,), ("x",) * 129])
def test_invalid_known_secret_configuration_is_rejected_safely(secrets):
    with pytest.raises(BusinessError):
        TextRedactor(secrets)


def _chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)


def _png(width=2, height=2, pixel=b"\xff\0\0", *, before=b"", after=b"", raw=None, compressed=None, color=2, bits=8, interlace=0):
    if raw is None:
        raw = (b"\0" + pixel * width) * height
    if compressed is None:
        compressed = zlib.compress(raw)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, bits, color, 0, 0, interlace))
            + before + _chunk(b"IDAT", compressed) + after + _chunk(b"IEND", b""))


def test_raw_pixels_and_ancillary_credentials_are_blocked_without_returned_bytes():
    # DOM text may be empty while credentials are painted by canvas/image/CSS.
    # There is no claim that a regex can interpret these opaque pixels.
    raw = _png(before=_chunk(b"tEXt", b"title\0password: fixture-canvas-credential"))
    outcome = ScreenshotRedactor().filter(raw)
    assert outcome.redaction_status == "BLOCKED" and outcome.data is None and not outcome.model_eligible
    assert (outcome.width, outcome.height) == (2, 2)
    assert "fixture" not in outcome.reason
    assert not is_neutral_png(raw)


def test_whole_mask_keeps_only_dimensions_and_fixed_pixels_and_proves_its_bytes():
    secret = b"fixture-image-secret"
    raw = _png(5, 3, before=_chunk(b"tEXt", b"hidden\0" + secret))
    outcome = ScreenshotRedactor().mask_all_png(raw)
    assert outcome.redaction_status == "FILTERED" and outcome.model_eligible
    assert (outcome.width, outcome.height) == (5, 3)
    assert outcome.data and outcome.data != raw and secret not in outcome.data
    assert is_neutral_png(outcome.data)
    assert outcome.reason == "whole_image_mask_loses_visual_context"
    # A valid non-neutral image, or any metadata-bearing image, cannot claim
    # to be a neutral derivative merely through a database status string.
    assert not is_neutral_png(_png(5, 3))
    assert not is_neutral_png(_png(5, 3, b"\x80\x80\x80", before=_chunk(b"tEXt", secret)))
    assert ScreenshotRedactor().filter(outcome.data).redaction_status == "BLOCKED"


@pytest.mark.parametrize("data", [
    b"", b"not a png", b"%PDF-1.7 hidden", b"{\"log\":\"HAR\"}",
    _png()[:-1], _png() + b"secret appended after IEND", _png(raw=b""),
    _png(raw=b"\0" + b"x" * 100), _png(raw=(b"\x05" + b"\0" * 6) * 2),
    _png(compressed=b"bad-zlib"), _png(compressed=zlib.compress(b"\0" * 1000)),
    _png(compressed=zlib.compress(b"\0" * 14) + b"extra stream"),
    _png(width=MAX_PNG_DIMENSION + 1, height=1), _png(width=0, height=1),
    _png(width=8192, height=8192, raw=b""), _png(color=3, bits=8),
    _png(after=_chunk(b"IDAT", zlib.compress(b"extra"))),
    _png(before=_chunk(b"ABCD", b"unsupported critical chunk")),
    _png(after=_chunk(b"tEXt", b"metadata") + _chunk(b"IDAT", b"extra")),
])
def test_malformed_or_unsupported_png_never_bypasses_the_gate(data):
    with pytest.raises(BusinessError) as error:
        ScreenshotRedactor().filter(data)
    assert error.value.code == "INPUT_BLOCKED" and error.value.status == 409
    assert not is_neutral_png(data)


def test_crc_and_encoded_size_are_validated_before_processing_pixels():
    broken = bytearray(_png())
    broken[-1] ^= 1
    with pytest.raises(BusinessError):
        ScreenshotRedactor().filter(bytes(broken))
    with pytest.raises(BusinessError):
        ScreenshotRedactor().filter(b"\x89PNG\r\n\x1a\n" + b"x" * MAX_PNG_BYTES)


def test_small_interlaced_and_indexed_inputs_are_checked_then_remain_blocked():
    # A 1x1 Adam7 image has only the first pass.
    assert ScreenshotRedactor().filter(_png(1, 1, interlace=1)).redaction_status == "BLOCKED"
    palette = _chunk(b"PLTE", b"\0\0\0\xff\xff\xff")
    indexed = _png(2, 2, before=palette, color=3, bits=1, raw=b"\0\x80\0\x40")
    assert ScreenshotRedactor().filter(indexed).redaction_status == "BLOCKED"


def test_neutral_proof_rejects_same_visible_color_when_encoding_or_metadata_is_not_canonical():
    # Filter 1 can reconstruct the same grey pixels, but is not the canonical
    # whole-mask derivative and must be denied until independently decoded.
    filtered_row = b"\x01\x80\x80\x80\0\0\0"
    assert not is_neutral_png(_png(raw=filtered_row * 2))
    assert not is_neutral_png(_png(color=6, pixel=b"\x80\x80\x80\xff"))
    assert is_neutral_png(_png(pixel=b"\x80\x80\x80"))
