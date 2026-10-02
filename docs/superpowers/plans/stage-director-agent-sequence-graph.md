# 무대 연출 디렉터 에이전트 — 시퀀스 그래프 구현 계획 (구간 경계 · LangGraph · 체크포인터 · `/sequence`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> 이 문서는 태스크 순서, 검증 절차, 명령, 기대 결과를 담는다.

**Goal:** 곡 전체를 에너지 곡선 기반 휴리스틱으로 구간으로 나누고, 구간마다 기존 `propose_section`을 LangGraph `Send`로 병렬 호출해 전체 시퀀스를 만들고, 게이트 위반 구간은 최대 2회 자동 재생성한 뒤, `sequence.validate_sequence()`를 통과하는 결과를 전용 Postgres 체크포인터에 남기며 `POST /sequence`로 내놓는다.

**Architecture:** 그래프는 4개 노드(`detect` → `propose`(Send 팬아웃) → `assemble` → 조건부 재생성 라우팅)로 구성되며, `propose` 노드는 기존 `propose_section`(싱글 제안 계획)을 변경 없이 그대로 호출한다. `assemble` 노드가 전체 구간에 `run_gate`를 다시 돌려 구간 간 규칙(에너지-밝기 방향)까지 잡아내고, `indices_to_regenerate`로 재생성 대상을 골라 조건부 엣지가 그 구간만 다시 `Send`한다. 체크포인터는 LangGraph `PostgresSaver`(로컬 docker-compose, 배포 시 Neon)를 쓰며, FastAPI 는 `create_app`의 `lifespan`에서 체크포인터를 열고 그래프를 한 번만 컴파일해 재사용한다.

**Tech Stack:** Python 3.12, uv, pydantic 2, FastAPI, LangGraph(`StateGraph`, `Send`), `langgraph-checkpoint-postgres`, Postgres 16(docker-compose, 로컬 전용), pytest

**Spec:** [docs/superpowers/specs/stage-director-agent-design.md](../specs/stage-director-agent-design.md) — 실행자는 이 계획과 스펙을 함께 읽는다. 이 계획은 스펙 §12 제약 4의 MVP 3단계(시퀀스)이며 기반 계획([stage-director-agent-foundation.md](stage-director-agent-foundation.md))과 싱글 제안 계획([stage-director-agent-single-proposal.md](stage-director-agent-single-proposal.md))의 함수 위에 짓는다.

## 이 계획의 범위

