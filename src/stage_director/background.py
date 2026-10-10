"""주기 작업(보존 정책, 데몬 스레드)"""

import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)


class Periodic:
    """start() 한 뒤 interval_sec 마다 fn 을 부른다(첫 호출은 interval_sec 뒤). fn 이 예외를 내도 계속 돈다."""

    def __init__(self, fn: Callable[[], object], interval_sec: float, name: str):
        self._fn = fn
        self._interval = interval_sec
        self._name = name
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._fn()
            except Exception:
                log.exception("주기 작업 %s 실패", self._name)
