import json
import math
from pathlib import Path

import pytest

from contracts.stage_state import (
    ClampNote,
    StageState,
    clamp_stage_state,
    default_stage_state,
    merge_stage_state,
)

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[2] / "contracts" / "fixtures" / "merge_stage_state_cases.json").read_text()
)
FALLBACK = StageState.model_validate(FIXTURE["fallback"])


def test_default_stage_state_matches_on_stage():
    # on-stage stageState.test.ts 의 defaultStageState 케이스와 같다
    assert default_stage_state("#D4537E").model_dump() == {
        "color": "#D4537E",
        "spots": {
            "left": {"on": True, "intensity": 300, "angle": 0.45, "penumbra": 0.6},
            "center": {"on": True, "intensity": 300, "angle": 0.45, "penumbra": 0.6},
            "right": {"on": False, "intensity": 300, "angle": 0.45, "penumbra": 0.6},
        },
        "camera": "front",
        "smoke": {"density": 0, "color": "#ffffff"},
    }


def test_fixture_fallback_is_the_default_state():
    assert FALLBACK == default_stage_state("#9F77DD")


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"][:60])
def test_merge_stage_state_matches_golden_vectors(case):
    expected = FALLBACK.model_dump() if case["expected"] == "FALLBACK" else case["expected"]
    assert merge_stage_state(case["input"], FALLBACK).model_dump() == expected


def test_merge_does_not_return_the_fallback_object_itself():
    # 파이썬은 가변 객체라 호출자가 결과를 고쳐도 fallback 이 오염되면 안 된다
    result = merge_stage_state(None, FALLBACK)
    result.spots.left.on = False
    assert FALLBACK.spots.left.on is True


def test_merge_ignores_ints_too_large_for_a_float():
    # 파이썬 int 는 크기 제한이 없다. float 로 못 바꾸는 값은 타입이 틀린 값처럼 fallback 으로 떨어져야 한다.
    # 예외를 던지지 않고 나머지 필드는 유지한다 (on-stage 와 다른 점: TS 에서는 Infinity 가 된다)
    huge = 10**400
    result = merge_stage_state({"color": "#111111", "spots": {"left": {"intensity": huge}}, "smoke": {"density": huge}}, FALLBACK)
    assert result.color == "#111111"
    assert result.spots.left.intensity == FALLBACK.spots.left.intensity
    assert result.smoke.density == FALLBACK.smoke.density


def test_clamp_leaves_valid_state_untouched():
    state = default_stage_state("#9F77DD")
    clamped, notes = clamp_stage_state(state, FALLBACK)
    assert clamped == state
    assert notes == []


def test_clamp_pulls_out_of_range_numbers_into_range_and_records_notes():
    state = merge_stage_state(
        {
            "spots": {
                "left": {"intensity": 5000, "angle": -2, "penumbra": 9},
                "center": {"intensity": -10},
            },
            "smoke": {"density": 7},
        },
        FALLBACK,
    )
    clamped, notes = clamp_stage_state(state, FALLBACK)
    assert clamped.spots.left.intensity == 1000
    assert clamped.spots.left.angle == 0.1
    assert clamped.spots.left.penumbra == 1.0
    assert clamped.spots.center.intensity == 0
    assert clamped.smoke.density == 1.0
    assert sorted(notes) == sorted(
        [
            ClampNote("spots.left.intensity", 5000, 1000.0),
            ClampNote("spots.left.angle", -2, 0.1),
            ClampNote("spots.left.penumbra", 9, 1.0),
            ClampNote("spots.center.intensity", -10, 0.0),
            ClampNote("smoke.density", 7, 1.0),
        ]
    )


def test_clamp_replaces_non_hex_colors_with_fallback_colors():
    state = merge_stage_state({"color": "not-a-color", "smoke": {"color": "#12345"}}, FALLBACK)
    clamped, notes = clamp_stage_state(state, FALLBACK)
    assert clamped.color == "#9F77DD"
    assert clamped.smoke.color == "#ffffff"
    assert {n.path for n in notes} == {"color", "smoke.color"}


def test_clamp_accepts_lowercase_and_uppercase_hex():
    state = merge_stage_state({"color": "#abcdef", "smoke": {"color": "#ABCDEF"}}, FALLBACK)
    clamped, notes = clamp_stage_state(state, FALLBACK)
    assert (clamped.color, clamped.smoke.color) == ("#abcdef", "#ABCDEF")
    assert notes == []


def test_clamp_replaces_non_finite_numbers_with_fallback_values():
    state = default_stage_state("#9F77DD")
    state.spots.left.intensity = math.nan
    state.smoke.density = math.inf
    clamped, notes = clamp_stage_state(state, FALLBACK)
    assert clamped.spots.left.intensity == 300
    assert clamped.smoke.density == 0
    assert {n.path for n in notes} == {"spots.left.intensity", "smoke.density"}