시작 상태: `main`(PR #2 "싱글 제안" 병합 후), 테스트 154개 통과(5개는 `llm` 마커로 제외).

| 포함 | 스펙 위치 |
| --- | --- |
| Task 1: 구간 경계 휴리스틱(`detect_sections`), 합성 신호로 TDD | §13 |
| Task 2: 곡 전체 요청·응답 모델(`SequenceRequest`, `SequenceResponse`), 그래프 상태 타입 | §6.3 |
| Task 3: LangGraph 그래프 — 구간 분해 → `Send` 팬아웃 → 조립 → 자동 재생성(최대 2회) | §6.3, §7 2층 |
| Task 4: Postgres 체크포인터 연동(로컬 docker-compose) | §6.1, D2 |
| Task 5: FastAPI `POST /sequence` — 그래프·체크포인터 연결 | §4.2(일부) |
| Task 6: 통합 검증, 구간 경계 스파이크(실제 곡 2개)와 스펙 §13 갱신, 스펙 §4.2 갱신, 실제 Gemini + 실제 Neon 확인 | §9, §13 |

**이 계획 밖.** 사람 개입(interrupt #1 구간 확인, interrupt #2 리뷰/피드백), `jobs` 테이블과 작업+폴링 프로토콜(`POST /runs`, 409/410, 멱등 재개), 체크포인트 보존 정책(§6.4), 오디오 입력 무드 해석 LLM 노드는 4단계로 넘긴다. `POST /sequence`는 이 단계의 잠정 동기 엔드포인트이며 스펙 §4.2의 `/runs` 비동기 프로토콜이 아니다(아래 §13 표와 "알려진 한계" 참고).

## §13 미결정 사항을 이 계획에서 어떻게 다루는가 (사용자 확인 완료)

| 항목 | 이 계획의 결정 | 이유 |
| --- | --- | --- |
| **구조 분석 모델** | **all-in-one 류 무거운 모델을 설치하지 않는다. 에너지 곡선 기반 휴리스틱(`detect_sections`)으로 확정한다.** | 싱글 제안 계획이 이미 지적한 설치 위험(PyTorch 등, numpy 2.5 + Python 3.12 조합에서 막힐 수 있음)을 감수할 근거가 부족하다. Task 1 은 합성 신호로만 TDD 하고, 실제 곡 2개로 듣고 조정하는 단계는 Task 6(사람 단계)으로 모은다 — subagent 실행이 Task 1~5 사이에서 멈추지 않게 하기 위해서다 |
| **오디오 입력 무드 해석** | **이 계획에 포함하지 않는다. 4단계(사람 개입)로 미룬다.** | `sections`의 `mood` 필드는 스펙 §6.3 상 interrupt #1(사람이 구간을 확인·수정)에서만 쓰인다. 이 계획은 interrupt 가 없으므로 소비자가 없는 필드와 오디오 멀티모달 LLM 호출을 지금 추가하면 추측성 구현(YAGNI 위반)이 된다 |
| **체크포인터 DB** | **로컬 docker-compose Postgres 로 전부 자동화한다. 실제 Neon 연결은 Task6에서 실행한다.** | 이전 계획들과 같은 패턴 |

## Global Constraints

모든 태스크의 요구사항에 아래가 암묵적으로 포함된다. 값은 스펙과 선행 계획에서 그대로 옮겼다.

- Python 서비스는 Supabase 에 접근하지 않는다. 필요한 컨텍스트는 요청 본문으로 받는다 (§3).
- **`../on-stage`를 열지 않는다** (§12 제약 2). 필요한 모양은 모두 `contracts/`에 있다.
- `propose_section`, `sanitize_state`, `run_gate`, `indices_to_regenerate`, `validate_sequence`, `merge_proposals`, `parse_analysis`는 **변경하지 않고 그대로 재사용**한다. 새 동작이 필요하면 호출하는 쪽(그래프 노드)에서 조합한다.
- Python 엔드포인트는 모두 `X-Internal-Key` 필수 (§4.2).
- 결정적 로직은 **TDD로 처음부터** 작성한다: 실패하는 테스트 → 실패 확인 → 최소 구현 → 통과 확인 (§12 제약 5).
- `items` 불변식(§5): 배열 순서 = 시간 순서, 첫 항목 `startSec=0`, 마지막 `endSec=duration_sec`, 틈·겹침 없음. `sequence.validate_sequence`가 이미 검사한다 — 새로 만들지 않는다.
- 위반 구간은 최대 2회 자동 재생성 후에도 남으면 경고로 남긴다(인터럽트 없음, §7 2층의 앞부분만 적용. "interrupt #2 에 노출"은 4단계).
- 그래프 상태의 분석 스냅샷 에너지 곡선은 1점/초 이하로 다운샘플한다 (§6.3) — 이미 `AnalysisSnapshot`이 그렇다.
- 전용 Postgres 는 LangGraph 체크포인터 전용이며 Python 외 접근하지 않는다 (§6.1).

## Review Focus

- 아주 짧은 곡(최소 구간 길이의 2배 미만)이나 에너지 곡선이 비어 있을 때 구간 경계 로직이 크래시하거나 0개 구간을 내지 않고 곡 전체를 구간 하나로 처리해야 한다 → Task 1
- 무음 곡(에너지 곡선이 전부 0)일 때 구간 수 계산이 0으로 나누기 등으로 깨지지 않아야 한다 → Task 1
- 노이즈가 많아 경계 후보가 과도하게 많이 잡히는 에너지 곡선에서 구간이 무한히 쪼개져 LLM 호출이 폭증하지 않아야 한다 → Task 1
- 게이트 위반이 2회 자동 재생성 후에도 남을 때 그래프가 멈추거나 크래시하지 않고, 남은 문제를 `issues`에 기록한 채로 **여전히 유효한(`validate_sequence` 통과) 전체 시퀀스**를 반환해야 한다 → Task 3
- 체크포인터 Postgres 가 뜨지 않은 상태로 서비스를 띄우면 일부만 동작하는 상태로 응답을 시작하지 말고 기동 단계에서 분명하게 실패해야 한다(기존 `INTERNAL_API_KEY`/`GEMINI_API_KEY` 누락과 같은 fail-fast 패턴) → Task 5

## 이 계획이 정한 값 (스펙이 정하지 않은 것)

| 값 | 초기값 | 위치 |
| --- | --- | --- |
| 구간 최소 길이 | `MIN_SECTION_SEC = 8.0`(초). 실제 곡 스파이크에서 조정 | `analysis/sections.py` |
| 경계 후보 비교 창 | `JUMP_WINDOW_SEC = 4`(초) | `analysis/sections.py` |
| 경계로 볼 에너지 변화 임계값 | `BOUNDARY_JUMP_THRESHOLD = 0.35`(곡 평균 대비 비율). 실제 곡 스파이크에서 조정 | `analysis/sections.py` |
| 구간 개수 상한 | `MAX_SECTIONS = 12` (노이즈로 인한 LLM 호출 폭증 방지) | `analysis/sections.py` |
| 구간 라벨 규칙 | 첫 구간 `intro`, 마지막 구간 `outro`, 나머지는 에너지 비 > 1.0 이면 `chorus` 아니면 `verse`. 실제 음악 구조 인식이 아니라 근사치임을 문서화 | `analysis/sections.py` |
| 자동 재생성 최대 횟수 | `MAX_SECTION_REGEN = 2` (스펙 §7: "최대 2회 자동 재생성") | `graph_nodes.py` |
| 구간 동시 호출 수 상한 | `MAX_CONCURRENT_PROPOSALS = 3`. `Send` 팬아웃이 구간 수만큼(최대 `MAX_SECTIONS=12`개) 동시에 LLM 을 부르면 Gemini 무료 티어의 분당 요청 한도를 바로 넘길 수 있어, LangGraph 의 `max_concurrency` 설정으로 한 슈퍼스텝에서 동시에 도는 `propose` 노드 수를 묶는다. **동시 실행 개수만 제한할 뿐 분당 요청 수(RPM)를 정확히 지키는 진짜 속도 제한·백오프는 아니다** — 아래 "알려진 한계" 참고 | `graph_nodes.py`, `api.py` |
| `POST /sequence`의 성격 | 동기 요청, 매 호출마다 새 `threadId`(uuid4) 생성. 스펙 §4.2 의 `/runs`(멱등, 작업+폴링) 프로토콜이 **아니다** — 4단계에서 교체 | `api.py` |
| 체크포인터 DB | 로컬 docker-compose Postgres(`localhost:5433`). 배포 시 Neon 은 `DATABASE_URL`만 바꾼다(D2) | `docker-compose.yml`, `.env.example` |
| `postgres` pytest 마커 | `llm`과 같은 패턴으로 기본 실행에서 제외. `docker compose up -d checkpointer-db` 후 `-m postgres`로만 돈다 | `pyproject.toml` |

## 파일 구조

| 파일 | 책임 | 태스크 |
| --- | --- | --- |
| `src/stage_director/analysis/sections.py` | 에너지 곡선 기반 구간 경계 휴리스틱 | 1 |
| `src/stage_director/models.py`(수정) | `SequenceRequest`, `SequenceResponse` 추가 | 2 |
| `src/stage_director/graph_state.py` | `GraphState`, `ProposeTask` 타입 | 2 |
| `src/stage_director/graph_nodes.py` | 그래프 노드·라우팅 함수, `MAX_SECTION_REGEN` | 3 |
| `src/stage_director/graph.py` | `build_sequence_graph(llm, checkpointer)` | 3 |
| `src/stage_director/checkpointer.py` | `postgres_checkpointer(database_url)` | 4 |
| `docker-compose.yml` | 로컬 체크포인터 Postgres | 4 |
| `src/stage_director/settings.py`(수정) | `database_url` 추가 | 5 |
| `src/stage_director/api.py`(수정) | `POST /sequence`, `lifespan`, 그래프 1회 컴파일 | 5 |
| `.env.example`(수정) | `DATABASE_URL` 추가 | 5 |
| `tests/analysis/test_sections.py`, `tests/test_models.py`(수정), `tests/test_graph.py`, `tests/test_checkpointer.py`, `tests/test_api.py`(수정), `tests/conftest.py`(수정) | 위 파일 각각의 테스트 | 1~5 |
| `tests/fixtures/sequence_request.json` | 곡 전체 요청 본문 예시 | 5 |
| `docs/superpowers/specs/stage-director-agent-design.md`(수정) | §13 갱신, §4.2 에 `/sequence` 잠정 성격 명시 | 6 |

---

### Task 1: 구간 경계 휴리스틱(`detect_sections`)

**Files:**
- Create: `src/stage_director/analysis/sections.py`
- Test: `tests/analysis/test_sections.py`

**Interfaces:**
- Consumes: `stage_director.models.Section`
- Produces (`stage_director.analysis.sections`):
  - `MIN_SECTION_SEC = 8.0`, `JUMP_WINDOW_SEC = 4`, `BOUNDARY_JUMP_THRESHOLD = 0.35`, `MAX_SECTIONS = 12`
  - `detect_sections(energy_curve: list[float], duration_sec: float) -> list[Section]` — 항상 길이 1 이상, 서로 이어지는(contiguous) 구간을 돌려준다. 예외를 던지지 않는다

- [ ] **Step 1: 브랜치를 만들고 시작 상태를 확인한다**

`main`은 싱글 제안 계획 PR이 병합된 상태여야 한다(이 문서 작성 시점에는 `feat/single-proposal`이 아직 병합 전이므로, 실행 전에 병합돼 있는지 먼저 확인한다).

```bash
git switch main && git pull --ff-only
git switch -c feat/sequence-graph
git add docs/superpowers/plans/stage-director-agent-sequence-graph.md
git commit -m "docs: add sequence graph plan"
uv sync && uv run pytest -q
```

Expected: `154 passed, 5 deselected`. 다르면 멈추고 원인을 확인한다(싱글 제안 계획의 최종 상태가 이 숫자다).

- [ ] **Step 2: 실패하는 테스트를 쓴다**

`tests/analysis/test_sections.py`:

_(코드 본문 생략 — 저장소의 `tests/analysis/test_sections.py` 참고)_

- [ ] **Step 3: 실패하는 것을 확인한다**

Run: `uv run pytest tests/analysis/test_sections.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.analysis.sections'`

- [ ] **Step 4: 구현한다**

`src/stage_director/analysis/sections.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/analysis/sections.py` 참고)_

- [ ] **Step 5: 통과하는 것을 확인한다**

Run: `uv run pytest tests/analysis/test_sections.py -q`
Expected: `9 passed`

Run: `uv run pytest -q`
Expected: `163 passed, 5 deselected`

- [ ] **Step 6: 커밋한다**

```bash
git add src/stage_director/analysis/sections.py tests/analysis/test_sections.py
git commit -m "feat: add energy-curve section boundary heuristic"
```

`MIN_SECTION_SEC`/`JUMP_WINDOW_SEC`/`BOUNDARY_JUMP_THRESHOLD`는 아직 초기값이다. 실제 곡으로 듣고 조정하는 사람 단계는 **Task 6**으로 미뤘다 — 전체 파이프라인(그래프·API)이 다 만들어진 뒤 한 번에 사람이 확인하기 위해서다. 이 태스크는 여기서 끝난다(합성 신호 테스트 9개만으로 완결).

---

### Task 2: 곡 전체 요청·응답 모델과 그래프 상태 타입

**Files:**
- Modify: `src/stage_director/models.py`
- Create: `src/stage_director/graph_state.py`
- Test: `tests/test_models.py`(수정)

**Interfaces:**
- Consumes: `stage_director.models.{Track, Artist, Preset, Section, Issue, CamelModel}`(기존), `stage_director.sequence.SequenceItem`, `stage_director.proposals.merge_proposals`
- Produces:
  - `stage_director.models`: `SequenceRequest(track, artist, presets=[], analysis=None, duration_sec: float > 0)`, `SequenceResponse(thread_id: str, sections: list[Section], items: list[SequenceItem], issues: list[Issue])`
  - `stage_director.graph_state`: `ProposeTask(TypedDict){idx: int, section: Section, request: SequenceRequest}`, `GraphState(TypedDict, total=False){request, sections, proposals: Annotated[dict[int, SectionProposal], merge_proposals], regen_round: int, regen_targets: set[int], final_items: list[SequenceItem], final_issues: list[Issue]}`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_models.py`에 추가(기존 import 에 `SequenceRequest`, `SequenceResponse` 포함):

_(코드 본문 생략 — 저장소의 `tests/test_models.py` 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'SequenceRequest' from 'stage_director.models'`

- [ ] **Step 3: `models.py`에 추가한다**

_(코드 본문 생략 — 저장소의 `src/stage_director/models.py` 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_models.py -q`
Expected: `18 passed`(기존 13개 + 신규 5개)

Run: `uv run pytest -q`
Expected: `168 passed, 5 deselected`

- [ ] **Step 5: 커밋한다**

```bash
git add src/stage_director/models.py tests/test_models.py
git commit -m "feat: add whole-song sequence request and response models"
```

- [ ] **Step 6: 그래프 상태 타입을 만든다 (테스트 없음 — 타입 정의뿐이며 Task 3 에서 실제로 쓰이며 검증된다)**

`src/stage_director/graph_state.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/graph_state.py` 참고)_

```bash
git add src/stage_director/graph_state.py
git commit -m "feat: add sequence graph state types"
```

---

### Task 3: LangGraph 그래프 — 구간 분해, 병렬 제안, 자동 재생성

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (`langgraph` 추가)
- Create: `src/stage_director/graph_nodes.py`, `src/stage_director/graph.py`
- Test: `tests/test_graph.py`

**Interfaces:**
- Consumes: `stage_director.graph_state.{GraphState, ProposeTask}`(Task 2), `stage_director.analysis.sections.detect_sections`(Task 1), `stage_director.analysis.snapshot.parse_analysis`, `stage_director.gate.{run_gate, indices_to_regenerate}`, `stage_director.sequence.validate_sequence`, `stage_director.propose.propose_section`, `stage_director.models.{ProposeRequest, Issue}`, `stage_director.llm.client.LLMClient`(모두 기존, 변경 없음)
- Produces:
  - `stage_director.graph_nodes`: `MAX_SECTION_REGEN = 2`, `MAX_CONCURRENT_PROPOSALS = 3`, `detect_node`, `fan_out_initial`, `make_propose_node(llm) -> Callable`, `assemble_node`, `decide_regen`
  - `stage_director.graph`: `build_sequence_graph(llm: LLMClient, checkpointer=None) -> CompiledStateGraph` — `.invoke({"request": SequenceRequest}, config={"configurable": {"thread_id": str}, "max_concurrency": MAX_CONCURRENT_PROPOSALS})`가 `GraphState`(`final_items`, `final_issues`, `sections`, `regen_round` 포함)를 돌려준다. `max_concurrency`는 호출하는 쪽(그래프 자체가 아니라 `invoke` config)이 책임진다

동작 요약: `detect` 가 `sections`를 채움 → 조건부 엣지가 구간마다 `Send("propose", ProposeTask)` → `propose` 가 **변경 없는** `propose_section`을 호출해 `proposals[idx]`에 병합 → `assemble` 이 `proposals`를 idx 순으로 모아 `items`·`energy_ratios`를 만들고 `run_gate`(곡 전체, 구간 간 방향 규칙 포함)와 `validate_sequence`를 돌려 `final_items`/`final_issues`/`regen_targets`를 채움 → `regen_targets`가 있고 `regen_round <= MAX_SECTION_REGEN`이면 그 구간만 다시 `Send`, 아니면 종료.

- [ ] **Step 1: 의존성을 추가한다**

```bash
uv add langgraph
```

Expected: `pyproject.toml`의 `dependencies`에 `langgraph`가 추가된다(실행 시점에 버전을 확인한다).

- [ ] **Step 2: 실패하는 테스트를 쓴다**

`tests/test_graph.py`:

_(코드 본문 생략 — 저장소의 `tests/test_graph.py` 참고)_

- [ ] **Step 3: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_graph.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.graph'` (collection 오류)

- [ ] **Step 4: 구현한다**

`src/stage_director/graph_nodes.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/graph_nodes.py` 참고)_

`src/stage_director/graph.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/graph.py` 참고)_

