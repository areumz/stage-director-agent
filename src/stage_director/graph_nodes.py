"""시퀀스 그래프의 노드와 라우팅 함수. 기존 propose_section/run_gate/validate_sequence 를 그대로 조합"""

from collections.abc import Callable

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from langgraph.types import Send, interrupt

from stage_director.analysis.sections import detect_sections
from stage_director.analysis.snapshot import parse_analysis
from stage_director.audio import AudioError, fetch_audio
from stage_director.gate import indices_to_regenerate, run_gate
from stage_director.graph_state import GraphState, ProposeTask
from stage_director.llm.client import LLMClient
from stage_director.models import Issue, ProposeRequest, Section
from stage_director.mood import interpret_moods
from stage_director.propose import propose_section
from stage_director.sequence import validate_sequence

MAX_SECTION_REGEN = 2  # 스펙 §7: 위반 구간은 최대 2회 자동 재생성

# ponytail: Send 팬아웃이 구간 수만큼 한꺼번에 LLM 을 부르면 Gemini 무료 티어의 분당 요청 한도를
# 바로 넘길 수 있다. invoke(config={"max_concurrency": ...})로 한 슈퍼스텝의 동시 실행 수만 묶는
# 가장 단순한 완화책이며, 분당 요청 수(RPM)를 정확히 지키는 진짜 속도 제한·백오프는 x
# 필요해지면 토큰 버킷 등으로 올릴 것
MAX_CONCURRENT_PROPOSALS = 3


def detect_node(state: GraphState) -> dict:
    req = state["request"]
    snapshot = parse_analysis(req.analysis)
    return {"sections": detect_sections(snapshot.energy_curve, req.duration_sec)}


def interrupt_id(config: RunnableConfig, revision: int, kind: str) -> str:
    """스펙 §6.3: 낡은 화면에서 온 resume 을 거부하기 위한 id."""
    return f"{config['configurable']['thread_id']}:{revision}:{kind}"


def make_mood_node(llm: LLMClient, fetch: Callable[[str], tuple[bytes, str]] = fetch_audio):
    """곡 전체 오디오로 구간 무드를 한 번에 해석한다. audioUrl 이 없거나 내려받기에 실패하면 무드 없이 진행(비치명적)."""

    def mood_node(state: GraphState) -> dict:
        req = state["request"]
        if not req.audio_url:
            return {}
        try:
            audio, mime_type = fetch(req.audio_url)
        except AudioError:
            return {}
        moods = interpret_moods(llm, audio, mime_type, state["sections"], req.track)
        return {"sections": [s.model_copy(update={"mood": m}) for s, m in zip(state["sections"], moods)]}

    return mood_node


def confirm_sections_node(state: GraphState, config: RunnableConfig) -> dict:
    """interrupt #1: 사람이 구간 경계·라벨·무드를 확인·수정한다.

    재개하면 이 노드가 처음부터 다시 실행된다 — interrupt() 앞에는 부수효과를 두지 않는다.
    resume 값은 API 계층이 validate_section_edit 로 이미 검증했다.
    """
    revision = state.get("revision", 0)
    answer = interrupt(
        {
            "interruptId": interrupt_id(config, revision, "confirm_sections"),
            "kind": "confirm_sections",
            "sections": [s.model_dump(by_alias=True) for s in state["sections"]],
            "energyCurve": parse_analysis(state["request"].analysis).energy_curve,
            "durationSec": state["request"].duration_sec,
        }
    )
    return {"sections": [Section.model_validate(s) for s in answer["sections"]], "revision": revision + 1}


def _propose_task(state: GraphState, idx: int) -> ProposeTask:
    """초기 제안·자동 재생성·피드백 재생성이 모두 쓰는 단일 생성자. 피드백 턴의 대상 구간이면 피드백과 이전 제안을 붙인다."""
    task = ProposeTask(idx=idx, section=state["sections"][idx], request=state["request"])
    if idx in (state.get("feedback_targets") or set()) and state.get("feedback_log"):
        task["feedback"] = state["feedback_log"][-1]["text"]
        task["previous"] = state["proposals"][idx].item
    return task


def fan_out_initial(state: GraphState) -> list[Send]:
    return [Send("propose", _propose_task(state, i)) for i in range(len(state["sections"]))]


