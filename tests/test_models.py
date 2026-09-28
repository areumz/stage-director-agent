import pytest
from pydantic import ValidationError

from stage_director.models import ProposeRequest

BODY = {
    "track": {"title": "나만의 작은 우주", "genre": "K-pop", "moodKeywords": ["몽환", "벅찬"]},
    "artist": {
        "slug": "aurora",
        "name": "AURORA",
        "nameKo": "오로라",
        "color": "#9F77DD",
        "shader": {"pattern": "wave", "freq": 9, "falloff": 0.75, "speed": 0.5},
    },
    "presets": [{"id": "p1", "name": "코러스용", "state": {"color": "#9F77DD"}}],
    "analysis": {"durationSec": 40, "bpm": 120, "energyCurve": [0.1, 0.2]},
    "section": {"label": "chorus", "startSec": 10, "endSec": 30},
}


def body(**patch):
    """BODY 의 얕은 복사본에서 최상위 키를 덮어쓴다."""
    return {**BODY, **patch}


def test_parses_camel_case_body():
    req = ProposeRequest.model_validate(BODY)
    assert req.track.mood_keywords == ["몽환", "벅찬"]
    assert req.artist.name_ko == "오로라"
    assert req.section.start_sec == 10
    assert req.presets[0].name == "코러스용"


def test_ignores_fields_next_js_passes_through():
    # GET /api/stage-presets 응답의 id, GET /api/artists 응답의 나머지 필드가 그대로 와도 받는다
    req = ProposeRequest.model_validate(body(artist={**BODY["artist"], "initials": "AUR", "orbit": 1}))
    assert req.artist.slug == "aurora"


def test_optional_fields_default():
    minimal = {k: v for k, v in BODY.items() if k != "presets"} | {"track": {"title": "t"}}
    req = ProposeRequest.model_validate(minimal)
    assert req.track.genre == "" and req.track.mood_keywords == []
    assert req.presets == []


def test_analysis_may_be_missing_or_garbage():
    without = {k: v for k, v in BODY.items() if k != "analysis"}
    assert ProposeRequest.model_validate(without).analysis is None
    assert ProposeRequest.model_validate(body(analysis="oops")).analysis == "oops"


@pytest.mark.parametrize("color", ["red", "#12345", "#GGGGGG", "#9F77DD\n", "9F77DD"])
def test_rejects_artist_color_that_is_not_hex(color):
    with pytest.raises(ValidationError):
        ProposeRequest.model_validate(body(artist={**BODY["artist"], "color": color}))


@pytest.mark.parametrize("start, end", [(10, 10), (30, 10), (-1, 5)])
def test_rejects_bad_section_range(start, end):
    with pytest.raises(ValidationError):
        ProposeRequest.model_validate(body(section={"label": "x", "startSec": start, "endSec": end}))


def test_rejects_unknown_shader_pattern():
    with pytest.raises(ValidationError):
        ProposeRequest.model_validate(body(artist={**BODY["artist"], "shader": {**BODY["artist"]["shader"], "pattern": "star"}}))