- [ ] **Step 5: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_graph.py -q`
Expected: `6 passed`

Run: `uv run pytest -q`
Expected: `174 passed, 5 deselected`

- [ ] **Step 6: 커밋한다**

```bash
git add pyproject.toml uv.lock src/stage_director/graph_nodes.py src/stage_director/graph.py tests/test_graph.py
git commit -m "feat: add sequence graph with section fan-out and auto-regeneration"
```

---

### Task 4: Postgres 체크포인터 연동 (로컬 docker-compose)

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (`langgraph-checkpoint-postgres` 추가)
- Create: `docker-compose.yml`, `src/stage_director/checkpointer.py`
- Test: `tests/test_checkpointer.py`

**Interfaces:**
- Consumes: `stage_director.graph.build_sequence_graph`(Task 3), `stage_director.llm.fake.FakeLLM`
- Produces: `stage_director.checkpointer.postgres_checkpointer(database_url: str) -> ContextManager[PostgresSaver]` — 열 때마다 `.setup()`(멱등)으로 체크포인트 테이블을 보장한다

- [ ] **Step 1: 의존성을 추가하고 로컬 Postgres 를 정의한다**

```bash
uv add langgraph-checkpoint-postgres
```

Expected: `dependencies`에 `langgraph-checkpoint-postgres`(psycopg 를 함께 끌어온다)가 추가된다. `uv run python -c "import psycopg"`가 에러 없이 끝나는지 확인한다.

`docker-compose.yml`:

_(코드 본문 생략 — 저장소의 `docker-compose.yml` 참고)_

- [ ] **Step 2: 실패하는 테스트를 쓴다**

`tests/test_checkpointer.py`:

_(코드 본문 생략 — 저장소의 `tests/test_checkpointer.py` 참고)_

- [ ] **Step 3: pytest 마커를 등록한다**

`pyproject.toml`의 `[tool.pytest.ini_options]`를 아래처럼 고친다.

```toml
[tool.pytest.ini_options]
pythonpath = ["src", "."]
testpaths = ["tests"]
markers = [
    "llm: 실제 LLM 을 호출한다. uv run --env-file .env pytest -m llm 으로만 돈다",
    "postgres: 로컬 Postgres(docker-compose)가 필요하다. docker compose up -d checkpointer-db 후 -m postgres 로만 돈다",
]
addopts = "-m 'not llm and not postgres'"
```

- [ ] **Step 4: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_checkpointer.py -m postgres -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.checkpointer'` (collection 오류)

