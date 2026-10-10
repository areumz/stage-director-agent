import threading
import time

import pytest

from stage_director.llm.ratelimit import RateLimiter


class FakeTime:
    """sleep 이 시계를 그만큼 앞으로 보낸다."""

    def __init__(self):
        self.now, self.slept = 0.0, []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def limiter(rpm, fake):
    return RateLimiter(rpm, clock=fake.clock, sleep=fake.sleep)


def test_requests_under_the_limit_do_not_wait():
    fake = FakeTime()
    rl = limiter(3, fake)
    for _ in range(3):
        rl.acquire()
    assert fake.slept == []


def test_the_request_over_the_limit_waits_until_the_oldest_leaves_the_window():
    fake = FakeTime()
    rl = limiter(2, fake)
    rl.acquire()
    fake.now = 10
    rl.acquire()
    rl.acquire()  # 3번째: 가장 오래된 요청(t=0)이 윈도우를 벗어나는 t=60 까지
    assert fake.slept == [50.0] and fake.now == 60.0


def test_no_sixty_second_slice_ever_holds_more_than_rpm_requests():
    fake = FakeTime()
    rl = limiter(3, fake)
    stamps = []
    for _ in range(10):
        rl.acquire()
        stamps.append(fake.now)
    for start in stamps:
        assert sum(1 for t in stamps if start <= t < start + 60) <= 3


def test_all_waiting_threads_get_through_and_the_second_window_is_reached():
    rl = RateLimiter(5, window=0.3)
    stamps, lock = [], threading.Lock()

    def worker():
        rl.acquire()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(stamps) == 10
    assert max(stamps) - min(stamps) >= 0.25  # 10개를 윈도우 하나에 다 보내지 못하고 두 번째 윈도우로 밀렸다


def test_rpm_must_be_positive():
    with pytest.raises(ValueError):
        RateLimiter(0)
