"""시드 곡 오프라인 분석. 음원 파일마다 JSON 한 개.

Python 은 Supabase 를 읽고 쓰지 않으므로 결과를 파일로 내보내고, on-stage 의 시드 스크립트가 audio_tracks 행
(analysis, file_hash, duration_sec)에 넣음. 업로드 곡의 분석 작업과 같은 build_result 를 써서 결과의 모양이 같음.

사용: uv run python -m stage_director.analysis.seed demo-tracks/*.mp3 --out seed-analysis
"""

import argparse
import json
import mimetypes
from pathlib import Path

from stage_director.analysis.sections import detect_sections
from stage_director.analysis.snapshot import parse_analysis
from stage_director.analyzer import AnalysisError, build_result


def analyze_seed(path: Path) -> dict:
    """{"fileName", "fileHash", "durationSec", "analysis"}. 분석할 수 없는 파일이면 AnalysisError."""
    result = build_result(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    return {
        "fileName": path.name,
        "fileHash": result["fileHash"],
        "durationSec": result["analysis"]["durationSec"],
        "analysis": result["analysis"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="시드 곡의 분석 결과를 JSON 파일로 내보낸다")
    parser.add_argument("paths", nargs="+", type=Path, help="음원 파일")
    parser.add_argument("--out", type=Path, default=Path("seed-analysis"), help="JSON 을 쓸 폴더")
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    failed = 0
    for path in args.paths:
        try:
            seed = analyze_seed(path)
        except AnalysisError as e:
            print(f"{path.name}: 실패 ({e.code})")
            failed += 1
            continue
        except OSError as e:  # 없는·읽을 수 없는 경로도 한 파일의 실패일 뿐이다
            print(f"{path.name}: 실패 ({e.strerror or e})")
            failed += 1
            continue
        target = args.out / f"{path.stem}.json"
        target.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
        snapshot = parse_analysis(seed["analysis"])
        sections = detect_sections(snapshot.energy_curve, snapshot.duration_sec)
        print(f"{path.name}: {seed['durationSec']}초, BPM {snapshot.bpm}, 구간 {len(sections)}개 → {target}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