- [ ] **Step 5: 구현한다**

`src/stage_director/checkpointer.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/checkpointer.py` 참고)_

- [ ] **Step 6: 로컬 Postgres 를 띄우고 통과하는 것을 확인한다**

```bash
docker compose up -d checkpointer-db
sleep 3  # Postgres 기동 대기
uv run pytest -m postgres -q
```

Expected: `2 passed`

```bash
uv run pytest -q
```

Expected: `174 passed, 7 deselected` (llm 5개 + postgres 2개)

```bash
docker compose down
```

- [ ] **Step 7: 커밋한다**

```bash
git add pyproject.toml uv.lock docker-compose.yml src/stage_director/checkpointer.py tests/test_checkpointer.py
git commit -m "feat: wire PostgresSaver checkpointer with local docker-compose"
```

---

### Task 5: FastAPI `POST /sequence`

**Files:**
- Modify: `src/stage_director/settings.py`, `src/stage_director/api.py`, `.env.example`, `tests/conftest.py`, `tests/test_api.py`
- Create: `tests/fixtures/sequence_request.json`

**Interfaces:**
- Consumes: `stage_director.graph.build_sequence_graph`(Task 3), `stage_director.checkpointer.postgres_checkpointer`(Task 4), `stage_director.models.{SequenceRequest, SequenceResponse}`(Task 2)
- Produces:
  - `stage_director.settings.Settings` — 기존 3개 필드 + `database_url: str`(`from_env`에서 `DATABASE_URL` 필수)
  - `stage_director.api.create_app(settings=None, llm=None, checkpointer_cm=None)` — `checkpointer_cm: Callable[[], ContextManager[BaseCheckpointSaver]]`. 기본값은 `lambda: postgres_checkpointer(settings.database_url)`. 앱의 `lifespan`에서 한 번 열어 그래프를 컴파일해 `app.state.sequence_graph`에 둔다
  - `POST /sequence` — 요청 `SequenceRequest`(camelCase), 200 `SequenceResponse`(`threadId`는 매 호출 새 uuid4). 인증 401, 본문 오류 422, LLM 실패 502. **체크포인터가 뜨지 않으면 앱이 기동에 실패한다**(요청 단위가 아니라 서비스 단위 fail-fast)

