"""Gemini 실제 호출 확인용 수동 스크립트. 자동 테스트가 아니다(실제 API 를 호출하고 쿼터를 쓴다).

DB(Neon 포함)에는 접속하지 않는다: Settings.from_env, 체크포인터, jobs, api 를 가져오지 않는다.
GEMINI_API_KEY 는 환경변수 또는 저장소 루트의 .env 에서 읽고, 값은 출력하지 않는다(오류 문구에 섞이면 *** 로 가린다).

사용: uv run python scripts/check_gemini.py {basic|fallback|rpm} [--n 12] [--track demo-tracks/곡.mp3] [--rpm N]
"""

import argparse
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from stage_director.analysis.measure import measure_file
from stage_director.analysis.sections import detect_sections
from stage_director.llm.client import LLMError
from stage_director.llm.gemini import GeminiClient
from stage_director.models import Track
from stage_director.mood import (
    MOOD_SCHEMA,
    SYSTEM_PROMPT,
    _user_prompt,
    interpret_moods,
)
from stage_director.settings import (
    DEFAULT_GEMINI_FALLBACK_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GEMINI_RPM,
)

BOGUS_MODEL = "this-model-does-not-exist-check-gemini"
WINDOW_SEC = 60.0  # 앱 제한기의 윈도우 길이(분당 제한)
MAX_INLINE_AUDIO_BYTES = 15 * 1024 * 1024  # 앱의 MAX_AUDIO_BYTES 와 같은 한도(Gemini 인라인 요청 한도 아래)
WANTED_ENV = ("GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_FALLBACK_MODEL", "GEMINI_RPM")
MIME_BY_SUFFIX = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg"}


def now() -> str:
    return datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]


def load_env() -> None:
    """저장소 루트 .env 에서 필요한 이름만 환경변수로 옮긴다(이미 설정된 환경변수가 우선). DATABASE_URL 등은 읽지 않는다."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        if name in WANTED_ENV and value.strip():
            os.environ.setdefault(name, value.strip().strip("\"'"))


class RedactingFilter(logging.Filter):
    def __init__(self, secret: str):
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = str(record.getMessage()).replace(self._secret, "***")
        record.args = ()
        return True


class Capture(logging.Handler):
    """앱 코드가 남기는 경고 로그(예비 모델 전환, 무드 해석 실패)를 모아서 마지막에 시간순으로 보여 준다."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{now()}  [{record.levelname}] {record.name}: {record.getMessage()}")


def prepare(track_path: Path):
    """실제 앱이 쓰는 분석 함수로 구간을 만든다(오프라인 계산, Gemini 호출 없음)."""
    data = track_path.read_bytes()
    if len(data) > MAX_INLINE_AUDIO_BYTES:
        raise SystemExit(f"{track_path.name}: {len(data)} 바이트 — 15MiB 를 넘어 앱도 무드 해석을 건너뛴다. 더 작은 곡을 고른다")
    mime = MIME_BY_SUFFIX.get(track_path.suffix.lower(), "audio/mpeg")
    snapshot = measure_file(track_path)
    sections = detect_sections(snapshot.energy_curve, snapshot.duration_sec)
    track = Track(title=track_path.stem)
    return data, mime, sections, track, snapshot.duration_sec


def make_client(api_key: str, model: str, fallback: str | None, rpm: int) -> GeminiClient:
    client = GeminiClient(api_key, model, fallback_model=fallback, rpm=rpm)
    # 앱 코드는 바꾸지 않고, SDK 호출 지점만 감싸서 "어느 모델이 성공/실패했는지"를 기록한다(진단용)
    attempts: list[tuple[str, str]] = []
    models = client._client.models
    original = models.generate_content

    def recording(**kwargs):
        model_name = kwargs.get("model", "?")
        try:
            result = original(**kwargs)
        except Exception as e:
            attempts.append((model_name, f"실패 {type(e).__name__}"))
            raise
        attempts.append((model_name, "성공"))
        return result

    models.generate_content = recording
    client.attempts = attempts
    return client


