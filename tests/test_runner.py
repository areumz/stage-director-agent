import pytest
from langgraph.checkpoint.memory import InMemorySaver

from stage_director.graph import build_sequence_graph
from stage_director.jobs import InMemoryJobStore
from stage_director.llm.client import LLMError
from stage_director.llm.fake import FakeLLM
from stage_director.models import (
    ApproveResume,
    FeedbackPayload,
    FeedbackResume,
    Section,
    SectionsPayload,
    SectionsResume,
    SequenceRequest,
)
from stage_director.runner import InvalidResume, RunConflict, Runner, RunNotFound
from tests.conftest import SEQUENCE_REQUEST, DeferredExecutor, InlineExecutor

CONTEXT = SequenceRequest.model_validate(SEQUENCE_REQUEST)  # 60초: 0~30 잔잔, 30~60 큰 소리 → 구간 2개
SHORT = SequenceRequest.model_validate(
    {**SEQUENCE_REQUEST, "analysis": {"durationSec": 10, "bpm": 100, "energyCurve": [0.5] * 10}, "durationSec": 10}
)  # 구간 1개
GOOD = {
    "state": {
        "color": "#9F77DD",
        "spots": {k: {"on": True, "intensity": 300, "angle": 0.5} for k in ("left", "center", "right")},
        "camera": "front",
        "smoke": {"density": 0.3, "color": "#ffffff"},
    },
    "rationale": "측정값에 맞춘 연출",
}


def make(*llm_responses, executor=None):
    jobs = InMemoryJobStore()
    llm = FakeLLM(*llm_responses)
    runner = Runner(build_sequence_graph(llm, InMemorySaver()), jobs, executor or InlineExecutor())
    return runner, jobs, llm


def sections_resume(status, sections=None, interrupt_id=None):
    sections = sections or [Section.model_validate(s) for s in status.interrupt["sections"]]
    return SectionsResume(interrupt_id=interrupt_id or status.interrupt["interruptId"], kind="sections", payload=SectionsPayload(sections=sections))


def approve(status):
    return ApproveResume(interrupt_id=status.interrupt["interruptId"], kind="approve")


# ── 시작·조회 ─────────────────────────────────────────────────


def test_start_runs_to_the_first_interrupt():
    runner, jobs, _ = make()
    status = runner.start("t1", CONTEXT)
    assert status.status == "waiting_input" and status.interrupt["kind"] == "confirm_sections"
    assert jobs.get("t1").status == "waiting_input"


def test_start_is_idempotent_for_an_existing_thread():
    runner, _, llm = make()
    first = runner.start("t1", CONTEXT)
    second = runner.start("t1", CONTEXT)
    assert second.interrupt["interruptId"] == first.interrupt["interruptId"]
    assert llm.calls == []  # 그래프를 다시 돌리지 않았다


def test_unknown_thread_is_not_found():
    runner, _, _ = make()
    with pytest.raises(RunNotFound):
        runner.status("nope")
    with pytest.raises(RunNotFound):
        runner.resume("nope", ApproveResume(interrupt_id="x", kind="approve"))


# ── 정상 흐름 ─────────────────────────────────────────────────


def test_full_flow_confirm_sections_then_approve():
    runner, _, _ = make(GOOD, GOOD)
    status = runner.start("t1", CONTEXT)
    status = runner.resume("t1", sections_resume(status))
    assert status.status == "waiting_input" and status.interrupt["kind"] == "review"
    status = runner.resume("t1", approve(status))
    assert status.status == "done"
    assert len(status.result["items"]) == 2 and len(status.result["sections"]) == 2


def test_feedback_turn_returns_to_review_with_a_new_interrupt_id():
    runner, _, llm = make(GOOD, GOOD, GOOD)
    status = runner.start("t1", CONTEXT)
    review = runner.resume("t1", sections_resume(status))
    feedback = FeedbackResume(
        interrupt_id=review.interrupt["interruptId"], kind="feedback", payload=FeedbackPayload(text="더 밝게", targets=[1])
    )
    again = runner.resume("t1", feedback)
    assert again.interrupt["kind"] == "review"
    assert again.interrupt["interruptId"] != review.interrupt["interruptId"]
    assert len(llm.calls) == 3


# ── 방어: 낡은 resume, 더블 클릭, 잘못된 입력 ─────────────────


def test_stale_interrupt_id_is_rejected_and_the_interrupt_stays():
    runner, _, _ = make()
    status = runner.start("t1", CONTEXT)
    with pytest.raises(RunConflict) as e:
        runner.resume("t1", sections_resume(status, interrupt_id="t1:9:confirm_sections"))
    assert e.value.code == "stale_interrupt"
    assert runner.status("t1").interrupt["interruptId"] == status.interrupt["interruptId"]


