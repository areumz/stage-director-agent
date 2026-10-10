"""승인까지 끝난 예시 시퀀스 JSON 을 만든다. on-stage 의 화면 개발과 "시드 곡 폴백(미리 만든 예시 시퀀스)"에 쓴다.

DB(Neon 포함)와 서버에는 접속하지 않는다. 음원 분석은 오프라인(librosa)으로 하고, 결과는 `/runs` 가 `done` 일 때 주는
`result` 와 같은 모양 `{sections, items, issues}` 이다.

두 가지 모드:
- rule   (기본) Gemini 를 부르지 않는다. 에너지 비로 규칙 기반 연출을 만든다. 쿼터를 쓰지 않는다.
- gemini 실제 서비스와 같은 그래프(구간 확인 → 제안 → 승인)를 로컬에서 돌린다. Gemini 호출을 쓴다(곡당 5~9회). GEMINI_API_KEY 필요.

사용: uv run python scripts/make_example_sequence.py [--mode rule|gemini] [음원 ...] [--out seed-analysis]
음원을 생략하면 demo-tracks/ 의 mp3 전부. 결과는 <out>/<곡 이름>.sequence.json (seed-analysis/ 는 git 에 올리지 않는다).
"""

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from langgraph.checkpoint.memory import InMemorySaver

from contracts.stage_state import default_stage_state
from stage_director.analysis.sections import detect_sections
from stage_director.analysis.seed import analyze_seed
from stage_director.analysis.snapshot import parse_analysis, section_energy_ratio
from stage_director.gate import (
    CALM_ENERGY_RATIO,
    CALM_MAX_INTENSITY,
    run_gate,
    sanitize_state,
)
from stage_director.graph import build_sequence_graph
from stage_director.jobs import InMemoryJobStore
from stage_director.llm.gemini import GeminiClient
from stage_director.models import (
    ApproveResume,
    Issue,
    Section,
    SectionsPayload,
    SectionsResume,
    SequenceRequest,
)
from stage_director.propose import DEFAULT_TRANSITION_MS
from stage_director.runner import Runner
from stage_director.sequence import SequenceItem, validate_sequence
from stage_director.settings import (
    DEFAULT_GEMINI_FALLBACK_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GEMINI_RPM,
)

FIXTURE = ROOT / "tests" / "fixtures" / "sequence_request.json"  # 아티스트(aurora) 컨텍스트를 빌려 쓴다
CAMERA_CYCLE = ("front", "audience", "top")
PLACEHOLDER_AUDIO_URL = "https://local.invalid/example.mp3"  # gemini 모드에서 무드 노드를 켜는 자리. 실제 내려받기는 fetch 대체 함수가 한다


def build_request(track_path: Path) -> tuple[SequenceRequest, dict]:
    """음원을 오프라인으로 분석해 /runs 의 context 와 같은 요청을 만든다."""
    seed = analyze_seed(track_path)
    base = json.loads(FIXTURE.read_text(encoding="utf-8"))
    request = SequenceRequest.model_validate(
        {"track": {"title": track_path.stem}, "artist": base["artist"], "presets": [], "analysis": seed["analysis"], "durationSec": seed["durationSec"]}
    )
    return request, seed


def rule_based_result(request: SequenceRequest) -> dict:
    """Gemini 없이 만든 예시. 에너지 비가 클수록 밝게(단조 증가라 에너지-밝기 방향 규칙을 지킨다), 잔잔한 구간은 500 이하."""
    snapshot = parse_analysis(request.analysis)
    sections = detect_sections(snapshot.energy_curve, request.duration_sec)
    ratios = [section_energy_ratio(snapshot.energy_curve, s.start_sec, s.end_sec) for s in sections]
    color = request.artist.color
    items = []
    for i, (s, ratio) in enumerate(zip(sections, ratios, strict=True)):
        calm = ratio <= CALM_ENERGY_RATIO
        intensity = min(CALM_MAX_INTENSITY, 200 + 300 * ratio) if calm else min(900.0, 300 + 500 * ratio)
        intensity = float(round(intensity / 10) * 10)
        spot = {"on": True, "intensity": intensity, "angle": 0.5}
        raw = {
            "color": color,
            "spots": {"left": {**spot, "on": not calm}, "center": spot, "right": {**spot, "on": not calm}},  # 잔잔한 구간은 가운데만 켠다
            "camera": CAMERA_CYCLE[i % len(CAMERA_CYCLE)],
            "smoke": {"density": round(min(1.0, 0.1 + 0.3 * ratio), 2), "color": "#ffffff"},
        }
        state, _ = sanitize_state(raw, default_stage_state(color), i)
        rationale = f"에너지 비 {ratio:.2f}: 곡 평균 대비 {'잔잔한' if calm else '강한'} 구간이라 밝기를 {intensity:g} 로 한다. (규칙 기반 예시)"
        items.append(
            SequenceItem(
                section_label=s.label,
                start_sec=s.start_sec,
                end_sec=s.end_sec,
                transition_ms=int(min(DEFAULT_TRANSITION_MS, (s.end_sec - s.start_sec) * 1000)),
                state=state,
                rationale=rationale,
            )
        )
    violations = validate_sequence(items, request.duration_sec)
    if violations:
        raise SystemExit(f"예시 시퀀스가 불변식을 어겼다: {violations}")
    issues = [Issue(rule=g.rule, message=g.message, idx=g.idx) for g in run_gate(items, ratios, color)]
    return {
        "sections": [s.model_dump(by_alias=True) for s in sections],
        "items": [i.model_dump(by_alias=True, mode="json") for i in items],
        "issues": [i.model_dump(by_alias=True) for i in issues],
    }


