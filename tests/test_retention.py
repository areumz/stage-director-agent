"""보존 정책 (스펙 §6.4). 규칙 1: 승인(done) 24시간 후 체크포인트만. 규칙 2: 미승인 7일 방치 시 체크포인트와 jobs 행 모두."""

import contextlib
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from stage_director.api import create_app
from stage_director.checkpointer import postgres_checkpointer
from stage_director.graph import build_sequence_graph
from stage_director.jobs import InMemoryJobStore, PostgresJobStore
from stage_director.llm.fake import FakeLLM
from stage_director.models import (
    ApproveResume,
    Section,
    SectionsPayload,
    SectionsResume,
)
from stage_director.retention import (
    ANALYSIS_TTL,
    DONE_CHECKPOINT_TTL,
    DONE_ROW_TTL,
    DRAFT_TTL,
    purge,
)
from stage_director.runner import Runner
from stage_director.settings import Settings
from tests.conftest import InlineExecutor
from tests.test_checkpointer import DATABASE_URL
from tests.test_runner import CONTEXT, GOOD


def setup():
    saver, jobs = InMemorySaver(), InMemoryJobStore()
    runner = Runner(build_sequence_graph(FakeLLM(GOOD, GOOD), saver), jobs, InlineExecutor())
    return runner, jobs, saver


def has_checkpoint(saver, thread_id) -> bool:
    return saver.get_tuple({"configurable": {"thread_id": thread_id}}) is not None


def later(delta: timedelta) -> datetime:
    return datetime.now(UTC) + delta


def approve_run(runner, thread_id="t1"):
    status = runner.start(thread_id, CONTEXT)
    sections = [Section.model_validate(s) for s in status.interrupt["sections"]]
    status = runner.resume(
        thread_id, SectionsResume(interrupt_id=status.interrupt["interruptId"], kind="sections", payload=SectionsPayload(sections=sections))
    )
    return runner.resume(thread_id, ApproveResume(interrupt_id=status.interrupt["interruptId"], kind="approve"))


def test_approved_thread_loses_its_checkpoint_after_24_hours_but_keeps_the_result():
    runner, jobs, saver = setup()
    approve_run(runner)
    purge(jobs, saver, now=later(DONE_CHECKPOINT_TTL - timedelta(hours=1)))
    assert has_checkpoint(saver, "t1")  # 23시간: 아직 Next.js 가 결과를 다시 받아 저장할 수 있다
    purge(jobs, saver, now=later(DONE_CHECKPOINT_TTL + timedelta(hours=1)))
    assert not has_checkpoint(saver, "t1")
    assert jobs.get("t1").status == "done" and jobs.get("t1").result["items"]  # 조회는 jobs.result 에서


def test_abandoned_waiting_thread_is_deleted_entirely_after_7_days():
    runner, jobs, saver = setup()
    runner.start("t1", CONTEXT)  # confirm_sections 에서 대기
    purge(jobs, saver, now=later(DRAFT_TTL - timedelta(days=1)))
    assert jobs.get("t1") is not None and has_checkpoint(saver, "t1")
    purge(jobs, saver, now=later(DRAFT_TTL + timedelta(days=1)))
    assert jobs.get("t1") is None and not has_checkpoint(saver, "t1")


@pytest.mark.parametrize("status", ["running", "error"])
def test_stuck_threads_follow_the_draft_rule(status):
    _, jobs, saver = setup()
    jobs.create("t1")
    if status == "error":
        jobs.transition("t1", from_={"running"}, to="error", error="llm_failed")
    purge(jobs, saver, now=later(DRAFT_TTL + timedelta(days=1)))
    assert jobs.get("t1") is None


def test_purge_is_safe_to_repeat():
    runner, jobs, saver = setup()
    approve_run(runner)
    now = later(DONE_CHECKPOINT_TTL + timedelta(days=30))
    assert purge(jobs, saver, now=now) == (1, 0, 1, 0)
    purge(jobs, saver, now=now)  # 이미 지운 스레드를 다시 지워도 에러 없음


