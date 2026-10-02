"""싱글 제안의 요청·응답 모델. JSON 키는 camelCase.

Python 은 Supabase 를 읽지 않으므로 곡·아티스트·프리셋·분석 결과를 전부 요청 본문으로 받음
아티스트·프리셋 모양은 contracts/artist_context.md, contracts/api_stage_presets.md 를 따르며,
Next.js 가 응답을 그대로 넘겨도 되도록 모르는 필드는 무시
"""

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from contracts.stage_state import HEX_COLOR
from stage_director.sequence import SequenceItem


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


class SequenceRequest(CamelModel):
    """곡 전체 요청. `section` 대신 `duration_sec`을 받는다 — 구간은 그래프가 직접 나눈다."""

    track: Track
    artist: Artist
    presets: list[Preset] = Field(default_factory=list)
    analysis: Any = None
    duration_sec: float = Field(gt=0)


class Issue(CamelModel):
    rule: str
    message: str


class SectionProposal(CamelModel):
    item: SequenceItem
    energy_ratio: float  # 구간 평균 에너지 / 곡 평균 에너지
    issues: list[Issue]


class SequenceResponse(CamelModel):
    thread_id: str
    sections: list[Section]
    items: list[SequenceItem]
    issues: list[Issue]
