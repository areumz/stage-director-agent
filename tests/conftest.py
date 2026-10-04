"""테스트 전역에서 공유하는 상수. `tests/fixtures/propose_request.json`을 여러 테스트 파일이 쓴다."""

import json
from pathlib import Path

PROPOSE_REQUEST = json.loads((Path(__file__).parent / "fixtures" / "propose_request.json").read_text())
SEQUENCE_REQUEST = json.loads((Path(__file__).parent / "fixtures" / "sequence_request.json").read_text())


class InlineExecutor:
    """submit 한 작업을 호출한 스레드에서 바로 실행한다. 테스트가 결과를 동기로 볼 수 있다."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


class DeferredExecutor:
    """submit 한 작업을 쌓아 두었다가 run_all() 이 부를 때만 실행한다. 더블 클릭·프로세스 사망 시나리오용."""

    def __init__(self):
        self.pending = []

    def submit(self, fn, *args, **kwargs):
        self.pending.append(lambda: fn(*args, **kwargs))

    def run_all(self):
        while self.pending:
            self.pending.pop(0)()

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass
