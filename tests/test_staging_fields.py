"""구간 선택 필드(backdrop·dust·reflection): 필드 없음 / 허용값 / 잘못된 값. 잘못된 값은 에러 없이 None."""

import pytest

from stage_director.llm.fake import FakeLLM
from stage_director.prompts import INTENSE_ENERGY_RATIO, PROPOSAL_SCHEMA, SYSTEM_PROMPT
from stage_director.propose import propose_section
from stage_director.sequence import BACKDROPS
from tests.test_propose import llm_output, request


def propose(**extra):
    return propose_section(FakeLLM({**llm_output(), **extra}), request())


def test_missing_fields_stay_none():
    proposal = propose()
    item = proposal.item
    assert (item.backdrop, item.dust, item.reflection) == (None, None, None)
    assert proposal.issues == []


def test_allowed_values_are_kept():
    item = propose(backdrop="laser", dust=80, reflection=35.5).item
    assert (item.backdrop, item.dust, item.reflection) == ("laser", 80, 35.5)
    dumped = item.model_dump(by_alias=True, mode="json")
    assert (dumped["backdrop"], dumped["dust"], dumped["reflection"]) == ("laser", 80, 35.5)


@pytest.mark.parametrize("backdrop", BACKDROPS)
def test_every_backdrop_is_allowed(backdrop):
    assert propose(backdrop=backdrop).item.backdrop == backdrop


@pytest.mark.parametrize(
    "extra",
    [
        {"backdrop": "disco", "dust": 101, "reflection": -1},  # 허용값·범위 밖
        {"backdrop": 3, "dust": "많이", "reflection": True},  # 타입이 틀림
        {"backdrop": None, "dust": float("nan"), "reflection": float("inf")},
        {"backdrop": ["laser"], "dust": [50], "reflection": {"a": 1}},
    ],
)
def test_invalid_values_become_none_without_error_or_issue(extra):
    proposal = propose(**extra)
    item = proposal.item
    assert (item.backdrop, item.dust, item.reflection) == (None, None, None)
    assert proposal.issues == []  # clamped·invalid_state 이슈를 만들지 않는다(재생성 대상이 되지 않는다)


def test_one_bad_field_does_not_drop_the_others():
    item = propose(backdrop="cube", dust=500, reflection=20).item
    assert (item.backdrop, item.dust, item.reflection) == ("cube", None, 20)


def test_prompt_names_allowed_values_and_contrast_guidance():
    for name in BACKDROPS:
        assert name in SYSTEM_PROMPT
    assert "100~250" in SYSTEM_PROMPT and "800~1000" in SYSTEM_PROMPT
    properties = PROPOSAL_SCHEMA["properties"]
    assert properties["backdrop"]["enum"] == list(BACKDROPS)
    assert {"backdrop", "dust", "reflection"}.isdisjoint(PROPOSAL_SCHEMA["required"])  # 선택 필드


def test_no_extra_llm_call():
    llm = FakeLLM({**llm_output(), "backdrop": "gyro"})
    propose_section(llm, request())
    assert len(llm.calls) == 1


def test_old_sequence_without_the_fields_is_still_valid():
    from stage_director.sequence import SequenceItem

    old = {"sectionLabel": "x", "startSec": 0, "endSec": 5, "transitionMs": 500, "rationale": "r", "state": propose().item.state.model_dump()}
    item = SequenceItem.model_validate(old)
    assert (item.backdrop, item.dust, item.reflection) == (None, None, None)


def test_prompt_maps_energy_ratio_to_backdrop_tiers():
    from stage_director.gate import CALM_ENERGY_RATIO

    assert f"에너지 비 {CALM_ENERGY_RATIO:g} 이하: gyro" in SYSTEM_PROMPT
    assert f"{INTENSE_ENERGY_RATIO:g} 초과: laser 또는 cube" in SYSTEM_PROMPT
    assert CALM_ENERGY_RATIO < INTENSE_ENERGY_RATIO  # 단계가 겹치지 않는다
