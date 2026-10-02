import pytest

from stage_director.graph import build_sequence_graph
from stage_director.graph_nodes import MAX_CONCURRENT_PROPOSALS
from stage_director.llm.client import LLMError
from stage_director.llm.fake import FakeLLM
from stage_director.models import Artist, SequenceRequest, Shader, Track
from stage_director.sequence import validate_sequence

ARTIST = Artist(
    slug="aurora", name="AURORA", name_ko="오로라", color="#9F77DD",
    shader=Shader(pattern="wave", freq=9, falloff=0.75, speed=0.5),
)
TRACK = Track(title="나만의 작은 우주", genre="K-pop", mood_keywords=["몽환"])


def good_output(intensity: int = 800) -> dict:
    spot = {"on": True, "intensity": intensity, "angle": 0.5}
    return {
        "state": {
            "color": "#9F77DD",
            "spots": {"left": spot, "center": spot, "right": spot},
            "camera": "front",
            "smoke": {"density": 0.3, "color": "#ffffff"},
        },
        "rationale": "측정값에 맞춘 연출",
    }


def request(duration: float, analysis: dict | None = None) -> SequenceRequest:
    analysis = analysis or {"durationSec": duration, "bpm": 120, "energyCurve": [0.5] * int(duration)}
    return SequenceRequest(track=TRACK, artist=ARTIST, presets=[], analysis=analysis, duration_sec=duration)


def run(llm, duration: float = 40, analysis: dict | None = None):
    graph = build_sequence_graph(llm)
    # max_concurrency 는 FakeLLM 의 순서 보장 동작과는 무관하다(동시성 자체는 자동 테스트로 확인하지
    # 않는다 — Task 6 사람 단계에서 실제 Gemini 호출로 429 가 뜨는지 본다). 여기서는 운영과 같은
    # config 로 호출 경로가 깨지지 않는지만 확인한다.
    config = {"configurable": {"thread_id": "t1"}, "max_concurrency": MAX_CONCURRENT_PROPOSALS}
    return graph.invoke({"request": request(duration, analysis)}, config=config)


TWO_SECTION_ANALYSIS = {"durationSec": 40, "bpm": 120, "energyCurve": [0.1] * 20 + [0.9] * 20}


# ── 정상 경로 ─────────────────────────────────────────────────


def test_short_song_produces_a_single_item_covering_the_whole_duration():
    result = run(FakeLLM(good_output()), duration=10, analysis={"durationSec": 10, "bpm": 100, "energyCurve": [0.5] * 10})
    assert len(result["final_items"]) == 1
    assert (result["final_items"][0].start_sec, result["final_items"][0].end_sec) == (0, 10)
    assert result["final_issues"] == []


def test_two_section_song_produces_a_valid_contiguous_sequence():
    # intro(에너지 비 0.2, calm)는 밝기 500 이하여야 calm_too_bright 를 피한다(스펙 §7, gate.py
    # CALM_MAX_INTENSITY). good_output() 기본값 800 은 calm 구간엔 너무 밝으므로 intro 에는 300 을 쓴다.
    result = run(FakeLLM(good_output(300), good_output()), analysis=TWO_SECTION_ANALYSIS)
    items = result["final_items"]
    assert len(items) == 2
    assert validate_sequence(items, 40) == []
    assert result["final_issues"] == []
    assert result["sections"][0].label == "intro" and result["sections"][1].label == "outro"


# ── 방어: LLM 출력은 믿지 않는다(싱글 제안 계획과 동일 원칙, 곡 전체에도 적용) ──


def test_clamped_values_are_reported_but_not_regenerated():
    # intensity 5000 은 clamp(최대 1000, contracts.stage_state.INTENSITY_RANGE)되어 '값 자체'는 더
    # 이상 문제가 아니므로 재생성 대상이 아니다. outro(에너지 비 1.8, calm 아님)에 줘서 clamp 상한
    # (1000)이 calm_too_bright(밝기 <= 500) 규칙과 또 얽히지 않게 한다 — intro 는 calm-safe 값(300).
    llm = FakeLLM(good_output(300), good_output(intensity=5000))
    result = run(llm, analysis=TWO_SECTION_ANALYSIS)
    assert any(i.rule == "clamped" for i in result["final_issues"])
    assert result["regen_round"] == 1  # 재생성 없이 한 번에 끝난다
    assert len(llm.calls) == 2


# ── 자동 재생성 ──────────────────────────────────────────────


def test_a_gate_violation_triggers_exactly_one_regeneration_round():
    # intro(에너지 비 낮음)를 너무 밝게 내면 calm_too_bright 와, outro 와의 방향 불일치도 함께 걸린다.
    llm = FakeLLM(
        good_output(intensity=900),  # idx0(intro): 너무 밝다
        good_output(intensity=800),  # idx1(outro): idx0 이 비정상이라 방향 규칙도 걸린다
        good_output(intensity=300),  # idx0 재생성: calm 기준 충족
        good_output(intensity=800),  # idx1 재생성: 방향이 다시 맞는다
    )
    result = run(llm, analysis=TWO_SECTION_ANALYSIS)
    assert result["final_issues"] == []
    assert result["regen_round"] == 2
    assert len(llm.calls) == 4


def test_regen_gives_up_after_max_rounds_but_still_returns_a_usable_sequence():
    bad, other = good_output(intensity=900), good_output(intensity=800)
    llm = FakeLLM(bad, other, bad, other, bad, other)  # 초기 + 재생성 2회, 매번 그대로 어긋난다
    result = run(llm, analysis=TWO_SECTION_ANALYSIS)
    assert len(result["final_items"]) == 2
    assert "calm_too_bright" in {i.rule for i in result["final_issues"]}
    assert result["regen_round"] == 3  # 최초 1회 + 재생성 2회
    assert len(llm.calls) == 6


# ── LLM 실패 ──────────────────────────────────────────────────


def test_invalid_state_triggers_regeneration():
    # idx0(intro)의 state 가 객체가 아니면 sanitize_state 가 fallback + invalid_state 이슈를 남긴다.
    # fallback 자체는 gate 규칙을 통과하므로 run_gate 는 아무 것도 잡지 못한다 — invalid_state 는
    # gate_issues 가 아니라 proposal.issues 로만 존재하므로, assemble 이 이를 직접 regen_targets 에
    # 반영하지 않으면 재생성 없이 fallback 상태가 그대로 최종 결과가 된다.
    bad_state_response = {"state": "nope", "rationale": "모델 출력이 깨졌다"}
    llm = FakeLLM(bad_state_response, good_output(), good_output(300))
    result = run(llm, analysis=TWO_SECTION_ANALYSIS)
    assert len(llm.calls) == 3  # idx0 가 재생성됐다 (초기 2 + 재생성 1)
    assert result["regen_round"] == 2
    assert result["final_issues"] == []


def test_llm_error_propagates_out_of_the_graph():
    llm = FakeLLM(LLMError("a"), LLMError("b"), LLMError("c"))
    with pytest.raises(LLMError):
        run(llm, duration=10, analysis={"durationSec": 10, "bpm": 100, "energyCurve": [0.5] * 10})