class _InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def pipeline_result(llm, request: SequenceRequest, fetch) -> dict:
    """실제 서비스와 같은 Runner·그래프로 구간 확인(그대로 승인) → 제안 → 최종 승인까지 돌려 result 를 돌려준다."""
    runner = Runner(build_sequence_graph(llm, InMemorySaver(), fetch=fetch), InMemoryJobStore(), _InlineExecutor())

    def expect(status, kind: str):
        if status.status != "waiting_input" or status.interrupt["kind"] != kind:
            raise SystemExit(f"{kind} 대기 상태가 아니다: status={status.status} error={status.error}")
        return status

    status = expect(runner.start("example", request), "confirm_sections")
    sections = [Section.model_validate(s) for s in status.interrupt["sections"]]
    status = runner.resume("example", SectionsResume(interrupt_id=status.interrupt["interruptId"], kind="sections", payload=SectionsPayload(sections=sections)))
    status = expect(status, "review")
    status = runner.resume("example", ApproveResume(interrupt_id=status.interrupt["interruptId"], kind="approve"))
    if status.status != "done":
        raise SystemExit(f"완료되지 않았다: status={status.status} error={status.error}")
    return status.result


def load_gemini_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        name, _, value = line.strip().partition("=")
        if name in ("GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_FALLBACK_MODEL", "GEMINI_RPM") and value.strip():
            os.environ.setdefault(name, value.strip().strip("\"'"))


def gemini_result(track_path: Path, request: SequenceRequest) -> dict:
    load_gemini_env()
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY 가 없다. 저장소 루트 .env 에 채우거나 환경변수로 넘긴다.")
    llm = GeminiClient(
        api_key,
        os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL,
        fallback_model=os.environ.get("GEMINI_FALLBACK_MODEL") or DEFAULT_GEMINI_FALLBACK_MODEL,
        rpm=int(os.environ.get("GEMINI_RPM") or DEFAULT_GEMINI_RPM),
    )
    data = track_path.read_bytes()
    mime = mimetypes.guess_type(track_path.name)[0] or "audio/mpeg"
    with_audio = request.model_copy(update={"audio_url": PLACEHOLDER_AUDIO_URL})  # 무드 해석에 로컬 음원을 쓰도록 fetch 를 대신 넘긴다
    return pipeline_result(llm, with_audio, fetch=lambda url: (data, mime))


def main() -> int:
    parser = argparse.ArgumentParser(description="승인까지 끝난 예시 시퀀스 JSON 만들기 (DB 접속 없음)")
    parser.add_argument("tracks", nargs="*", type=Path, help="음원 파일(기본: demo-tracks/*.mp3)")
    parser.add_argument("--mode", choices=["rule", "gemini"], default="rule")
    parser.add_argument("--out", type=Path, default=ROOT / "seed-analysis")
    args = parser.parse_args()

    tracks = args.tracks or sorted((ROOT / "demo-tracks").glob("*.mp3"))
    if not tracks:
        print("음원이 없다. 경로를 넘기거나 demo-tracks/ 에 mp3 를 둔다.")
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    for track_path in tracks:
        request, seed = build_request(track_path)
        result = rule_based_result(request) if args.mode == "rule" else gemini_result(track_path, request)
        target = args.out / f"{track_path.stem}.sequence.json"
        document = {
            "fileName": track_path.name,
            "fileHash": seed["fileHash"],
            "durationSec": seed["durationSec"],
            "artistSlug": request.artist.slug,
            "generatedBy": "rule-based example (no LLM)" if args.mode == "rule" else "gemini",
            "result": result,
        }
        target.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        labels = ", ".join(i["sectionLabel"] for i in result["items"])
        print(f"{track_path.name}: 구간 {len(result['items'])}개({labels}), 경고 {len(result['issues'])}개 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
