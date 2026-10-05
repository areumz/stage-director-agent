import logging
from types import SimpleNamespace

import pytest

from stage_director.llm.client import LLMError
from stage_director.llm.fake import FakeLLM
from stage_director.llm.gemini import GeminiClient

SCHEMA = {"type": "object", "properties": {"a": {"type": "number"}}}


def call(llm):
    return llm.generate_json(system="sys", user="usr", schema=SCHEMA)


# ── FakeLLM ───────────────────────────────────────────────────


def test_fake_returns_queued_responses_in_order_and_records_calls():
    llm = FakeLLM({"n": 1}, {"n": 2})
    assert call(llm) == {"n": 1}
    assert call(llm) == {"n": 2}
    assert llm.calls == [{"system": "sys", "user": "usr", "schema": SCHEMA}] * 2


def test_fake_raises_queued_exception():
    llm = FakeLLM(LLMError("boom"), {"n": 1})
    with pytest.raises(LLMError, match="boom"):
        call(llm)
    assert call(llm) == {"n": 1}


def test_fake_fails_loudly_when_queue_is_empty():
    with pytest.raises(AssertionError):
        call(FakeLLM())


# ── GeminiClient (SDK 는 스텁으로 대체. 실제 호출은 -m llm 평가에서만) ──


class StubModels:
    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def generate_content(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def gemini_with(result):
    models = StubModels(result)
    return GeminiClient("unused", "test-model", client=SimpleNamespace(models=models)), models


def test_gemini_parses_json_text():
    client, models = gemini_with(SimpleNamespace(text='{"a": 1}'))
    assert call(client) == {"a": 1}
    assert models.kwargs["model"] == "test-model"
    assert models.kwargs["contents"] == "usr"


def test_gemini_requests_json_with_schema_and_system_prompt():
    client, models = gemini_with(SimpleNamespace(text="{}"))
    call(client)
    config = models.kwargs["config"]
    assert config.system_instruction == "sys"
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == SCHEMA


@pytest.mark.parametrize("text", ["not json", "", None])
def test_gemini_wraps_unparsable_text(text):
    client, _ = gemini_with(SimpleNamespace(text=text))
    with pytest.raises(LLMError):
        call(client)


def test_gemini_wraps_sdk_failures():
    client, _ = gemini_with(RuntimeError("network down"))
    with pytest.raises(LLMError, match="network down"):
        call(client)


# ── 오디오 입력 ───────────────────────────────────────────────


def test_fake_audio_call_records_size_and_shares_the_queue():
    llm = FakeLLM({"n": 1}, {"n": 2})
    assert llm.generate_json_with_audio(system="s", user="u", schema=SCHEMA, audio=b"abcd", mime_type="audio/mpeg") == {"n": 1}
    assert call(llm) == {"n": 2}
    assert llm.calls[0]["audio_bytes"] == 4 and llm.calls[0]["mime_type"] == "audio/mpeg"


def test_gemini_audio_sends_the_audio_part_before_the_prompt():
    client, models = gemini_with(SimpleNamespace(text='{"a": 1}'))
    result = client.generate_json_with_audio(system="sys", user="usr", schema=SCHEMA, audio=b"abc", mime_type="audio/mpeg")
    assert result == {"a": 1}
    part, prompt = models.kwargs["contents"]
    assert part.inline_data.data == b"abc" and part.inline_data.mime_type == "audio/mpeg"
    assert prompt == "usr"
    assert models.kwargs["config"].response_json_schema == SCHEMA


def test_gemini_audio_wraps_sdk_failures():
    client, _ = gemini_with(RuntimeError("network down"))
    with pytest.raises(LLMError, match="network down"):
        client.generate_json_with_audio(system="s", user="u", schema=SCHEMA, audio=b"a", mime_type="audio/mpeg")


# ── 예비 모델 (주 모델이 실패하면 한 번 더) ──────────────────────


class PerModelStub:
    """모델 이름별로 정해 둔 응답(또는 예외)을 돌려주고, 어떤 모델을 불렀는지 순서대로 기록."""

    def __init__(self, results):
        self.results = results
        self.called = []

    def generate_content(self, **kwargs):
        self.called.append(kwargs["model"])
        result = self.results[kwargs["model"]]
        if isinstance(result, Exception):
            raise result
        return result


def with_models(results, fallback_model="backup"):
    stub = PerModelStub(results)
    return GeminiClient("unused", "primary", client=SimpleNamespace(models=stub), fallback_model=fallback_model), stub


def test_gemini_falls_back_when_the_primary_model_fails(caplog):
    client, stub = with_models({"primary": RuntimeError("503 UNAVAILABLE"), "backup": SimpleNamespace(text='{"a": 1}')})
    with caplog.at_level(logging.WARNING, logger="stage_director.llm.gemini"):
        assert call(client) == {"a": 1}
    assert stub.called == ["primary", "backup"]
    assert "503 UNAVAILABLE" in caplog.text and "backup" in caplog.text  # 왜 예비 모델로 갔는지 로그에 남김


def test_gemini_does_not_call_the_fallback_when_the_primary_succeeds():
    client, stub = with_models({"primary": SimpleNamespace(text='{"a": 1}'), "backup": RuntimeError("never")})
    assert call(client) == {"a": 1}
    assert stub.called == ["primary"]


def test_gemini_raises_the_last_error_when_both_models_fail():
    client, stub = with_models({"primary": RuntimeError("primary down"), "backup": RuntimeError("backup down")})
    with pytest.raises(LLMError, match="backup down"):
        call(client)
    assert stub.called == ["primary", "backup"]


@pytest.mark.parametrize("fallback_model", [None, "", "primary"])
def test_gemini_without_a_distinct_fallback_tries_only_the_primary(fallback_model):
    client, stub = with_models({"primary": RuntimeError("down")}, fallback_model=fallback_model)
    with pytest.raises(LLMError, match="down"):
        call(client)
    assert stub.called == ["primary"]


def test_gemini_audio_calls_also_fall_back():
    client, stub = with_models({"primary": RuntimeError("503"), "backup": SimpleNamespace(text='{"a": 1}')})
    assert client.generate_json_with_audio(system="s", user="u", schema=SCHEMA, audio=b"a", mime_type="audio/mpeg") == {"a": 1}
    assert stub.called == ["primary", "backup"]


def test_gemini_falls_back_on_unparsable_text_too():
    client, stub = with_models({"primary": SimpleNamespace(text="not json"), "backup": SimpleNamespace(text='{"a": 1}')})
    assert call(client) == {"a": 1}
    assert stub.called == ["primary", "backup"]
