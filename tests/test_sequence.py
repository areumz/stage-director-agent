import json

from contracts.stage_state import default_stage_state
from stage_director.sequence import SequenceItem, validate_sequence


def item(start: float, end: float, transition_ms: int = 1000, label: str = "verse") -> SequenceItem:
    return SequenceItem(
        section_label=label,
        start_sec=start,
        end_sec=end,
        transition_ms=transition_ms,
        state=default_stage_state("#9F77DD"),
        rationale="테스트용",
    )


def codes(items: list[SequenceItem], duration: float) -> list[str]:
    return [v.code for v in validate_sequence(items, duration)]


def test_valid_sequence_has_no_violations():
    assert validate_sequence([item(0, 40), item(40, 100), item(100, 180)], 180) == []


def test_empty_sequence_is_a_violation_of_the_whole_track():
    result = validate_sequence([], 180)
    assert [(v.idx, v.code) for v in result] == [(None, "empty")]


def test_first_item_must_start_at_zero():
    assert codes([item(2, 100), item(100, 180)], 180) == ["start_not_zero"]


def test_last_item_must_end_at_track_duration():
    assert codes([item(0, 100), item(100, 170)], 180) == ["end_not_duration"]


def test_gap_between_items_is_reported_on_the_later_item():
    result = validate_sequence([item(0, 40), item(45, 180)], 180)
    assert [(v.idx, v.code) for v in result] == [(1, "gap_or_overlap")]


def test_overlap_between_items_is_reported_on_the_later_item():
    result = validate_sequence([item(0, 50), item(40, 180)], 180)
    assert [(v.idx, v.code) for v in result] == [(1, "gap_or_overlap")]


def test_boundary_difference_within_tolerance_is_accepted():
    assert validate_sequence([item(0, 40), item(40.0005, 180)], 180) == []


def test_non_positive_length_is_reported():
    result = validate_sequence([item(0, 0), item(0, 180)], 180)
    assert (0, "non_positive_length") in [(v.idx, v.code) for v in result]


def test_transition_longer_than_the_section_is_reported():
    # 구간 길이 10초 = 10000ms. 10001ms 는 넘는다
    result = validate_sequence([item(0, 10, transition_ms=10_001), item(10, 180)], 180)
    assert [(v.idx, v.code) for v in result] == [(0, "transition_out_of_range")]


def test_transition_equal_to_the_section_length_is_accepted():
    assert validate_sequence([item(0, 10, transition_ms=10_000), item(10, 180)], 180) == []


def test_negative_transition_is_reported():
    result = validate_sequence([item(0, 180, transition_ms=-1)], 180)
    assert [(v.idx, v.code) for v in result] == [(0, "transition_out_of_range")]


def test_item_serializes_with_the_camel_case_keys_of_the_spec():
    dumped = json.loads(item(0, 10, 500, "chorus").model_dump_json(by_alias=True))
    assert set(dumped) == {"sectionLabel", "startSec", "endSec", "transitionMs", "state", "rationale"}
    assert dumped["sectionLabel"] == "chorus"


def test_item_parses_the_camel_case_json_of_the_spec():
    raw = {
        "sectionLabel": "chorus",
        "startSec": 42.3,
        "endSec": 71.8,
        "transitionMs": 2000,
        "state": default_stage_state("#9F77DD").model_dump(),
        "rationale": "코러스 에너지가 곡 평균의 1.8배라 세 스팟 모두 켜고 밝기 800",
    }
    parsed = SequenceItem.model_validate(raw)
    assert (parsed.section_label, parsed.start_sec, parsed.transition_ms) == ("chorus", 42.3, 2000)
