"""구간 하나 연출 노드: 구간 + 분석 + 아티스트 컨텍스트 -> StageState 1개(SequenceItem).

LLM 만 주입받는 순수 함수. LangGraph 도 FastAPI 도 모름. 이후 그래프가 이 함수를 Send 노드로 감쌈.
"""

import json
from typing import Any

from contracts.stage_state import default_stage_state
from stage_director.analysis.snapshot import parse_analysis, section_energy_ratio
from stage_director.gate import run_gate, sanitize_state
from stage_director.llm.client import LLMClient, LLMError
from stage_director.models import Issue, ProposeRequest, SectionProposal
from stage_director.prompts import PROPOSAL_SCHEMA, SYSTEM_PROMPT
from stage_director.sequence import SequenceItem

MAX_RETRIES = 2  # 스펙 §8: 노드 단위 재시도 2회 (최초 시도 + 2회)
DEFAULT_TRANSITION_MS = 2000  # 구간이 이보다 짧으면 구간 길이로 줄인다
MAX_PRESETS_IN_PROMPT = 10  # 프리셋이 많아도 프롬프트가 커지지 않게 앞에서부터 자른다


def _without_penumbra(state: Any) -> Any:
    """에이전트는 penumbra 를 바꾸지 않음 (스펙 §2). 모델이 보내도 버려서 기본값이 남게 함."""
    if not isinstance(state, dict) or not isinstance(state.get("spots"), dict):
        return state
    spots = {k: ({f: v for f, v in s.items() if f != "penumbra"} if isinstance(s, dict) else s) for k, s in state["spots"].items()}
    return {**state, "spots": spots}


def _user_prompt(req: ProposeRequest, bpm: float, energy_ratio: float, onset_ratio: float, presets: list[tuple[str, dict]]) -> str:
    a, s, t = req.artist, req.section, req.track
    lines = [
        "## 곡",
        f"제목: {t.title}",
        f"장르: {t.genre or '-'}",
        f"무드 키워드: {', '.join(t.mood_keywords) or '-'}",
        "",
        "## 아티스트",
        f"{a.name}({a.name_ko}) 시그니처 컬러 {a.color}, 셰이더 {a.shader.pattern}(freq {a.shader.freq:g}, falloff {a.shader.falloff:g}, speed {a.shader.speed:g})",
        "",
        "## 기존 프리셋",
        *(f"- {name}: {json.dumps(state, ensure_ascii=False)}" for name, state in presets),
        *([] if presets else ["(없음)"]),
        "",
        "## 구간",
        f"라벨 {s.label}, {s.start_sec:.1f}초 ~ {s.end_sec:.1f}초 (길이 {s.end_sec - s.start_sec:.1f}초)",
        *([f"분위기: {s.mood}"] if s.mood else []),
        "",
        "## 측정값",
        f"BPM {bpm:g}",
        f"에너지 비 {energy_ratio:.2f} (구간 평균 에너지 / 곡 평균 에너지)",
        f"온셋 밀도 비 {onset_ratio:.2f} (구간 평균 / 곡 평균)",
    ]
    if req.feedback:
        lines += ["", "## 사용자 피드백 (수정 요청)"]
        if req.previous:
            previous = req.previous.model_dump(mode="json", include={"state", "rationale"})
            lines.append(f"이전 제안: {json.dumps(previous, ensure_ascii=False)}")
        lines.append(f"피드백: {req.feedback}")
    return "\n".join(lines)


def _generate(llm: LLMClient, user: str) -> Any:
    for attempt in range(MAX_RETRIES + 1):
        try:
            return llm.generate_json(system=SYSTEM_PROMPT, user=user, schema=PROPOSAL_SCHEMA)
        except LLMError:
            if attempt == MAX_RETRIES:
                raise


def propose_section(llm: LLMClient, req: ProposeRequest) -> SectionProposal:
    """구간 하나의 연출을 제안. LLM 이 재시도 후에도 실패하면 LLMError.

    LLM 출력은 믿지 않는다: 필드별 병합과 clamp(sanitize_state)를 거치고 게이트 규칙을 적용해
    문제를 issues 로 돌려준다. 자동 재생성은 하지 않는다(그래프의 몫).
    """
    s = req.section
    fallback = default_stage_state(req.artist.color)
    snapshot = parse_analysis(req.analysis)
    energy_ratio = section_energy_ratio(snapshot.energy_curve, s.start_sec, s.end_sec)
    onset_ratio = section_energy_ratio(snapshot.onset_density, s.start_sec, s.end_sec)  # 1초 단위 곡선이면 무엇이든 같은 계산

    presets = [(p.name, sanitize_state(p.state, fallback, 0)[0].model_dump(mode="json")) for p in req.presets[:MAX_PRESETS_IN_PROMPT]]
    raw = _generate(llm, _user_prompt(req, snapshot.bpm, energy_ratio, onset_ratio, presets))
    if not isinstance(raw, dict):
        raw = {}

    state, issues = sanitize_state(_without_penumbra(raw.get("state")), fallback, 0)
    rationale = raw.get("rationale")
    item = SequenceItem(
        section_label=s.label,
        start_sec=s.start_sec,
        end_sec=s.end_sec,
        transition_ms=int(min(DEFAULT_TRANSITION_MS, (s.end_sec - s.start_sec) * 1000)),
        state=state,
        rationale=rationale if isinstance(rationale, str) else "",
    )
    issues += run_gate([item], [energy_ratio], req.artist.color)
    return SectionProposal(item=item, energy_ratio=energy_ratio, issues=[Issue(rule=i.rule, message=i.message) for i in issues])
