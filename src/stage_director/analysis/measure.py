"""수치 측정층 (기획서 §5 의 1층): librosa 로 BPM, 비트, RMS 에너지 곡선, 온셋 밀도를 잰다.

결정론적이다. 구간 구조 분석(all-in-one 등)과 무드 해석은 이 파일의 범위가 아니다.
그래프 밖의 일반 함수이며 LangGraph 를 모른다.
"""

import math
from pathlib import Path

import librosa
import numpy as np

from stage_director.analysis.snapshot import AnalysisSnapshot

SAMPLE_RATE = 22_050
HOP_LENGTH = 512


def _per_second_mean(values: np.ndarray, times: np.ndarray, n_bins: int) -> list[float]:
    bins = np.minimum(np.floor(times).astype(int), n_bins - 1)
    sums = np.bincount(bins, weights=values, minlength=n_bins)
    counts = np.bincount(bins, minlength=n_bins)
    return [round(float(s / c), 4) if c else 0.0 for s, c in zip(sums, counts)]


def measure_audio(y: np.ndarray, sr: int) -> AnalysisSnapshot:
    """모노 신호 y(샘플레이트 sr)를 측정해 AnalysisSnapshot 을 돌려준다."""
    duration = float(len(y)) / sr
    n_bins = max(1, math.ceil(duration))

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, hop_length=HOP_LENGTH)
    beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP_LENGTH)

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
