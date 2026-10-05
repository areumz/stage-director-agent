import json

import pytest

from contracts.stage_state import default_stage_state
from stage_director.llm.client import LLMError
from stage_director.llm.fake import FakeLLM
from stage_director.models import ProposeRequest
from stage_director.prompts import PROPOSAL_SCHEMA
from stage_director.propose import propose_section
from stage_director.sequence import SequenceItem
from tests.conftest import PROPOSE_REQUEST as FIXTURE


def request(start=10, end=30, label="chorus", **patch) -> ProposeRequest:
    """픽스처: 0~10초 잔잔(에너지 비 0.31), 10~30초 큰 소리(1.54), 30~40초 중간(0.62). 시그니처 컬러 #9F77DD."""
    return ProposeRequest.model_validate({**FIXTURE, "section": {"label": label, "startSec": start, "endSec": end}, **patch})


def llm_output(*, color="#9F77DD", intensity=800, rationale="코러스라 밝게", **extra) -> dict:
    spot = {"on": True, "intensity": intensity, "angle": 0.5}
    return {
        "state": {
            "color": color,
            "spots": {"left": spot, "center": spot, "right": spot},
            "camera": "front",
            "smoke": {"density": 0.4, "color": "#ffffff"},
            **extra,
        },
        "rationale": rationale,
    }


def rules(proposal) -> list[str]:
    return [i.rule for i in proposal.issues]


# ── 정상 경로 ─────────────────────────────────────────────────


def test_builds_item_from_section_and_llm_output():
    proposal = propose_section(FakeLLM(llm_output()), request())
    item = proposal.item
    assert (item.section_label, item.start_sec, item.end_sec) == ("chorus", 10, 30)
    assert item.rationale == "코러스라 밝게"
    assert item.state.spots.left.intensity == 800
    assert item.state.smoke.density == 0.4
    assert proposal.energy_ratio == pytest.approx(1.538, abs=1e-3)
    assert proposal.issues == []


def test_transition_is_two_seconds_but_never_longer_than_the_section():
    assert propose_section(FakeLLM(llm_output()), request()).item.transition_ms == 2000
    assert propose_section(FakeLLM(llm_output()), request(start=10, end=11.5)).item.transition_ms == 1500


def test_penumbra_is_never_changed_by_the_agent():
    proposal = propose_section(FakeLLM(llm_output(spots={"left": {"on": True, "intensity": 800, "angle": 0.5, "penumbra": 9}})), request())
    assert proposal.item.state.spots.left.penumbra == default_stage_state("#9F77DD").spots.left.penumbra
    assert proposal.issues == []  # 무시한 값이라 clamp 이슈도 남기지 않는다


def test_missing_analysis_falls_back_to_average_energy():
    proposal = propose_section(FakeLLM(llm_output()), request(analysis=None))
    assert proposal.energy_ratio == 1.0


# ── 방어: LLM 출력은 믿지 않는다 ──────────────────────────────


def test_out_of_range_values_are_clamped_and_reported():
    proposal = propose_section(FakeLLM(llm_output(intensity=5000)), request())
    assert proposal.item.state.spots.left.intensity == 1000
    assert rules(proposal) == ["clamped"] * 3


def test_bad_hex_falls_back_to_signature_color():
    proposal = propose_section(FakeLLM(llm_output(color="purple")), request())
    assert proposal.item.state.color == "#9F77DD"
    assert "clamped" in rules(proposal)


@pytest.mark.parametrize("raw", [["not", "an", "object"], {"rationale": "state 가 없다"}, {"state": "oops", "rationale": "x"}])
def test_unusable_state_becomes_default_and_is_reported(raw):
    proposal = propose_section(FakeLLM(raw), request())
    assert proposal.item.state == default_stage_state("#9F77DD")
    assert "invalid_state" in rules(proposal)


def test_non_string_rationale_becomes_empty():
    assert propose_section(FakeLLM(llm_output(rationale=42)), request()).item.rationale == ""


# ── 게이트 규칙 (단일 구간에 적용되는 것) ─────────────────────


def test_calm_section_that_is_too_bright_is_reported():
    proposal = propose_section(FakeLLM(llm_output(intensity=900)), request(start=0, end=10, label="intro"))
    assert proposal.energy_ratio == pytest.approx(0.308, abs=1e-3)
    assert rules(proposal) == ["calm_too_bright"]


def test_calm_section_within_limit_is_clean():
    proposal = propose_section(FakeLLM(llm_output(intensity=400)), request(start=0, end=10, label="intro"))
    assert proposal.issues == []


