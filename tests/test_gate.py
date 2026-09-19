import pytest

from contracts.stage_state import StageState, default_stage_state
from stage_director.gate import (
    CALM_MAX_INTENSITY,
    GateIssue,
    brightness,
    indices_to_regenerate,
    run_gate,
    sanitize_state,
)
from stage_director.sequence import SequenceItem

SIGNATURE = "#9F77DD"
FALLBACK = default_stage_state(SIGNATURE)


def state(intensity: float = 300, color: str = SIGNATURE, on: tuple[bool, bool, bool] = (True, True, False)) -> StageState:
    s = default_stage_state(color)
    for spot, is_on in zip((s.spots.left, s.spots.center, s.spots.right), on):
        spot.on = is_on
        spot.intensity = intensity
    return s


def item(start: float, end: float, st: StageState, rationale: str = "이유") -> SequenceItem:
    return SequenceItem(section_label="x", start_sec=start, end_sec=end, transition_ms=500, state=st, rationale=rationale)


def rules(issues: list[GateIssue]) -> list[tuple[int, str]]:
    return [(i.idx, i.rule) for i in issues]


def test_brightness_is_the_highest_intensity_among_lit_spots():
    s = state(intensity=100)
    s.spots.center.intensity = 800
    s.spots.right.on = False
    s.spots.right.intensity = 1000  # 꺼진 스팟은 세지 않는다
    assert brightness(s) == 800


def test_brightness_is_zero_when_every_spot_is_off():
    assert brightness(state(on=(False, False, False))) == 0


def test_gate_passes_a_well_formed_sequence():
    items = [item(0, 30, state(200)), item(30, 90, state(800)), item(90, 120, state(200))]
    assert run_gate(items, [0.5, 1.8, 0.5], SIGNATURE) == []


def test_calm_section_brighter_than_the_cap_is_flagged():
    items = [item(0, 30, state(CALM_MAX_INTENSITY + 10))]
    assert rules(run_gate(items, [0.5], SIGNATURE)) == [(0, "calm_too_bright")]


def test_calm_section_exactly_at_the_cap_passes():
    assert run_gate([item(0, 30, state(CALM_MAX_INTENSITY))], [0.5], SIGNATURE) == []


def test_loud_section_may_exceed_the_calm_cap():
    assert run_gate([item(0, 30, state(900))], [1.8], SIGNATURE) == []


def test_energy_rising_while_brightness_falls_is_flagged_on_the_later_section():
    items = [item(0, 30, state(700)), item(30, 60, state(300))]
    assert (1, "energy_brightness_direction") in rules(run_gate(items, [1.0, 1.8], SIGNATURE))


def test_energy_falling_while_brightness_rises_is_flagged_on_the_later_section():
    items = [item(0, 30, state(300)), item(30, 60, state(700))]
    assert (1, "energy_brightness_direction") in rules(run_gate(items, [1.8, 1.0], SIGNATURE))


def test_small_energy_change_does_not_constrain_brightness_direction():
    items = [item(0, 30, state(700)), item(30, 60, state(300))]
    assert run_gate(items, [1.00, 1.10], SIGNATURE) == []


def test_color_deviation_needs_a_rationale():
    items = [item(0, 30, state(300, color="#FF0000"), rationale="   ")]
    assert rules(run_gate(items, [1.0], SIGNATURE)) == [(0, "color_deviation_without_rationale")]


def test_color_deviation_with_a_rationale_passes():
    items = [item(0, 30, state(300, color="#ff0000"), rationale="클라이맥스라 붉은 계열로 이탈")]
    assert run_gate(items, [1.0], SIGNATURE) == []


def test_signature_color_comparison_ignores_case():
    items = [item(0, 30, state(300, color=SIGNATURE.lower()), rationale="")]
    assert run_gate(items, [1.0], SIGNATURE) == []


def test_gate_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        run_gate([item(0, 30, state())], [1.0, 1.0], SIGNATURE)


def test_sanitize_state_clamps_and_reports_each_change_with_the_section_index():
    raw = {"color": SIGNATURE, "spots": {"left": {"intensity": 5000}}, "smoke": {"density": 2}}
    cleaned, issues = sanitize_state(raw, FALLBACK, idx=3)
    assert cleaned.spots.left.intensity == 1000
    assert cleaned.smoke.density == 1.0
    assert {(i.idx, i.rule) for i in issues} == {(3, "clamped")}
    assert len(issues) == 2


def test_sanitize_state_with_garbage_input_falls_back_without_issues():
    cleaned, issues = sanitize_state("nope", FALLBACK, idx=0)
    assert cleaned == FALLBACK
    assert issues == []


def test_indices_to_regenerate_skips_clamped_notes():
    issues = [GateIssue(0, "clamped", "x"), GateIssue(2, "calm_too_bright", "y"), GateIssue(2, "energy_brightness_direction", "z")]
    assert indices_to_regenerate(issues) == {2}
