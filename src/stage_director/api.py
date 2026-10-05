"""FastAPI 앱. Next.js API Routes 만 호출하는 내부 서비스라 모든 엔드포인트가 X-Internal-Key 를 요구.

실행: uv run --env-file .env uvicorn --factory stage_director.api:create_app
"""

import logging
import secrets
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import AbstractContextManager, asynccontextmanager
from functools import partial

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from langgraph.checkpoint.base import BaseCheckpointSaver

from stage_director.analyzer import (
    MAX_CONCURRENT_ANALYSES,
    AnalysisRunner,
    build_result,
)
from stage_director.audio import fetch_audio
from stage_director.background import Periodic
from stage_director.checkpointer import postgres_checkpointer
from stage_director.graph import build_sequence_graph
from stage_director.jobs import JobStore, PostgresJobStore
from stage_director.llm.client import LLMClient, LLMError
from stage_director.llm.gemini import GeminiClient
from stage_director.models import (
    AnalysisStatus,
    AnalyzeCreate,
    ProposeRequest,
    ResumeRequest,
    RunCreate,
    RunStatus,
    SectionProposal,
)
from stage_director.propose import propose_section
from stage_director.retention import RETENTION_INTERVAL_SEC, purge
from stage_director.runner import MAX_CONCURRENT_RUNS, RunError, Runner
from stage_director.settings import Settings

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    checkpointer_cm: Callable[[], AbstractContextManager[BaseCheckpointSaver]] | None = None,
    job_store: JobStore | None = None,
    executor: Executor | None = None,
    analysis_executor: Executor | None = None,
) -> FastAPI:
    """settings·llm·checkpointer_cm·job_store·executor·analysis_executor 는 테스트에서 주입. 운영에서는 환경변수 + Gemini + Postgres(Neon) + 스레드 풀 2개(그래프·분석)"""
    settings = settings or Settings.from_env()
    if not settings.internal_api_key.strip():
        raise ValueError("INTERNAL_API_KEY 가 비어 있다")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")  # 이미 설정돼 있으면 아무 일도 하지 않는다
    log.info("Gemini 모델 %s (예비 %s), 모델당 분당 %s회", settings.gemini_model, settings.gemini_fallback_model, settings.gemini_rpm or "무제한")
    llm = llm or GeminiClient(
        settings.gemini_api_key, settings.gemini_model, fallback_model=settings.gemini_fallback_model, rpm=settings.gemini_rpm
    )
    checkpointer_cm = checkpointer_cm or (lambda: postgres_checkpointer(settings.database_url))

    def require_internal_key(x_internal_key: str | None = Header(default=None)) -> None:
        if x_internal_key is None or not secrets.compare_digest(x_internal_key.encode(), settings.internal_api_key.encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 체크포인터가 뜨지 않으면 서비스 전체가 기동에 실패
        with checkpointer_cm() as saver:
            store = job_store or PostgresJobStore(saver.conn)
            purge(store, saver)  # 먼저: fail_running 이 updated_at 을 갱신하기 전에 방치된 작업을 정리
            store.fail_running()  # 죽기 전에 queued·running 이던 작업을 error(interrupted) 로. 사용자가 다시 시도하면 체크포인트에서 재개
            pool = executor or ThreadPoolExecutor(max_workers=MAX_CONCURRENT_RUNS)
            graph = build_sequence_graph(llm, checkpointer=saver, fetch=partial(fetch_audio, allowed_hosts=settings.audio_allowed_hosts))
            app.state.runner = Runner(graph, store, pool)
            analysis_pool = analysis_executor or ThreadPoolExecutor(max_workers=MAX_CONCURRENT_ANALYSES)
            app.state.analyzer = AnalysisRunner(
                store, analysis_pool, fetch=fetch_audio, build=build_result, allowed_hosts=settings.audio_allowed_hosts
            )
            retention = Periodic(lambda: purge(store, saver), RETENTION_INTERVAL_SEC, "retention")  # 오래 떠 있는 인스턴스도 정리한다
            retention.start()
            try:
                yield
            finally:
                retention.stop()
                # shutdown 은 이미 도는 그래프 스레드를 멈추지 못한다(인터프리터 종료 때 join). 그 작업은 queued·running 으로 남고 다음 기동의 fail_running 이 복구
                if executor is None:
                    pool.shutdown(wait=False, cancel_futures=True)
                if analysis_executor is None:
                    analysis_pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def _invalid_body(request, exc):
        # 422 본문에 입력값을 되돌려주지 않는다: NaN 같은 값이 JSON 직렬화에 실패해 500 이 된다
        errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(RunError)
    async def _run_error(request, exc):
        content = {"detail": exc.code, **({"message": exc.message} if exc.message else {})}
        return JSONResponse(status_code=exc.status, content=content)

    @app.post("/propose", dependencies=[Depends(require_internal_key)])
    def propose(req: ProposeRequest) -> SectionProposal:
        # ponytail: 동기 요청. 최악은 MAX_RETRIES+1 = 3회 x TIMEOUT_MS 60초 = 약 3분. 필요하면 /runs 처럼 작업+폴링으로
        try:
            return propose_section(llm, req)
        except LLMError:
            raise HTTPException(status_code=502, detail="llm_failed")

    @app.post("/runs", status_code=202, dependencies=[Depends(require_internal_key)])
    def create_run(body: RunCreate) -> RunStatus:
        return app.state.runner.start(body.thread_id, body.context)

    @app.get("/runs/{thread_id}", dependencies=[Depends(require_internal_key)])
    def get_run(thread_id: str) -> RunStatus:
        return app.state.runner.status(thread_id)

    @app.post("/runs/{thread_id}/resume", status_code=202, dependencies=[Depends(require_internal_key)])
    def resume_run(thread_id: str, body: ResumeRequest) -> RunStatus:
        return app.state.runner.resume(thread_id, body)

    @app.post("/analyze", status_code=202, dependencies=[Depends(require_internal_key)])
    def create_analysis(body: AnalyzeCreate) -> AnalysisStatus:
        return app.state.analyzer.start(body.job_id, body.audio_url)

    @app.get("/analyze/{job_id}", dependencies=[Depends(require_internal_key)])
    def get_analysis(job_id: str) -> AnalysisStatus:
        return app.state.analyzer.status(job_id)

    return app
