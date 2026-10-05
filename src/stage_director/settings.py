"""환경변수 설정"""

import os
from dataclasses import dataclass

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_GEMINI_FALLBACK_MODEL = "gemini-3.6-flash"  # 주 모델이 혼잡할 때 쓴다. 주 모델과 같게 두면 예비 시도를 하지 않는다
DEFAULT_GEMINI_RPM = 10  # 모델당 분당 요청 수. 실제 계정 한도에 맞춰 GEMINI_RPM 으로 올린다. 0 이면 제한하지 않는다


@dataclass(frozen=True)
class Settings:
    internal_api_key: str  # Next.js 가 X-Internal-Key 로 보내는 값. 서버 환경변수에만 둔다
    gemini_api_key: str
    gemini_model: str
    database_url: str  # LangGraph 체크포인터. 로컬 docker-compose, 배포 시 Neon (스펙 D2)
    gemini_fallback_model: str = DEFAULT_GEMINI_FALLBACK_MODEL
    gemini_rpm: int = DEFAULT_GEMINI_RPM

    @classmethod
    def from_env(cls) -> "Settings":
        def required(name: str) -> str:
            value = os.environ.get(name, "")
            if not value:
                raise RuntimeError(f"환경변수 {name} 가 설정되지 않았다")
            return value

        def non_negative_int(name: str, default: int) -> int:
            raw = os.environ.get(name) or str(default)
            try:
                value = int(raw)
            except ValueError:
                value = -1
            if value < 0:
                raise RuntimeError(f"환경변수 {name} 는 0 이상의 정수여야 한다: {raw!r}")
            return value

        return cls(
            internal_api_key=required("INTERNAL_API_KEY"),
            gemini_api_key=required("GEMINI_API_KEY"),
            gemini_model=os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL,
            database_url=required("DATABASE_URL"),
            gemini_fallback_model=os.environ.get("GEMINI_FALLBACK_MODEL") or DEFAULT_GEMINI_FALLBACK_MODEL,
            gemini_rpm=non_negative_int("GEMINI_RPM", DEFAULT_GEMINI_RPM),
        )
