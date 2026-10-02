"""에너지 곡선 기반 구간 경계 휴리스틱 (스펙 §13).

all-in-one 류 구조 분석 모델은 쓰지 않는다. 설치 위험(PyTorch 등 무거운 의존성, numpy 2.5 +
Python 3.12 조합에서 막힐 수 있음)이 이 휴리스틱만 시도해 볼 근거다. 진짜 음악 구조(벌스/코러스)
인식이 아니라 에너지 레벨 변화 지점을 구간 경계로 쓰는 근사치이며, label 도 내용 인식이 아니라
상대적 에너지 수준에서 따온 이름이다. 아래 상수는 초기값이며, 실제 곡 2개로 듣고 조정하는 단계는
이 계획의 Task 6(사람 단계)에서 한다 — 그때 이 docstring과 상수가 바뀔 수 있다.
"""

from stage_director.models import Section

MIN_SECTION_SEC = 8.0  # 이보다 짧은 구간은 만들지 않는다. 실제 곡 스파이크에서 조정
JUMP_WINDOW_SEC = 4  # 경계 후보 좌우로 비교하는 창 크기(초)
BOUNDARY_JUMP_THRESHOLD = 0.35  # 좌우 평균 에너지 차(곡 평균 대비 비율)가 이보다 크면 경계 후보
MAX_SECTIONS = 12  # 노이즈로 구간이 과도하게 쪼개져 LLM 호출이 폭증하는 것을 막는 상한


def _window_mean(curve: list[float], start: int, end: int) -> float:
    window = curve[max(0, start) : max(0, end)]
    return sum(window) / len(window) if window else 0.0


def _boundary_candidates(curve: list[float], duration_sec: float) -> list[tuple[int, float]]:
    """(시각, jump 점수) 후보. 시간순이며 점수로 정렬하지 않는다."""
    n = len(curve)
    track_mean = sum(curve) / n if n else 0.0
    if track_mean <= 0:
        return []
    candidates = []
    t = int(MIN_SECTION_SEC)
    last_t = int(duration_sec - MIN_SECTION_SEC)
    while t <= last_t and t < n:
        jump = abs(_window_mean(curve, t, t + JUMP_WINDOW_SEC) - _window_mean(curve, t - JUMP_WINDOW_SEC, t))
        if jump / track_mean > BOUNDARY_JUMP_THRESHOLD:
            candidates.append((t, jump))
        t += 1
    return candidates


def _suppress_close_candidates(candidates: list[tuple[int, float]]) -> list[int]:
    """MIN_SECTION_SEC 보다 가까운 후보끼리는 점수가 높은 것만 남긴다(비최대 억제)."""
    chosen: list[tuple[int, float]] = []
    for t, score in candidates:
        near = [c for c in chosen if abs(c[0] - t) < MIN_SECTION_SEC]
        if not near:
            chosen.append((t, score))
        elif score > max(s for _, s in near):
            chosen = [c for c in chosen if c not in near] + [(t, score)]
    # ponytail: 상한을 넘으면 앞에서부터 자른다. 점수 상위 N개를 고르는 게 더 정확하지만
    # 노이즈가 이 정도로 많은 곡은 드물고, 생기면 사람 스파이크에서 임계값을 조정하는 쪽이 간단하다.
    return sorted(t for t, _ in chosen)[: MAX_SECTIONS - 1]


def _label_for(idx: int, last_idx: int, energy_ratio: float) -> str:
    if idx == 0:
        return "intro"
    if idx == last_idx:
        return "outro"
    return "chorus" if energy_ratio > 1.0 else "verse"


def detect_sections(energy_curve: list[float], duration_sec: float) -> list[Section]:
    """에너지 곡선에서 구간 경계를 찾아 Section 목록을 돌려준다. 항상 길이 1 이상이며 이어진다.

    곡이 2*MIN_SECTION_SEC 보다 짧거나 에너지 곡선이 비어 있으면(분석 없음) 곡 전체를 구간 하나로 본다.
    """
    if duration_sec <= 2 * MIN_SECTION_SEC or not energy_curve:
        return [Section(label="intro", start_sec=0.0, end_sec=duration_sec)]

    boundaries = _suppress_close_candidates(_boundary_candidates(energy_curve, duration_sec))
    edges = [0.0, *(float(b) for b in boundaries), duration_sec]
    track_mean = sum(energy_curve) / len(energy_curve)

    last_idx = len(edges) - 2
    sections = []
    for i in range(last_idx + 1):
        start, end = edges[i], edges[i + 1]
        inside = [v for j, v in enumerate(energy_curve) if start <= j + 0.5 < end]
        ratio = (sum(inside) / len(inside)) / track_mean if inside and track_mean > 0 else 1.0
        sections.append(Section(label=_label_for(i, last_idx, ratio), start_sec=start, end_sec=end))
    return sections
