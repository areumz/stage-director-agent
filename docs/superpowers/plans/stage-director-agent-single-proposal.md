# 무대 연출 디렉터 에이전트 — 싱글 제안 구현 계획 (LLM 클라이언트 · 구간 하나 연출 · FastAPI)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> 이 문서는 태스크 순서, 검증 절차, 명령, 기대 결과를 담는다.

**Goal:** 구간 하나(라벨·시작·끝)와 곡 분석·아티스트 컨텍스트를 받아 검증된 `StageState` 1개와 근거(`rationale`)를 돌려주는 연출 노드를 만들고, `X-Internal-Key` 로 보호되는 FastAPI 엔드포인트(`POST /propose`)로 노출한다.

**Architecture:** 연출 노드 `propose_section(llm, req)` 는 LLM 클라이언트만 주입받는 순수 함수다. LangGraph 도 FastAPI 도 모른다. LLM 출력은 믿지 않고 기반 계획의 `sanitize_state`(병합 + clamp)와 `run_gate`(게이트 규칙)를 거친다. FastAPI 앱은 이 함수를 감싸는 얇은 껍데기이며 `create_app(settings, llm)` 팩토리로 설정과 LLM 을 주입받는다. 3단계 그래프는 같은 함수를 Send 노드로 감싼다.

**Tech Stack:** Python 3.12, uv, pydantic 2, FastAPI, uvicorn, google-genai(Gemini), pytest, httpx2(FastAPI TestClient용, dev)

**Spec:** [docs/superpowers/specs/stage-director-agent-design.md](../specs/stage-director-agent-design.md) — 실행자는 이 계획과 스펙을 함께 읽는다. 이 계획은 스펙 §12 제약 4의 MVP 2단계(싱글 제안)이며 기반 계획([stage-director-agent-foundation.md](stage-director-agent-foundation.md))의 함수 위에 짓는다.

## 이 계획의 범위

