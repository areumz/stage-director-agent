"""환경변수 설정. 키가 없으면 서비스가 뜨지 않는다(빈 키로 인증이 열리는 것을 막는다)."""

import os
from dataclasses import dataclass

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


@dataclass(frozen=True)
class Settings:
    internal_api_key: str  # Next.js 가 X-Internal-Key 로 보내는 값. 서버 환경변수에만 둔다
    gemini_api_key: str
    gemini_model: str

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
        )
