"""jobs 테이블 (스펙 §6.1). 체크포인트만으로는 '실행 중이었는지'를 알 수 없어서 작업 상태를 따로 둔다.

status: running / waiting_input / done / error.
전이는 모두 조건부(transition)라 같은 thread_id 의 중복 실행과 더블 클릭을 막는 잠금 역할을 한다.
"""

import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb

# ponytail: progress 컬럼은 분석 작업(5단계)이 필요할 때 ALTER TABLE ... ADD COLUMN IF NOT EXISTS 로 추가한다.
_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id         text PRIMARY KEY,
    status     text NOT NULL,
    result     jsonb,
    error      text,
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""


@dataclass(frozen=True)
class Job:
    id: str
    status: str
    result: dict[str, Any] | None
    error: str | None
    updated_at: datetime


class JobStore(Protocol):
    def create(self, job_id: str) -> bool:
        """running 상태로 만든다. 이미 있으면 False."""
        ...

    def get(self, job_id: str) -> Job | None: ...

    def transition(
        self, job_id: str, *, from_: set[str], to: str, result: dict[str, Any] | None = None, error: str | None = None
    ) -> bool:
        """현재 상태가 from_ 안일 때만 to 로 바꾸고 updated_at 을 갱신한다. 바꿨으면 True."""
        ...

    def fail_running(self) -> int:
        """서비스 시작 시 호출: running 이던 graph 작업을 error("interrupted") 로. 바꾼 개수."""
        ...

    def stale(self, statuses: set[str], before: datetime) -> list[str]:
        """status 가 statuses 안이고 updated_at 이 before 보다 오래된 graph 작업 id."""
        ...

    def delete(self, job_id: str) -> None: ...


class InMemoryJobStore:
    """테스트와 InMemorySaver 조합용. 락 하나로 조건부 전이의 원자성을 흉내 낸다."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, job_id: str) -> bool:
        with self._lock:
            if job_id in self._jobs:
                return False
            now = datetime.now(UTC)
            self._jobs[job_id] = Job(job_id, "running", None, None, now)
            return True

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def transition(
        self, job_id: str, *, from_: set[str], to: str, result: dict[str, Any] | None = None, error: str | None = None
    ) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in from_:
                return False
            self._jobs[job_id] = replace(job, status=to, result=result, error=error, updated_at=datetime.now(UTC))
            return True

    def fail_running(self) -> int:
        with self._lock:
            now = datetime.now(UTC)
            running = [j for j in self._jobs.values() if j.status == "running"]
            for job in running:
                self._jobs[job.id] = replace(job, status="error", error="interrupted", updated_at=now)
            return len(running)

    def stale(self, statuses: set[str], before: datetime) -> list[str]:
        return [j.id for j in list(self._jobs.values()) if j.status in statuses and j.updated_at < before]

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)


class PostgresJobStore:
    """pool 은 postgres_checkpointer 가 연 psycopg ConnectionPool(saver.conn). autocommit·dict_row 설정을 전제한다."""

    def __init__(self, pool) -> None:
        self._pool = pool
        with pool.connection() as conn:
            conn.execute(_DDL)

    def create(self, job_id: str) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "INSERT INTO jobs (id, status) VALUES (%s, 'running') ON CONFLICT (id) DO NOTHING", (job_id,)
            )
            return cur.rowcount == 1

    def get(self, job_id: str) -> Job | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT id, status, result, error, updated_at FROM jobs WHERE id = %s", (job_id,)
            ).fetchone()
        return Job(**row) if row else None

    def transition(
        self, job_id: str, *, from_: set[str], to: str, result: dict[str, Any] | None = None, error: str | None = None
    ) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = %s, result = %s, error = %s, updated_at = now() WHERE id = %s AND status = ANY(%s)",
                (to, Jsonb(result) if result is not None else None, error, job_id, list(from_)),
            )
            return cur.rowcount == 1

    def fail_running(self) -> int:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = 'error', error = 'interrupted', updated_at = now() WHERE status = 'running'"
            )
            return cur.rowcount

    def stale(self, statuses: set[str], before: datetime) -> list[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status = ANY(%s) AND updated_at < %s", (list(statuses), before)
            ).fetchall()
        return [r["id"] for r in rows]

    def delete(self, job_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