시작 상태: `main`(PR #1 병합, `908a718`), 테스트 89개 통과. 1단계 분석 스파이크의 수치 측정층(librosa)은 병합되어 있다.

| 포함 | 스펙 위치 |
| --- | --- |
| Task 1: LLM 클라이언트 인터페이스, 가짜 LLM, Gemini 어댑터 | §9 |
| Task 2: 싱글 제안 요청·응답 모델 (컨텍스트는 전부 요청 본문) | §3, §7 1층 |
| Task 3: 프롬프트·출력 스키마와 구간 하나 연출 노드 `propose_section` | §7, §8, §9 |
| Task 4: FastAPI 골격, `X-Internal-Key`, `POST /propose` | §3, §4.2 |
| Task 5: 실제 모델 평가 세트(`llm` 마커), 스펙 §13 갱신, 사람이 하는 실호출 확인 | §9, §13 |

**이 계획 밖.** 프리셋 API 로 저장하는 절반(기획서 §10 2단계 완료 기준의 "기존 프리셋 API로 저장")은 Next.js 쪽이라 이 저장소에서 할 수 없다. 이 계획은 저장에 넘길 수 있는 `state` 를 돌려주고, 그 값이 `mergeStageState` 를 왕복해도 변하지 않는다는 것까지 테스트로 보증한다(Task 4).

## §13 미결정 사항을 이 계획에서 어떻게 다루는가

| 항목 | 이 계획의 결정 | 이유 |
| --- | --- | --- |
| **LLM provider** | **Gemini 로 확정한다. 범위는 텍스트 입력 → 구조화 JSON 출력까지.** 기본 모델 `gemini-3.8-flash`(환경변수 `GEMINI_MODEL` 로 교체) | 이 계획의 노드는 오디오를 받지 않는다(측정값은 코드가 계산해 텍스트로 준다). 클라이언트가 주입 인터페이스라 provider 를 바꾸는 비용은 어댑터 파일 하나다. 모델 ID 는 2026-09-20 시점 Google 공식 문서의 stable 목록에서 골랐고, 이 계획을 쓴 환경에는 API 키가 없어 **실호출은 Task 5 의 사람 단계에서만 검증된다** |
| LLM 오디오 입력 무드 해석 | 이 계획에서 정하지 않는다 → 시퀀스 그래프 계획 | 무드 해석 노드가 오디오를 받는 첫 노드다. 인터페이스에 오디오 인자를 더하는 것도 그때 한다(지금 넣으면 쓰이지 않는 인자다) |
| **구조 분석 모델** (all-in-one 등) | **이 계획에서 정하지 않는다. 정할 필요가 없다.** 시퀀스 그래프 계획의 첫 태스크(스파이크)로 넘긴다 | 아래 세 가지 |

구조 분석 모델을 정하지 않는 이유

1. **소비하는 코드가 이 계획에 없다.** 싱글 제안의 입력은 구간(라벨·시작·끝) 하나다. 구조 분석 모델의 산출물(구간 경계·라벨)을 읽는 곳은 시퀀스 그래프의 `sections` 상태와 interrupt #1 이다. 이 계획의 `Section{label, startSec, endSec}` 은 스펙 §6.3 `sections` 항목의 부분집합이라 나중에 그대로 이어진다.
2. **판단 근거가 아직 없다.** 스파이크가 검증한 것은 수치층(BPM·에너지·온셋)뿐이다. 구간 경계는 에너지 급변 후보를 뽑아 본 수준이었고(귀로 검증하지 않음), 구조 분석 모델을 실제 곡에 돌려 본 기록이 저장소에 없다.
3. **지금 정하면 비용이 든다.** all-in-one 계열은 PyTorch 등 무거운 의존성이 붙는다. `pyproject.toml` 에 넣으면 이 계획의 설치·테스트가 무거워지고, numpy 2.5 + Python 3.12 + macOS 조합에서 설치가 막힐 수 있다(이 계획이 검증한 사실이 아니라 스파이크에서 확인할 위험이다).

시퀀스 그래프 계획에 넘기는 제안: 첫 태스크를 "구조 분석 스파이크"로 두고, 실제 곡 2개(`demo-tracks/`)에 (a) 에너지 곡선 기반 경계와 (b) all-in-one 을 돌려 귀로 비교한다. 설치 성공 여부도 결과에 포함한다. 부정확하거나 설치할 수 없으면 스펙 §13 기본값대로 에너지 곡선 기반 경계 제안 + interrupt #1 수정으로 간다. 스펙 §13 표는 Task 5 에서 이 판단대로 갱신한다.

## Global Constraints

모든 태스크의 요구사항에 아래가 암묵적으로 포함된다. 값은 스펙에서 그대로 옮겼다.

- Python 서비스는 Supabase에 접근하지 않는다. 필요한 컨텍스트는 요청 본문으로 받는다 (§3). `supabase` 패키지를 의존성에 넣지 않는다.
- Python 이 요청으로 받는 컨텍스트: 분석 JSON, 곡 메타(제목·장르·무드 키워드), 아티스트 컨셉(slug, color, shader 파라미터), 기존 프리셋 목록 (§3).
- **`../on-stage`를 열지 않는다** (§12 제약 2). 이 계획에서 필요한 모양은 모두 `contracts/` 에 있다(`artist_context.md`, `api_stage_presets.md`). 없는 정보가 필요하면 구현을 멈추고 "계약 갱신" 태스크를 따로 만든다.
- Python 엔드포인트는 모두 `X-Internal-Key` 필수이며 키는 서버 환경변수에만 둔다 (§4.2).
- LLM provider 는 클라이언트를 주입하는 인터페이스 뒤에 둔다 (§9). LLM 노드는 결정적 가짜 LLM 주입 + 소규모 평가 세트의 규칙 체크로 테스트하고, 실제 모델 호출 테스트는 별도 마커로 분리한다 (§9).
- LLM 호출 실패는 노드 단위 재시도 2회 후 오류로 올린다 (§8).
- 결정적 로직은 **TDD로 처음부터** 작성한다: 실패하는 테스트 → 실패 확인 → 최소 구현 → 통과 확인 (§12 제약 5).
- StageState 범위: `intensity` 0–1000, `angle` 0.1–1.0, `penumbra` 0–1, `smoke.density` 0–1. `color`·`smoke.color` 는 `^#[0-9a-fA-F]{6}$`. 범위 밖 숫자는 **clamp 하고 이슈를 기록**하며 거부하지 않는다. `camera` 가 enum(`front`/`audience`/`top`) 밖이면 기본값 (§7 1층).
- `penumbra` 는 모델에 유지하고(기본 0.6) 에이전트는 값을 바꾸지 않는다 (§2).
- 컨텍스트로 받은 기존 프리셋도 모델을 통과시켜 clamp 한 뒤 사용한다 (§7).
- 시퀀스 항목의 JSON 키는 camelCase: `sectionLabel`, `startSec`, `endSec`, `transitionMs`, `state`, `rationale` (§5).
- 커밋 메시지에 `Co-Authored-By` 트레일러를 붙이지 않고, PR 설명에 "Generated with Claude Code" 문구를 붙이지 않는다 (사용자 지시). 서브에이전트에게 커밋을 시킬 때도 같다.

## 이 계획이 정한 값 (스펙이 정하지 않은 것)

| 값 | 초기값 | 위치 |
| --- | --- | --- |
| 노드 재시도 횟수 | `MAX_RETRIES = 2` (최초 시도 + 재시도 2회 = 최대 3번 호출) | `propose.py` |
| 전환 시간 | `DEFAULT_TRANSITION_MS = 2000` (기획서 §6 예시 값). 구간이 2초보다 짧으면 구간 길이 | `propose.py` |
| 프롬프트에 넣는 기존 프리셋 수 | 앞에서부터 `MAX_PRESETS_IN_PROMPT = 10` 개 | `propose.py` |
| 분석이 없을 때의 에너지 비 | `1.0` (`section_energy_ratio` 의 기존 동작. 평균과 같다고 봄) | `snapshot.py`(기존) |
| Gemini 요청 타임아웃 | `TIMEOUT_MS = 60_000` | `llm/gemini.py` |
| 기본 Gemini 모델 | `DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"` | `settings.py` |
| 엔드포인트와 상태 코드 | `POST /propose`. 401 인증 실패, 422 본문 오류, 502 LLM 실패(재시도 후). 오류 본문은 FastAPI 기본 `{"detail": ...}` | `api.py` |
| 스키마 문서 엔드포인트 | `/docs`, `/redoc`, `/openapi.json` 은 끈다(인증 밖 엔드포인트가 생기지 않게) | `api.py` |
| 요청의 아티스트 컬러 | `#RRGGBB` 가 아니면 422. 기반 계획의 `clamp_stage_state` 가 fallback 값을 믿기 때문에(기반 계획 Task 1 리뷰의 보류 항목) 입구에서 보증한다 | `models.py` |

## 파일 구조

| 파일 | 책임 | 태스크 |
| --- | --- | --- |
| `pyproject.toml`, `uv.lock` | 의존성(`google-genai`, `fastapi`, `uvicorn`, dev `httpx2`), pytest 마커 | 1, 4, 5 |
| `src/stage_director/llm/client.py` | `LLMClient` Protocol, `LLMError` | 1 |
| `src/stage_director/llm/fake.py` | 테스트용 결정적 가짜 LLM | 1 |
| `src/stage_director/llm/gemini.py` | Gemini 어댑터 | 1 |
| `src/stage_director/models.py` | 요청·응답 pydantic 모델 | 2 |
| `src/stage_director/prompts.py` | 시스템 프롬프트, 출력 JSON Schema | 3 |
| `src/stage_director/propose.py` | `propose_section` (구간 하나 연출 노드) | 3 |
| `src/stage_director/settings.py` | 환경변수 설정 | 4 |
| `src/stage_director/api.py` | FastAPI 앱 팩토리, `X-Internal-Key` | 4 |
| `.env.example` | 필요한 환경변수 목록 | 4 |
| `tests/test_llm.py`, `test_models.py`, `test_propose.py`, `test_api.py` | 위 파일 각각의 테스트 | 1~4 |
| `tests/fixtures/propose_request.json` | 요청 본문 예시. 테스트와 수동 `curl` 이 함께 쓴다 | 3 |
| `tests/test_eval_llm.py` | 실제 모델 평가 세트(`llm` 마커) | 5 |
| `docs/superpowers/specs/stage-director-agent-design.md` | §13 표 갱신 | 5 |

---

### Task 1: LLM 클라이언트 인터페이스, 가짜 LLM, Gemini 어댑터

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (`google-genai` 추가)
- Create: `src/stage_director/llm/__init__.py`, `client.py`, `fake.py`, `gemini.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `stage_director.llm.client`:
    - `LLMError(Exception)` — 호출 실패 또는 JSON 으로 읽을 수 없는 응답. 재시도 대상
    - `LLMClient` (Protocol) — `generate_json(self, *, system: str, user: str, schema: dict[str, Any]) -> Any`. 파싱된 JSON 값을 돌려주고 실패하면 `LLMError`. 돌려받은 값이 `schema` 를 지킨다고 믿지 않는다
  - `stage_director.llm.fake.FakeLLM(*responses: Any)` — 응답을 순서대로 돌려준다. 응답이 `Exception` 이면 던진다. `calls: list[dict]` 에 `{system, user, schema}` 를 기록한다. 응답이 떨어지면 `AssertionError`
  - `stage_director.llm.gemini.GeminiClient(api_key: str, model: str, client: Any = None)` — `client` 는 테스트에서 SDK 대신 스텁을 넣는 자리. SDK·네트워크·JSON 파싱 실패는 모두 `LLMError`. `TIMEOUT_MS = 60_000`

- [ ] **Step 1: 브랜치를 만들고 시작 상태를 확인한다**

`main` 은 기반 계획 PR 이 병합된 상태다. 이 계획 문서(요약본)는 새 브랜치의 첫 커밋으로 올린다. 코드가 든 상세본(`detail*`)은 gitignore 대상이라 로컬에만 둔다.

```bash
git switch main && git pull --ff-only
git switch -c feat/single-proposal
git add docs/superpowers/plans/stage-director-agent-single-proposal.md
git commit -m "docs: add single-proposal plan"
uv sync && uv run pytest -q
```

Expected: `89 passed`. 다르면 멈추고 원인을 확인한다(기반 계획의 최종 상태가 89개다).

- [ ] **Step 2: 의존성을 추가한다**

```bash
uv add google-genai
mkdir -p src/stage_director/llm
touch src/stage_director/llm/__init__.py
```

Expected: `pyproject.toml` 의 `dependencies` 에 `google-genai` 가 추가된다(작성 시점 검증 버전: google-genai 2.24.0). 이 SDK 의 `client.models.generate_content(model, contents, config)` 와 `types.GenerateContentConfig(system_instruction, response_mime_type, response_json_schema)` 를 쓴다.

- [ ] **Step 3: 실패하는 테스트를 쓴다**

`tests/test_llm.py`:

_(코드 본문 생략 — 저장소의 `tests/test_llm.py` 참고)_

- [ ] **Step 4: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_llm.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.llm.client'` (collection 오류)

- [ ] **Step 5: 구현한다**

`src/stage_director/llm/client.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/llm/client.py` 참고)_