def print_answering_model(client: GeminiClient) -> None:
    """성공한 마지막 호출의 모델 이름과, 모든 시도(실패 포함)의 모델별 결과를 한 줄씩 보여 준다."""
    answered = [m for m, outcome in client.attempts if outcome == "성공"]
    print(f"   실제로 답한 모델: {answered[-1] if answered else '(없음 — 모든 시도가 실패)'}")
    print(f"   시도 {len(client.attempts)}회: " + ", ".join(f"{m} {o}" for m, o in client.attempts))


def run_mood(client: GeminiClient, data, mime, sections, track) -> tuple[list[str], float]:
    """앱의 무드 해석(mood.interpret_moods)을 그대로 호출한다. 실패해도 예외 없이 빈 문자열로 돌아오는 것이 앱의 동작이다."""
    start = time.perf_counter()
    moods = interpret_moods(client, data, mime, sections, track)
    return moods, time.perf_counter() - start


def print_moods(sections, moods) -> None:
    for i, (s, m) in enumerate(zip(sections, moods, strict=True), 1):
        print(f"   {i}. {s.label:<8} {s.start_sec:6.1f}~{s.end_sec:6.1f}초  →  {m or '(빈 문자열)'}")


def mode_basic(args, ctx) -> int:
    api_key, model, fallback = ctx["api_key"], ctx["model"], ctx["fallback"]
    print(f"[basic] 주 모델 {model} 로 무드 해석 1회 (예비 모델 {fallback} 는 주 모델이 실패할 때만 쓰인다)")
    client = make_client(api_key, model, fallback, rpm=0)
    moods, elapsed = run_mood(client, *ctx["prepared"][:4])
    ok = any(moods)
    print(f"{now()}  {'성공' if ok else '실패(모든 무드가 빈 문자열 — 아래 로그의 원인을 본다)'}  걸린 시간 {elapsed:.1f}초")
    print_answering_model(client)
    print_moods(ctx["prepared"][2], moods)
    return 0 if ok else 1


def mode_fallback(args, ctx) -> int:
    api_key, fallback = ctx["api_key"], ctx["fallback"]
    print(f"[fallback] 주 모델을 존재하지 않는 이름({BOGUS_MODEL})으로 덮어쓰고 호출 → 예비 모델 {fallback} 로 넘어가는지 확인")
    client = make_client(api_key, BOGUS_MODEL, fallback, rpm=0)
    moods, elapsed = run_mood(client, *ctx["prepared"][:4])
    switched = any("예비 모델" in line for line in ctx["capture"].lines)
    ok = any(moods)
    print(f"{now()}  예비 모델 전환 로그: {'있음' if switched else '없음'} / 무드 해석: {'성공' if ok else '실패'}  걸린 시간 {elapsed:.1f}초")
    print_answering_model(client)
    print_moods(ctx["prepared"][2], moods)
    if ok and switched:
        print("→ 판정: 폴백 정상. 예비 모델도 오디오 입력과 구조화 출력을 지원한다.")
    elif switched and not ok:
        print("→ 판정: 폴백은 시도됐지만 예비 모델도 실패했다. 모델 ID, 오디오 지원, 쿼터를 확인한다.")
    else:
        print("→ 판정: 주 모델 이름을 틀리게 했는데 전환 로그가 없다. 코드가 의도대로 동작하지 않는다.")
    return 0 if (ok and switched) else 1


def max_in_window(times: list[float], window: float) -> int:
    """times 안에서 길이 window 초짜리 구간 하나에 들어가는 최대 개수."""
    times = sorted(times)
    return max((sum(1 for u in times[i:] if u < t + window) for i, t in enumerate(times)), default=0)


