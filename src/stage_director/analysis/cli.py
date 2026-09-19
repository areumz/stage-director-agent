"""1단계 분석 스파이크용 CLI: 음원 파일 하나를 분석해 JSON 을 stdout 으로 낸다 (육안 검토용).

사용: uv run python -m stage_director.analysis.cli path/to/song.mp3
"""

import argparse
import sys

from stage_director.analysis.measure import measure_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="음원 파일의 수치 측정 결과(JSON)를 출력한다")
    parser.add_argument("path", help="음원 파일 경로 (wav/mp3 등 librosa 가 읽는 형식)")
    args = parser.parse_args(argv)

    snapshot = measure_file(args.path)
    sys.stdout.write(snapshot.model_dump_json(by_alias=True, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
