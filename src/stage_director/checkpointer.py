"""LangGraph 체크포인터. 전용 Postgres(로컬 docker-compose, 배포 시 Neon) (스펙 D2).

Python 코드의 나머지 부분은 체크포인터 종류를 모른다: 테스트는 InMemorySaver, 운영은 이 Postgres 어댑터.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver


@contextmanager
def postgres_checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    """PostgresSaver 를 열고 체크포인트 테이블을 보장(멱등)한 뒤 돌려준다."""
    with PostgresSaver.from_conn_string(database_url) as saver:
        saver.setup()
        yield saver
