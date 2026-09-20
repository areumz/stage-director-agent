import pytest

from contracts.stage_state import default_stage_state
from stage_director.proposals import apply_regeneration, merge_proposals
from stage_director.sequence import SequenceItem


def item(idx: int, rationale: str = "원본") -> SequenceItem:
    return SequenceItem(
        section_label=f"s{idx}",
        start_sec=idx * 10,
        end_sec=idx * 10 + 10,
        transition_ms=500,
        state=default_stage_state("#9F77DD"),
        rationale=rationale,
    )


def test_merge_combines_disjoint_indices_from_parallel_branches():
    merged = merge_proposals({0: item(0)}, {1: item(1)})
    assert set(merged) == {0, 1}


def test_merge_lets_the_right_side_win_on_the_same_index():
    merged = merge_proposals({0: item(0, "old")}, {0: item(0, "new")})
    assert merged[0].rationale == "new"


def test_merge_treats_none_as_empty():
    assert merge_proposals(None, {0: item(0)}).keys() == {0}
    assert merge_proposals({0: item(0)}, None).keys() == {0}
    assert merge_proposals(None, None) == {}


def test_merge_does_not_mutate_its_inputs():
    left, right = {0: item(0)}, {1: item(1)}
    merge_proposals(left, right)
    assert set(left) == {0} and set(right) == {1}


def test_regeneration_replaces_only_the_targets():
    existing = {0: item(0), 1: item(1), 2: item(2)}
    result = apply_regeneration(existing, {1: item(1, "재생성")}, targets={1})
    assert result[1].rationale == "재생성"
    assert result[0] is existing[0] and result[2] is existing[2]


def test_untouched_sections_are_byte_identical_after_regeneration():
    # 스펙 §6.3 불변식: 대상 밖 구간은 바이트 단위로 같아야 한다
    existing = {0: item(0), 1: item(1), 2: item(2)}
    before = {i: existing[i].model_dump_json() for i in (0, 2)}
    result = apply_regeneration(existing, {1: item(1, "재생성")}, targets={1})
    assert {i: result[i].model_dump_json() for i in (0, 2)} == before


def test_regeneration_does_not_mutate_the_existing_proposals():
    existing = {0: item(0), 1: item(1)}
    apply_regeneration(existing, {1: item(1, "재생성")}, targets={1})
    assert existing[1].rationale == "원본"


def test_regeneration_rejects_results_outside_the_targets():
    with pytest.raises(ValueError, match="대상이 아닌"):
        apply_regeneration({0: item(0), 1: item(1)}, {0: item(0, "몰래"), 1: item(1, "x")}, targets={1})


def test_regeneration_rejects_missing_targets():
    with pytest.raises(ValueError, match="빠져"):
        apply_regeneration({0: item(0), 1: item(1)}, {1: item(1, "x")}, targets={0, 1})
