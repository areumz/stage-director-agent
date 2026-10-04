"""체크포인트 보존 정책 (스펙 §6.4). 서비스 시작 시와 주기 스크립트(`python -m stage_director.retention`)에서 실행.

규칙 1: 승인된 스레드(jobs.status=done)는 승인 24시간 후 체크포인트만 지운다. 승인본은 stage_sequences.items 에 있고
        조회는 jobs.result 를 읽는다. 24시간은 Next.js 가 저장에 실패했을 때 같은 스레드에서 결과를 다시 받을 여유다.
규칙 2: 승인되지 않은 스레드(running / waiting_input / error)는 updated_at 이후 7일 방치되면 체크포인트와 jobs 행을 모두 지운다.
        Supabase 의 draft 행은 Python 이 못 지우므로 다음 조회 때 Next.js 가 404 를 보고 410 + 행 삭제로 정리한다.
"""

import os
from datetime import UTC, datetime, timedelta

from stage_director.checkpointer import postgres_checkpointer
from stage_director.jobs import JobStore, PostgresJobStore

DONE_CHECKPOINT_TTL = timedelta(hours=24)
DRAFT_TTL = timedelta(days=7)
DRAFT_STATUSES = {"running", "waiting_input", "error"}


def purge(jobs: JobStore, saver, now: datetime | None = None) -> tuple[int, int]:
    """(규칙 1 로 체크포인트를 지운 스레드 수, 규칙 2 로 통째로 지운 스레드 수).

    ponytail: done 행은 지운 뒤에도 남아 매 실행마다 다시 훑는다(delete_thread 는 멱등). 행 수가 문제가 되면 정리 표시를 둔다.
    """
    now = now or datetime.now(UTC)
    done = jobs.stale({"done"}, now - DONE_CHECKPOINT_TTL)
    for thread_id in done:
        saver.delete_thread(thread_id)
    drafts = jobs.stale(DRAFT_STATUSES, now - DRAFT_TTL)
    for thread_id in drafts:
        saver.delete_thread(thread_id)
        jobs.delete(thread_id)
    return len(done), len(drafts)


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("환경변수 DATABASE_URL 가 설정되지 않았다")
    with postgres_checkpointer(database_url) as saver:
        done, drafts = purge(PostgresJobStore(saver.conn), saver)
    print(f"체크포인트 삭제 {done}건(승인 24시간 경과), 초안 삭제 {drafts}건(7일 방치)")


if __name__ == "__main__":
    main()