`src/stage_director/llm/fake.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/llm/fake.py` 참고)_

`src/stage_director/llm/gemini.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/llm/gemini.py` 참고)_

- [ ] **Step 6: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_llm.py -q`
Expected: `9 passed`

Run: `uv run pytest -q`
Expected: `98 passed`

- [ ] **Step 7: 커밋한다**

```bash
git add pyproject.toml uv.lock src/stage_director/llm tests/test_llm.py
git commit -m "feat: add LLM client interface, fake LLM and Gemini adapter"
```

---

### Task 2: 싱글 제안 요청·응답 모델

**Files:**
- Create: `src/stage_director/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `contracts.stage_state.HEX_COLOR`, `stage_director.sequence.SequenceItem`
- Produces (`stage_director.models`, 모두 pydantic, JSON 키 camelCase, `populate_by_name=True`, 모르는 필드는 무시):
  - `Shader(pattern: "wave"|"ripple"|"grain", freq, falloff, speed: float)`
  - `Artist(slug, name, name_ko: str, color: str, shader: Shader)` — `color` 는 `#RRGGBB` 가 아니면 검증 오류
  - `Preset(name: str, state: Any)` — `state` 는 저장된 jsonb 그대로(모양 보장 없음)
  - `Track(title: str, genre: str = "", mood_keywords: list[str] = [])`
  - `Section(label: str, start_sec: float, end_sec: float)` — `start_sec >= 0`, `start_sec < end_sec` 아니면 검증 오류
  - `ProposeRequest(track: Track, artist: Artist, presets: list[Preset] = [], analysis: Any = None, section: Section)`
  - `Issue(rule: str, message: str)`
  - `SectionProposal(item: SequenceItem, energy_ratio: float, issues: list[Issue])`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_models.py`:

_(코드 본문 생략 — 저장소의 `tests/test_models.py` 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_models.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.models'`

