import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from stage_director.audio import AudioError
from stage_director.graph import build_sequence_graph
from stage_director.graph_nodes import MAX_CONCURRENT_PROPOSALS
from stage_director.llm.client import LLMError
from stage_director.llm.fake import FakeLLM
from stage_director.models import Artist, SequenceRequest, Shader, Track
from stage_director.sequence import validate_sequence
from tests.graph_helpers import drive

ARTIST = Artist(
    slug="aurora", name="AURORA", name_ko="오로라", color="#9F77DD",
    shader=Shader(pattern="wave", freq=9, falloff=0.75, speed=0.5),
)
TRACK = Track(title="나만의 작은 우주", genre="K-pop", mood_keywords=["몽환"])
# max_concurrency 는 운영과 같은 config 로 호출 경로가 깨지지 않는지만 확인한다(동시성 자체는 자동 테스트 대상이 아님)
CONFIG = {"configurable": {"thread_id": "t1"}, "max_concurrency": MAX_CONCURRENT_PROPOSALS}


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


def request(duration: float, analysis: dict | None = None, audio_url: str | None = None) -> SequenceRequest:
    analysis = analysis or {"durationSec": duration, "bpm": 120, "energyCurve": [0.5] * int(duration)}
    return SequenceRequest(track=TRACK, artist=ARTIST, presets=[], analysis=analysis, duration_sec=duration, audio_url=audio_url)


def run(llm, duration: float = 40, analysis: dict | None = None, **graph_kwargs):
    """interrupt 를 전부 기본 답으로 통과시켜 끝까지 돌리고 최종 상태 값을 돌려준다."""
    graph = build_sequence_graph(llm, InMemorySaver(), **graph_kwargs)
    return drive(graph, {"request": request(duration, analysis)}, CONFIG)


def start(llm, duration: float = 40, analysis: dict | None = None, audio_url: str | None = None, **graph_kwargs):
    """첫 interrupt 까지만 돌린다. (graph, config, invoke 결과)"""
    graph = build_sequence_graph(llm, InMemorySaver(), **graph_kwargs)
    return graph, CONFIG, graph.invoke({"request": request(duration, analysis, audio_url)}, CONFIG)


TWO_SECTION_ANALYSIS = {"durationSec": 40, "bpm": 120, "energyCurve": [0.1] * 20 + [0.9] * 20}


# ── 정상 경로 ─────────────────────────────────────────────────


def test_short_song_produces_a_single_item_covering_the_whole_duration():
    result = run(FakeLLM(good_output()), duration=10, analysis={"durationSec": 10, "bpm": 100, "energyCurve": [0.5] * 10})
    assert len(result["final_items"]) == 1
    assert (result["final_items"][0].start_sec, result["final_items"][0].end_sec) == (0, 10)
    assert result["final_issues"] == []


def test_two_section_song_produces_a_valid_contiguous_sequence():
    # intro(에너지 비 0.2, calm)는 밝기 500 이하여야 calm_too_bright 를 피할 수 있음(스펙 §7, gate.py
    # CALM_MAX_INTENSITY). good_output() 기본값 800 은 calm 구간엔 너무 밝으므로 intro 에는 300 을 씀
    result = run(FakeLLM(good_output(300), good_output()), analysis=TWO_SECTION_ANALYSIS)
    items = result["final_items"]
    assert len(items) == 2
    assert validate_sequence(items, 40) == []
    assert result["final_issues"] == []
    assert result["sections"][0].label == "intro" and result["sections"][1].label == "outro"


# ── 방어: LLM 출력은 믿지 않는다(싱글 제안 계획과 동일 원칙, 곡 전체에도 적용) ──


def test_clamped_values_are_reported_with_section_idx_but_not_regenerated():
    # intensity 5000 은 clamp(최대 1000, contracts.stage_state.INTENSITY_RANGE)되어 '값 자체'는 더
    # 이상 문제가 아니므로 재생성 대상이 아님. 
    llm = FakeLLM(good_output(300), good_output(intensity=5000))
    result = run(llm, analysis=TWO_SECTION_ANALYSIS)
    clamped = [i for i in result["final_issues"] if i.rule == "clamped"]
    assert clamped and all(i.idx == 1 for i in clamped)
    assert result["regen_round"] == 1  # 재생성 없이 한 번에 끝난다
    assert len(llm.calls) == 2


def test_gate_issues_carry_their_section_idx():
    # 초기 + 재생성 2회 모두 같은 방식으로 어긋나 give-up 라운드에서도 calm_too_bright(idx0)와
    # energy_brightness_direction(idx1)이 그대로 남음 — run_gate 가 낸 idx 가 그대로 전달돼야 함
    bad, other = good_output(intensity=900), good_output(intensity=800)
    llm = FakeLLM(bad, other, bad, other, bad, other)
    result = run(llm, analysis=TWO_SECTION_ANALYSIS)
    by_rule = {i.rule: i for i in result["final_issues"]}
    assert by_rule["calm_too_bright"].idx == 0
    assert by_rule["energy_brightness_direction"].idx == 1


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
    # idx0(intro)의 state 가 객체가 아니면 sanitize_state 가 fallback + invalid_state 이슈를 남김
    # fallback 자체는 gate 규칙을 통과하므로 run_gate 는 아무 것도 잡지 못함 
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


# ── interrupt #1: 구간 확인·수정 ─────────────────────────────


def test_pauses_at_confirm_sections_before_any_proposal():
    llm = FakeLLM()
    _, _, result = start(llm, analysis=TWO_SECTION_ANALYSIS)
    value = result["__interrupt__"][0].value
    assert value["kind"] == "confirm_sections"
    assert value["interruptId"] == "t1:0:confirm_sections"
    assert [s["label"] for s in value["sections"]] == ["intro", "outro"]
    assert value["durationSec"] == 40 and len(value["energyCurve"]) == 40
    assert llm.calls == []  # 사람이 확인하기 전에는 LLM 을 부르지 않는다


def test_resume_with_edited_sections_proposes_for_the_edited_boundaries():
    llm = FakeLLM(good_output(300), good_output(300), good_output(300))
    graph, config, _ = start(llm, analysis=TWO_SECTION_ANALYSIS)
    edited = [
        {"label": "intro", "startSec": 0, "endSec": 10},
        {"label": "verse", "startSec": 10, "endSec": 20},
        {"label": "outro", "startSec": 20, "endSec": 40},
    ]
    graph.invoke(Command(resume={"sections": edited}), config)
    items = graph.get_state(config).values["final_items"]
    assert [(i.section_label, i.start_sec, i.end_sec) for i in items] == [("intro", 0, 10), ("verse", 10, 20), ("outro", 20, 40)]
    assert validate_sequence(items, 40) == []


def test_mood_is_interpreted_before_the_pause():
    llm = FakeLLM({"moods": ["잔잔", "폭발적"]})
    _, _, result = start(
        llm, analysis=TWO_SECTION_ANALYSIS, audio_url="https://x/a.mp3", fetch=lambda url: (b"audio", "audio/mpeg")
    )
    assert [s["mood"] for s in result["__interrupt__"][0].value["sections"]] == ["잔잔", "폭발적"]
    assert llm.calls[0]["audio_bytes"] == 5


def test_mood_failure_does_not_block_the_run():
    def broken(url):
        raise AudioError("down")

    llm = FakeLLM()
    _, _, result = start(llm, analysis=TWO_SECTION_ANALYSIS, audio_url="https://x/a.mp3", fetch=broken)
    assert [s["mood"] for s in result["__interrupt__"][0].value["sections"]] == ["", ""]
    assert llm.calls == []
