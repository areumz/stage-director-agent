import itertools

from stage_director.analysis.sections import (
    MAX_SECTIONS,
    MIN_SECTION_SEC,
    detect_sections,
)


def flat(duration: int, value: float = 0.5) -> list[float]:
    return [value] * duration


def step(*levels_and_lengths: tuple[float, int]) -> list[float]:
    curve: list[float] = []
    for value, length in levels_and_lengths:
        curve += [value] * length
    return curve


def test_short_song_is_a_single_section():
    sections = detect_sections(flat(10), duration_sec=10)
    assert len(sections) == 1
    assert (sections[0].start_sec, sections[0].end_sec, sections[0].label) == (0, 10, "intro")


def test_missing_energy_curve_is_a_single_section():
    sections = detect_sections([], duration_sec=60)
    assert len(sections) == 1
    assert (sections[0].start_sec, sections[0].end_sec) == (0, 60)


def test_silent_track_is_a_single_section():
    # 평균 에너지가 0이면 비교 기준이 없다. 0으로 나누기 없이 구간 하나로 처리해야 한다.
    sections = detect_sections(flat(40, value=0.0), duration_sec=40)
    assert len(sections) == 1


def test_two_level_step_produces_one_boundary():
    curve = step((0.1, 20), (0.9, 20))
    sections = detect_sections(curve, duration_sec=40)
    assert len(sections) == 2
    assert 18 <= sections[0].end_sec <= 22
    assert sections[0].end_sec == sections[1].start_sec
    assert sections[0].label == "intro" and sections[1].label == "outro"


def test_three_level_step_produces_two_boundaries():
    curve = step((0.1, 15), (0.9, 15), (0.2, 15))
    sections = detect_sections(curve, duration_sec=45)
    assert len(sections) == 3
    assert sections[0].label == "intro"
    assert sections[1].label == "chorus"  # 곡 평균보다 훨씬 큰 구간
    assert sections[2].label == "outro"


def test_small_fluctuations_below_threshold_are_ignored():
    curve = [0.5 + (0.01 if i % 2 == 0 else -0.01) for i in range(40)]
    sections = detect_sections(curve, duration_sec=40)
    assert len(sections) == 1


def test_minimum_section_length_is_respected():
    curve = step((0.1, 12), (0.9, 2), (0.1, 2), (0.95, 24))
    sections = detect_sections(curve, duration_sec=40)
    for s in sections:
        assert s.end_sec - s.start_sec >= MIN_SECTION_SEC - 1e-6


def test_section_count_is_capped():
    # 10초 블록으로 0.1/0.9 를 30번 번갈아(300초) 켜면 JUMP_WINDOW_SEC(4초) 창 기준으로 매 경계마다
    # 진짜 에너지 점프가 생겨 MAX_SECTIONS 를 훌쩍 넘는 원시 후보가 나온다 — 상한이 실제로 작동해야
    # 통과한다(2초 주기 신호는 JUMP_WINDOW_SEC 창에서 좌우 평균이 같아져 후보가 0개가 되므로 쓰지 않는다).
    curve = sum(([0.1 if i % 2 == 0 else 0.9] * 10 for i in range(30)), [])
    sections = detect_sections(curve, duration_sec=300)
    assert len(sections) == MAX_SECTIONS


def test_sections_cover_whole_song_contiguously():
    curve = step((0.1, 15), (0.9, 15), (0.2, 15), (0.8, 15))
    sections = detect_sections(curve, duration_sec=60)
    assert sections[0].start_sec == 0
    assert sections[-1].end_sec == 60
    for a, b in itertools.pairwise(sections):
        assert a.end_sec == b.start_sec


def test_zero_duration_does_not_throw():
    # 예외를 던지지 않는다 — duration_sec <= 0 도 안전하게 처리
    sections = detect_sections([], duration_sec=0)
    assert len(sections) == 1
    assert sections[0].start_sec == 0
    # start_sec < end_sec 를 보장하기 위해 clamping됨
    assert sections[0].end_sec > 0


def test_negative_duration_does_not_throw():
    # 예외를 던지지 않는다 — 음수 duration도 안전하게 처리
    sections = detect_sections([], duration_sec=-10)
    assert len(sections) == 1
    assert sections[0].start_sec == 0
    # 음수 duration도 처리 가능하도록 안전하게 보정됨
    assert sections[0].end_sec > 0
