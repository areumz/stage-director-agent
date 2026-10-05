"""jobs 테이블 (스펙 §6.1). 체크포인트만으로는 '실행 중이었는지'를 알 수 없어서 작업 상태를 따로 둠.

kind: graph(시퀀스 생성) / analysis(음원 분석). status: queued / running / waiting_input / done / error.
전이는 모두 조건부(transition)라 같은 id 의 중복 실행과 더블 클릭을 막는 잠금 역할.
"""

import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb

ACTIVE = frozenset({"queued", "running"})  # 이 프로세스의 스레드가 일하고 있어야 하는 상태. 프로세스가 죽으면 이 상태로 남는다

_DDL_LOCK = 7_240_501  # pg_advisory_xact_lock 키. 인스턴스 둘이 동시에 기동해도 DDL 이 겹치지 않게 한다
# 컬럼을 더할 때는 맨 아래에 ADD COLUMN IF NOT EXISTS 한 줄을 추가한다. 새 DB 도 기존 DB 도 같은 경로를 탄다.
_DDL = (
    """CREATE TABLE IF NOT EXISTS jobs (
        id         text PRIMARY KEY,
        status     text NOT NULL,
        result     jsonb,
        error      text,
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'graph'",
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS progress real",
)


def migrate(conn) -> None:
    """jobs 테이블을 최신 모양으로 맞춘다(멱등). 4단계까지 만들어진 테이블의 기존 행은 그대로 두고 컬럼만 더한다."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_DDL_LOCK,))
        for statement in _DDL:
            conn.execute(statement)


@dataclass(frozen=True)
class Job:
    id: str
    status: str
    result: dict[str, Any] | None
    error: str | None
    updated_at: datetime
    kind: str = "graph"
    progress: float | None = None  # 0.0~1.0. 분석 작업만 쓴다


class JobStore(Protocol):
    def create(self, job_id: str, *, kind: str = "graph", status: str = "running") -> bool:
        """status(기본 running) 상태로 만든다. 이미 있으면 False."""
        ...

    def get(self, job_id: str) -> Job | None: ...

    def transition(
        self, job_id: str, *, from_: set[str], to: str, result: dict[str, Any] | None = None, error: str | None = None
    ) -> bool:
        """현재 상태가 from_ 안일 때만 to 로 바꾸고 updated_at 을 갱신한다. 바꿨으면 True."""
        ...

    def set_progress(self, job_id: str, progress: float) -> None: ...

    def fail_running(self) -> int:
        """서비스 시작 시 호출: queued·running 이던 작업(그래프·분석 모두)을 error("interrupted") 로. 바꾼 개수."""
        ...

    def stale(self, statuses: set[str], before: datetime, kind: str = "graph") -> list[str]:
        """kind 작업 중 status 가 statuses 안이고 updated_at 이 before 보다 오래된 id."""
        ...

    def delete(self, job_id: str) -> None: ...


class InMemoryJobStore:
    """테스트와 InMemorySaver 조합용. 락 하나로 조건부 전이의 원자성을 흉내 낸다."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, job_id: str, *, kind: str = "graph", status: str = "running") -> bool:
        with self._lock:
            if job_id in self._jobs:
                return False
            self._jobs[job_id] = Job(job_id, status, None, None, datetime.now(UTC), kind=kind)
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

    def set_progress(self, job_id: str, progress: float) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id] = replace(self._jobs[job_id], progress=progress)

    def fail_running(self) -> int:
        with self._lock:
            now = datetime.now(UTC)
            active = [j for j in self._jobs.values() if j.status in ACTIVE]
            for job in active:
                self._jobs[job.id] = replace(job, status="error", error="interrupted", updated_at=now)
            return len(active)

    def stale(self, statuses: set[str], before: datetime, kind: str = "graph") -> list[str]:
        return [j.id for j in list(self._jobs.values()) if j.kind == kind and j.status in statuses and j.updated_at < before]

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)


class PostgresJobStore:
    """pool 은 postgres_checkpointer 가 연 psycopg ConnectionPool(saver.conn). autocommit·dict_row 설정을 전제한다."""

    def __init__(self, pool) -> None:
        self._pool = pool
        with pool.connection() as conn:
            migrate(conn)

    def create(self, job_id: str, *, kind: str = "graph", status: str = "running") -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "INSERT INTO jobs (id, status, kind) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (job_id, status, kind),
            )
            return cur.rowcount == 1

    def get(self, job_id: str) -> Job | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT id, status, result, error, updated_at, kind, progress FROM jobs WHERE id = %s", (job_id,)
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

    def set_progress(self, job_id: str, progress: float) -> None:
        with self._pool.connection() as conn:
            conn.execute("UPDATE jobs SET progress = %s WHERE id = %s", (progress, job_id))

    def fail_running(self) -> int:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE jobs SET status = 'error', error = 'interrupted', updated_at = now() WHERE status = ANY(%s)",
                (list(ACTIVE),),
            )
            return cur.rowcount

    def stale(self, statuses: set[str], before: datetime, kind: str = "graph") -> list[str]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE kind = %s AND status = ANY(%s) AND updated_at < %s",
                (kind, list(statuses), before),
            ).fetchall()
        return [r["id"] for r in rows]

    def delete(self, job_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
