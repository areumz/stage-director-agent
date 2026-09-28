"""FastAPI 앱. Next.js API Routes 만 호출하는 내부 서비스라 모든 엔드포인트가 X-Internal-Key 를 요구한다 (스펙 §4.2).

실행: uv run --env-file .env uvicorn --factory stage_director.api:create_app
"""

import secrets

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from stage_director.llm.client import LLMClient, LLMError
from stage_director.llm.gemini import GeminiClient
from stage_director.models import ProposeRequest, SectionProposal
from stage_director.propose import propose_section
from stage_director.settings import Settings


def create_app(settings: Settings | None = None, llm: LLMClient | None = None) -> FastAPI:
    """settings 와 llm 은 테스트에서 주입한다. 운영에서는 환경변수와 Gemini 를 쓴다."""
    settings = settings or Settings.from_env()
    if not settings.internal_api_key.strip():
        raise ValueError("INTERNAL_API_KEY 가 비어 있다")
    llm = llm or GeminiClient(settings.gemini_api_key, settings.gemini_model)

    def require_internal_key(x_internal_key: str | None = Header(default=None)) -> None:
        # 상수 시간 비교. 비 ASCII 헤더에도 TypeError 가 나지 않게 bytes 로 비교한다
        if x_internal_key is None or not secrets.compare_digest(x_internal_key.encode(), settings.internal_api_key.encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    # 스키마 문서 엔드포인트도 인증 밖의 엔드포인트라 끈다
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def _invalid_body(request, exc):
        # 422 본문에 입력값을 되돌려주지 않는다: NaN 같은 값이 JSON 직렬화에 실패해 500 이 된다
        errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.post("/propose", dependencies=[Depends(require_internal_key)])
    def propose(req: ProposeRequest) -> SectionProposal:
        # ponytail: 동기 요청. LLM 응답을 기다리는 동안 연결을 잡는다. 시퀀스 단계에서 작업+폴링(스펙 D4)으로 바꾼다.
        # 최악은 MAX_RETRIES+1 = 3회 x TIMEOUT_MS 60초 = 약 3분. Next.js/Vercel 라우트 제한이 더 짧으면 llm/gemini.py 의 TIMEOUT_MS 를 낮추거나 작업+폴링으로 옮긴다
        try:
            return propose_section(llm, req)
        except LLMError:
            raise HTTPException(status_code=502, detail="llm_failed")

    return app