def test_resume_after_done_is_not_waiting_input():
    runner, _, _ = make(GOOD, GOOD)
    status = runner.resume("t1", sections_resume(runner.start("t1", CONTEXT)))
    done = runner.resume("t1", approve(status))
    with pytest.raises(RunConflict) as e:
        runner.resume("t1", ApproveResume(interrupt_id="whatever", kind="approve"))
    assert done.status == "done" and e.value.code == "not_waiting_input"


def test_double_click_runs_the_graph_exactly_once():
    executor = DeferredExecutor()
    runner, _, llm = make(GOOD, GOOD, executor=executor)
    runner.start("t1", CONTEXT)
    executor.run_all()  # → confirm_sections 에서 대기
    request = sections_resume(runner.status("t1"))
    runner.resume("t1", request)  # 첫 번째: 접수(running)
    with pytest.raises(RunConflict) as e:
        runner.resume("t1", request)  # 두 번째: 이미 running
    assert e.value.code == "not_waiting_input"
    executor.run_all()
    assert len(llm.calls) == 2  # 구간 2개를 한 번만 제안했다
    assert runner.status("t1").interrupt["kind"] == "review"


def test_invalid_sections_are_rejected_and_the_interrupt_stays():
    runner, _, _ = make()
    status = runner.start("t1", CONTEXT)
    gap = [Section(label="a", start_sec=0, end_sec=20), Section(label="b", start_sec=25, end_sec=60)]
    with pytest.raises(InvalidResume) as e:
        runner.resume("t1", sections_resume(status, sections=gap))
    assert e.value.code == "invalid_sections"
    after = runner.status("t1")
    assert after.status == "waiting_input" and after.interrupt["interruptId"] == status.interrupt["interruptId"]


def test_feedback_targets_out_of_range_are_rejected():
    runner, _, _ = make(GOOD, GOOD)
    review = runner.resume("t1", sections_resume(runner.start("t1", CONTEXT)))
    bad = FeedbackResume(interrupt_id=review.interrupt["interruptId"], kind="feedback", payload=FeedbackPayload(text="x", targets=[5]))
    with pytest.raises(InvalidResume) as e:
        runner.resume("t1", bad)
    assert e.value.code == "invalid_targets"
    assert runner.status("t1").status == "waiting_input"


def test_kind_must_match_the_pending_interrupt():
    runner, _, _ = make()
    status = runner.start("t1", CONTEXT)
    with pytest.raises(RunConflict) as e:
        runner.resume("t1", ApproveResume(interrupt_id=status.interrupt["interruptId"], kind="approve"))
    assert e.value.code == "kind_mismatch"


# ── 실패와 복구 ───────────────────────────────────────────────


def test_llm_failure_marks_error_and_retry_continues_from_the_checkpoint():
    runner, _, llm = make(LLMError("a"), LLMError("b"), LLMError("c"), GOOD)
    status = runner.resume("t1", sections_resume(runner.start("t1", SHORT)))
    assert status.status == "error" and status.error == "llm_failed"
    status = runner.start("t1", SHORT)  # 다시 시도: 같은 thread_id, 마지막 체크포인트에서 재개
    assert status.status == "waiting_input" and status.interrupt["kind"] == "review"
    assert len(llm.calls) == 4  # 3번 실패 + 재시도 1번. 구간 확인(interrupt #1)부터 다시 하지 않았다


def test_unexpected_errors_do_not_leak_details():
    runner, _, _ = make(RuntimeError("secret detail"))
    status = runner.resume("t1", sections_resume(runner.start("t1", SHORT)))
    assert status.status == "error" and status.error == "internal_error"
    assert "secret" not in status.error


def test_a_run_killed_before_it_started_is_marked_interrupted_and_can_be_restarted():
    executor = DeferredExecutor()
    runner, jobs, _ = make(executor=executor)
    assert runner.start("t1", CONTEXT).status == "running"  # 스레드가 돌기 전에 프로세스가 죽었다고 가정
    executor.pending.clear()  # 죽은 프로세스의 작업은 사라진다
    assert jobs.fail_running() == 1  # 서비스 재시작 시 복구
    assert runner.status("t1").error == "interrupted"
    runner.start("t1", CONTEXT)  # 다시 시도: 체크포인트가 없으므로 처음부터
    executor.run_all()
    assert runner.status("t1").status == "waiting_input"
