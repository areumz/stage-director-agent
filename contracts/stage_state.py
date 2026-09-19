"""StageState 계약 — ../on-stage/src/lib/stageState.ts 의 파이썬 포트.

이 파일에는 성격이 다른 두 부분이 있다. 섞지 말 것.

[A] on-stage 동작 (계약): 모델, default_stage_state, merge_stage_state.
    on-stage 의 stageState.ts / stageState.test.ts 와 같은 동작이어야 하며,
    tests/contracts/test_stage_state.py 가 fixtures/merge_stage_state_cases.json
    (골든 벡터)로 이를 검증한다. on-stage 는 숫자 범위 clamp 와 hex 검증을 하지 않는다.

[B] 이 프로젝트의 규칙 (계약 아님): clamp_stage_state.
    on-stage 에 없는 범위·hex 방어. 스펙 §7 의 1층. 범위는 design-v2.md §5.3 슬라이더 범위.
"""

import math
import re
from typing import Any, Literal, NamedTuple, get_args

from pydantic import BaseModel

Camera = Literal["front", "audience", "top"]
CAMERAS: tuple[str, ...] = get_args(Camera)


# ── [A] on-stage 동작 ─────────────────────────────────────────


class SpotState(BaseModel):
    on: bool
    intensity: float
    angle: float
    penumbra: float  # 씬(SpotLight)에는 아직 전달되지만 UI 슬라이더는 제거됨. 에이전트는 바꾸지 않는다.


class Spots(BaseModel):
    left: SpotState
    center: SpotState
    right: SpotState


class Smoke(BaseModel):
    density: float
    color: str


class StageState(BaseModel):
    color: str
    spots: Spots
    camera: Camera
    smoke: Smoke


def _default_spot(on: bool = True) -> SpotState:
    return SpotState(on=on, intensity=300, angle=0.45, penumbra=0.6)


def default_stage_state(color: str) -> StageState:
    """on-stage defaultStageState(color) 와 동일: left/center on, right off, front, 스모그 꺼짐."""
    return StageState(
        color=color,
        spots=Spots(left=_default_spot(), center=_default_spot(), right=_default_spot(on=False)),
        camera="front",
        smoke=Smoke(density=0, color="#ffffff"),
    )


def _is_number(value: Any) -> bool:
    # TS 의 typeof v === "number". 파이썬에서 bool 은 int 의 하위 타입이라 명시적으로 제외한다.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    # 파이썬 int 는 크기 제한이 없어 float 로 못 바꾸는 값이 있다. 그런 값은 타입이 틀린 것으로 본다.
    try:
        float(value)
    except OverflowError:
        return False
    return True


def _merge_spot(value: Any, fallback: SpotState) -> SpotState:
    if not isinstance(value, dict):
        return fallback.model_copy()
    return SpotState(
        on=value["on"] if isinstance(value.get("on"), bool) else fallback.on,
        intensity=value["intensity"] if _is_number(value.get("intensity")) else fallback.intensity,
        angle=value["angle"] if _is_number(value.get("angle")) else fallback.angle,
        penumbra=value["penumbra"] if _is_number(value.get("penumbra")) else fallback.penumbra,
    )


def _merge_spots(value: Any, fallback: Spots) -> Spots:
    if not isinstance(value, dict):
        return fallback.model_copy(deep=True)
    return Spots(
        left=_merge_spot(value.get("left"), fallback.left),
        center=_merge_spot(value.get("center"), fallback.center),
        right=_merge_spot(value.get("right"), fallback.right),
    )


def _merge_smoke(value: Any, fallback: Smoke) -> Smoke:
    if not isinstance(value, dict):
        return fallback.model_copy()
    return Smoke(
        density=value["density"] if _is_number(value.get("density")) else fallback.density,
        color=value["color"] if isinstance(value.get("color"), str) else fallback.color,
    )


def merge_stage_state(value: Any, fallback: StageState) -> StageState:
    """on-stage mergeStageState: 필드별로 값이 있고 타입이 맞으면 그 값, 아니면 fallback 의 값.

    타입만 본다. 숫자 범위와 hex 형식은 검사하지 않는다(on-stage 와 동일). 범위 방어는 [B].
    """
    if not isinstance(value, dict):
        return fallback.model_copy(deep=True)
    return StageState(
        color=value["color"] if isinstance(value.get("color"), str) else fallback.color,
        spots=_merge_spots(value.get("spots"), fallback.spots),
        camera=value["camera"] if value.get("camera") in CAMERAS else fallback.camera,
        smoke=_merge_smoke(value.get("smoke"), fallback.smoke),
    )


# ── [B] 이 프로젝트의 규칙 (on-stage 에 없음) ─────────────────

INTENSITY_RANGE = (0.0, 1000.0)
ANGLE_RANGE = (0.1, 1.0)
PENUMBRA_RANGE = (0.0, 1.0)
DENSITY_RANGE = (0.0, 1.0)
HEX_COLOR = re.compile(r"\A#[0-9a-fA-F]{6}\Z")


class ClampNote(NamedTuple):
    path: str
    original: Any
    applied: Any


def clamp_stage_state(state: StageState, fallback: StageState) -> tuple[StageState, list[ClampNote]]:
    """범위 밖 숫자는 clamp, 유한하지 않은 숫자·잘못된 hex 는 fallback 값으로 바꾸고 노트를 남긴다. 거부하지 않는다."""
    notes: list[ClampNote] = []

    def num(path: str, value: float, bounds: tuple[float, float], fb: float) -> float:
        if not math.isfinite(value):
            notes.append(ClampNote(path, value, fb))
            return fb
        clamped = min(max(value, bounds[0]), bounds[1])
        if clamped != value:
            notes.append(ClampNote(path, value, clamped))
        return clamped

    def color(path: str, value: str, fb: str) -> str:
        if HEX_COLOR.fullmatch(value):
            return value
        notes.append(ClampNote(path, value, fb))
        return fb

    def spot(name: str, s: SpotState, fb: SpotState) -> SpotState:
        return SpotState(
            on=s.on,
            intensity=num(f"spots.{name}.intensity", s.intensity, INTENSITY_RANGE, fb.intensity),
            angle=num(f"spots.{name}.angle", s.angle, ANGLE_RANGE, fb.angle),
            penumbra=num(f"spots.{name}.penumbra", s.penumbra, PENUMBRA_RANGE, fb.penumbra),
        )

    result = StageState(
        color=color("color", state.color, fallback.color),
        spots=Spots(
            left=spot("left", state.spots.left, fallback.spots.left),
            center=spot("center", state.spots.center, fallback.spots.center),
            right=spot("right", state.spots.right, fallback.spots.right),
        ),
        camera=state.camera,
        smoke=Smoke(
            density=num("smoke.density", state.smoke.density, DENSITY_RANGE, fallback.smoke.density),
            color=color("smoke.color", state.smoke.color, fallback.smoke.color),
        ),
    )
    return result, notes
