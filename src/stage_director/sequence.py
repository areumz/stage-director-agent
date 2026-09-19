"""시퀀스 항목 모델과 불변식 (스펙 §5). Python 검증 게이트와 Next.js 저장 직전이 같은 규칙을 쓴다."""

from typing import NamedTuple

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from contracts.stage_state import StageState

TOLERANCE_SEC = 1e-3


class SequenceItem(BaseModel):
    """구간 1개. JSON 키는 스펙 §6 의 camelCase (sectionLabel, startSec, endSec, transitionMs)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    section_label: str
    start_sec: float
    end_sec: float
    transition_ms: int
    state: StageState
    rationale: str


class Violation(NamedTuple):
    idx: int | None  # 곡 전체에 대한 위반이면 None
    code: str
    message: str


def validate_sequence(items: list[SequenceItem], duration_sec: float) -> list[Violation]:
    """스펙 §5 불변식을 검사해 위반 목록을 돌려준다. 위반이 없으면 빈 리스트."""
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
