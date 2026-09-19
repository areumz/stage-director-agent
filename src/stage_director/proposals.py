"""그래프 상태의 proposals 필드용 병합 (스펙 §6.3). 순수 함수만 둔다.

proposals 는 {구간 idx: SequenceItem}. 두 가지 병합이 있다.
- merge_proposals: LangGraph 리듀서. Send 로 병렬 실행된 노드들의 결과를 idx 단위로 합친다.
- apply_regeneration: 부분 재생성. 피드백 라우팅이 지정한 targets 밖의 구간은 바이트 단위로 그대로다.
"""

from stage_director.sequence import SequenceItem

Proposals = dict[int, SequenceItem]


def merge_proposals(left: Proposals | None, right: Proposals | None) -> Proposals:
    """LangGraph 리듀서. 같은 idx 는 right 가 이긴다. 입력은 수정하지 않는다."""
    return {**(left or {}), **(right or {})}


def apply_regeneration(existing: Proposals, regenerated: Proposals, targets: set[int]) -> Proposals:
    """targets 구간만 regenerated 로 교체한 새 dict 를 돌려준다. 나머지 구간은 같은 객체를 유지한다.

    regenerated 에 targets 밖의 idx 가 있거나 targets 의 idx 가 빠져 있으면 ValueError.
    """
    extra = set(regenerated) - targets
    if extra:
        raise ValueError(f"재생성 결과에 대상이 아닌 구간이 있다: {sorted(extra)}")
    missing = targets - set(regenerated)
    if missing:
        raise ValueError(f"재생성 결과에 대상 구간이 빠져 있다: {sorted(missing)}")
    return {**existing, **regenerated}
