"""Protocol tests use synthetic responses only; no credentials or paid calls."""
import asyncio
import base64
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from webagent.models.transport import (
    MAX_IMAGE_BYTES, MAX_RESPONSE_BYTES, DeepSeekTransport, ModelConfig, ModelImage,
    ProviderFailure, ProviderReply, validate_images,
)

KEY = "synthetic-model-key-not-a-real-credential"
INPUT = {"image_evidence_ids": [], "observation": {"visible_excerpt": "Synthetic page"}}
SCHEMA = {"type": "object", "properties": {"type": {"const": "RequestInput"}, "reason": {"type": "string"}}}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNoaGgAAAMEAYFL09IQAAAAAElFTkSuQmCC")


def response_payload(**updates):
    payload = {
        "id": "chatcmpl-synthetic", "model": "deepseek-flash", "system_fingerprint": "fp_test",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": '{"type":"RequestInput","requested_fields":["target"],"reason":"Missing"}',
            "reasoning_content": "private-chain-of-thought-do-not-export",
        }}],
        "usage": {"prompt_tokens": 15, "completion_tokens": 12, "total_tokens": 27,
                  "prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 10},
    }
    payload.update(updates)
    return payload


async def _call(handler, **kwargs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = DeepSeekTransport(ModelConfig(), SecretStr(KEY), client=client)
        return await transport.complete(INPUT, SCHEMA, **kwargs)


def test_text_json_protocol_and_metadata_whitelist():
    seen = []

    def handler(request):
        seen.append(request)
        payload = response_payload()
        payload["usage"]["private"] = "private-usage"
        return httpx.Response(200, json=payload)

    reply = asyncio.run(_call(handler))
    request = seen[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.headers["Authorization"] == "Bearer " + KEY
    assert KEY not in request.content.decode()
    assert body["model"] == "deepseek-flash"
    assert body["max_tokens"] == 1024 and body["stream"] is False
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    assert "tools" not in body
    assert "ProposeResult.items must be an object" in body["messages"][0]["content"]
    assert "scenario-specific arrays and fields must follow the complete schema" in body["messages"][0]["content"]
    assert json.loads(body["messages"][1]["content"]) == INPUT
    assert reply.provider_request_id == "chatcmpl-synthetic"
    assert reply.invalid_reason is None
    assert reply.usage == {"input_tokens": 15, "output_tokens": 12, "image_units": None,
                          "provider_usage": {"total_tokens": 27, "prompt_cache_hit_tokens": 5,
                                             "prompt_cache_miss_tokens": 10, "response_model": "deepseek-flash",
                                             "system_fingerprint": "fp_test", "finish_reason": "stop"}}
    assert "private" not in repr(reply)
    assert "private" not in json.dumps(dataclasses.asdict(reply))
    assert request.extensions["timeout"] == {"connect": 10.0, "read": 30.0, "write": 30.0, "pool": 10.0}


def test_direct_metadata_prompt_preserves_four_output_choices_without_source_answers():
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response_payload())
    asyncio.run(_call(handler))
    prompt = seen[0]['messages'][0]['content']
    for name in ('Action', 'RequestEvidence', 'ProposeResult', 'RequestInput'):
        assert name in prompt
    assert 'arxiv:<id>vN' in prompt and 'visible [v1] UTC submission history' in prompt
    assert 'frozen cutoff' in prompt and 'one content page' in prompt
    assert 'never invent them' in prompt
    assert '2401.00001' not in prompt and 'Ada Example' not in prompt
    assert 'only items.values is an array' not in prompt
    assert 'JSON Schema: ' + json.dumps(SCHEMA, ensure_ascii=False, sort_keys=True, separators=(',', ':')) in prompt


def test_image_protocol_is_inline_and_matches_evidence():
    seen = []

    async def exercise():
        def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=response_payload())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            transport = DeepSeekTransport(ModelConfig(), KEY, client)
            return await transport.complete({**INPUT, "image_evidence_ids": ["shot-1"]}, SCHEMA,
                                            (ModelImage("shot-1", PNG, "image/png"),))

    asyncio.run(exercise())
    content = seen[0]["messages"][1]["content"]
    assert content[0]["type"] == "text"
    assert content[1] == {"type": "text", "text": "Image evidence ID: shot-1"}
    assert content[2]["image_url"]["detail"] == "original"
    assert content[2]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert "image_url" not in seen[0]["messages"][0]["content"]


