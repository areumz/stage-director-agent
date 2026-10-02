import contextlib
import json

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from contracts.stage_state import default_stage_state, merge_stage_state
from stage_director.api import create_app
from stage_director.llm.client import LLMError
from stage_director.llm.fake import FakeLLM
from stage_director.sequence import SequenceItem, validate_sequence
from stage_director.settings import Settings
from tests.conftest import PROPOSE_REQUEST, SEQUENCE_REQUEST

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


@pytest.mark.parametrize("missing", ["INTERNAL_API_KEY", "GEMINI_API_KEY", "DATABASE_URL"])
def test_settings_refuse_to_start_without_keys(monkeypatch, missing):
    monkeypatch.setenv("INTERNAL_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv(missing, "")
    with pytest.raises(RuntimeError, match=missing):
        Settings.from_env()


# ── POST /sequence ───────────────────────────────────────────

SEQUENCE_GOOD = {
    "state": {
        "color": "#9F77DD",
        "spots": {k: {"on": True, "intensity": 400, "angle": 0.5} for k in ("left", "center", "right")},
        "camera": "front",
        "smoke": {"density": 0.3, "color": "#ffffff"},
    },
    "rationale": "측정값에 맞춘 연출",
}


def sequence_app(*llm_responses, internal_api_key: str = KEY):
    settings = Settings(internal_api_key=internal_api_key, gemini_api_key="unused", gemini_model="unused", database_url="unused")
    checkpointer_cm = lambda: contextlib.nullcontext(InMemorySaver())  # 테스트 전용, Postgres 대신 InMemorySaver
    return create_app(settings, FakeLLM(*llm_responses), checkpointer_cm)


def test_sequence_requires_internal_key():
    app = sequence_app()  # FakeLLM 응답을 큐에 넣지 않는다 — 인증 실패라 그래프까지 가면 안 된다
    with TestClient(app) as c:
        response = c.post("/sequence", json=SEQUENCE_REQUEST)
    assert response.status_code == 401


def test_sequence_returns_a_valid_contiguous_sequence_with_a_fresh_thread_id():
    app = sequence_app(SEQUENCE_GOOD, SEQUENCE_GOOD)
    with TestClient(app) as c:
        response = c.post("/sequence", json=SEQUENCE_REQUEST, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["threadId"]
    items = [SequenceItem.model_validate(d) for d in body["items"]]
    assert validate_sequence(items, 60) == []
    assert len(body["sections"]) == 2


def test_sequence_llm_failure_is_502():
    app = sequence_app(*([LLMError("x")] * 10))
    with TestClient(app) as c:
        response = c.post("/sequence", json=SEQUENCE_REQUEST, headers=AUTH)
    assert response.status_code == 502
    assert response.json() == {"detail": "llm_failed"}


def test_sequence_invalid_body_is_422():
    app = sequence_app()
    body = {**SEQUENCE_REQUEST, "durationSec": 0}
    with TestClient(app) as c:
        response = c.post("/sequence", json=body, headers=AUTH)
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
