import json

import numpy as np
import pytest
import soundfile as sf

from stage_director.analysis.cli import main
from stage_director.analysis.measure import measure_audio, measure_file
from stage_director.analysis.snapshot import parse_analysis, section_energy_ratio

SR = 22_050


def click_track(bpm: float, seconds: float, amplitude: float = 0.8) -> np.ndarray:
    """bpm 간격으로 짧은 감쇠 사인 클릭을 놓은 합성 신호. 난수 없음."""
    y = np.zeros(int(SR * seconds), dtype=np.float32)
    click_t = np.arange(int(0.03 * SR)) / SR
    click = (amplitude * np.sin(2 * np.pi * 1000 * click_t) * np.exp(-click_t * 150)).astype(np.float32)
    step = 60.0 / bpm
    for k in range(int(seconds / step)):
        start = int(k * step * SR)
        y[start : start + len(click)] += click[: len(y) - start]
    return y


def tone(seconds: float, amplitude: float, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_bpm_of_a_120_bpm_click_track_is_close_to_120():
    snapshot = measure_audio(click_track(120, 12), SR)
    assert snapshot.bpm == pytest.approx(120, abs=5)


def test_beats_are_roughly_half_a_second_apart_at_120_bpm():
    snapshot = measure_audio(click_track(120, 12), SR)
    gaps = np.diff(snapshot.beats_sec)
    assert len(gaps) > 5
    assert np.median(gaps) == pytest.approx(0.5, abs=0.05)


def test_duration_is_measured_from_the_signal_length():
    assert measure_audio(click_track(120, 12), SR).duration_sec == pytest.approx(12, abs=0.01)


def test_energy_curve_has_one_value_per_second_and_follows_loudness():
    y = np.concatenate([tone(4, 0.05), tone(4, 0.5)])
    snapshot = measure_audio(y, SR)
    assert len(snapshot.energy_curve) == 8
    quiet, loud = snapshot.energy_curve[:3], snapshot.energy_curve[5:]  # 경계 근처 1초씩은 제외
    assert max(quiet) < min(loud)


def test_section_ratio_of_a_quiet_then_loud_track_is_below_and_above_one():
    y = np.concatenate([tone(4, 0.05), tone(4, 0.5)])
    curve = measure_audio(y, SR).energy_curve
    assert section_energy_ratio(curve, 0, 4) < 1.0 < section_energy_ratio(curve, 4, 8)


def test_onset_density_has_one_count_per_second_and_finds_the_clicks():
    snapshot = measure_audio(click_track(120, 8), SR)
    assert len(snapshot.onset_density) == 8
    assert sum(snapshot.onset_density) >= 10  # 클릭 16개 중 대부분을 잡는다


def test_silence_does_not_crash_and_has_zero_energy():
    snapshot = measure_audio(np.zeros(SR * 3, dtype=np.float32), SR)
    assert snapshot.energy_curve == [0.0, 0.0, 0.0]
    assert snapshot.duration_sec == pytest.approx(3, abs=0.01)


def test_measure_file_reads_a_wav(tmp_path):
    path = tmp_path / "clicks.wav"
    sf.write(path, click_track(120, 8), SR)
    assert measure_file(path).duration_sec == pytest.approx(8, abs=0.05)


def test_cli_prints_json_that_the_defensive_parser_accepts(tmp_path, capsys):
    path = tmp_path / "clicks.wav"
    sf.write(path, click_track(120, 8), SR)
    assert main([str(path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert set(printed) == {"durationSec", "bpm", "beatsSec", "energyCurve", "onsetDensity"}
    assert parse_analysis(printed).duration_sec == pytest.approx(8, abs=0.05)
