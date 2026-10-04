import pytest

from stage_director.llm.client import LLMError
from stage_director.llm.fake import FakeLLM
from stage_director.models import Section, Track
from stage_director.mood import MOOD_OUTPUT_MAX, interpret_moods

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


def test_llm_failure_is_not_fatal():
    llm = FakeLLM(LLMError("down"))
    assert interpret_moods(llm, b"audio", "audio/mpeg", SECTIONS, TRACK) == ["", ""]