@pytest.mark.parametrize(("status", "kind", "subtype"), [
    (400, "provider_error", "invalid_request"), (401, "invalid_credentials", "provider_authentication"),
    (402, "provider_error", "insufficient_balance"), (422, "provider_error", "invalid_request"),
    (429, "rate_limit", "model_service"), (500, "provider_error", "provider_error"),
    (503, "provider_error", "provider_unavailable"), (302, "provider_error", "redirect_refused"),
])
def test_http_errors_are_safe_and_never_retried(status, kind, subtype):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status, headers={"retry-after": "7", "location": "https://attacker.invalid/"},
                              text=KEY + " sensitive provider message")

    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(_call(handler))
    error = caught.value
    assert (error.error_class, error.subtype, error.http_status) == (kind, subtype, status)
    assert error.retry_after_seconds == (7.0 if status == 429 else None)
    assert len(seen) == 1
    assert KEY not in str(error) and "sensitive" not in repr(error)
    assert error.__cause__ is None


def test_injected_client_cannot_enable_redirects():
    seen = []

    async def exercise():
        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(307, headers={"location": "https://attacker.invalid/"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            await DeepSeekTransport(ModelConfig(), KEY, client).complete(INPUT, SCHEMA)

    with pytest.raises(ProviderFailure, match="redirect_refused"):
        asyncio.run(exercise())
    assert seen == ["https://api.deepseek.com/chat/completions"]


@pytest.mark.parametrize("header", ["-1", "nan", "Infinity", "not-a-date", "1.5", "a" * 101])
def test_invalid_retry_after_is_unknown(header):
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(_call(lambda _: httpx.Response(429, headers={"retry-after": header})))
    assert caught.value.retry_after_seconds is None


def test_retry_after_date_and_upper_bound():
    header = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(_call(lambda _: httpx.Response(429, headers={"retry-after": header})))
    assert 25 <= caught.value.retry_after_seconds <= 30
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(_call(lambda _: httpx.Response(429, headers={"retry-after": "9999999"})))
    assert caught.value.retry_after_seconds == 86400


@pytest.mark.parametrize(("exception", "kind"), [
    (httpx.ConnectTimeout, "timeout"), (httpx.ReadTimeout, "timeout"),
    (httpx.WriteTimeout, "timeout"), (httpx.PoolTimeout, "timeout"),
    (httpx.ConnectError, "provider_error"), (httpx.RemoteProtocolError, "provider_error"),
])
def test_transport_exceptions_hide_request_and_credentials(exception, kind):
    def handler(request):
        raise exception(KEY + " sensitive transport detail", request=request)

    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(_call(handler))
    assert caught.value.error_class == kind
    assert KEY not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_total_deadline_and_caller_cancellation():
    async def slow(request):
        await asyncio.sleep(10)
        return httpx.Response(200, json=response_payload())

    async def exercise():
        config = ModelConfig(connect_seconds=0.01, read_seconds=0.01, total_seconds=0.02)
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
            transport = DeepSeekTransport(config, KEY, client)
            with pytest.raises(ProviderFailure) as caught:
                await transport.complete(INPUT, SCHEMA)
            assert caught.value.error_class == "timeout"
            pending = asyncio.create_task(transport.complete(INPUT, SCHEMA))
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
    asyncio.run(exercise())


@pytest.mark.parametrize(("change", "reason"), [
    ({"content": ""}, "empty_output"), ({"content": "  \n"}, "empty_output"),
    ({"content": None}, "non_text_output"), ({"content": []}, "non_text_output"),
    ({"tool_calls": [{"name": "navigate"}]}, "tool_calls_forbidden"),
    ({"tool_calls": []}, "tool_calls_forbidden"),
    ({"function_call": {"name": "navigate"}}, "tool_calls_forbidden"),
    ({"role": "user"}, "invalid_message_role"),
])
def test_invalid_message_preserves_usage(change, reason):
    payload = response_payload()
    payload["choices"][0]["message"].update(change)
    reply = asyncio.run(_call(lambda _: httpx.Response(200, json=payload)))
    assert reply.invalid_reason == reason
    assert reply.usage["input_tokens"] == 15
    assert reply.provider_request_id == "chatcmpl-synthetic"


@pytest.mark.parametrize(("finish", "reason"), [
    ("length", "truncated_output"), ("tool_calls", "invalid_finish_reason"),
    ("content_filter", "invalid_finish_reason"), (None, "invalid_finish_reason"),
    ("insufficient_system_resource", "invalid_finish_reason"),
])
def test_nonstop_completion_rejected(finish, reason):
    payload = response_payload()
    payload["choices"][0]["finish_reason"] = finish
    reply = asyncio.run(_call(lambda _: httpx.Response(200, json=payload)))
    assert reply.invalid_reason == reason


@pytest.mark.parametrize("choices", [[], [response_payload()["choices"][0]] * 2])
def test_exactly_one_choice_required(choices):
    reply = asyncio.run(_call(lambda _: httpx.Response(200, json=response_payload(choices=choices))))
    assert reply.invalid_reason == "multiple_or_missing_choices"


@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", b"{\"choices\":{},\"id\":\"x\"}",
                                 b'{"choices":[],"choices":[]}', b'{"choices":[],"usage":NaN}',
                                 b'\xff', b'{"choices":[null]}'])
def test_malformed_provider_envelope_is_safe(body):
    with pytest.raises(ProviderFailure, match="malformed_response"):
        asyncio.run(_call(lambda _: httpx.Response(200, content=body)))


def test_usage_unknowns_and_metadata_do_not_echo_secrets():
    payload = response_payload(id=KEY, model=KEY, system_fingerprint=KEY,
                               usage={"prompt_tokens": True, "completion_tokens": -1,
                                      "total_tokens": "27", "prompt_cache_hit_tokens": 2**64,
                                      "image_units": 1, "cost": 0, "secret": KEY})
    reply = asyncio.run(_call(lambda _: httpx.Response(200, json=payload)))
    assert reply.provider_request_id is None
    assert reply.usage["input_tokens"] is None
    assert reply.usage["output_tokens"] is None
    assert reply.usage["image_units"] is None
    assert reply.usage["provider_usage"] == {"total_tokens": None, "prompt_cache_hit_tokens": None,
                                            "prompt_cache_miss_tokens": None, "response_model": None,
                                            "system_fingerprint": None, "finish_reason": "stop"}
    assert KEY not in json.dumps(dataclasses.asdict(reply))


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def test_whitespace_keepalive_still_has_body_size_limit_and_closes_stream():
    stream = Chunks([b" " * (MAX_RESPONSE_BYTES // 2)] * 3)
    with pytest.raises(ProviderFailure, match="response_too_large"):
        asyncio.run(_call(lambda _: httpx.Response(200, stream=stream)))
    assert stream.closed


def test_total_deadline_includes_response_body_and_closes_stream():
    class SlowChunks(Chunks):
        async def __aiter__(self):
            while True:
                yield b" "
                await asyncio.sleep(0.01)

    stream = SlowChunks([])

    async def exercise():
        config = ModelConfig(connect_seconds=0.01, read_seconds=0.01, total_seconds=0.03)
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=stream))) as client:
            with pytest.raises(ProviderFailure, match="model_timeout"):
                await DeepSeekTransport(config, KEY, client).complete(INPUT, SCHEMA)

    asyncio.run(exercise())
    assert stream.closed


def test_cancellation_during_body_closes_connection_without_converting_error():
    class WaitingChunks(Chunks):
        async def __aiter__(self):
            ready.set()
            yield b" "
            await asyncio.sleep(10)

    async def exercise():
        nonlocal ready
        ready = asyncio.Event()
        stream = WaitingChunks([])
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=stream))) as client:
            transport = DeepSeekTransport(ModelConfig(), KEY, client)
            pending = asyncio.create_task(transport.complete(INPUT, SCHEMA))
            await ready.wait()
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert stream.closed

    ready = None
    asyncio.run(exercise())


