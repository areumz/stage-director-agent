"""FastAPI 앱. Next.js API Routes 만 호출하는 내부 서비스라 모든 엔드포인트가 X-Internal-Key 를 요구.

실행: uv run --env-file .env uvicorn --factory stage_director.api:create_app
"""

import secrets
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager, asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from langgraph.checkpoint.base import BaseCheckpointSaver

from stage_director.checkpointer import postgres_checkpointer
from stage_director.graph import build_sequence_graph
from stage_director.graph_nodes import MAX_CONCURRENT_PROPOSALS
from stage_director.llm.client import LLMClient, LLMError
from stage_director.llm.gemini import GeminiClient
from stage_director.models import (
    ProposeRequest,
    SectionProposal,
    SequenceRequest,
    SequenceResponse,
)
from stage_director.propose import propose_section
from stage_director.settings import Settings


def create_app(
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    checkpointer_cm: Callable[[], AbstractContextManager[BaseCheckpointSaver]] | None = None,
) -> FastAPI:
    """settings·llm·checkpointer_cm 은 테스트에서 주입. 운영에서는 환경변수 + Gemini + Postgres(Neon)"""
    settings = settings or Settings.from_env()
    if not settings.internal_api_key.strip():
        raise ValueError("INTERNAL_API_KEY 가 비어 있다")
    llm = llm or GeminiClient(settings.gemini_api_key, settings.gemini_model)
    checkpointer_cm = checkpointer_cm or (lambda: postgres_checkpointer(settings.database_url))

    def require_internal_key(x_internal_key: str | None = Header(default=None)) -> None:
        if x_internal_key is None or not secrets.compare_digest(x_internal_key.encode(), settings.internal_api_key.encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 체크포인터가 뜨지 않으면 서비스 전체가 기동에 실패한다(요청 단위가 아니라 서비스 단위 fail-fast).
        with checkpointer_cm() as saver:
            app.state.sequence_graph = build_sequence_graph(llm, checkpointer=saver)
            yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def _invalid_body(request, exc):
        # 422 본문에 입력값을 되돌려주지 않는다: NaN 같은 값이 JSON 직렬화에 실패해 500 이 된다
        errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.post("/propose", dependencies=[Depends(require_internal_key)])
    def propose(req: ProposeRequest) -> SectionProposal:
        # ponytail: 동기 요청. 최악은 MAX_RETRIES+1 = 3회 x TIMEOUT_MS 60초 = 약 3분. 4단계에서 작업+폴링으로
        try:
            return propose_section(llm, req)
        except LLMError:
            raise HTTPException(status_code=502, detail="llm_failed")

    @app.post("/sequence", dependencies=[Depends(require_internal_key)])
    def sequence(req: SequenceRequest) -> SequenceResponse:
        # ponytail: 동기 요청이고 threadId 를 매번 새로 만든다. /runs 의 멱등 프로토콜(스펙 §4.2)은 4단계 몫.
        # 구간 수만큼 propose_section 이 걸리므로 /propose 보다 훨씬 오래 걸릴 수 있다 — 4단계에서 작업+폴링.
        thread_id = str(uuid.uuid4())
        config = {"configurable": {"thread_id": thread_id}, "max_concurrency": MAX_CONCURRENT_PROPOSALS}
        try:
            result = app.state.sequence_graph.invoke({"request": req}, config=config)
        except LLMError:
            raise HTTPException(status_code=502, detail="llm_failed")
        return SequenceResponse(
            thread_id=thread_id, sections=result["sections"], items=result["final_items"], issues=result["final_issues"]
        )

    return app