def make_propose_node(llm: LLMClient):
    """llm 을 클로저로 주입한 propose 노드를 만든다. FastAPI create_app(llm=...)과 같은 패턴."""

    def propose_node(task: ProposeTask) -> dict:
        req = task["request"]
        propose_req = ProposeRequest(
            track=req.track,
            artist=req.artist,
            presets=req.presets,
            analysis=req.analysis,
            section=task["section"],
            feedback=task.get("feedback"),
            previous=task.get("previous"),
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

    # clamped/invalid_state 는 각 구간의 sanitize_state 결과. run_gate 가 다시 내지 않으므로 그대로 들고감.
    # proposals[i].issues 자체의 idx 는 propose_section 내부 싱글 아이템 gate 호출의 산물이라 항상 0 —
    # 여기서 바깥 루프의 진짜 구간 번호 i 를 붙여야 사람 리뷰 UI 가 구간을 가리킬 수 있음.
    sanitize_issues = [
        Issue(rule=iss.rule, message=iss.message, idx=i)
        for i in order
        for iss in proposals[i].issues
        if iss.rule in ("clamped", "invalid_state")
    ]
    gate_issues = run_gate(items, energy_ratios, req.artist.color)
    seq_violations = validate_sequence(items, req.duration_sec)

    # invalid_state 는 run_gate(곡 전체 재검사)가 알지 못하는, 구간 자체의 sanitize_state 결과.
    # gate.py 의 의도대로 재생성 대상이 되려면 여기서 직접 regen_targets 에 합쳐야함
    broken = {i for i in order if any(iss.rule == "invalid_state" for iss in proposals[i].issues)}

    issues = sanitize_issues + [Issue(rule=i.rule, message=i.message, idx=i.idx) for i in gate_issues]
    issues += [Issue(rule=f"sequence_{v.code}", message=v.message, idx=v.idx) for v in seq_violations]

    targets = indices_to_regenerate(gate_issues) | broken
    if state.get("feedback_targets"):
        # 피드백 턴에서는 targets 밖 구간을 다시 만들지 않는다 (스펙 §6.3 부분 재생성 불변식).
        # 그 구간의 위반은 issues 로 남아 사람이 다음 리뷰에서 보게 된다.
        targets &= state["feedback_targets"]

    return {
        "final_items": items,
        "final_issues": issues,
        "regen_targets": targets,
        "regen_round": state.get("regen_round", 0) + 1,
    }


def decide_regen(state: GraphState):
    targets = state.get("regen_targets") or set()
    if targets and state["regen_round"] <= MAX_SECTION_REGEN:
        return [Send("propose", _propose_task(state, i)) for i in sorted(targets)]
    return "review"


def review_node(state: GraphState, config: RunnableConfig) -> dict:
    """interrupt #2: 사람이 구간별 제안을 보고 승인하거나 구간을 골라 피드백한다.

    재개하면 이 노드가 처음부터 다시 실행된다 — interrupt() 앞에는 부수효과를 두지 않는다.
    resume 값(targets 가 유효한 idx 인지 등)은 API 계층이 이미 검증했다.
    """
    revision = state.get("revision", 0)
    answer = interrupt(
        {
            "interruptId": interrupt_id(config, revision, "review"),
            "kind": "review",
            "items": [i.model_dump(by_alias=True, mode="json") for i in state["final_items"]],
            "issues": [i.model_dump(by_alias=True, mode="json") for i in state["final_issues"]],
        }
    )
    if answer["action"] == "feedback":
        targets = sorted(set(answer["targets"]))
        entry = {"turn": len(state.get("feedback_log") or []) + 1, "text": answer["text"], "targets": targets}
        return {
            "revision": revision + 1,
            "feedback_log": [entry],
            "feedback_targets": set(targets),
            "regen_round": 0,  # 피드백 턴마다 자동 재생성 예산을 다시 준다
            "review_action": "feedback",
        }
    return {"revision": revision + 1, "review_action": "approve"}


def decide_review(state: GraphState):
    if state.get("review_action") == "feedback":
        return [Send("propose", _propose_task(state, i)) for i in sorted(state["feedback_targets"])]
    return END
