import json

from stage_director.analysis.snapshot import AnalysisSnapshot, parse_analysis, section_energy_ratio


def test_parse_restores_a_valid_camel_case_snapshot():
    raw = {
        "durationSec": 12.5,
        "bpm": 120.0,
        "beatsSec": [0.5, 1.0],
        "energyCurve": [0.1, 0.2],
        "onsetDensity": [2, 3],
    }
    parsed = parse_analysis(raw)
    assert parsed == AnalysisSnapshot(duration_sec=12.5, bpm=120.0, beats_sec=[0.5, 1.0], energy_curve=[0.1, 0.2], onset_density=[2, 3])


def test_parse_round_trips_through_json():
    original = AnalysisSnapshot(duration_sec=3, bpm=100, beats_sec=[0.6], energy_curve=[0.1, 0.2, 0.3], onset_density=[1, 0, 2])
    raw = json.loads(original.model_dump_json(by_alias=True))
    assert parse_analysis(raw) == original


def test_parse_returns_defaults_when_not_an_object():
    for garbage in (None, "nope", 3, [1, 2]):
        assert parse_analysis(garbage) == AnalysisSnapshot()


def test_parse_keeps_valid_fields_when_one_field_is_broken():
    raw = {"durationSec": 10, "bpm": "fast", "beatsSec": "nope", "energyCurve": [0.1, 0.2], "onsetDensity": None}
    parsed = parse_analysis(raw)
    assert parsed.duration_sec == 10
    assert parsed.bpm == 0.0
    assert parsed.beats_sec == []
    assert parsed.energy_curve == [0.1, 0.2]
    assert parsed.onset_density == []


def test_parse_keeps_per_second_positions_by_zeroing_bad_elements():
    # energyCurve/onsetDensity 는 인덱스가 곧 시각이다. 원소를 버리면 뒤의 초가 앞으로 당겨져 구간 에너지 비가 틀어진다
    raw = {"energyCurve": [0.1, "x", None, True, float("nan"), float("inf"), 0.4], "onsetDensity": [2, None, 3]}
    parsed = parse_analysis(raw)
    assert parsed.energy_curve == [0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.4]
    assert parsed.onset_density == [2.0, 0.0, 3.0]


def test_parse_drops_bad_beat_timestamps_instead_of_inventing_beats_at_zero():
    assert parse_analysis({"beatsSec": [0.5, "x", None, 1.0]}).beats_sec == [0.5, 1.0]


def test_parse_survives_ints_too_large_for_a_float():
    # 파이썬 int 는 크기 제한이 없어 float 로 바꾸면 OverflowError. 예외 없이 기본값으로 떨어져야 한다
    huge = 10**400
    parsed = parse_analysis({"durationSec": 10, "bpm": huge, "beatsSec": [0.5, huge], "energyCurve": [0.1, huge], "onsetDensity": [huge]})
    assert parsed.duration_sec == 10
    assert parsed.bpm == 0.0
    assert parsed.beats_sec == [0.5]
    assert parsed.energy_curve == [0.1, 0.0]
    assert parsed.onset_density == [0.0]


def test_parse_ignores_non_finite_scalars():
    assert parse_analysis({"bpm": float("nan"), "durationSec": float("inf")}) == AnalysisSnapshot()


def test_section_ratio_is_the_section_mean_over_the_track_mean():
    curve = [1.0, 1.0, 1.0, 1.0, 4.0, 4.0]  # 곡 평균 2.0
    assert section_energy_ratio(curve, 4, 6) == 2.0
    assert section_energy_ratio(curve, 0, 4) == 0.5


def test_section_ratio_counts_a_second_by_its_center():
    curve = [1.0, 3.0]  # 곡 평균 2.0. 1초 구간 0 의 중심은 0.5, 구간 1 의 중심은 1.5
    assert section_energy_ratio(curve, 0.4, 1.4) == 0.5  # 중심 0.5 만 든다
    assert section_energy_ratio(curve, 0.6, 2.0) == 1.5  # 중심 1.5 만 든다


def test_section_ratio_is_one_for_silence_or_missing_data():
    assert section_energy_ratio([], 0, 10) == 1.0
    assert section_energy_ratio([0.0, 0.0], 0, 2) == 1.0
    assert section_energy_ratio([1.0, 2.0], 50, 60) == 1.0
