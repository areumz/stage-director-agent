"""검증 게이트의 결정적 규칙 (스펙 §7 의 1층 후처리와 2층).

LLM 노드도 그래프도 모른다. 순수 함수만 둔다.
"""

from typing import Any, NamedTuple

from contracts.stage_state import StageState, clamp_stage_state, merge_stage_state
from stage_director.sequence import SequenceItem

# 스펙 §7 은 "잔잔한 구간"과 "같은 방향"의 기준 수치를 정하지 않았다. 아래 세 값은 이 계획이 정한
# 초기값이다. 1단계 스파이크에서 실제 곡으로 보고 조정한다.
CALM_ENERGY_RATIO = 0.9  # 구간 평균 에너지 / 곡 평균 에너지가 이 값 이하면 잔잔한 구간. 실측 2곡: 잔잔한 구간 0.08~0.83, 일반 구간 1.05~1.37
CALM_MAX_INTENSITY = 500.0  # 잔잔한 구간의 밝기 상한 (스펙 §7: 잔잔한 구간 밝기 <= 500)
DIRECTION_EPS = 0.15  # 인접 구간의 에너지 비 변화가 이 값 이하면 방향 규칙을 적용하지 않는다


class GateIssue(NamedTuple):
    idx: int
    rule: str
    message: str


def brightness(state: StageState) -> float:
    """구간의 밝기 = 켜져 있는 스팟 중 가장 높은 intensity. 켜진 스팟이 없으면 0."""
    return max((s.intensity for s in (state.spots.left, state.spots.center, state.spots.right) if s.on), default=0.0)


def sanitize_state(raw: Any, fallback: StageState, idx: int) -> tuple[StageState, list[GateIssue]]:
    """LLM 이 낸 state 를 필드별 병합 → clamp 한다 (스펙 §7 1층). 바꾼 값마다 이슈를 남긴다."""
    merged = merge_stage_state(raw, fallback)
    clamped, notes = clamp_stage_state(merged, fallback)
    issues = [GateIssue(idx, "clamped", f"{n.path}: {n.original!r} -> {n.applied!r}") for n in notes]
    return clamped, issues


def run_gate(items: list[SequenceItem], energy_ratios: list[float], signature_color: str) -> list[GateIssue]:
    """구간별 규칙 위반을 돌려준다. energy_ratios[i] 는 items[i] 구간 평균 에너지 / 곡 평균 에너지."""
    if len(items) != len(energy_ratios):
        raise ValueError(f"items({len(items)})와 energy_ratios({len(energy_ratios)})의 길이가 다르다")

    issues: list[GateIssue] = []
    levels = [brightness(item.state) for item in items]

    for i, item in enumerate(items):
        if energy_ratios[i] <= CALM_ENERGY_RATIO and levels[i] > CALM_MAX_INTENSITY:
            issues.append(
                GateIssue(i, "calm_too_bright", f"에너지 비 {energy_ratios[i]:.2f}인 잔잔한 구간의 밝기가 {levels[i]:g} (> {CALM_MAX_INTENSITY:g})")
            )
        if item.state.color.lower() != signature_color.lower() and not item.rationale.strip():
            issues.append(GateIssue(i, "color_deviation_without_rationale", f"시그니처 컬러({signature_color})에서 벗어났는데 rationale 이 비어 있다"))
        if i > 0:
            d_energy = energy_ratios[i] - energy_ratios[i - 1]
            d_level = levels[i] - levels[i - 1]
            if (d_energy > DIRECTION_EPS and d_level < 0) or (d_energy < -DIRECTION_EPS and d_level > 0):
                issues.append(
                    GateIssue(i, "energy_brightness_direction", f"에너지 비 {d_energy:+.2f} 변화에 밝기가 {d_level:+g} 변화해 방향이 반대다")
                )
    return issues


def indices_to_regenerate(issues: list[GateIssue]) -> set[int]:
    """자동 재생성 대상 구간. 이슈가 하나라도 있는 구간. 'clamped' 는 이미 고쳐졌으므로 제외한다."""
    return {issue.idx for issue in issues if issue.rule != "clamped"}
