"""환경변수 설정"""

import os
from dataclasses import dataclass

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_GEMINI_FALLBACK_MODEL = "gemini-3.6-flash"  # 주 모델이 혼잡할 때 쓴다. 주 모델과 같게 두면 예비 시도를 하지 않는다


@dataclass(frozen=True)
class Settings:
    internal_api_key: str  # Next.js 가 X-Internal-Key 로 보내는 값. 서버 환경변수에만 둔다
    gemini_api_key: str
    gemini_model: str
    database_url: str  # LangGraph 체크포인터. 로컬 docker-compose, 배포 시 Neon (스펙 D2)
    gemini_fallback_model: str = DEFAULT_GEMINI_FALLBACK_MODEL

    @classmethod
    def from_env(cls) -> "Settings":
        def required(name: str) -> str:
            value = os.environ.get(name, "")
            if not value:
                raise RuntimeError(f"환경변수 {name} 가 설정되지 않았다")
            return value

        return cls(
            internal_api_key=required("INTERNAL_API_KEY"),
            gemini_api_key=required("GEMINI_API_KEY"),
            gemini_model=os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL,
            database_url=required("DATABASE_URL"),
            gemini_fallback_model=os.environ.get("GEMINI_FALLBACK_MODEL") or DEFAULT_GEMINI_FALLBACK_MODEL,
        )