def test_startup_purges_stale_jobs_before_marking_running_ones_interrupted():
    store = InMemoryJobStore()
    store.create("old")
    store.create("fresh")
    store._jobs["old"] = replace(store._jobs["old"], updated_at=datetime.now(UTC) - DRAFT_TTL - timedelta(days=1))
    settings = Settings(internal_api_key="k", gemini_api_key="unused", gemini_model="unused", database_url="unused")
    app = create_app(settings, FakeLLM(), lambda: contextlib.nullcontext(InMemorySaver()), job_store=store, executor=InlineExecutor())
    with TestClient(app):
        pass
    assert store.get("old") is None  # 방치로 삭제 (interrupted 로 바뀌며 시계가 리셋되기 전에)
    assert store.get("fresh").error == "interrupted"


def test_approved_job_rows_are_deleted_after_7_days_but_kept_before():
    runner, jobs, saver = setup()
    approve_run(runner)
    purge(jobs, saver, now=later(DONE_ROW_TTL - timedelta(days=1)))
    assert jobs.get("t1") is not None and not has_checkpoint(saver, "t1")  # 체크포인트는 24시간에 이미 사라졌다
    assert purge(jobs, saver, now=later(DONE_ROW_TTL + timedelta(days=1))).done_rows == 1
    assert jobs.get("t1") is None


def test_analysis_rows_are_deleted_7_days_after_their_last_update_in_any_state():
    _, jobs, saver = setup()
    for job_id, status in (("done", "done"), ("failed", "error"), ("stuck", "running")):
        jobs.create(job_id, kind="analysis")
        if status != "running":
            jobs.transition(job_id, from_={"running"}, to=status, error="x" if status == "error" else None)
    assert purge(jobs, saver, now=later(ANALYSIS_TTL - timedelta(days=1))).analyses == 0  # 탭을 닫았다 돌아와도 결과를 받을 수 있다
    assert purge(jobs, saver, now=later(ANALYSIS_TTL + timedelta(days=1))).analyses == 3
    assert [jobs.get(j) for j in ("done", "failed", "stuck")] == [None, None, None]


def test_graph_rules_do_not_touch_analysis_rows_and_the_analysis_rule_does_not_touch_graph_rows():
    runner, jobs, saver = setup()
    approve_run(runner)
    jobs.create("a1", kind="analysis")
    jobs.transition("a1", from_={"running"}, to="done", result={"analysis": {}})
    counts = purge(jobs, saver, now=later(DONE_CHECKPOINT_TTL + timedelta(hours=1)))
    assert counts == (1, 0, 0, 0) and jobs.get("a1") is not None  # 분석 결과는 24시간이 지나도 남는다
    purge(jobs, saver, now=later(ANALYSIS_TTL + timedelta(days=1)))
    assert jobs.get("a1") is None and jobs.get("t1") is None  # 둘 다 7일 뒤 각자의 규칙으로 사라진다


def test_the_running_service_purges_periodically(monkeypatch):
    runs = threading.Event()
    calls = []

    def fake_purge(jobs, saver):
        calls.append(1)
        if len(calls) >= 2:
            runs.set()

    monkeypatch.setattr("stage_director.api.purge", fake_purge)
    monkeypatch.setattr("stage_director.api.RETENTION_INTERVAL_SEC", 0.02)
    settings = Settings(internal_api_key="k", gemini_api_key="unused", gemini_model="unused", database_url="unused")
    app = create_app(settings, FakeLLM(), lambda: contextlib.nullcontext(InMemorySaver()), job_store=InMemoryJobStore(), executor=InlineExecutor())
    with TestClient(app):
        assert runs.wait(2)  # 시작 시 한 번 + 주기 실행


@pytest.mark.postgres
def test_purge_against_postgres_uses_updated_at():
    old, fresh = f"test-{uuid.uuid4()}", f"test-{uuid.uuid4()}"
    with postgres_checkpointer(DATABASE_URL) as saver:
        jobs = PostgresJobStore(saver.conn)
        for job_id in (old, fresh):
            jobs.create(job_id)
            jobs.transition(job_id, from_={"running"}, to="waiting_input")
        with saver.conn.connection() as conn:
            conn.execute("UPDATE jobs SET updated_at = now() - interval '8 days' WHERE id = %s", (old,))
        purge(jobs, saver)
        assert jobs.get(old) is None
        assert jobs.get(fresh) is not None
        jobs.delete(fresh)
