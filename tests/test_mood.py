import logging

import pytest

from stage_director import mood
from stage_director.llm.client import LLMError
from stage_director.llm.fake import FakeLLM
from stage_director.models import Section, Track
from stage_director.mood import (
    MOOD_MAX_RETRIES,
    MOOD_OUTPUT_MAX,
    MOOD_RETRY_DELAY_SEC,
    interpret_moods,
)

SECTIONS = [Section(label="intro", start_sec=0, end_sec=20), Section(label="outro", start_sec=20, end_sec=40)]
TRACK = Track(title="나만의 작은 우주", genre="K-pop", mood_keywords=["몽환"])


def interpret(response):
    llm = FakeLLM(response)
    return interpret_moods(llm, b"audio", "audio/mpeg", SECTIONS, TRACK), llm


def test_returns_one_mood_per_section_in_order_with_one_audio_call():
    moods, llm = interpret({"moods": ["잔잔하고 몽환적", "벅찬 클라이맥스"]})
    assert moods == ["잔잔하고 몽환적", "벅찬 클라이맥스"]
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["audio_bytes"] == 5 and call["mime_type"] == "audio/mpeg"
    assert "0.0초 ~ 20.0초" in call["user"] and "20.0초 ~ 40.0초" in call["user"]
    assert "나만의 작은 우주" in call["user"]


@pytest.mark.parametrize(
    "moods, expected",
    [(["a"], ["a", ""]), (["a", "b", "c"], ["a", "b"])],
)
def test_count_mismatch_is_padded_or_truncated(moods, expected):
    assert interpret({"moods": moods})[0] == expected


def test_non_string_and_oversized_entries_are_cleaned():
    moods, _ = interpret({"moods": [123, "x" * 300]})
    assert moods == ["", "x" * MOOD_OUTPUT_MAX]


@pytest.mark.parametrize("garbage", ["text", {"x": 1}, {"moods": "a"}])
def test_garbage_response_yields_empty_moods(garbage):
    assert interpret(garbage)[0] == ["", ""]


@pytest.fixture
def sleeps(monkeypatch):
    """재시도 사이 대기를 실제로 하지 않고 기록만."""
    recorded = []
    monkeypatch.setattr(mood.time, "sleep", recorded.append)
    return recorded


def test_llm_failure_is_not_fatal_after_retries(sleeps):
    llm = FakeLLM(*[LLMError("down")] * (MOOD_MAX_RETRIES + 1))
    assert interpret_moods(llm, b"audio", "audio/mpeg", SECTIONS, TRACK) == ["", ""]
    assert len(llm.calls) == MOOD_MAX_RETRIES + 1
    assert sleeps == [MOOD_RETRY_DELAY_SEC * (i + 1) for i in range(MOOD_MAX_RETRIES)]  # 점점 늘려 가며 기다린다


def test_transient_failure_is_retried_and_then_succeeds(sleeps):
    llm = FakeLLM(LLMError("503 UNAVAILABLE"), {"moods": ["잔잔", "벅참"]})
    assert interpret_moods(llm, b"audio", "audio/mpeg", SECTIONS, TRACK) == ["잔잔", "벅참"]
    assert len(llm.calls) == 2
    assert sleeps == [MOOD_RETRY_DELAY_SEC]


def test_failure_reason_is_logged(sleeps, caplog):
    llm = FakeLLM(*[LLMError("503 UNAVAILABLE")] * (MOOD_MAX_RETRIES + 1))
    with caplog.at_level(logging.WARNING, logger="stage_director.mood"):
        interpret_moods(llm, b"audio", "audio/mpeg", SECTIONS, TRACK)
    assert "503 UNAVAILABLE" in caplog.text
