"""분석 결과 모델과 방어적 파서.

audio_tracks.analysis(jsonb)에 저장되는 모양. jsonb 는 손으로 고쳐지거나 버전이 달라질 수 있어서,
mergeStageState 처럼 필드별로 방어하는 파서를 둠: 필드 하나가 깨져도 나머지는 유지됨.
"""

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class AnalysisSnapshot(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    duration_sec: float = 0.0
    bpm: float = 0.0
    beats_sec: list[float] = Field(default_factory=list)  # 비트 위치(초)
    energy_curve: list[float] = Field(default_factory=list)  # 1초 구간별 평균 RMS. 인덱스 i = [i, i+1)초
    onset_density: list[float] = Field(default_factory=list)  # 1초 구간별 온셋 개수. 인덱스 규칙은 energy_curve 와 같다


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # 파이썬 int 는 크기 제한이 없어 float 로 못 바꾸는 값이 있다
        return None
    return number if math.isfinite(number) else None


def _timestamps(value: Any) -> list[float]:
    """시각 목록(비트 위치). 잘못된 원소는 버림. 0.0 으로 채우면 0초에 없는 비트가 생김."""
    if not isinstance(value, list):
        return []
    return [n for n in (_finite_number(v) for v in value) if n is not None]


def _per_second(value: Any) -> list[float]:
    """1초 구간별 값 목록. 인덱스가 곧 시각이라 잘못된 원소는 0.0 으로 채워 뒤의 초가 밀리지 않게함."""
    if not isinstance(value, list):
        return []
    return [_finite_number(v) or 0.0 for v in value]


def parse_analysis(raw: Any) -> AnalysisSnapshot:
    """필드별 방어 파서. 예외를 던지지 않는다. 없거나 타입이 틀린 필드는 기본값(0 또는 빈 리스트)."""
    if not isinstance(raw, dict):
        return AnalysisSnapshot()
    return AnalysisSnapshot(
        duration_sec=_finite_number(raw.get("durationSec")) or 0.0,
        bpm=_finite_number(raw.get("bpm")) or 0.0,
        beats_sec=_timestamps(raw.get("beatsSec")),
        energy_curve=_per_second(raw.get("energyCurve")),
        onset_density=_per_second(raw.get("onsetDensity")),
    )


def section_energy_ratio(energy_curve: list[float], start_sec: float, end_sec: float) -> float:
    """구간 평균 에너지 / 곡 평균 에너지 (기획서의 "곡 평균의 1.8배"). 1초 구간의 중심(i + 0.5)이 [start, end]에 든 것만 셈.

    곡 평균이 0(무음)이거나 구간에 든 값이 없으면 1.0 (평균과 같다고 봄).
    """
    if not energy_curve:
        return 1.0
    track_mean = sum(energy_curve) / len(energy_curve)
    inside = [v for i, v in enumerate(energy_curve) if start_sec <= i + 0.5 < end_sec]
    if track_mean <= 0 or not inside:
        return 1.0
    return (sum(inside) / len(inside)) / track_mean
