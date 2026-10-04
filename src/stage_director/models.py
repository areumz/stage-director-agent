"""싱글 제안의 요청·응답 모델. JSON 키는 camelCase.

Python 은 Supabase 를 읽지 않으므로 곡·아티스트·프리셋·분석 결과를 전부 요청 본문으로 받음
아티스트·프리셋 모양은 contracts/artist_context.md, contracts/api_stage_presets.md 를 따르며,
Next.js 가 응답을 그대로 넘겨도 되도록 모르는 필드는 무시
"""

from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from pydantic.alias_generators import to_camel

from contracts.stage_state import HEX_COLOR
from stage_director.sequence import SequenceItem

SECTION_MOOD_MAX = 200  # 무드는 프롬프트에 들어가는 사용자 입력이라 길이를 막는다
MAX_FEEDBACK_CHARS = 500


def _hex_color(value: str) -> str:
    # 아티스트 컬러는 default_stage_state 의 기본 색이 되고 clamp 는 fallback 을 믿는다. 여기서 형식을 보증한다.
    if not HEX_COLOR.fullmatch(value):
        raise ValueError(f"#RRGGBB 형식이 아니다: {value!r}")
    return value


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, allow_inf_nan=False)


class Shader(CamelModel):
    pattern: Literal["wave", "ripple", "grain"]
    freq: float
    falloff: float
    speed: float


class Artist(CamelModel):
    slug: str
    name: str
    name_ko: str
    color: Annotated[str, AfterValidator(_hex_color)]
    shader: Shader


class Preset(CamelModel):
    name: str
    state: Any  # 저장된 jsonb 그대로. StageState 모양이라는 보장이 없어 소비 전에 sanitize_state 를 거침


class Track(CamelModel):
    title: str
    genre: str = ""
    mood_keywords: list[str] = Field(default_factory=list)


class Section(CamelModel):
    label: str
    start_sec: float = Field(ge=0)
    end_sec: float
    mood: str = Field(default="", max_length=SECTION_MOOD_MAX)  # 무드 해석 노드 또는 사람이 채운다. 비어 있어도 된다

    @model_validator(mode="after")
    def _start_before_end(self):
        if self.start_sec >= self.end_sec:
            raise ValueError(f"startSec({self.start_sec}) 는 endSec({self.end_sec}) 보다 작아야 한다")
        return self


class ProposeRequest(CamelModel):
    track: Track
    artist: Artist
    presets: list[Preset] = Field(default_factory=list)
    analysis: Any = None  # audio_tracks.analysis jsonb. parse_analysis 가 필드별로 방어한다
    section: Section
    feedback: str | None = Field(default=None, max_length=MAX_FEEDBACK_CHARS)  # interrupt #2 에서 사람이 쓴 수정 요청
    previous: SequenceItem | None = None  # 피드백이 가리키는 직전 제안


class SequenceRequest(CamelModel):
    """곡 전체 요청. `section` 대신 `duration_sec`을 받는다 — 구간은 그래프가 직접 나눈다."""

    track: Track
    artist: Artist
    presets: list[Preset] = Field(default_factory=list)
    analysis: Any = None
    duration_sec: float = Field(gt=0)
    audio_url: str | None = None  # 무드 해석용 음원 서명 URL (스펙 §3). 없으면 무드 노드를 건너뛴다


class Issue(CamelModel):
    rule: str
    message: str
    idx: int | None = None  # 어느 구간의 이슈인지. 곡 전체 이슈(예: empty)는 None


class SectionProposal(CamelModel):
    item: SequenceItem
    energy_ratio: float  # 구간 평균 에너지 / 곡 평균 에너지
    issues: list[Issue]


class SequenceResponse(CamelModel):
    thread_id: str
    sections: list[Section]
    items: list[SequenceItem]
    issues: list[Issue]


class RunCreate(CamelModel):
    """POST /runs 본문. thread_id 는 Next.js 가 만든 stage_sequences.id (스펙 §6.2)."""

    thread_id: str = Field(min_length=1, max_length=64)
    context: SequenceRequest


class RunStatus(CamelModel):
    """GET/POST /runs 응답. interrupt·result 는 이미 camelCase 로 직렬화된 dict 이다."""

    thread_id: str
    status: Literal["running", "waiting_input", "done", "error"]
    interrupt: dict[str, Any] | None = None  # waiting_input 일 때 현재 interrupt 페이로드
    result: dict[str, Any] | None = None  # done 일 때 {sections, items, issues}
    error: str | None = None  # error 일 때 코드 문자열(llm_failed / internal_error / interrupted)


class SectionsPayload(CamelModel):
    sections: list[Section] = Field(min_length=1)


class FeedbackPayload(CamelModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_FEEDBACK_CHARS)]
    targets: list[Annotated[int, Field(ge=0)]] = Field(min_length=1)  # 사용자가 고른 재생성 대상 구간 idx


class SectionsResume(CamelModel):
    interrupt_id: str
    kind: Literal["sections"]
    payload: SectionsPayload


class FeedbackResume(CamelModel):
    interrupt_id: str
    kind: Literal["feedback"]
    payload: FeedbackPayload


class ApproveResume(CamelModel):
    interrupt_id: str
    kind: Literal["approve"]
    payload: dict[str, Any] = Field(default_factory=dict)


ResumeRequest = Annotated[SectionsResume | FeedbackResume | ApproveResume, Field(discriminator="kind")]