- [ ] **Step 3: 구현한다**

`src/stage_director/models.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/models.py` 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_models.py -q`
Expected: `13 passed`

Run: `uv run pytest -q`
Expected: `111 passed`

- [ ] **Step 5: 커밋한다**

```bash
git add src/stage_director/models.py tests/test_models.py
git commit -m "feat: add single-proposal request and response models"
```

---

### Task 3: 프롬프트·출력 스키마와 구간 하나 연출 노드

**Files:**
- Create: `src/stage_director/prompts.py`, `src/stage_director/propose.py`
- Create: `tests/fixtures/propose_request.json`
- Test: `tests/test_propose.py`

**Interfaces:**
- Consumes:
  - `stage_director.llm.client.{LLMClient, LLMError}` (Task 1), `stage_director.models.{ProposeRequest, SectionProposal, Issue}` (Task 2)
  - 기반 계획: `contracts.stage_state.{default_stage_state, CAMERAS}`, `stage_director.gate.{sanitize_state, run_gate, CALM_ENERGY_RATIO, CALM_MAX_INTENSITY}`, `stage_director.analysis.snapshot.{parse_analysis, section_energy_ratio}`, `stage_director.sequence.SequenceItem`
- Produces:
  - `stage_director.prompts`: `SYSTEM_PROMPT: str`(게이트 임계값을 상수에서 끌어와 문구가 어긋나지 않게 한다), `PROPOSAL_SCHEMA: dict`(출력 `{state, rationale}` 의 JSON Schema. `penumbra` 는 묻지 않는다), `SPOT_KEYS`
  - `stage_director.propose`:
    - `MAX_RETRIES = 2`, `DEFAULT_TRANSITION_MS = 2000`, `MAX_PRESETS_IN_PROMPT = 10`
    - `propose_section(llm: LLMClient, req: ProposeRequest) -> SectionProposal` — `LLMError` 는 재시도 후에도 실패하면 그대로 올린다. 그 밖의 출력 문제는 예외 없이 `issues` 로 돌려준다(`clamped`, `invalid_state`, 게이트 규칙)

동작 요약: 분석에서 구간 에너지 비·온셋 밀도 비를 계산 → 기존 프리셋을 `sanitize_state` 로 clamp 해 프롬프트에 포함 → LLM 호출(재시도) → `penumbra` 제거 → `sanitize_state`(기본값 = `default_stage_state(artist.color)`) → `SequenceItem` 조립 → `run_gate` 1구간 적용. 자동 재생성은 하지 않는다(그래프의 조건부 엣지 몫).

- [ ] **Step 1: 요청 본문 픽스처를 만든다**

수동 `curl`(Task 5)과 테스트가 함께 쓴다. 0~10초 잔잔(에너지 비 0.31), 10~30초 큰 소리(1.54), 30~40초 중간(0.62).

`tests/fixtures/propose_request.json`:

_(코드 본문 생략 — 저장소의 `tests/fixtures/propose_request.json` 참고)_

- [ ] **Step 2: 실패하는 테스트를 쓴다**

`tests/test_propose.py`:

_(코드 본문 생략 — 저장소의 `tests/test_propose.py` 참고)_

- [ ] **Step 3: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_propose.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.propose'` (collection 오류)

