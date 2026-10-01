"""테스트 전역에서 공유하는 상수. `tests/fixtures/propose_request.json`을 여러 테스트 파일이 쓴다."""

import json
from pathlib import Path

PROPOSE_REQUEST = json.loads((Path(__file__).parent / "fixtures" / "propose_request.json").read_text())