- [ ] **Step 1: `Settings`에 `database_url`을 추가하고 기존 테스트를 고친다**

`src/stage_director/settings.py`에 `database_url: str` 필드와 `from_env`의 `DATABASE_URL` 필수화를 추가한다:

_(코드 본문 생략 — 저장소의 `src/stage_director/settings.py` 참고)_

`tests/test_api.py`에서 `Settings(...)`를 직접 생성하는 두 곳(`client()` 헬퍼와 `test_rejects_missing_or_wrong_key`)에 `database_url="unused"`를 추가하고, `test_settings_from_env`/`test_settings_refuse_to_start_without_keys`에 `DATABASE_URL`을 반영한다:

_(코드 본문 생략 — 저장소의 `tests/test_api.py` 참고)_

- [ ] **Step 2: `uv run pytest -q`가 깨지는지 확인하고(의도된 중간 상태), 곡 전체 요청 픽스처를 만든다**

Run: `uv run pytest tests/test_api.py -q`
Expected: FAIL — `client()` 헬퍼의 `Settings(...)` 호출에 `database_url`이 빠져 `TypeError`. (바로 위 Step 1 에서 이미 고쳤다면 이 단계는 건너뛰고 통과해야 한다. 고치지 않았다면 지금 고친다.)

`tests/fixtures/sequence_request.json`(0~30초 잔잔, 30~60초 큰 소리 — `detect_sections`가 정확히 구간 2개를 내도록 설계):

