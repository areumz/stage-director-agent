import pytest
from pydantic import TypeAdapter, ValidationError

from stage_director.models import (
    Issue,
    ProposeRequest,
    ResumeRequest,
    RunCreate,
    RunStatus,
    Section,
    SequenceRequest,
    SequenceResponse,
)

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


@pytest.mark.parametrize("field", ["startSec", "endSec"])
@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_rejects_non_finite_section_times(field, value):
    with pytest.raises(ValidationError):
        ProposeRequest.model_validate(body(section={"label": "chorus", "startSec": 10, "endSec": 30, field: value}))


SEQUENCE_BODY = {
    "track": BODY["track"],
    "artist": BODY["artist"],
    "presets": [],
    "analysis": {"durationSec": 60, "bpm": 120, "energyCurve": [0.1] * 30 + [0.9] * 30},
    "durationSec": 60,
}


def test_sequence_request_parses_camel_case_body():
    req = SequenceRequest.model_validate(SEQUENCE_BODY)
    assert req.duration_sec == 60
    assert req.track.title == BODY["track"]["title"]


@pytest.mark.parametrize("duration", [0, -1])
def test_sequence_request_rejects_non_positive_duration(duration):
    with pytest.raises(ValidationError):
        SequenceRequest.model_validate({**SEQUENCE_BODY, "durationSec": duration})


def test_sequence_request_optional_fields_default():
    minimal = {"track": {"title": "t"}, "artist": BODY["artist"], "durationSec": 30}
    req = SequenceRequest.model_validate(minimal)
    assert req.presets == [] and req.analysis is None


def test_issue_idx_defaults_to_none_and_serializes():
    assert Issue(rule="clamped", message="m").idx is None
    issue = Issue(rule="clamped", message="m", idx=2)
    assert issue.idx == 2
    assert issue.model_dump(mode="json", by_alias=True)["idx"] == 2


def test_sequence_response_serializes_camel_case():
    from contracts.stage_state import default_stage_state
    from stage_director.models import Section
    from stage_director.sequence import SequenceItem

    item = SequenceItem(
        section_label="intro", start_sec=0, end_sec=30, transition_ms=2000,
        state=default_stage_state("#9F77DD"), rationale="r",
    )
    response = SequenceResponse(
        thread_id="t1", sections=[Section(label="intro", start_sec=0, end_sec=30)], items=[item], issues=[]
    )
    dumped = response.model_dump(mode="json", by_alias=True)
    assert dumped["threadId"] == "t1"
    assert dumped["sections"][0]["startSec"] == 0
    assert dumped["items"][0]["sectionLabel"] == "intro"


# ── 4단계: 무드, 음원 URL, /runs ─────────────────────────────

RESUME = TypeAdapter(ResumeRequest)


def test_section_mood_defaults_to_empty():
    assert Section(label="a", start_sec=0, end_sec=10).mood == ""


def test_section_mood_is_capped():
    with pytest.raises(ValidationError):
        Section(label="a", start_sec=0, end_sec=10, mood="x" * 201)


def test_sequence_request_audio_url_is_optional():
    assert SequenceRequest.model_validate(SEQUENCE_BODY).audio_url is None
    req = SequenceRequest.model_validate({**SEQUENCE_BODY, "audioUrl": "https://x/y.mp3"})
    assert req.audio_url == "https://x/y.mp3"


def test_run_create_parses_camel_case_body():
    run = RunCreate.model_validate({"threadId": "t-1", "context": SEQUENCE_BODY})
    assert run.thread_id == "t-1"
    assert run.context.duration_sec == 60


@pytest.mark.parametrize("thread_id", ["", "x" * 65])
def test_run_create_rejects_bad_thread_id(thread_id):
    with pytest.raises(ValidationError):
        RunCreate.model_validate({"threadId": thread_id, "context": SEQUENCE_BODY})


@pytest.mark.parametrize(
    "body",
    [
        {"interruptId": "t:0:confirm_sections", "kind": "sections", "payload": {"sections": [{"label": "a", "startSec": 0, "endSec": 30}]}},
        {"interruptId": "t:1:review", "kind": "feedback", "payload": {"text": " 더 어둡게 ", "targets": [1]}},
        {"interruptId": "t:1:review", "kind": "approve"},
    ],
)
def test_resume_request_parses_each_kind(body):
    parsed = RESUME.validate_python(body)
    assert parsed.kind == body["kind"]
    assert parsed.interrupt_id == body["interruptId"]


def test_feedback_text_is_stripped():
    parsed = RESUME.validate_python({"interruptId": "i", "kind": "feedback", "payload": {"text": " 더 어둡게 ", "targets": [1]}})
    assert parsed.payload.text == "더 어둡게"


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "   ", "targets": [1]},
        {"text": "x" * 501, "targets": [1]},
        {"text": "ok", "targets": []},
        {"text": "ok", "targets": [-1]},
    ],
)
def test_feedback_resume_rejects_bad_payload(payload):
    with pytest.raises(ValidationError):
        RESUME.validate_python({"interruptId": "i", "kind": "feedback", "payload": payload})


def test_run_status_serializes_camel_case():
    status = RunStatus(thread_id="t1", status="waiting_input", interrupt={"interruptId": "t1:0:confirm_sections"})
    dumped = status.model_dump(mode="json", by_alias=True)
    assert dumped["threadId"] == "t1"
    assert dumped["interrupt"]["interruptId"] == "t1:0:confirm_sections"
    assert dumped["result"] is None and dumped["error"] is None
