"""그래프 작업 실행기

jobs 행이 작업 상태의 진실 공급원이고, 그래프(동기 invoke)는 executor 의 스레드에서 돌아감.
같은 thread_id 의 중복 실행·더블 클릭은 jobs 의 조건부 전이(JobStore.transition)가 막음.

작업은 queued(풀 대기) → running → waiting_input / done / error 로 흐른다. 서비스가 시작될 때 queued·running 으로 남은
작업은 죽은 프로세스의 것이라 error(interrupted) 로 바뀐다(JobStore.fail_running).

ponytail: 프로세스 하나를 가정. 인스턴스가 여러 개면 한쪽이 죽었을 때 다른 쪽이 queued·running 작업을 알아채지 못하고,
새로 뜬 인스턴스의 fail_running 이 아직 살아 있는 인스턴스의 작업을 죽일 수 있다 — 스케일 아웃이 필요해지면 heartbeat 나 별도 워커로 올릴 것.
"""

import logging
import threading
from concurrent.futures import Executor
from typing import Any

from langgraph.types import Command

from stage_director.analysis.sections import validate_section_edit
from stage_director.graph_nodes import MAX_CONCURRENT_PROPOSALS
from stage_director.jobs import Job, JobStore
from stage_director.llm.client import LLMError
from stage_director.models import (
    FeedbackResume,
    ResumeRequest,
    RunStatus,
    SectionsResume,
    SequenceRequest,
)

log = logging.getLogger(__name__)

MAX_CONCURRENT_RUNS = 2  # 그래프 동시 실행 수(스레드 풀 크기). 구간 단위 동시 호출(MAX_CONCURRENT_PROPOSALS)과 곱해져 LLM RPM 에 영향


class RunError(Exception):
    """요청을 거절한다. api 가 status·code 를 그대로 HTTP 응답으로 바꾼다.

    404 thread_not_found: jobs 에 없는 thread_id(존재한 적이 없거나 보존 정책으로 삭제됨, 구별 못 함).
    409 not_waiting_input / stale_interrupt / kind_mismatch: 상태와 맞지 않는 요청.
    422 invalid_sections / invalid_targets: resume 페이로드가 잘못됨. interrupt 는 그대로 남는다.
    """

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message


