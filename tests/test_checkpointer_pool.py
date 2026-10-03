"""postgres_checkpointer 의 연결 구성 로직을 실제 DB 없이 검증한다.

tests/test_checkpointer.py 는 실제 Postgres 가 필요한 통합 테스트(-m postgres)다. 이 파일은 그 반대:
ConnectionPool/PostgresSaver 를 mock 으로 바꿔치기해 "풀을 만들어 PostgresSaver 에 넘기고, setup 을
부르고, 컨텍스트가 끝나면 풀을 닫는다"는 배선만 기본 테스트 스위트에서 빠르게 확인한다.
"""

from unittest.mock import patch

from stage_director.checkpointer import postgres_checkpointer


def test_checkpointer_builds_a_reconnecting_pool_instead_of_a_bare_connection():
    with (
        patch("stage_director.checkpointer.ConnectionPool") as MockPool,
        patch("stage_director.checkpointer.PostgresSaver") as MockSaver,
    ):
        pool = MockPool.return_value.__enter__.return_value

        with postgres_checkpointer("postgresql://u:p@host/db") as saver:
            yielded_saver = saver

        call = MockPool.call_args
        assert call.args == ("postgresql://u:p@host/db",)
        assert call.kwargs["min_size"] == 1
        assert call.kwargs["max_size"] == 5
        assert call.kwargs["kwargs"]["autocommit"] is True
        assert call.kwargs["kwargs"]["prepare_threshold"] == 0
        assert call.kwargs["check"] is MockPool.check_connection
        MockPool.return_value.__exit__.assert_called_once()  # 컨텍스트를 빠져나오면 풀도 닫힌다

        MockSaver.assert_called_once_with(pool)  # 단일 커넥션이 아니라 풀 자체를 PostgresSaver 에 넘긴다
        MockSaver.return_value.setup.assert_called_once()
        assert yielded_saver is MockSaver.return_value