- [ ] **Step 4: 구현한다**

`src/stage_director/prompts.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/prompts.py` 참고)_

`src/stage_director/propose.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/propose.py` 참고)_

- [ ] **Step 5: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_propose.py -q`
Expected: `20 passed`

Run: `uv run pytest -q`
Expected: `131 passed`

- [ ] **Step 6: 커밋한다**

```bash
git add src/stage_director/prompts.py src/stage_director/propose.py tests/fixtures/propose_request.json tests/test_propose.py
git commit -m "feat: add single-section proposal node with prompt and output schema"
```

---

### Task 4: FastAPI 골격과 `X-Internal-Key`

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (`fastapi`, `uvicorn`, dev `httpx2` 추가)
- Create: `src/stage_director/settings.py`, `src/stage_director/api.py`, `.env.example`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `propose_section` (Task 3), `GeminiClient` (Task 1), `ProposeRequest`·`SectionProposal` (Task 2)
- Produces:
  - `stage_director.settings.Settings(internal_api_key: str, gemini_api_key: str, gemini_model: str)` — frozen dataclass. `Settings.from_env()` 는 `INTERNAL_API_KEY`, `GEMINI_API_KEY` 가 비어 있으면 `RuntimeError`(이름을 메시지에 포함), `GEMINI_MODEL` 이 비면 `DEFAULT_GEMINI_MODEL`
  - `stage_director.api.create_app(settings: Settings | None = None, llm: LLMClient | None = None) -> FastAPI` — 인자가 없으면 환경변수와 Gemini 를 쓴다
  - `POST /propose` — 요청 `ProposeRequest`(camelCase), 200 `SectionProposal`(camelCase: `item{sectionLabel,startSec,endSec,transitionMs,state,rationale}`, `energyRatio`, `issues[{rule,message}]`). 헤더 `X-Internal-Key` 가 없거나 다르면 401(본문 검증보다 먼저, LLM 호출 없음), 본문 오류 422, LLM 실패 502 `{"detail": "llm_failed"}`
  - 실행: `uv run --env-file .env uvicorn --factory stage_director.api:create_app`

- [ ] **Step 1: 의존성을 추가한다**

```bash
uv add fastapi uvicorn
uv add --dev httpx2
```

Expected: `dependencies` 에 `fastapi`, `uvicorn`, `[dependency-groups] dev` 에 `httpx2` 가 추가된다(작성 시점 검증 버전: fastapi 0.141.1, uvicorn 0.53.0, httpx2 2.13.0). FastAPI 의 `TestClient` 는 `httpx` 대신 `httpx2` 를 요구한다(`httpx` 는 deprecation 경고).

- [ ] **Step 2: 실패하는 테스트를 쓴다**

`tests/test_api.py`:

_(코드 본문 생략 — 저장소의 `tests/test_api.py` 참고)_

- [ ] **Step 3: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_api.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.api'` (collection 오류)