class Runner:
    def __init__(self, graph, jobs: JobStore, executor: Executor):
        self._graph = graph
        self._jobs = jobs
        self._executor = executor
        self._resume_lock = threading.Lock()

    # ── 공개 API ──────────────────────────────────────────────

    def start(self, thread_id: str, context: SequenceRequest) -> RunStatus:
        """멱등. 새 스레드면 시작, error 면 마지막 체크포인트에서 재개, 그 밖에는 현재 상태를 그대로 돌려준다."""
        if self._jobs.create(thread_id, status="queued"):
            self._submit(thread_id, {"request": context})
        else:
            self._graph_job(thread_id)
            if self._jobs.transition(thread_id, from_={"error"}, to="queued"):
                has_checkpoint = bool(self._graph.get_state(self._config(thread_id)).values)
                self._submit(thread_id, None if has_checkpoint else {"request": context})
        return self.status(thread_id)

    def status(self, thread_id: str) -> RunStatus:
        job = self._graph_job(thread_id)
        interrupt = self._pending_interrupt(thread_id) if job.status == "waiting_input" else None
        return RunStatus(thread_id=thread_id, status=job.status, interrupt=interrupt, result=job.result, error=job.error)

    def resume(self, thread_id: str, req: ResumeRequest) -> RunStatus:
        # ponytail: 프로세스 하나 가정
        with self._resume_lock:
            job = self._graph_job(thread_id)
            pending = self._pending_interrupt(thread_id) if job.status == "waiting_input" else None
            if pending is None:
                raise RunError(409, "not_waiting_input")
            if req.interrupt_id != pending["interruptId"]:
                raise RunError(409, "stale_interrupt")
            value = self._resume_value(thread_id, pending, req)
            # 검증을 다 통과한 뒤에야 전이한다 — 잘못된 요청이 interrupt 를 소모하지 않는다. 이 전이가 더블 클릭 방어다.
            if not self._jobs.transition(thread_id, from_={"waiting_input"}, to="queued"):
                raise RunError(409, "not_waiting_input")
        self._submit(thread_id, Command(resume=value))
        return self.status(thread_id)

    # ── 내부 ──────────────────────────────────────────────────

    def _graph_job(self, thread_id: str) -> Job:
        job = self._jobs.get(thread_id)
        if job is None or job.kind != "graph":  # 분석 작업 id 로 들어온 요청도 모르는 스레드로 취급
            raise RunError(404, "thread_not_found")
        return job

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}, "max_concurrency": MAX_CONCURRENT_PROPOSALS}

    def _submit(self, thread_id: str, graph_input: Any) -> None:
        self._executor.submit(self._run, thread_id, graph_input)

    def _run(self, thread_id: str, graph_input: Any) -> None:
        try:
            started = self._jobs.transition(thread_id, from_={"queued"}, to="running")
        except Exception:  # 버려지는 Future 안에서 조용히 사라지지 않게 남긴다. 작업은 queued 로 남고 다음 서비스 시작 때 정리된다
            log.exception("run %s: queued -> running 전이 실패", thread_id)
            return
        if not started:
            return  # 대기하는 사이 다른 인스턴스의 시작 정리(fail_running)가 error 로 바꿨거나 행이 지워짐 -> 실행하지 않는다
        # ponytail: error 전이 자체가 실패하면 작업은 running 으로 남고, 다음 서비스 시작 때 error(interrupted) 로 복구.
        try:
            result = self._graph.invoke(graph_input, self._config(thread_id))
            if "__interrupt__" in result:
                self._jobs.transition(thread_id, from_={"running"}, to="waiting_input")
            else:
                self._jobs.transition(thread_id, from_={"running"}, to="done", result=self._result(thread_id))
        except Exception as e:
            log.exception("run %s failed", thread_id)
            error = "llm_failed" if isinstance(e, LLMError) else "internal_error"
            self._jobs.transition(thread_id, from_={"running"}, to="error", error=error)

    def _pending_interrupt(self, thread_id: str) -> dict[str, Any] | None:
        tasks = self._graph.get_state(self._config(thread_id)).tasks
        return next((i.value for t in tasks for i in t.interrupts), None)

    def _result(self, thread_id: str) -> dict[str, Any]:
        values = self._graph.get_state(self._config(thread_id)).values

        def dump(models):
            return [m.model_dump(by_alias=True, mode="json") for m in models]

        return {"sections": dump(values["sections"]), "items": dump(values["final_items"]), "issues": dump(values["final_issues"])}

    def _resume_value(self, thread_id: str, pending: dict[str, Any], req: ResumeRequest) -> dict[str, Any]:
        state = self._graph.get_state(self._config(thread_id)).values
        if isinstance(req, SectionsResume):
            if pending["kind"] != "confirm_sections":
                raise RunError(409, "kind_mismatch")
            reason = validate_section_edit(req.payload.sections, state["request"].duration_sec)
            if reason:
                raise RunError(422, "invalid_sections", reason)
            return {"sections": [s.model_dump(by_alias=True) for s in req.payload.sections]}
        if pending["kind"] != "review":
            raise RunError(409, "kind_mismatch")
        if isinstance(req, FeedbackResume):
            count = len(state["sections"])
            bad = [t for t in req.payload.targets if t >= count]
            if bad:
                raise RunError(422, "invalid_targets", f"구간 번호 {bad} 는 0~{count - 1} 범위 밖이다")
            return {"action": "feedback", "text": req.payload.text, "targets": sorted(set(req.payload.targets))}
        return {"action": "approve"}
