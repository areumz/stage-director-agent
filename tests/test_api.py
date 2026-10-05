import contextlib
import json

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from contracts.stage_state import default_stage_state, merge_stage_state
from stage_director.api import create_app
from stage_director.audio import AudioError
from stage_director.jobs import InMemoryJobStore
from stage_director.llm.client import LLMError
from stage_director.llm.fake import FakeLLM
from stage_director.sequence import SequenceItem, validate_sequence
from stage_director.settings import Settings
from tests.conftest import PROPOSE_REQUEST, SEQUENCE_REQUEST, InlineExecutor

BODY = PROPOSE_REQUEST
KEY = "test-internal-key"
AUTH = {"X-Internal-Key": KEY}

LLM_OUTPUT = {
    "state": {
        "color": "#9F77DD",
        "spots": {k: {"on": True, "intensity": 800, "angle": 0.5} for k in ("left", "center", "right")},
        "camera": "audience",
        "smoke": {"density": 0.4, "color": "#ffffff"},
    },
    "rationale": "코러스 에너지가 곡 평균의 1.5배라 밝게",
}


def client(*llm_responses, internal_api_key: str = KEY) -> TestClient:
    settings = Settings(internal_api_key=internal_api_key, gemini_api_key="unused", gemini_model="unused", database_url="unused")
    return TestClient(create_app(settings, FakeLLM(*llm_responses)))


# ── X-Internal-Key ────────────────────────────────────────────


@pytest.mark.parametrize("headers", [{}, {"X-Internal-Key": "wrong"}, {"X-Internal-Key": ""}, {"X-Internal-Key": "키".encode()}])
def test_rejects_missing_or_wrong_key(headers):
    llm = FakeLLM(LLM_OUTPUT)
    app = create_app(Settings(internal_api_key=KEY, gemini_api_key="unused", gemini_model="unused", database_url="unused"), llm)
    response = TestClient(app).post("/propose", json=BODY, headers=headers)
    assert response.status_code == 401
    assert llm.calls == []  # 인증 실패로 LLM 비용이 나가지 않는다


def test_auth_is_checked_before_body_validation():
    assert client().post("/propose", json={}).status_code == 401


def test_docs_endpoints_are_not_exposed():
    c = client()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path, headers=AUTH).status_code == 404


# ── POST /propose ─────────────────────────────────────────────


