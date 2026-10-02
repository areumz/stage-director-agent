"""postgres_checkpointer 의 연결 구성 로직을 실제 DB 없이 검증한다.

tests/test_checkpointer.py 는 실제 Postgres 가 필요한 통합 테스트(-m postgres)다. 이 파일은 그 반대:
ConnectionPool/PostgresSaver 를 가짜로 바꿔치기해 "풀을 만들어 PostgresSaver 에 넘기고, setup 을
부르고, 컨텍스트가 끝나면 풀을 닫는다"는 배선만 기본 테스트 스위트에서 빠르게 확인한다.
"""

from typing import ClassVar

import stage_director.checkpointer as checkpointer_module
from stage_director.checkpointer import postgres_checkpointer


class FakePool:
    instances: ClassVar[list["FakePool"]] = []

    @staticmethod
    def check_connection(conn):  # real ConnectionPool.check_connection 자리를 대신하는 더미
        pass

    def __init__(self, conninfo, *, min_size, max_size, kwargs, check):
        self.conninfo = conninfo
        self.min_size = min_size
        self.max_size = max_size
        self.kwargs = kwargs
        self.check = check
        self.closed = False
        FakePool.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


class FakeSaver:
    instances: ClassVar[list["FakeSaver"]] = []

    def __init__(self, conn):
        self.conn = conn
        self.setup_called = False
        FakeSaver.instances.append(self)

    def setup(self):
        self.setup_called = True


def test_checkpointer_builds_a_reconnecting_pool_instead_of_a_bare_connection(monkeypatch):
    FakePool.instances.clear()
    FakeSaver.instances.clear()
    monkeypatch.setattr(checkpointer_module, "ConnectionPool", FakePool)
    monkeypatch.setattr(checkpointer_module, "PostgresSaver", FakeSaver)

    with postgres_checkpointer("postgresql://u:p@host/db") as saver:
        yielded_saver = saver

    assert len(FakePool.instances) == 1
    pool = FakePool.instances[0]
    assert pool.conninfo == "postgresql://u:p@host/db"
    assert pool.min_size == 1
    assert pool.max_size == 5
    assert pool.kwargs["autocommit"] is True
    assert pool.kwargs["prepare_threshold"] == 0
    assert pool.closed  # 컨텍스트를 빠져나오면 풀도 닫힌다

    assert len(FakeSaver.instances) == 1
    saver = FakeSaver.instances[0]
    assert saver.conn is pool  # 단일 커넥션이 아니라 풀 자체를 PostgresSaver 에 넘긴다
    assert saver.setup_called
    assert yielded_saver is saver
