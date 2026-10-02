"""시퀀스 그래프 상태 타입. LangGraph 가 이 TypedDict 를 보고 필드별 리듀서를 고른다.

Annotated 가 없는 필드는 기본 리듀서(마지막으로 쓴 값으로 교체)를 쓴다 (스펙 §6.3 "교체").
"""

from typing import Annotated, TypedDict

from stage_director.models import Issue, Section, SectionProposal, SequenceRequest
from stage_director.proposals import merge_proposals
from stage_director.sequence import SequenceItem


class ProposeTask(TypedDict):
    """Send 로 propose 노드에 전달하는 입력. 그래프 전체 상태(GraphState)와는 다른, 이 노드 전용 모양."""

    idx: int
    section: Section
    request: SequenceRequest


class GraphState(TypedDict, total=False):
    request: SequenceRequest  # 시작 시 1회 설정, 이후 불변
    sections: list[Section]  # detect 노드가 교체
    # idx 단위 병합. merge_proposals 는 값 타입을 가리지 않으므로(dict 를 합칠 뿐) SequenceItem 대신
    # SectionProposal 을 담아 energy_ratio·issues 를 assemble 의 곡 전체 재검사까지 들고 간다.
    proposals: Annotated[dict[int, SectionProposal], merge_proposals]
    regen_round: int  # assemble 이 실행될 때마다 1씩 증가 (몇 번째 조립인지)
    regen_targets: set[int]  # 가장 최근 assemble 이 재생성이 필요하다고 판단한 구간
    final_items: list[SequenceItem]  # assemble 이 실행될 때마다 최신값으로 덮어씀
    final_issues: list[Issue]