def mode_rpm(args, ctx) -> int:
    api_key, model, fallback = ctx["api_key"], ctx["model"], ctx["fallback"]
    rpm = args.rpm if args.rpm is not None else int(os.environ.get("GEMINI_RPM") or DEFAULT_GEMINI_RPM)
    n = args.n
    print(f"[rpm] 모델 {model} 로 {n}개 호출을 스레드로 동시에 시작, 앱 설정과 같은 분당 제한 {rpm}회/모델.")
    print(f"      기대: 처음 {min(rpm, n)}개는 바로 나가고 나머지는 약 60초 뒤까지 대기한 뒤 나가며, 서버 429 는 0회 (전체 1~2분)")
    client = make_client(api_key, model, fallback, rpm=rpm)

    local = threading.local()
    events: list[tuple[str, float, float]] = []  # (모델, 요청 시도 시각, 제한기 통과 시각) — 시작 시각 기준 초
    lock = threading.Lock()
    t_all = time.perf_counter()
    for model_name, limiter in client._limiters.items():  # 진단용: 제한기의 대기 시간을 잰다(앱 코드는 바꾸지 않는다)
        original = limiter.acquire

        def timed(original=original, model_name=model_name):
            called = time.perf_counter()
            original()
            released = time.perf_counter()
            local.wait += released - called
            if local.sent_at is None:
                local.sent_at = released - t_all
            with lock:
                events.append((model_name, called - t_all, released - t_all))

        limiter.acquire = timed

    data, mime, sections, track, _ = ctx["prepared"]

    def one_call(i: int) -> dict:
        local.wait, local.sent_at = 0.0, None
        t0 = time.perf_counter()
        status = "성공"
        kind = "ok"
        try:
            # 앱의 무드 해석과 같은 프롬프트·스키마로 호출한다(mood.py 의 상수와 함수 재사용). 재시도로 429 를 가리지 않도록 interpret_moods 는 쓰지 않는다
            client.generate_json_with_audio(
                system=SYSTEM_PROMPT, user=_user_prompt(sections, track), schema=MOOD_SCHEMA, audio=data, mime_type=mime
            )
        except LLMError as e:
            text = str(e)
            if "429" in text or "RESOURCE_EXHAUSTED" in text:
                status, kind = "429 (서버 한도 초과)", "429"
            elif "503" in text or "UNAVAILABLE" in text:
                status, kind = "503 (서버 혼잡)", "503"
            else:
                status, kind = f"실패: {text[:80]}", "other"
        total = time.perf_counter() - t0
        return {"i": i, "sent_at": local.sent_at or 0.0, "wait": local.wait, "response": max(total - local.wait, 0.0), "status": status, "kind": kind}

    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(one_call, range(1, n + 1)))

    print(f"\n{'발송(+초)':>9}  {'#':>3}  {'제한 대기':>9}  {'응답 시간':>9}  결과")
    for r in sorted(results, key=lambda r: r["sent_at"]):
        print(f"{r['sent_at']:>9.1f}  {r['i']:>3}  {r['wait']:>8.1f}s  {r['response']:>8.1f}s  {r['status']}")

    by_model: dict[str, int] = {}
    for m, outcome in client.attempts:
        if outcome == "성공":
            by_model[m] = by_model.get(m, 0) + 1
    immediate = sum(1 for r in results if r["wait"] <= 0.05)
    waited = n - immediate
    c429 = sum(1 for r in results if r["kind"] == "429")
    c503 = sum(1 for r in results if r["kind"] == "503")
    other = sum(1 for r in results if r["kind"] == "other")
    print(f"\n총 {time.perf_counter() - t_all:.1f}초 — 바로 나간 호출 {immediate}회, 제한 대기 후 나간 호출 {waited}회, 429 {c429}회, 503 {c503}회, 그 밖의 실패 {other}회")
    print(f"실제로 답한 모델별 성공 횟수: {by_model or '(없음)'} / 전체 시도 {len(client.attempts)}회")

    # 판정은 주 모델의 제한기 기록으로 한다: 요청이 60초 안에 몰렸는지(수요) / 실제로 나간 간격(발송)
    slack = 0.5  # 시간 측정 오차 허용(초)
    called = [c for m, c, _ in events if m == model]
    sent = [r for m, _, r in events if m == model]
    peak_demand = max_in_window(called, WINDOW_SEC)
    peak_sent = max_in_window(sent, WINDOW_SEC - slack)
    print(f"주 모델 요청: 60초 안에 시도된 최대 {peak_demand}회, 60초 구간에 실제로 나간 최대 {peak_sent}회 (한도 {rpm}회)")
    if peak_demand <= rpm:
        print(f"→ 판정: 한도에 도달하지 못해 판단 불가 (60초 안에 {peak_demand}회만 시도돼 한도 {rpm}회를 넘지 않았다). --n 을 {rpm + 2} 이상으로 올린다.")
        return 2
    if peak_sent > rpm:
        print(f"→ 판정: 제한기가 한도를 지키지 못했다. 60초 구간에 {peak_sent}회가 나갔다(한도 {rpm}회).")
        return 1
    if c429:
        print("→ 판정: 앱의 제한(--rpm)을 지켰는데도 서버가 429 를 냈다. 서버의 실제 한도가 더 낮으니 GEMINI_RPM 을 낮춘다.")
        return 1
    note = f" 다만 503 {c503}회·기타 실패 {other}회가 있었다(서버 혼잡이면 한도와 무관)." if (c503 or other) else ""
    print(f"→ 판정: 제한기 동작 확인. 한도를 넘는 요청은 대기했고 60초 구간 최대 발송은 {peak_sent}회(≤ {rpm}), 429 는 0회.{note}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Gemini 실제 호출 확인 (DB 접속 없음)")
    parser.add_argument("mode", choices=["basic", "fallback", "rpm"])
    parser.add_argument("--track", type=Path, help="음원 파일(기본: demo-tracks/ 의 첫 mp3)")
    parser.add_argument("--n", type=int, default=12, help="rpm 모드의 연속 호출 수(기본 12)")
    parser.add_argument("--rpm", type=int, help="rpm 모드에서 쓸 모델당 분당 제한(기본: GEMINI_RPM 또는 앱 기본값)")
    args = parser.parse_args()

    load_env()
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("GEMINI_API_KEY 가 없다. 저장소 루트 .env 에 채우거나 환경변수로 넘긴다.")
        return 2
    print(f"GEMINI_API_KEY: 설정됨 (길이 {len(api_key)}, 값은 출력하지 않는다)")

    track_path = args.track or next(iter(sorted((ROOT / "demo-tracks").glob("*.mp3"))), None)
    if track_path is None or not track_path.exists():
        print("음원을 찾지 못했다. --track 으로 파일을 지정하거나 demo-tracks/ 에 mp3 를 둔다.")
        return 2

    capture = Capture()
    for name in ("stage_director.llm.gemini", "stage_director.mood"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.addFilter(RedactingFilter(api_key))
        lg.addHandler(capture)
        lg.propagate = False

    print(f"음원: {track_path.name} — 구간 분석(오프라인)...")
    prepared = prepare(track_path)
    print(f"  길이 {prepared[4]:.1f}초, 구간 {len(prepared[2])}개, 파일 {len(prepared[0]) / 1024 / 1024:.1f}MiB")
    ctx = {
        "api_key": api_key,
        "model": os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL,
        "fallback": os.environ.get("GEMINI_FALLBACK_MODEL") or DEFAULT_GEMINI_FALLBACK_MODEL,
        "prepared": prepared,
        "capture": capture,
    }
    try:
        code = {"basic": mode_basic, "fallback": mode_fallback, "rpm": mode_rpm}[args.mode](args, ctx)
    finally:
        if capture.lines:
            print("\n── 앱 로그(시간순) ──")
            print("\n".join(capture.lines).replace(api_key, "***"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
