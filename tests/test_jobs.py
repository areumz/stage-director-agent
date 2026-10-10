"""JobStore 계약 테스트. 메모리 구현은 항상, Postgres 구현은 -m postgres 일때만 실행.

실행: docker compose up -d checkpointer-db && uv run pytest tests/test_jobs.py -m postgres -q
"""

import threading
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from stage_director.checkpointer import postgres_checkpointer
from stage_director.jobs import InMemoryJobStore, PostgresJobStore, migrate

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
    assert (job.status, job.result, job.error) == ("running", None, None)


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


# ── 5단계: kind · progress · queued ──────────────────────────────


def test_create_can_start_queued_and_records_the_kind(store):
    graph, analysis = new_id(), new_id()
    store.create(graph, status="queued")
    store.create(analysis, kind="analysis")
    assert (store.get(graph).status, store.get(graph).kind, store.get(graph).progress) == ("queued", "graph", None)
    assert (store.get(analysis).status, store.get(analysis).kind) == ("running", "analysis")


def test_progress_is_stored(store):
    job_id = new_id()
    store.create(job_id, kind="analysis")
    store.set_progress(job_id, 0.4)
    assert store.get(job_id).progress == pytest.approx(0.4)
    store.set_progress(new_id(), 0.5)  # 없는 작업이어도 에러 없음


def test_stale_filters_by_kind(store):
    graph, analysis = new_id(), new_id()
    store.create(graph)
    store.create(analysis, kind="analysis")
    future = datetime.now(UTC) + timedelta(hours=1)
    assert graph in store.stale({"running"}, future) and analysis not in store.stale({"running"}, future)
    assert analysis in store.stale({"running"}, future, kind="analysis")


def test_fail_running_marks_queued_and_running_jobs_but_not_waiting_or_finished_ones(store):
    queued, running, waiting, done = new_id(), new_id(), new_id(), new_id()
    store.create(queued, status="queued")
    store.create(running)
    store.create(waiting)
    store.transition(waiting, from_={"running"}, to="waiting_input")
    store.create(done)
    store.transition(done, from_={"running"}, to="done", result={"items": []})
    assert store.fail_running() >= 2
    assert [(store.get(j).status, store.get(j).error) for j in (queued, running)] == [("error", "interrupted")] * 2
    assert store.get(waiting).status == "waiting_input" and store.get(done).status == "done"  # 사람 응답 대기와 완료는 그대로


@pytest.mark.postgres
def test_migrate_upgrades_the_stage_4_table_without_losing_rows():
    schema = f"mig_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        try:
            conn.execute(f"SET search_path TO {schema}")
            conn.execute(
                "CREATE TABLE jobs (id text PRIMARY KEY, status text NOT NULL, result jsonb, error text,"
                " updated_at timestamptz NOT NULL DEFAULT now())"
            )
            conn.execute("INSERT INTO jobs (id, status, result) VALUES ('old', 'done', '{\"items\": []}')")
            migrate(conn)
            migrate(conn)  # 두 번째는 아무 일도 하지 않는다
            row = conn.execute("SELECT status, result, kind, progress FROM jobs").fetchone()
            assert row == ("done", {"items": []}, "graph", None)
        finally:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.mark.postgres
def test_two_instances_migrating_at_once_do_not_collide():
    schema = f"mig_{uuid.uuid4().hex[:8]}"
    errors: list[Exception] = []
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        barrier = threading.Barrier(4)

        def boot():
            try:
                with psycopg.connect(DATABASE_URL, autocommit=True, options=f"-c search_path={schema}") as conn:
                    barrier.wait()
                    migrate(conn)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=boot) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        admin.execute(f"DROP SCHEMA {schema} CASCADE")
    assert errors == []
