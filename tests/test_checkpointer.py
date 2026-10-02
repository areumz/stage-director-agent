"""Postgres 체크포인터 통합 테스트. 로컬 docker-compose 가 필요하다.

실행: docker compose up -d checkpointer-db && uv run pytest -m postgres -q && docker compose down
"""

import uuid

import pytest

from stage_director.checkpointer import postgres_checkpointer
from stage_director.graph import build_sequence_graph
from stage_director.llm.fake import FakeLLM
from stage_director.models import Artist, SequenceRequest, Shader, Track

pytestmark = pytest.mark.postgres

DATABASE_URL = "postgresql://stage_director:stage_director@localhost:5433/stage_director_checkpoints"

ARTIST = Artist(slug="aurora", name="AURORA", name_ko="오로라", color="#9F77DD", shader=Shader(pattern="wave", freq=9, falloff=0.75, speed=0.5))
GOOD = {
    "state": {
        "color": "#9F77DD",
        "spots": {k: {"on": True, "intensity": 300, "angle": 0.5} for k in ("left", "center", "right")},
        "camera": "front",
        "smoke": {"density": 0.2, "color": "#ffffff"},
    },
    "rationale": "ok",
}


def request() -> SequenceRequest:
    return SequenceRequest(
        track=Track(title="t"), artist=ARTIST, presets=[],
        analysis={"durationSec": 10, "bpm": 100, "energyCurve": [0.5] * 10}, duration_sec=10,
    )


def test_checkpoint_can_be_reloaded_after_a_new_connection():
    thread_id = str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    with postgres_checkpointer(DATABASE_URL) as saver:
        graph = build_sequence_graph(FakeLLM(GOOD), checkpointer=saver)
        result = graph.invoke({"request": request()}, config=config)

    # 프로세스를 새로 띄운 것처럼 새 PostgresSaver 로 같은 thread_id 를 읽는다
    with postgres_checkpointer(DATABASE_URL) as saver2:
        graph2 = build_sequence_graph(FakeLLM(), checkpointer=saver2)
        state = graph2.get_state(config)

    assert state.values["final_items"] == result["final_items"]


def test_setup_is_idempotent():
    with postgres_checkpointer(DATABASE_URL):
        pass
    with postgres_checkpointer(DATABASE_URL):  # 두 번째도 에러 없이 열려야 한다
        pass