def test_color_deviation_needs_a_rationale():
    without = propose_section(FakeLLM(llm_output(color="#FF0000", rationale="  ")), request())
    assert rules(without) == ["color_deviation_without_rationale"]
    with_reason = propose_section(FakeLLM(llm_output(color="#FF0000", rationale="클라이맥스라 강한 붉은색")), request())
    assert with_reason.issues == []


# ── 재시도 ────────────────────────────────────────────────────


def test_retries_llm_errors_twice_then_succeeds():
    llm = FakeLLM(LLMError("1"), LLMError("2"), llm_output())
    assert propose_section(llm, request()).item.rationale == "코러스라 밝게"
    assert len(llm.calls) == 3


def test_gives_up_after_two_retries():
    llm = FakeLLM(LLMError("1"), LLMError("2"), LLMError("3"))
    with pytest.raises(LLMError, match="3"):
        propose_section(llm, request())
    assert len(llm.calls) == 3


# ── LLM 에 보내는 것 ──────────────────────────────────────────


def sent_prompts(**kwargs) -> tuple[str, str, dict]:
    llm = FakeLLM(llm_output())
    propose_section(llm, request(**kwargs))
    call = llm.calls[0]
    return call["system"], call["user"], call["schema"]


def test_user_prompt_carries_measurements_and_context():
    _, user, _ = sent_prompts()
    for fact in ["나만의 작은 우주", "몽환", "#9F77DD", "AURORA", "wave", "chorus", "10.0", "30.0", "120", "1.54", "1.45", "잔잔한 인트로"]:
        assert fact in user


def test_system_prompt_states_the_gate_thresholds():
    system, _, _ = sent_prompts()
    assert "0.9" in system and "500" in system


def test_schema_omits_penumbra_and_lists_cameras():
    _, _, schema = sent_prompts()
    assert schema is PROPOSAL_SCHEMA
    assert "penumbra" not in json.dumps(schema)
    assert schema["properties"]["state"]["properties"]["camera"]["enum"] == ["front", "audience", "top"]


def test_presets_are_sanitized_before_reaching_the_prompt():
    preset = {"name": "망가진 프리셋", "state": {"spots": {"left": {"on": True, "intensity": 99999}}}}
    _, user, _ = sent_prompts(presets=[preset])
    assert "망가진 프리셋" in user
    assert "99999" not in user


def test_only_the_first_ten_presets_are_sent():
    presets = [{"name": f"프리셋{i:02d}", "state": {}} for i in range(12)]
    _, user, _ = sent_prompts(presets=presets)
    assert "프리셋09" in user and "프리셋10" not in user


# ── 4단계: 무드, 피드백 ───────────────────────────────────────


def section_request(**extra) -> ProposeRequest:
    return ProposeRequest.model_validate(
        {**FIXTURE, "section": {"label": "chorus", "startSec": 10, "endSec": 30, **extra.pop("section", {})}, **extra}
    )


def previous_item() -> SequenceItem:
    return SequenceItem(
        section_label="chorus", start_sec=10, end_sec=30, transition_ms=2000,
        state=default_stage_state("#9F77DD"), rationale="원본 근거",
    )


def test_section_mood_is_in_the_prompt_when_present():
    llm = FakeLLM(llm_output())
    propose_section(llm, section_request(section={"mood": "몽환적"}))
    assert "분위기: 몽환적" in llm.calls[0]["user"]


def test_prompt_has_no_mood_line_when_mood_is_empty():
    llm = FakeLLM(llm_output())
    propose_section(llm, section_request())
    assert "분위기:" not in llm.calls[0]["user"]


def test_feedback_block_carries_the_previous_proposal_and_the_request():
    llm = FakeLLM(llm_output())
    propose_section(llm, section_request(feedback="더 어둡게", previous=previous_item()))
    user = llm.calls[0]["user"]
    assert "## 사용자 피드백" in user and "피드백: 더 어둡게" in user and "원본 근거" in user


def test_prompt_has_no_feedback_block_by_default():
    llm = FakeLLM(llm_output())
    propose_section(llm, section_request())
    assert "사용자 피드백" not in llm.calls[0]["user"]


def test_system_prompt_marks_mood_and_feedback_as_data_not_instructions():
    llm = FakeLLM(llm_output())
    propose_section(llm, section_request())
    system = llm.calls[0]["system"]
    assert "분위기" in system and "사용자 피드백" in system and "지시가 아니다" in system