- [ ] **Step 4: 구현한다**

`src/stage_director/settings.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/settings.py` 참고)_

`src/stage_director/api.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/api.py` 참고)_

`.env.example`:

_(코드 본문 생략 — 저장소의 `.env.example` 참고)_

- [ ] **Step 5: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_api.py -q`
Expected: `14 passed` (starlette 내부의 anyio `DeprecationWarning` 1건은 우리 코드와 무관하다)

Run: `uv run pytest -q`
Expected: `145 passed`

- [ ] **Step 6: 실제 서버를 띄워 확인한다**

가짜 키로 띄운다. 인증과 문서 비노출은 실제 프로세스에서도 그대로여야 한다.

```bash
INTERNAL_API_KEY=dev-key GEMINI_API_KEY=fake-key uv run uvicorn --factory stage_director.api:create_app --port 8765 &
sleep 5
curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:8765/propose -H 'Content-Type: application/json' --data @tests/fixtures/propose_request.json
curl -s -o /dev/null -w "%{http_code}\n" localhost:8765/docs -H 'X-Internal-Key: dev-key'
curl -s -w " -> %{http_code}\n" -X POST localhost:8765/propose -H 'X-Internal-Key: dev-key' -H 'Content-Type: application/json' --data @tests/fixtures/propose_request.json
kill %1
```

Expected: `401`, `404`, 그리고 Gemini 가 가짜 키를 거절해 재시도 후 `{"detail":"llm_failed"} -> 502`(수 초 걸린다). 실제 어댑터 경로가 이어져 있고 실패가 502 로 매핑된다는 확인이다.

키 없이 띄우면 서비스가 뜨지 않아야 한다.

```bash
env -u INTERNAL_API_KEY uv run uvicorn --factory stage_director.api:create_app --port 8766
```

Expected: `RuntimeError: 환경변수 INTERNAL_API_KEY 가 설정되지 않았다` 로 종료.

- [ ] **Step 7: 커밋한다**

```bash
git add pyproject.toml uv.lock src/stage_director/settings.py src/stage_director/api.py .env.example tests/test_api.py
git commit -m "feat: add FastAPI skeleton with X-Internal-Key and POST /propose"
```

---

### Task 5: 실제 모델 평가 세트, 스펙 §13 갱신, 실호출 확인

**Files:**
- Modify: `pyproject.toml` (`[tool.pytest.ini_options]`)
- Modify: `docs/superpowers/specs/stage-director-agent-design.md` (§13 표 2행)
- Create: `tests/test_eval_llm.py`

**Interfaces:**
- Consumes: `propose_section` (Task 3), `GeminiClient` (Task 1), `Settings`의 `DEFAULT_GEMINI_MODEL` (Task 4), `tests/fixtures/propose_request.json` (Task 3), `stage_director.gate.brightness`
- Produces: pytest 마커 `llm`(기본 실행에서 제외, `-m llm` 으로만 실행). 이후 계획의 실제 모델 테스트도 같은 마커를 쓴다

- [ ] **Step 1: pytest 마커를 등록하고 기본 실행에서 뺀다**

`pyproject.toml` 의 `[tool.pytest.ini_options]` 를 아래처럼 고친다(`markers` 와 `addopts` 두 줄 추가).

```toml
[tool.pytest.ini_options]
pythonpath = ["src", "."]
testpaths = ["tests"]
markers = ["llm: 실제 LLM 을 호출한다. uv run --env-file .env pytest -m llm 으로만 돈다"]
addopts = "-m 'not llm'"
```

`-m llm` 을 직접 주면 `addopts` 의 `-m` 을 덮어쓴다.

- [ ] **Step 2: 평가 세트를 쓴다**

세 구간(잔잔한 인트로 / 큰 소리 코러스 / 중간 아웃트로)을 실제 모델로 제안받아, 결과값이 아니라 게이트 규칙을 지켰는지만 본다. 모델 출력은 확률적이라 값을 고정하지 않는다.

`tests/test_eval_llm.py`:

_(코드 본문 생략 — 저장소의 `tests/test_eval_llm.py` 참고)_

- [ ] **Step 3: 기본 실행에서 빠지는지, 키 없이도 안전한지 확인한다**

Run: `uv run pytest -q`
Expected: `145 passed, 5 deselected`

Run: `uv run pytest -m llm -q` (키 없이)
Expected: `5 skipped, 145 deselected`

- [ ] **Step 4: 스펙 §13 표를 갱신한다**

`docs/superpowers/specs/stage-director-agent-design.md` §13 의 아래 두 행을

```
| LLM provider | Gemini(무드 해석), 클라이언트 주입 구조 | 1단계 스파이크 후 |
| 구조 분석 모델 | 기획서 그대로(예: all-in-one). 부정확하면 에너지 곡선 기반 경계 제안으로 전환 | 1단계 스파이크 후 |
```

다음으로 바꾼다.

```
| LLM provider | Gemini, 클라이언트 주입 구조. 텍스트 입력 구조화 출력은 싱글 제안 계획에서 확정(기본 모델 `gemini-3.8-flash`, 환경변수 `GEMINI_MODEL` 로 교체). 오디오 입력 무드 해석은 시퀀스 그래프 계획에서 검증 | 텍스트: 확정 / 오디오: 시퀀스 그래프 계획 |
| 구조 분석 모델 | 기획서 그대로(예: all-in-one). 부정확하면 에너지 곡선 기반 경계 제안으로 전환. 싱글 제안은 구간을 입력으로 받아 이 모델을 쓰지 않는다 | 시퀀스 그래프 계획의 첫 태스크(스파이크) |
```

- [ ] **Step 5: 커밋한다**

```bash
git add pyproject.toml tests/test_eval_llm.py docs/superpowers/specs/stage-director-agent-design.md
git commit -m "test: add LLM eval set behind the llm marker and update spec section 13"
```

- [ ] **Step 6: (사람이 하는 단계) 실제 Gemini 로 확인한다**

에이전트가 할 수 없는 단계다. `GEMINI_API_KEY` 는 사용자가 준비한다. 이 결과가 Task 3 의 프롬프트와 기본 모델 ID 를 확정한다.

```bash
cp .env.example .env    # INTERNAL_API_KEY(아무 긴 문자열)와 GEMINI_API_KEY 를 채운다
uv run --env-file .env pytest -m llm -v
```

Expected: 5개 통과. 모델 출력은 확률적이라 실패할 수 있다. 실패하면 메시지의 게이트 규칙 이름을 보고 `src/stage_director/prompts.py` 의 문구를 고친 뒤 다시 돌린다(프롬프트 조정은 이 저장소의 정상적인 작업이다). 모델 ID 가 거절되면 [공식 모델 목록](https://ai.google.dev/gemini-api/docs/models)에서 stable ID 를 골라 `.env` 의 `GEMINI_MODEL` 과 `settings.py` 의 `DEFAULT_GEMINI_MODEL` 을 함께 고친다.

서버로도 한 번 확인한다. 응답의 `item.state` 가 곧 Next.js 가 프리셋 API 로 저장할 값이다.

```bash
uv run --env-file .env uvicorn --factory stage_director.api:create_app --port 8765 &
sleep 5
curl -s -X POST localhost:8765/propose -H "X-Internal-Key: $(grep ^INTERNAL_API_KEY .env | cut -d= -f2)" -H 'Content-Type: application/json' --data @tests/fixtures/propose_request.json | python3 -m json.tool
kill %1
```

`rationale` 가 측정값(에너지 비 1.54 등)을 인용하는지, 색이 시그니처 컬러 `#9F77DD` 인지, `issues` 가 비어 있는지 눈으로 본다. 결과(통과 여부, 조정한 프롬프트, 실제 모델 ID)는 PR 설명에 적는다.

