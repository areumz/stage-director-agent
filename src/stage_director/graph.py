"""시퀀스 그래프 조립. build_sequence_graph(llm, checkpointer) 가 LLM 과 체크포인터를 주입받는다.

LangGraph 도 FastAPI 도 모르는 propose_section 과 달리, 이 파일은 LangGraph 를 안다 — 그래프 자체이기 때문이다.
"""

from langgraph.graph import END, START, StateGraph

from stage_director.graph_nodes import (
    assemble_node,
    decide_regen,
    detect_node,
    fan_out_initial,
    make_propose_node,
)
from stage_director.graph_state import GraphState
from stage_director.llm.client import LLMClient


def build_sequence_graph(llm: LLMClient, checkpointer=None):
    graph = StateGraph(GraphState)
    graph.add_node("detect", detect_node)
    graph.add_node("propose", make_propose_node(llm))
    graph.add_node("assemble", assemble_node)

    graph.add_edge(START, "detect")
    graph.add_conditional_edges("detect", fan_out_initial, ["propose"])
    graph.add_edge("propose", "assemble")
    graph.add_conditional_edges("assemble", decide_regen, ["propose", END])

    return graph.compile(checkpointer=checkpointer)
