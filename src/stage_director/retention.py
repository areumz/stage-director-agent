"""보존 정책. 서비스 시작 시, 실행 중 RETENTION_INTERVAL_SEC 마다, 주기 스크립트(`python -m stage_director.retention`)에서 실행.

규칙 1: 승인된 스레드(jobs.status=done)는 승인 24시간 후 체크포인트만 지운다. 승인본은 stage_sequences.items 에 있고
        조회는 jobs.result 를 읽는다. (24시간: Next.js 가 저장에 실패했을 때 같은 스레드에서 결과를 다시 받을 여유)
규칙 2: 승인되지 않은 스레드(running / waiting_input / error)는 updated_at 이후 7일 방치되면 체크포인트와 jobs 행을 모두 지운다.
        Supabase 의 draft 행은 Python 이 못 지우므로 다음 조회 때 Next.js 가 404 를 보고 410 + 행 삭제로 정리.
규칙 3: 승인된 스레드의 jobs 행은 done 7일 후 지운다(체크포인트는 규칙 1 로 이미 없다). 승인본은 stage_sequences 에 있으므로
        Next.js 는 approved 행에 대해 Python 을 부르지 않아야 한다 — 부르면 404 를 410 으로 오해해 승인본을 지울 수 있다.
규칙 4: 분석 작업(kind=analysis)은 어떤 상태든 updated_at 7일 후 jobs 행을 지운다. 체크포인트는 없다.
        탭을 닫아도 다음 폴링 때 결과가 저장되게 하는 기간(스펙 §4.1).
"""

import os
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from stage_director.checkpointer import postgres_checkpointer
from stage_director.jobs import JobStore, PostgresJobStore

DONE_CHECKPOINT_TTL = timedelta(hours=24)
DRAFT_TTL = timedelta(days=7)
DONE_ROW_TTL = timedelta(days=7)
ANALYSIS_TTL = timedelta(days=7)
RETENTION_INTERVAL_SEC = 6 * 3600
DRAFT_STATUSES = {"queued", "running", "waiting_input", "error"}
ANALYSIS_STATUSES = {"queued", "running", "done", "error"}  # 7일 넘게 queued·running 이면 죽은 작업이다


class PurgeCounts(NamedTuple):
    done_checkpoints: int  # 규칙 1
    drafts: int  # 규칙 2
    done_rows: int  # 규칙 3
    analyses: int  # 규칙 4


def purge(jobs: JobStore, saver, now: datetime | None = None) -> PurgeCounts:
    now = now or datetime.now(UTC)
    done = jobs.stale({"done"}, now - DONE_CHECKPOINT_TTL)
    for thread_id in done:
        saver.delete_thread(thread_id)  # 멱등. 규칙 3 이 행을 지울 때까지 매번 다시 지운다
    drafts = jobs.stale(DRAFT_STATUSES, now - DRAFT_TTL)
    for thread_id in drafts:
        saver.delete_thread(thread_id)
        jobs.delete(thread_id)
    done_rows = jobs.stale({"done"}, now - DONE_ROW_TTL)
    for thread_id in done_rows:
        jobs.delete(thread_id)
    analyses = jobs.stale(ANALYSIS_STATUSES, now - ANALYSIS_TTL, kind="analysis")
    for job_id in analyses:
        jobs.delete(job_id)
    return PurgeCounts(len(done), len(drafts), len(done_rows), len(analyses))


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("환경변수 DATABASE_URL 가 설정되지 않았다")
    with postgres_checkpointer(database_url) as saver:
        counts = purge(PostgresJobStore(saver.conn), saver)
    print(
        f"체크포인트 삭제 {counts.done_checkpoints}건(승인 24시간 경과), 초안 삭제 {counts.drafts}건(7일 방치), "
        f"승인 작업 행 삭제 {counts.done_rows}건(7일 경과), 분석 작업 행 삭제 {counts.analyses}건(7일 경과)"
    )


if __name__ == "__main__":
    main()
