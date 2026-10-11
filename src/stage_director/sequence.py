"""시퀀스 항목 모델과 불변식. Python 검증 게이트와 Next.js 저장 직전이 같은 규칙을 씀."""

import math
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic.alias_generators import to_camel

from contracts.stage_state import StageState

TOLERANCE_SEC = 1e-3
BACKDROPS = ("bars", "tunnel", "laser", "gyro", "cube", "none")  # 구간 배경 연출 허용값
PROPS = ("amp", "drum", "mic")  # 곡 전체 소품 허용값
LEVEL_RANGE = (0.0, 100.0)  # dust·reflection 범위


def clean_backdrop(value: Any) -> str | None:
    """허용값이 아니면 None (에러도 clamp 도 하지 않는다)."""
    return value if isinstance(value, str) and value in BACKDROPS else None


def clean_level(value: Any) -> float | None:
    """0~100 의 숫자만 받는다. 숫자가 아니거나 범위 밖이면 None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and LEVEL_RANGE[0] <= number <= LEVEL_RANGE[1] else None


def clean_props(value: Any) -> list[str] | None:
    """None = 정하지 않음, [] = 소품 없음. 목록이 아니면 None, 목록이면 허용값만 중복 없이 순서대로 남긴다."""
    if not isinstance(value, list):
        return None
    return list(dict.fromkeys(p for p in value if isinstance(p, str) and p in PROPS))

class SequenceItem(BaseModel):
    """구간 1개 : 무대 연출 한 구간 데이터 모델 (시작 시간, 종료 시간, 전환 시간, 무대 상태 등)"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    section_label: str
    start_sec: float
    end_sec: float
    transition_ms: int
    state: StageState
    rationale: str
    # 선택 필드. 비어 있으면(None) on-stage 기본값을 쓴다. 허용값이 아니면 에러 없이 None.
    backdrop: str | None = None
    dust: float | None = None
    reflection: float | None = None

    _clean_backdrop = field_validator("backdrop", mode="before")(clean_backdrop)
    _clean_dust = field_validator("dust", mode="before")(clean_level)
    _clean_reflection = field_validator("reflection", mode="before")(clean_level)


class Violation(NamedTuple):
    idx: int | None  # 곡 전체에 대한 위반이면 None
    code: str
    message: str


def validate_sequence(items: list[SequenceItem], duration_sec: float) -> list[Violation]:
    """불변식을 검사해 위반 목록을 돌려줌. 위반이 없으면 빈 리스트."""
    if not items:
        return [Violation(None, "empty", "구간이 하나도 없다")]

    violations: list[Violation] = []
    for i, item in enumerate(items):
        length_sec = item.end_sec - item.start_sec
        if length_sec <= 0:
            violations.append(Violation(i, "non_positive_length", f"startSec({item.start_sec}) >= endSec({item.end_sec})"))
        elif not 0 <= item.transition_ms <= length_sec * 1000:
            violations.append(
                Violation(i, "transition_out_of_range", f"transitionMs({item.transition_ms})가 0 ~ {length_sec * 1000:g} 범위 밖")
            )
        if i > 0 and abs(items[i - 1].end_sec - item.start_sec) > TOLERANCE_SEC:
            violations.append(
                Violation(i, "gap_or_overlap", f"이전 endSec({items[i - 1].end_sec})와 startSec({item.start_sec})가 이어지지 않는다")
            )

    if abs(items[0].start_sec) > TOLERANCE_SEC:
        violations.append(Violation(0, "start_not_zero", f"첫 구간 startSec({items[0].start_sec})가 0이 아니다"))
    if abs(items[-1].end_sec - duration_sec) > TOLERANCE_SEC:
        violations.append(
            Violation(len(items) - 1, "end_not_duration", f"마지막 구간 endSec({items[-1].end_sec})가 곡 길이({duration_sec})와 다르다")
        )
    return violations
