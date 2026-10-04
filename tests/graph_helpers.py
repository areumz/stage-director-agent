"""interrupt 가 있는 그래프를 테스트에서 끝까지 돌리는 헬퍼."""

from langgraph.types import Command


def default_answer(value: dict) -> dict:
    """interrupt 마다 가장 평범한 답: 구간은 그대로 확인, 리뷰는 승인."""
    if value["kind"] == "confirm_sections":
        return {"sections": value["sections"]}
    return {"action": "approve"}


def drive(graph, graph_input, config, answer=default_answer) -> dict:
    """interrupt 마다 answer(value) 로 재개하며 그래프가 끝날 때까지 돌리고 최종 상태 값을 돌려준다."""
    result = graph.invoke(graph_input, config)
    while "__interrupt__" in result:
        result = graph.invoke(Command(resume=answer(result["__interrupt__"][0].value)), config)
    return graph.get_state(config).values
