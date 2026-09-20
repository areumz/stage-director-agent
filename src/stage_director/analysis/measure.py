"""수치 측정층. librosa 로 BPM, 비트, RMS 에너지 곡선, 온셋 밀도 측정.

결정론적. 구간 구조 분석(all-in-one 등)과 무드 해석은 이 파일의 범위가 아님.
그래프 밖의 일반 함수이며 LangGraph 를 모름.
"""

import math
from pathlib import Path

import librosa
import numpy as np

from stage_director.analysis.snapshot import AnalysisSnapshot

# 음악을 초당 22,050개 샘플로 변환해서 분석
SAMPLE_RATE = 22_050
# 오디오를 일정 간격으로 조금씩 이동하면서 분석하는 단위
HOP_LENGTH = 512


def _per_second_mean(values: np.ndarray, times: np.ndarray, n_bins: int) -> list[float]:
    """일정 간격으로 조금씩 이동하면서 분석한 값을 초당 평균으로 변환."""
    bins = np.minimum(np.floor(times).astype(int), n_bins - 1)
    sums = np.bincount(bins, weights=values, minlength=n_bins)
    counts = np.bincount(bins, minlength=n_bins)
    return [round(float(s / c), 4) if c else 0.0 for s, c in zip(sums, counts)]


def measure_audio(y: np.ndarray, sr: int) -> AnalysisSnapshot:
    """모노 신호 y(샘플레이트 sr)를 측정해 AnalysisSnapshot 을 돌려줌."""
    duration = float(len(y)) / sr
    n_bins = max(1, math.ceil(duration))

    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, hop_length=HOP_LENGTH, units="time")

    rms = librosa.feature.rms(y=y, hop_length=HOP_LENGTH)[0]
    rms_times = librosa.times_like(rms, sr=sr, hop_length=HOP_LENGTH)

    onsets = librosa.onset.onset_detect(y=y, sr=sr, hop_length=HOP_LENGTH, units="time")
    onset_counts, _ = np.histogram(onsets, bins=np.arange(n_bins + 1))

    return AnalysisSnapshot(
        duration_sec=round(duration, 3),
        bpm=round(float(np.atleast_1d(tempo)[0]), 2),
        beats_sec=[round(float(b), 3) for b in beats],
        energy_curve=_per_second_mean(rms, rms_times, n_bins),
        onset_density=[float(c) for c in onset_counts],
    )


def measure_file(path: str | Path) -> AnalysisSnapshot:
    y, sr = librosa.load(path, sr=SAMPLE_RATE, mono=True)
    return measure_audio(y, sr)