---

## 스펙 대응

| 스펙 | 이 계획의 태스크 |
| --- | --- |
| §3 경계(Python 은 컨텍스트를 요청 본문으로 받음, Supabase 접근 없음) | Task 2, 4 |
| §4.2 `X-Internal-Key` 필수, 키는 서버 환경변수에만 | Task 4 |
| §7 1층(모델·clamp), 프리셋도 clamp 후 사용 | Task 3 (`sanitize_state` 재사용), Task 2(아티스트 컬러 검증) |
| §7 2층(게이트 규칙) | Task 3 (`run_gate` 1구간 적용. 방향 규칙은 이웃 구간이 필요해 그래프 몫) |
| §8 LLM 호출 실패 재시도 2회 → 오류, 구조 출력 불일치 시 필드별 방어 | Task 3, 4(502) |
| §9 LLM 인터페이스 + 가짜 LLM + 평가 세트(별도 마커), 프로토콜 테스트 | Task 1, 3, 4, 5 |
| §13 LLM provider, 구조 분석 모델 | 이 문서 상단 표, Task 5 Step 4 |
| §4.2 나머지 프로토콜(`/runs`, 409/410, 폴링), §6 체크포인터·`jobs`, §6.4 보존, §4.1 분석 작업 | 이 계획 밖 (후속 계획 3~5) |

