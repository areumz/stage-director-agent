"""JobStore 계약 테스트. 메모리 구현은 항상, Postgres 구현은 -m postgres 로만 돈다.

실행: docker compose up -d checkpointer-db && uv run pytest tests/test_jobs.py -m postgres -q
"""

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from stage_director.checkpointer import postgres_checkpointer
from stage_director.jobs import InMemoryJobStore, PostgresJobStore

DATABASE_URL = "postgresql://stage_director:stage_director@localhost:5433/stage_director_checkpoints"


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def store(request):
    if request.param == "memory":
        yield InMemoryJobStore()
    else:
        with postgres_checkpointer(DATABASE_URL) as saver:
            yield PostgresJobStore(saver.conn)


def new_id() -> str:
    return f"test-{uuid.uuid4()}"


def test_create_starts_running_and_is_idempotent(store):
    job_id = new_id()
    assert store.create(job_id) is True
    assert store.create(job_id) is False
    job = store.get(job_id)
    assert (job.status, job.kind, job.result, job.error) == ("running", "graph", None, None)


def test_get_unknown_returns_none(store):
    assert store.get(new_id()) is None


def test_transition_only_applies_from_an_allowed_status(store):
    job_id = new_id()
    store.create(job_id)
    assert store.transition(job_id, from_={"running"}, to="waiting_input") is True
    assert store.transition(job_id, from_={"running"}, to="done") is False  # 이미 running 이 아니다
    assert store.get(job_id).status == "waiting_input"
    assert store.transition(new_id(), from_={"running"}, to="done") is False  # 없는 작업


def test_transition_stores_result_or_error(store):
    done, failed = new_id(), new_id()
    store.create(done)
    store.create(failed)
    store.transition(done, from_={"running"}, to="done", result={"items": [1]})
    store.transition(failed, from_={"running"}, to="error", error="llm_failed")
    assert store.get(done).result == {"items": [1]} and store.get(done).error is None
    assert store.get(failed).error == "llm_failed" and store.get(failed).result is None


def test_fail_running_marks_only_running_jobs(store):
    running, waiting = new_id(), new_id()
    store.create(running)
    store.create(waiting)
    store.transition(waiting, from_={"running"}, to="waiting_input")
    assert store.fail_running() >= 1
    assert (store.get(running).status, store.get(running).error) == ("error", "interrupted")
    assert store.get(waiting).status == "waiting_input"


def test_stale_filters_by_status_and_age(store):
    job_id = new_id()
    store.create(job_id)
    now = datetime.now(UTC)
    assert job_id in store.stale({"running"}, now + timedelta(hours=1))
    assert job_id not in store.stale({"running"}, now - timedelta(hours=1))  # 아직 최근이다
    assert job_id not in store.stale({"done"}, now + timedelta(hours=1))  # 상태가 다르다


def test_delete_removes_the_row(store):
    job_id = new_id()
    store.create(job_id)
    store.delete(job_id)
    assert store.get(job_id) is None
    store.delete(job_id)  # 없어도 에러 없음


def test_only_one_of_many_concurrent_transitions_wins(store):
    job_id = new_id()
    store.create(job_id)
    store.transition(job_id, from_={"running"}, to="waiting_input")
    wins: list[bool] = []
    barrier = threading.Barrier(8)

    def attempt():
        barrier.wait()
        wins.append(store.transition(job_id, from_={"waiting_input"}, to="running"))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1  # 더블 클릭 방어의 근거
