"""LangGraph 체크포인터. 전용 Postgres(로컬 docker-compose, 배포 시 Neon) (스펙 D2).

Python 코드의 나머지 부분은 체크포인터 종류를 모른다: 테스트는 InMemorySaver, 운영은 이 Postgres 어댑터.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


@contextmanager
def postgres_checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    """PostgresSaver 를 열고 체크포인트 테이블을 보장(멱등)한 뒤 돌려준다.

    단일 커넥션(from_conn_string) 대신 ConnectionPool 을 쓴다 — 배포 대상인 Neon 은 유휴 컴퓨트를
    autosuspend 하며 끊긴 커넥션을 되살리지 않으므로, 단일 커넥션은 유휴 뒤 모든 체크포인트
    읽기/쓰기가 프로세스 재시작 전까지 계속 실패하게 된다. 풀은 끊긴 커넥션을 알아서 재연결한다.
    """
    with ConnectionPool(
        database_url,
        min_size=1,
        max_size=5,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        check=ConnectionPool.check_connection,
    ) as pool:
        saver = PostgresSaver(pool)
        saver.setup()
        yield saver