_(코드 본문 생략 — 저장소의 `tests/fixtures/sequence_request.json` 참고)_

`tests/conftest.py`에 `SEQUENCE_REQUEST` 상수를 추가한다:

_(코드 본문 생략 — 저장소의 `tests/conftest.py` 참고)_

- [ ] **Step 3: 실패하는 테스트를 쓴다**

`tests/test_api.py`에 `/sequence` 테스트와 보조 헬퍼(`sequence_app`)를 추가한다:

_(코드 본문 생략 — 저장소의 `tests/test_api.py` 참고)_

- [ ] **Step 4: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_api.py -q`
Expected: FAIL — `/sequence` 라우트가 없어 404 (`test_sequence_requires_internal_key` 등에서 401 대신 404), 혹은 `create_app`에 `checkpointer_cm` 인자가 없어 `TypeError`

- [ ] **Step 5: 구현한다**

`src/stage_director/api.py` 전체를 바꾼다:

_(코드 본문 생략 — 저장소의 `src/stage_director/api.py` 참고)_

`.env.example`에 `DATABASE_URL` 한 줄을 추가한다:

_(코드 본문 생략 — 저장소의 `.env.example` 참고)_

- [ ] **Step 6: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_api.py -q`
Expected: `20 passed`(기존 14개 + 신규 5개 + 기존 parametrize 1개 증가)

Run: `uv run pytest -q`
Expected: `180 passed, 7 deselected`

- [ ] **Step 7: 실제 서버를 로컬 Postgres 와 함께 띄워 확인한다**

```bash
docker compose up -d checkpointer-db && sleep 3
INTERNAL_API_KEY=dev-key GEMINI_API_KEY=fake-key DATABASE_URL=postgresql://stage_director:stage_director@localhost:5433/stage_director_checkpoints \
  uv run uvicorn --factory stage_director.api:create_app --port 8765 &
sleep 5
curl -s -w " -> %{http_code}\n" -X POST localhost:8765/sequence -H 'X-Internal-Key: dev-key' -H 'Content-Type: application/json' --data @tests/fixtures/sequence_request.json
kill %1
docker compose down
```

Expected: Gemini 가 가짜 키를 거절해 재시도 후 502(`{"detail":"llm_failed"}`, 구간이 2개라 `/propose`보다 오래 걸릴 수 있다). 체크포인터가 정상적으로 열려 앱이 기동에 성공했다는 확인이다(기동 자체가 실패하면 curl 이 연결을 거부한다).

- [ ] **Step 8: 커밋한다**

```bash
git add src/stage_director/settings.py src/stage_director/api.py .env.example tests/conftest.py tests/test_api.py tests/fixtures/sequence_request.json
git commit -m "feat: add POST /sequence endpoint wired to the checkpointer"
```

---

### Task 6: 통합 검증 · 구간 경계 스파이크 · 스펙 갱신 · 실제 Gemini + 실제 Neon

**Files:**
- Modify: `src/stage_director/analysis/sections.py`(Step 2 에서 상수를 조정할 수도 있음), `docs/superpowers/specs/stage-director-agent-design.md`(§13, §4.2)

**Interfaces:**
- Consumes: 이 계획의 모든 산출물
- Produces: 없음(검증·보정·문서 작업)