## 알려진 한계와 후속 계획으로 넘기는 것

| 한계 | 넘기는 곳 |
| --- | --- |
| 동기 요청이라 LLM 응답 시간 동안 연결을 잡는다(Vercel 실행 시간 제한과 부딪힐 수 있음). 스펙 D4 의 작업 + 폴링 프로토콜이 아니다 | 계획 4 (실행·재개 프로토콜) |
| 이웃 구간 컨텍스트가 없다("앞뒤 구간을 컨텍스트로 넘겨 전환을 자연스럽게", 기획서 §7). `propose_section` 에 선택 인자를 더한다 | 계획 3 (Send 노드) |
| 게이트 위반 시 자동 재생성 루프가 없다. 이슈를 프롬프트에 되먹이는 방식도 그때 정한다 | 계획 3 (조건부 엣지, 스펙 §7) |
| `transitionMs` 를 코드가 정한다(2000, 구간이 짧으면 구간 길이). 모델은 정하지 않는다 | 계획 3 (구간 간 전환 설계) |
| 구조 분석 모델 미정, 오디오 입력 무드 해석 미검증 | 계획 3 첫 태스크(스파이크) |
| 프리셋 저장(`POST /api/stage-presets`), Next.js 라우트, 레이트 리밋 | on-stage (스펙 §11) |
| `POST /propose` 는 시퀀스 그래프가 생긴 뒤에도 남겨 둘지 계획 3 에서 정한다 | 계획 3 |


---

## 실행 후 변경 (최종 리뷰 반영)

각 태스크의 `Expected` 통과 개수는 그 태스크를 처음 실행했을 때의 값이다. 전체 브랜치 리뷰를 반영해 아래를 고쳤고, 그 결과 **전체 154개 통과(5개는 `llm` 마커로 제외)**가 최종 상태다. 태스크별 리뷰는 모두 통과했고, 최종 리뷰의 Important 2건과 정리 항목을 한 번의 수정 커밋(`59cbb59`)으로 처리했다.

| 변경 | 이유 | 테스트 변화 |
| --- | --- | --- |
| `CamelModel` 에 `allow_inf_nan=False`, `RequestValidationError` 핸들러 추가 (422 본문은 `{"detail": [{loc, msg, type}]}`, 입력값·ctx 는 되돌려주지 않음) | JSON 의 `NaN`/`Infinity` 가 `endSec: null` 로 200 이 되거나(구간 끝 검증 우회), 422 본문이 `nan` 을 되돌려주다 직렬화에 실패해 500 이 됐다 | models +4, api +2 |
| `create_app` 이 비어 있거나 공백뿐인 `INTERNAL_API_KEY` 를 `ValueError` 로 거부 | `create_app(Settings(internal_api_key=""))` 나 `INTERNAL_API_KEY="   "` 이면 빈 헤더가 인증됐다 | api +2 |
| 스펙 §13 아래 문단(216행)을 표와 맞춤 | 표는 갱신했는데 바로 아래 문장이 "1단계 스파이크 후 확정"으로 남아 모순이었다 | - |
| `propose.py` 미사용 `SPOT_KEYS` import 제거, `GeminiClient` 의 `client if client is not None` | 정리 | - |
| `test_propose.py` 사실 목록에 온셋 밀도 비 `"1.45"` 추가 | 프롬프트의 온셋 밀도 비 줄이 검증되지 않았다 | - |
| `api.py` 동기 엔드포인트 `ponytail:` 주석에 최악 대기 시간(3회 × 60초 = 약 3분) 기록 | Next.js/Vercel 제한이 더 짧으면 `TIMEOUT_MS` 를 줄이거나 작업+폴링(스펙 D4)으로 옮긴다 | - |

이 계획의 "이 계획이 정한 값" 표 중 "오류 본문은 FastAPI 기본 `{"detail": ...}`" 은 422 에 한해 위처럼 `detail` 항목이 `loc`·`msg`·`type` 만 남는 형태가 되었다.

**보류(후속 계획에서 다룬다):** 재시도 사이 대기(backoff)와 `_generate` 명시적 return, `GeminiClient` 생성 경로(타임아웃) 테스트(Task 5 Step 6 실호출과 함께), 곡 제목·프리셋 이름 길이 상한(토큰 비용), `api.py` 의 `llm or GeminiClient(...)` 를 `is not None` 으로 통일, `ruff` 도입.

**아직 실행하지 않은 것:** Task 5 Step 6(실제 Gemini 호출 확인). 기본 모델 ID(`gemini-3.8-flash`)와 프롬프트 품질은 이 단계가 끝나기 전까지 검증되지 않았다.
