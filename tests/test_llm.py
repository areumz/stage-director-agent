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
