"""시퀀스 그래프의 노드와 라우팅 함수. 기존 propose_section/run_gate/validate_sequence 를 그대로 조합한다."""

from langgraph.graph import END
from langgraph.types import Send

from stage_director.analysis.sections import detect_sections
from stage_director.analysis.snapshot import parse_analysis
from stage_director.gate import indices_to_regenerate, run_gate
from stage_director.graph_state import GraphState, ProposeTask
from stage_director.llm.client import LLMClient
from stage_director.models import Issue, ProposeRequest
from stage_director.propose import propose_section
from stage_director.sequence import validate_sequence

MAX_SECTION_REGEN = 2  # 스펙 §7: 위반 구간은 최대 2회 자동 재생성

# ponytail: Send 팬아웃이 구간 수만큼 한꺼번에 LLM 을 부르면 Gemini 무료 티어의 분당 요청 한도를
# 바로 넘길 수 있다. invoke(config={"max_concurrency": ...})로 한 슈퍼스텝의 동시 실행 수만 묶는
# 가장 단순한 완화책이며, 분당 요청 수(RPM)를 정확히 지키는 진짜 속도 제한·백오프는 아니다.
# 필요해지면(실제 과금 티어가 정해지면) 토큰 버킷 등으로 올린다.
MAX_CONCURRENT_PROPOSALS = 3


def detect_node(state: GraphState) -> dict:
    req = state["request"]
    snapshot = parse_analysis(req.analysis)
    return {"sections": detect_sections(snapshot.energy_curve, req.duration_sec)}


def fan_out_initial(state: GraphState) -> list[Send]:
    req = state["request"]
    return [Send("propose", ProposeTask(idx=i, section=s, request=req)) for i, s in enumerate(state["sections"])]


def make_propose_node(llm: LLMClient):
    """llm 을 클로저로 주입한 propose 노드를 만든다. FastAPI create_app(llm=...)과 같은 패턴."""

    def propose_node(task: ProposeTask) -> dict:
        req = task["request"]
        propose_req = ProposeRequest(
            track=req.track, artist=req.artist, presets=req.presets, analysis=req.analysis, section=task["section"]
        )
        proposal = propose_section(llm, propose_req)
        return {"proposals": {task["idx"]: proposal}}

    return propose_node


def assemble_node(state: GraphState) -> dict:
    req = state["request"]
    proposals = state["proposals"]
    order = sorted(proposals)
    items = [proposals[i].item for i in order]
    energy_ratios = [proposals[i].energy_ratio for i in order]

    # clamped/invalid_state 는 각 구간의 sanitize_state 결과다. run_gate 가 다시 내지 않으므로 그대로 들고 간다.
    # proposals[i].issues 자체의 idx 는 propose_section 내부 싱글 아이템 gate 호출의 산물이라 항상 0 —
    # 여기서 바깥 루프의 진짜 구간 번호 i 를 붙여야 사람 리뷰 UI 가 구간을 가리킬 수 있다.
    sanitize_issues = [
        Issue(rule=iss.rule, message=iss.message, idx=i)
        for i in order
        for iss in proposals[i].issues
        if iss.rule in ("clamped", "invalid_state")
    ]
    gate_issues = run_gate(items, energy_ratios, req.artist.color)
    seq_violations = validate_sequence(items, req.duration_sec)

    # invalid_state 는 run_gate(곡 전체 재검사)가 알지 못하는, 구간 자체의 sanitize_state 결과다.
    # gate.py 의 의도대로 재생성 대상이 되려면 여기서 직접 regen_targets 에 합쳐야 한다.
    broken = {i for i in order if any(iss.rule == "invalid_state" for iss in proposals[i].issues)}

    issues = sanitize_issues + [Issue(rule=i.rule, message=i.message, idx=i.idx) for i in gate_issues]
    issues += [Issue(rule=f"sequence_{v.code}", message=v.message, idx=v.idx) for v in seq_violations]

    return {
        "final_items": items,
        "final_issues": issues,
        "regen_targets": indices_to_regenerate(gate_issues) | broken,
        "regen_round": state.get("regen_round", 0) + 1,
    }


def decide_regen(state: GraphState):
    targets = state.get("regen_targets") or set()
    if targets and state["regen_round"] <= MAX_SECTION_REGEN:
        req, sections = state["request"], state["sections"]
        return [Send("propose", ProposeTask(idx=i, section=sections[i], request=req)) for i in sorted(targets)]
    return END
