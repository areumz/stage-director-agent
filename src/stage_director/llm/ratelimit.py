"""분당 요청 수(RPM) 제한. 모델마다 하나씩 두고, 요청을 보내기 직전에 acquire() 로 순서를 기다린다.

슬라이딩 윈도우: 최근 window 초 안에 보낸 요청이 rpm 개 미만일 때만 통과시킨다. 토큰 버킷은 버스트를 허용해서
어느 60초 구간을 잘라도 rpm 을 넘지 않는다는 보장을 주지 못하는데, 서버 쪽 한도는 바로 그 보장을 요구한다.

ponytail: 프로세스 하나 기준. 인스턴스가 여러 개가 되면 한도가 인스턴스 수만큼 늘어난다 — 그때 DB 로 옮길 것.
"""

import threading
import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        rpm: int,
        *,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if rpm < 1:
            raise ValueError("rpm 은 1 이상이어야 한다")
        self._rpm, self._window, self._clock, self._sleep = rpm, window, clock, sleep
        self._sent: deque[float] = deque()  # 최근에 보낸 요청 시각(오래된 것이 앞)
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """한도 안에 들어올 때까지 기다린 뒤 한 자리를 차지한다. 기다리는 스레드는 락을 잡고 자지 않는다."""
        while True:
            with self._lock:
                now = self._clock()
                while self._sent and now - self._sent[0] >= self._window:
                    self._sent.popleft()
                if len(self._sent) < self._rpm:
                    self._sent.append(now)
                    return
                wait = self._window - (now - self._sent[0])
            self._sleep(wait)