- [ ] **Step 1: 전체 테스트를 돌린다**

```bash
uv run pytest -q
```

Expected: `180 passed, 7 deselected`

```bash
docker compose up -d checkpointer-db && sleep 3
uv run pytest -m postgres -q
docker compose down
```

Expected: `2 passed`

```bash
uv run ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 2: (사람이 하는 단계) 실제 곡 2개로 구간 경계 스파이크를 실행한다**

에이전트가 들을 수 없는 단계다. Task 1 에서 합성 신호로만 검증한 `detect_sections`가 `demo-tracks/`의 실제 곡 2개(`나만의_작은_우주.mp3`, `burn it up.mp3`)에서 들을 만한 경계를 내는지 확인한다.

```bash
uv run python -c "
from stage_director.analysis.measure import measure_file
from stage_director.analysis.sections import detect_sections

for path in ['demo-tracks/나만의_작은_우주.mp3', 'demo-tracks/burn it up.mp3']:
    snap = measure_file(path)
    sections = detect_sections(snap.energy_curve, snap.duration_sec)
    print(path, 'bpm=', snap.bpm)
    for s in sections:
        print(f'  {s.label:7s} {s.start_sec:6.1f} ~ {s.end_sec:6.1f}')
"
```

곡을 들으면서 아래를 메모한다(`docs/superpowers/notes/sections-spike.md` 등 편한 곳에):

- 경계가 실제로 들리는 전환(인트로→벌스, 벌스→코러스 등) 근처에 있는가
- 구간이 너무 잘게 쪼개지거나(노이즈) 너무 안 쪼개지는가(변화를 놓침) — 쪼개지면 `BOUNDARY_JUMP_THRESHOLD`를 올리고, 안 쪼개지면 내린다
- `chorus`/`verse` 라벨이 실제 코러스/벌스와 대체로 맞는가(완벽히 맞을 필요는 없다 — 근사치임을 Step 3에서 문서화한다)

결과에 따라 `MIN_SECTION_SEC`, `JUMP_WINDOW_SEC`, `BOUNDARY_JUMP_THRESHOLD`를 `src/stage_director/analysis/sections.py`에서 조정하고 아래를 다시 돌려 아무것도 깨지지 않았는지 확인한다.

```bash
uv run pytest tests/analysis/test_sections.py -q && uv run pytest -q
```

Expected: `9 passed`, 그리고 `180 passed, 7 deselected`(합성 신호 테스트의 숫자 기대값과 상수가 맞지 않게 되면 테스트를 실패시킨 원인을 보고 임계값과 테스트 기대값 중 무엇이 틀렸는지 판단해 고친다). 값을 바꿨다면 수정 커밋을 하나 만든다.

`test_graph.py`의 `TWO_SECTION_ANALYSIS`와 `tests/fixtures/sequence_request.json`도 "정확히 구간 2개"를 전제로 만든 고정 픽스처라, 임계값을 많이 바꾸면 이쪽에서 실패가 날 수 있다(위 `uv run pytest -q`가 이미 잡아준다). 실패하면 두 픽스처의 에너지 곡선(잔잔한 구간/큰 소리 구간의 대비)을 새 임계값에서도 구간 2개가 나오도록 함께 고치고 같은 커밋에 넣는다.

```bash
git add src/stage_director/analysis/sections.py tests/test_graph.py tests/fixtures/sequence_request.json
git commit -m "fix: calibrate section boundary thresholds against real tracks"
```

값을 바꾸지 않았다면 이 커밋은 건너뛴다.

- [ ] **Step 3: 스펙 §13 표를 갱신한다**

`docs/superpowers/specs/stage-director-agent-design.md` §13 의 아래 행을

```
| 구조 분석 모델 | 기획서 그대로(예: all-in-one). 부정확하면 에너지 곡선 기반 경계 제안으로 전환. 싱글 제안은 구간을 입력으로 받아 이 모델을 쓰지 않는다 | 시퀀스 그래프 계획의 첫 태스크(스파이크) |
```

다음으로 바꾼다(Step 2 스파이크 결과를 반영해 구체적인 수치로 채운다).

```
| 구조 분석 모델 | all-in-one 류는 도입하지 않는다. 에너지 곡선 기반 휴리스틱(`detect_sections`)으로 확정. 설치 위험(PyTorch 등)과 실제 곡 2개 스파이크 결과가 근거(시퀀스 그래프 계획 Task 6) | 확정 |
```

같은 섹션의 "LLM provider" 행 중 "오디오 입력 무드 해석은 시퀀스 그래프 계획에서 검증" 부분을 "오디오 입력 무드 해석은 4단계(사람 개입)로 미룬다 — `sections.mood`는 interrupt #1 에서만 쓰여 3단계에는 소비자가 없다"로 바꾼다.

- [ ] **Step 4: 커밋한다**

```bash
git add docs/superpowers/specs/stage-director-agent-design.md
git commit -m "docs: finalize section-boundary heuristic over all-in-one in spec section 13"
```

- [ ] **Step 5: 스펙 §4.2 에 `/sequence`의 잠정적 성격을 명시한다**

`docs/superpowers/specs/stage-director-agent-design.md`의 §4.2 표 바로 아래에 한 문단을 추가한다(표와 "Python 엔드포인트는 모두..." 문단 사이):

_(문구 생략 — 상세본 Task 6 Step 5 참고)_

- [ ] **Step 6: 커밋한다**

```bash
git add docs/superpowers/specs/stage-director-agent-design.md
git commit -m "docs: note that POST /sequence is provisional pending the /runs protocol"
```

- [ ] **Step 7: (사람이 하는 단계) 실제 Gemini + 실제 Neon 으로 확인한다**

에이전트가 할 수 없는 단계다. `GEMINI_API_KEY`와 Neon 연결 문자열은 사용자가 준비한다.

```bash
# .env 에 INTERNAL_API_KEY, GEMINI_API_KEY, 그리고 DATABASE_URL 을 Neon 연결 문자열로 채운다
uv run --env-file .env pytest -m postgres -q
```

Expected: 로컬 docker 대신 실제 Neon 인스턴스에 `checkpoints` 등 테이블이 생기고 2개 통과. 실패하면 Neon 쪽 SSL 모드(`?sslmode=require`)나 연결 문자열 형식을 확인한다.

```bash
uv run --env-file .env uvicorn --factory stage_director.api:create_app --port 8765 &
sleep 5
curl -s -X POST localhost:8765/sequence -H "X-Internal-Key: $(grep ^INTERNAL_API_KEY .env | cut -d= -f2)" \
  -H 'Content-Type: application/json' --data @tests/fixtures/sequence_request.json | python3 -m json.tool