def test_propose_returns_camel_case_item_and_issues():
    response = client(LLM_OUTPUT).post("/propose", json=BODY, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["item"]["sectionLabel"] == "chorus"
    assert (body["item"]["startSec"], body["item"]["endSec"], body["item"]["transitionMs"]) == (10, 30, 2000)
    assert body["item"]["state"]["camera"] == "audience"
    assert body["item"]["rationale"].startswith("코러스")
    assert body["energyRatio"] == pytest.approx(1.538, abs=1e-3)
    assert body["issues"] == []


def test_returned_state_survives_the_on_stage_merge_unchanged():
    # Next.js 가 이 state 를 프리셋 API 로 저장하고 씬이 mergeStageState 로 읽는다. 왕복해도 값이 변하면 안 된다 (penumbra 포함).
    state = client(LLM_OUTPUT).post("/propose", json=BODY, headers=AUTH).json()["item"]["state"]
    merged = merge_stage_state(state, default_stage_state("#000000"))
    assert merged.model_dump() == state
    assert state["spots"]["left"]["penumbra"] == 0.6


def test_reports_gate_issues_in_the_response():
    calm = {**BODY, "section": {"label": "intro", "startSec": 0, "endSec": 10}}
    issues = client(LLM_OUTPUT).post("/propose", json=calm, headers=AUTH).json()["issues"]
    assert [i["rule"] for i in issues] == ["calm_too_bright"]


def test_invalid_body_is_422():
    body = {**BODY, "artist": {**BODY["artist"], "color": "purple"}}
    assert client().post("/propose", json=body, headers=AUTH).status_code == 422


@pytest.mark.parametrize("field", ["startSec", "endSec"])
def test_nan_in_body_is_422_without_echoing_input(field):
    # TestClient 의 json= 은 NaN 을 거부하므로 원문을 직접 보낸다 (json.dumps 는 NaN 을 그대로 쓴다)
    body = {**BODY, "section": {**BODY["section"], field: float("nan")}}
    headers = {**AUTH, "Content-Type": "application/json"}
    response = client(LLM_OUTPUT).post("/propose", content=json.dumps(body), headers=headers)
    assert response.status_code == 422
    assert "input" not in response.text


def test_422_detail_entries_have_only_loc_msg_type():
    body = {**BODY, "artist": {**BODY["artist"], "color": "purple"}}
    detail = client().post("/propose", json=body, headers=AUTH).json()["detail"]
    assert detail and all(set(e) == {"loc", "msg", "type"} for e in detail)


def test_llm_failure_is_502():
    response = client(LLMError("a"), LLMError("b"), LLMError("c")).post("/propose", json=BODY, headers=AUTH)
    assert response.status_code == 502
    assert response.json() == {"detail": "llm_failed"}


@pytest.mark.parametrize("key", ["", "   "])
def test_refuses_to_start_with_blank_internal_key(key):
    with pytest.raises(ValueError):
        client(internal_api_key=key)


# ── Settings ──────────────────────────────────────────────────


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    settings = Settings.from_env()
    assert (settings.internal_api_key, settings.gemini_api_key, settings.database_url) == ("k", "g", "postgresql://x/y")
    assert settings.gemini_model  # 기본 모델이 있다


def test_settings_fallback_model_has_a_default_and_can_be_overridden(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    assert Settings.from_env().gemini_fallback_model  # 비워 두면 기본 예비 모델
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "my-backup")
    assert Settings.from_env().gemini_fallback_model == "my-backup"


@pytest.mark.parametrize("missing", ["INTERNAL_API_KEY", "GEMINI_API_KEY", "DATABASE_URL"])
def test_settings_refuse_to_start_without_keys(monkeypatch, missing):
    monkeypatch.setenv("INTERNAL_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv(missing, "")
    with pytest.raises(RuntimeError, match=missing):
        Settings.from_env()


# ── /runs 헬퍼 ────────────────────────────────────────────────

SEQUENCE_GOOD = {
    "state": {
        "color": "#9F77DD",
        "spots": {k: {"on": True, "intensity": 400, "angle": 0.5} for k in ("left", "center", "right")},
        "camera": "front",
        "smoke": {"density": 0.3, "color": "#ffffff"},
    },
    "rationale": "측정값에 맞춘 연출",
}


def runs_app(*llm_responses, internal_api_key: str = KEY):
    settings = Settings(internal_api_key=internal_api_key, gemini_api_key="unused", gemini_model="unused", database_url="unused")
    checkpointer_cm = lambda: contextlib.nullcontext(InMemorySaver())  # 테스트 전용, Postgres 대신 InMemorySaver
    return create_app(settings, FakeLLM(*llm_responses), checkpointer_cm, job_store=InMemoryJobStore(), executor=InlineExecutor())


RUN_BODY = {"threadId": "run-1", "context": SEQUENCE_REQUEST}


def resume_body(interrupt, kind, payload=None):
    body = {"interruptId": interrupt["interruptId"], "kind": kind}
    return {**body, "payload": payload} if payload is not None else body


# ── /runs ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method, path",
    [("post", "/runs"), ("get", "/runs/run-1"), ("post", "/runs/run-1/resume")],
)
def test_runs_require_internal_key(method, path):
    with TestClient(runs_app()) as c:
        response = c.request(method, path, json=RUN_BODY)
    assert response.status_code == 401


def test_create_run_returns_202_and_waits_for_the_first_interrupt():
    with TestClient(runs_app()) as c:
        response = c.post("/runs", json=RUN_BODY, headers=AUTH)
    assert response.status_code == 202
    body = response.json()
    assert body["threadId"] == "run-1" and body["status"] == "waiting_input"
    assert body["interrupt"]["kind"] == "confirm_sections" and len(body["interrupt"]["sections"]) == 2


def test_create_run_twice_is_idempotent():
    with TestClient(runs_app()) as c:
        first = c.post("/runs", json=RUN_BODY, headers=AUTH).json()
        second = c.post("/runs", json=RUN_BODY, headers=AUTH).json()
    assert second["interrupt"]["interruptId"] == first["interrupt"]["interruptId"]


def test_unknown_run_is_404():
    with TestClient(runs_app()) as c:
        response = c.get("/runs/nope", headers=AUTH)
    assert response.status_code == 404
    assert response.json() == {"detail": "thread_not_found"}


def test_full_flow_over_http():
    with TestClient(runs_app(SEQUENCE_GOOD, SEQUENCE_GOOD)) as c:
        first = c.post("/runs", json=RUN_BODY, headers=AUTH).json()
        sections = first["interrupt"]["sections"]
        r = c.post("/runs/run-1/resume", json=resume_body(first["interrupt"], "sections", {"sections": sections}), headers=AUTH)
        assert r.status_code == 202
        review = c.get("/runs/run-1", headers=AUTH).json()
        assert review["status"] == "waiting_input" and review["interrupt"]["kind"] == "review"
        c.post("/runs/run-1/resume", json=resume_body(review["interrupt"], "approve"), headers=AUTH)
        done = c.get("/runs/run-1", headers=AUTH).json()
    assert done["status"] == "done"
    items = [SequenceItem.model_validate(d) for d in done["result"]["items"]]
    assert validate_sequence(items, 60) == []


def test_stale_resume_is_409():
    with TestClient(runs_app()) as c:
        first = c.post("/runs", json=RUN_BODY, headers=AUTH).json()
        stale = {"interruptId": "run-1:9:confirm_sections", "kind": "sections", "payload": {"sections": first["interrupt"]["sections"]}}
        response = c.post("/runs/run-1/resume", json=stale, headers=AUTH)
    assert response.status_code == 409
    assert response.json() == {"detail": "stale_interrupt"}


def test_invalid_sections_resume_is_422_and_keeps_waiting():
    with TestClient(runs_app()) as c:
        first = c.post("/runs", json=RUN_BODY, headers=AUTH).json()
        gap = [{"label": "a", "startSec": 0, "endSec": 20}, {"label": "b", "startSec": 25, "endSec": 60}]
        response = c.post("/runs/run-1/resume", json=resume_body(first["interrupt"], "sections", {"sections": gap}), headers=AUTH)
        after = c.get("/runs/run-1", headers=AUTH).json()
    assert response.status_code == 422 and response.json()["detail"] == "invalid_sections"
    assert after["status"] == "waiting_input" and after["interrupt"]["interruptId"] == first["interrupt"]["interruptId"]


def test_unknown_resume_kind_is_422():
    with TestClient(runs_app()) as c:
        first = c.post("/runs", json=RUN_BODY, headers=AUTH).json()
        response = c.post("/runs/run-1/resume", json=resume_body(first["interrupt"], "bogus", {}), headers=AUTH)
    assert response.status_code == 422


def test_create_run_with_invalid_body_is_422():
    body = {"threadId": "run-1", "context": {**SEQUENCE_REQUEST, "durationSec": 0}}
    with TestClient(runs_app()) as c:
        response = c.post("/runs", json=body, headers=AUTH)
    assert response.status_code == 422


def test_app_refuses_to_start_when_checkpointer_is_unavailable():
    settings = Settings(internal_api_key=KEY, gemini_api_key="unused", gemini_model="unused", database_url="unused")

    @contextlib.contextmanager
    def broken_checkpointer():
        raise ConnectionError("checkpointer db is down")
        yield  # pragma: no cover

    app = create_app(settings, FakeLLM(), broken_checkpointer)
    with pytest.raises(ConnectionError), TestClient(app):
        pass


def test_run_audio_fetches_use_the_configured_host_allowlist(monkeypatch):
    seen = {}

    def fake_fetch(url, **kwargs):
        seen.update(kwargs)
        raise AudioError("테스트에는 네트워크가 없다")  # 무드 해석은 비치명적이라 실행은 계속된다

    monkeypatch.setattr("stage_director.api.fetch_audio", fake_fetch)
    settings = Settings(
        internal_api_key=KEY, gemini_api_key="unused", gemini_model="unused", database_url="unused", audio_allowed_hosts=("supabase.co",)
    )
    app = create_app(settings, FakeLLM(), lambda: contextlib.nullcontext(InMemorySaver()), job_store=InMemoryJobStore(), executor=InlineExecutor())
    body = {"threadId": "r1", "context": {**SEQUENCE_REQUEST, "audioUrl": "https://abc.supabase.co/a.mp3"}}
    with TestClient(app) as c:
        assert c.post("/runs", json=body, headers=AUTH).json()["status"] == "waiting_input"
    assert seen["allowed_hosts"] == ("supabase.co",)


# ── /analyze ──────────────────────────────────────────────────

ANALYZE_BODY = {"jobId": "a1", "audioUrl": "https://abc.supabase.co/a.mp3?token=t"}
ANALYSIS_RESULT = {"analysis": {"durationSec": 12.5, "bpm": 100.0}, "fileHash": "h"}


def analyze_app(monkeypatch, allowed=("supabase.co",)):
    monkeypatch.setattr("stage_director.api.fetch_audio", lambda url, **kw: (b"audio", "audio/mpeg"))
    monkeypatch.setattr("stage_director.api.build_result", lambda data, mime: ANALYSIS_RESULT)
    settings = Settings(
        internal_api_key=KEY, gemini_api_key="unused", gemini_model="unused", database_url="unused", audio_allowed_hosts=allowed
    )
    return create_app(
        settings,
        FakeLLM(),
        lambda: contextlib.nullcontext(InMemorySaver()),
        job_store=InMemoryJobStore(),
        executor=InlineExecutor(),
        analysis_executor=InlineExecutor(),
    )


@pytest.mark.parametrize(("method", "path"), [("post", "/analyze"), ("get", "/analyze/a1")])
def test_analyze_endpoints_require_the_internal_key(monkeypatch, method, path):
    with TestClient(analyze_app(monkeypatch)) as c:
        assert c.request(method, path, json=ANALYZE_BODY).status_code == 401


def test_create_analysis_returns_202_and_the_result(monkeypatch):
    with TestClient(analyze_app(monkeypatch)) as c:
        response = c.post("/analyze", json=ANALYZE_BODY, headers=AUTH)
        polled = c.get("/analyze/a1", headers=AUTH)
    assert response.status_code == 202
    assert response.json() == {"jobId": "a1", "status": "done", "progress": 1.0, "result": ANALYSIS_RESULT, "error": None}
    assert polled.json() == response.json()


def test_analyze_rejects_a_host_outside_the_allowlist(monkeypatch):
    with TestClient(analyze_app(monkeypatch)) as c:
        response = c.post("/analyze", json={**ANALYZE_BODY, "audioUrl": "https://evil.example.com/a.mp3"}, headers=AUTH)
    assert response.status_code == 422 and response.json()["detail"] == "invalid_audio_url"


def test_unknown_analysis_is_404(monkeypatch):
    with TestClient(analyze_app(monkeypatch)) as c:
        response = c.get("/analyze/nope", headers=AUTH)
    assert response.status_code == 404 and response.json()["detail"] == "job_not_found"


def test_run_and_analysis_ids_do_not_cross(monkeypatch):
    with TestClient(analyze_app(monkeypatch)) as c:
        c.post("/analyze", json=ANALYZE_BODY, headers=AUTH)
        c.post("/runs", json=RUN_BODY, headers=AUTH)
        assert c.get("/runs/a1", headers=AUTH).status_code == 404
        assert c.get("/analyze/run-1", headers=AUTH).status_code == 404


# ── 상태 확인 ─────────────────────────────────────────────────


def test_health_needs_no_key_and_reveals_nothing_else():
    with TestClient(runs_app()) as c:
        response = c.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_ready_reports_ok_when_the_job_store_answers():
    with TestClient(runs_app()) as c:
        response = c.get("/ready")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_ready_returns_503_without_leaking_the_error_when_the_database_is_down():
    class DownStore(InMemoryJobStore):
        def get(self, job_id):
            raise RuntimeError("connection to server at secret-host failed")

    settings = Settings(internal_api_key=KEY, gemini_api_key="unused", gemini_model="unused", database_url="unused")
    app = create_app(settings, FakeLLM(), lambda: contextlib.nullcontext(InMemorySaver()), job_store=DownStore(), executor=InlineExecutor())
    with TestClient(app) as c:
        response = c.get("/ready")
    assert response.status_code == 503 and response.json() == {"status": "db_unavailable"}
    assert "secret-host" not in response.text