@pytest.mark.parametrize("kwargs", [
    {"max_tokens": 0}, {"max_tokens": True}, {"max_tokens": 32769},
    {"read_seconds": float("nan")}, {"total_seconds": float("inf")},
    {"connect_seconds": -1.0}, {"connect_seconds": 601.0}, {"read_seconds": True},
    {"total_seconds": 20.0}, {"provider": "other"}, {"model_id": "foo\nbar"},
    {"api_key": KEY}, {"base_url": "https://user:password@api.deepseek.com"},
    {"base_url": "https://api.deepseek.com?api_key=secret"},
    {"base_url": "https://api.deepseek.com/#secret"},
    {"base_url": "https://api.deepseek.com/other"},
])
def test_nonsecret_config_is_strict_bounded_and_frozen(kwargs):
    with pytest.raises(ValidationError):
        ModelConfig(**kwargs)


def test_config_digest_is_stable_and_tracks_request_parameters():
    config = ModelConfig()
    assert config.config_sha256 == ModelConfig(connect_seconds=10.0).config_sha256
    assert config.config_sha256 != ModelConfig(max_tokens=2048).config_sha256
    assert config.config_sha256 != ModelConfig(price_version="test-price-v1").config_sha256
    assert len(config.config_sha256) == 64
    with pytest.raises(ValidationError):
        config.model_id = "another"