kill %1
```

`sections`의 경계와 라벨이 그럴듯한지, `items`가 `validate_sequence`를 통과할 모양인지(연속 구간, `startSec=0`, 마지막 `endSec=60`), `issues`가 비어 있거나 합리적인지 눈으로 본다. Neon 콘솔에서 `checkpoints` 테이블에 행이 쌓였는지도 확인한다. 결과(통과 여부, Neon 연결 이슈 유무)는 PR 설명에 적는다.

---

## 스펙 대응

| 스펙 | 이 계획의 태스크 |
| --- | --- |
| §13 구조 분석 모델 결정 | Task 1(구현), Task 6(실제 곡 검증·확정) |
| §6.3 `sections`(mood 제외), `proposals` 리듀서, 부분 재생성(idx 단위 Send 재전송) | Task 2, 3 |
| §5 `items` 불변식(`validate_sequence` 재사용) | Task 3 |
| §7 2층(위반 최대 2회 자동 재생성) | Task 3 |
| §6.1 체크포인터 전용 Postgres, D2(로컬 docker-compose / 배포 Neon) | Task 4 |
| §4.2(일부) 곡 전체 제안 엔드포인트 | Task 5 |
| §9 LLM 인터페이스·가짜 LLM 재사용, 프로토콜 테스트 | Task 3, 5 |
| §4.2 나머지(`/runs`, 409/410, 폴링, 멱등), §6(jobs, interrupt #1·#2, revision), §6.4 보존 정책, §13 오디오 입력 무드 해석 | 이 계획 밖 (4단계) |

## 알려진 한계와 4단계로 넘기는 것

| 한계 | 넘기는 곳 |
| --- | --- |
| `POST /sequence`는 동기 요청이며 구간 수만큼 LLM 호출이 걸려 `/propose`보다 훨씬 오래 걸릴 수 있다 | 4단계 (작업 + 폴링, D4) |
| `max_concurrency=3`은 한 번에 도는 `propose` 노드 수만 묶을 뿐, 분당 요청 수(RPM) 기반 진짜 속도 제한이나 재시도 사이 백오프는 없다(싱글 제안 계획이 이미 보류한 "재시도 사이 대기"와 같은 틈). 구간이 많고 모델의 실제 무료 티어 한도가 낮으면 `/sequence`가 여전히 429 로 실패할 수 있다 | 4단계(작업+폴링과 함께 토큰 버킷 등으로 재설계) |
| 매 호출 새 `threadId`를 만든다 — 스펙 §4.2 의 "이미 draft 가 있으면 그 행을 반환" 같은 멱등성이 없다 | 4단계 (`/runs` 프로토콜) |
| 이웃 구간 컨텍스트 없이 각 구간이 독립적으로 제안된다(전환을 더 자연스럽게 하려면 앞뒤 구간 정보가 필요) | 이 계획에도 넣지 않음 — 필요성이 확인되면 별도 작업 |
| `sections`에 `mood` 필드가 없다. interrupt #1 이 생길 때(4단계) 추가하고, 그때 오디오 입력 무드 해석 LLM 노드를 함께 설계한다 | 4단계 |
| 체크포인트 보존 정책(승인 24시간 후 삭제, 미승인 7일 방치 삭제)이 없다 — 이 계획은 체크포인트를 쌓기만 한다 | 4단계/5단계 (§6.4) |
| `jobs` 테이블이 없다. 프로세스가 죽었을 때 "실행 중이었는지" 알 방법이 없다 | 4단계 |
