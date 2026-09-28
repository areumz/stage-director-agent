"""실제 Gemini 를 호출하는 소규모 평가 세트 (스펙 §9). 기본 실행에서는 빠지고 `uv run --env-file .env pytest -m llm` 으로만 돈다.

결과는 확률적이라 값 자체가 아니라 게이트 규칙(스펙 §7 2층)을 지켰는지만 본다.
"""

import json
import os
from pathlib import Path

import pytest

from stage_director.gate import brightness
from stage_director.llm.gemini import GeminiClient
from stage_director.models import ProposeRequest
from stage_director.propose import propose_section
from stage_director.settings import DEFAULT_GEMINI_MODEL

pytestmark = pytest.mark.llm

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "propose_request.json").read_text())
SECTIONS = {"intro": (0, 10), "chorus": (10, 30), "outro": (30, 40)}  # 에너지 비 0.31 / 1.54 / 0.62


@pytest.fixture(scope="module")
def proposals():
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY 가 없다")
    llm = GeminiClient(os.environ["GEMINI_API_KEY"], os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL)
    return {
        label: propose_section(llm, ProposeRequest.model_validate({**FIXTURE, "section": {"label": label, "startSec": s, "endSec": e}}))
        for label, (s, e) in SECTIONS.items()
    }


@pytest.mark.parametrize("label", SECTIONS)
def test_no_gate_violations(proposals, label):
    # clamp 는 이미 고쳐진 값이라 허용한다. 그 밖의 위반은 자동 재생성 대상이라 남아 있으면 프롬프트를 손봐야 한다
    assert [i for i in proposals[label].issues if i.rule != "clamped"] == []


def test_loud_section_is_brighter_than_calm_section(proposals):
    assert brightness(proposals["chorus"].item.state) > brightness(proposals["intro"].item.state)


def test_every_proposal_explains_itself(proposals):
    assert all(p.item.rationale.strip() for p in proposals.values())