@pytest.mark.parametrize("url", ["http://api.deepseek.com", "https://evil.invalid", "https://api.deepseek.com.evil.invalid",
                                 "https://api.deepseek.com:444", "http://127.0.0.1:1234", "http://localhost:1234"])
def test_unapproved_endpoints_rejected(url):
    with pytest.raises(ValueError, match="provider URL"):
        DeepSeekTransport(ModelConfig(base_url=url), KEY)


def test_loopback_needs_explicit_opt_in_and_injected_client_remains_open():
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=response_payload()))) as client:
            transport = DeepSeekTransport(ModelConfig(base_url="http://127.0.0.1:1234"), KEY,
                                          client, allow_test_loopback=True)
            await transport.complete(INPUT, SCHEMA)
            await transport.aclose()
            assert not client.is_closed
    asyncio.run(exercise())


def test_owned_client_ignores_environment_and_closes(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://should-not-be-used.invalid:9999")
    async def exercise():
        transport = DeepSeekTransport(ModelConfig(), KEY)
        assert transport._client._trust_env is False
        assert transport._client.follow_redirects is False
        assert KEY not in repr(transport)
        await transport.aclose()
        assert transport._client.is_closed
    asyncio.run(exercise())


@pytest.mark.parametrize("key", ["", "contains space", "a\r\nb", "键", "x" * 8193])
def test_invalid_credentials_rejected_without_echo(key):
    with pytest.raises(ValueError, match="^invalid model credential$"):
        DeepSeekTransport(ModelConfig(), key)


@pytest.mark.parametrize(("data", "mime"), [
    (b"", "image/png"), (b"https://example.com/image.png", "image/png"),
    (b"<svg/>", "image/svg+xml"), (PNG, "image/jpeg"),
    (b"x" * (MAX_IMAGE_BYTES + 1), "image/png"),
])
def test_invalid_images_rejected(data, mime):
    with pytest.raises(ValueError):
        ModelImage("shot-1", data, mime)


@pytest.mark.parametrize(("data", "mime"), [
    (PNG, "image/png"), (b"\xff\xd8\xff\xe0", "image/jpeg"),
    (b"GIF89a", "image/gif"), (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
])
def test_supported_image_signatures(data, mime):
    image = ModelImage("shot-1", data, mime)
    assert image.mime_type == mime
    assert "data=" not in repr(image)


def test_image_selection_is_exact_and_bounded():
    image = ModelImage("shot-1", PNG, "image/png")
    with pytest.raises(ValueError):
        validate_images(INPUT, (image,))
    with pytest.raises(ValueError):
        validate_images({"image_evidence_ids": ["shot-1"]}, ())
    with pytest.raises(ValueError):
        validate_images({"image_evidence_ids": ["shot-1", "shot-1"]}, (image,))
    with pytest.raises(ValueError):
        validate_images({"image_evidence_ids": ["shot-1"]}, (image, image))
    with pytest.raises(ValueError):
        validate_images({"image_evidence_ids": ["shot-1"]}, (image,) * 5)
    with pytest.raises(ValueError):
        validate_images(INPUT, ("/private/image.png",))
    validate_images({"image_evidence_ids": ["shot-1"]}, (image,))


def test_repair_prompt_never_echoes_unknown_fields_values_or_exception_messages():
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response_payload())
    asyncio.run(_call(handler, repair_errors=[
        {"field": "reason", "reason": "string_type", "input": KEY, "msg": KEY},
        {"field": KEY, "reason": KEY}, {"field": "reason", "reason": {"unsafe": KEY}},
    ]))
    prompt = seen[0]["messages"][0]["content"]
    assert KEY not in prompt
    assert '"field":"reason","reason":"string_type"' in prompt
    assert '"field":"[unknown]","reason":"invalid_output"' in prompt


def test_large_input_is_rejected_before_http():
    called = []
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: called.append(request))) as client:
            transport = DeepSeekTransport(ModelConfig(), KEY, client)
            with pytest.raises(ValueError, match="request text limit"):
                await transport.complete({**INPUT, "text": "x" * (1024 * 1024)}, SCHEMA)
    asyncio.run(exercise())
    assert not called
