# 무대 연출 디렉터 에이전트 — 사람 개입 구현 계획 (interrupt #1·#2 · 무드 해석 · `/runs` 프로토콜 · 보존 정책)

> 이 문서는 태스크 순서, 검증 절차, 명령, 기대 결과를 담는다.

**Goal:** 시퀀스 그래프에 사람 개입 두 지점(interrupt #1 구간 확인·수정, interrupt #2 구간별 리뷰·피드백·재생성·전체 승인)과 오디오 입력 무드 해석 노드를 넣고, 잠정 `POST /sequence`를 `jobs` 테이블 기반의 비동기 `POST /runs` · `GET /runs/{threadId}` · `POST /runs/{threadId}/resume` 프로토콜(409 방어, 멱등 시작, 실패 후 마지막 체크포인트에서 재개)로 교체하고, 체크포인트 보존 정책(§6.4)을 구현한다.

**Architecture:** 그래프는 `detect → mood → confirm_sections(⏸#1) → propose(Send) → assemble ⟲(자동 재생성) → review(⏸#2) → [피드백이면 targets만 propose 재전송 → assemble → review …] → END` 로 확장된다. `interrupt()` 를 쓰는 두 노드는 재개 시 처음부터 다시 실행되므로 interrupt 앞에 부수효과를 두지 않는다. 실행은 FastAPI 프로세스 안의 스레드에서 동기 `graph.invoke` 를 돌리고, 전용 Postgres 의 `jobs` 행의 **조건부 상태 전이**가 중복 실행·더블 클릭을 막는다(`Runner`). 서버 기동 시 보존 정책을 돌린 뒤 `running` 이던 작업을 `error(interrupted)` 로 바꾸고, "다시 시도"는 같은 `thread_id` 로 `invoke(None)` 해 마지막 체크포인트에서 재개한다.

**Tech Stack:** Python 3.12, uv, pydantic 2, FastAPI, LangGraph(`interrupt`, `Command`, `Send`), `langgraph-checkpoint-postgres`, psycopg 3 pool, google-genai(오디오 입력), pytest

## 이 계획의 범위

시작 상태: `origin/main`(PR #3 "시퀀스 그래프" 병합 후), 테스트 186개 통과(7개는 `llm`·`postgres` 마커로 제외).

| 포함 | 스펙 위치 |
| --- | --- |
| Task 1: `Section.mood`, `audioUrl`, `/runs` 요청·응답·resume 모델 | §4.2, §6.3 |
| Task 2: `jobs` 테이블 — `JobStore`(메모리·Postgres), 조건부 전이, 기동 복구용 `fail_running` | §6.1, D4 |
| Task 3: 오디오 입력 무드 해석 — LLM 오디오 입력, 음원 내려받기, `interpret_moods` | §13 |
| Task 4: interrupt #1 — `mood` · `confirm_sections` 노드, 구간 수정 검증 | §6.3 |
| Task 5: `propose_section` 입력 확장(무드, 피드백, 이전 제안) | §6.3 |
| Task 6: interrupt #2 — `review` 노드, 피드백 부분 재생성, `revision`·`interruptId` | §6.3, §7 |
| Task 7: `Runner` + `POST /runs` · `GET /runs/{id}` · `POST /runs/{id}/resume` (`/sequence` 삭제) | §4.2, §6.1, §8 |
| Task 8: 체크포인트 보존 정책(규칙 1·2) | §6.4 |
| Task 9: 통합 검증(Postgres 프로세스 재시작 재개), 스펙 갱신, 사람 단계(실제 Gemini 오디오 + 실제 Neon) | §9, §13 |

**이 계획 밖.** 분석 작업(`jobs.kind=analysis`, `progress` 컬럼, 7일 결과 보관), 시드 곡 오프라인 분석, 배포(호스팅·콜드 스타트), Next.js 라우트(`/api/sequences*`)·프론트 마커 UI, Python 쪽 레이트 리밋/RPM 토큰 버킷은 5단계 또는 on-stage 쪽 작업이다.

## 확정한 결정 사항

| 항목 | 결정 | 이유 |
| --- | --- | --- |
| 피드백 `targets` | **사용자가 지정한다.** resume 페이로드를 `{action:"feedback", text, targets:[idx]}` 로 확장한다(스펙 §6.3 수정, Task 9). LLM 라우터 노드는 만들지 않는다 | 결정적이고 부분 재생성 불변식 테스트가 단순하다. 프론트가 구간 선택 UI 를 만든다 |
| 무드 해석 입력 | **Gemini 오디오 입력.** 구간마다 부르지 않고 **곡 전체 오디오 + 구간 시각 목록을 1회 호출**한다 | 호출이 1회라 RPM 부담이 없고 오디오 슬라이싱 코드가 필요 없다. 실패는 비치명적(`mood=""`) |
| 실행 방식 | **프로세스 내 스레드 + `jobs` 행 조건부 전이.** 별도 워커 없음 | 스펙 §3 구성 그대로. 인스턴스 1개 가정을 코드에 `ponytail:` 로 남긴다 |
| `/sequence` | **삭제하고 `/runs` 로 교체.** `/propose` 는 유지 | 스펙 §4.2 가 "엔드포인트만 교체"로 못박았다. interrupt 가 생기면 동기 호출은 첫 interrupt 에서 멈춰 의미가 없다 |
| 모르는 스레드 | Python 은 **404**(`thread_not_found`). 410 변환·초안 행 삭제는 Next.js 몫(스펙 §4.2·§6.4) | Python 은 "존재한 적 없음"과 "만료로 삭제됨"을 구별할 수 없다 |

## LangGraph 1.2.12 에서 확인한 사실 (이 계획의 설계 근거)

2026-10-03 스파이크로 확인했다.

- `interrupt(value)` 를 부른 노드는 `Command(resume=v)` 로 재개할 때 **처음부터 다시 실행**된다. 그래서 interrupt 앞에 부수효과를 두지 않는다.
- `graph.invoke(...)` 결과에 `"__interrupt__"` 가 들어 있고, 대기 중인 값은 `graph.get_state(config).tasks[i].interrupts[j].value` 로도 읽힌다.
- `Send` 팬아웃 중 한 태스크가 예외를 내면 `invoke` 가 예외를 던지고 **성공한 태스크의 결과는 체크포인트에 남는다.** `invoke(None, config)` 로 다시 부르면 **실패한 태스크만** 다시 돈다. "다시 시도"는 이것으로 구현한다.
- LangGraph 가 붙이는 interrupt id 는 쓰지 않는다. 스펙 §6.3 의 `interruptId = f"{thread_id}:{revision}:{kind}"` 를 페이로드에 직접 넣는다.

## Global Constraints

모든 태스크의 요구사항에 아래가 암묵적으로 포함된다. 값은 스펙과 선행 계획에서 그대로 옮겼다.

- Python 서비스는 Supabase 에 접근하지 않는다. 필요한 컨텍스트는 요청 본문으로 받는다 (§3).
- **`../on-stage`를 열지 않는다** (§12 제약 2). 필요한 모양은 모두 `contracts/`에 있다.
- `sanitize_state`, `run_gate`, `indices_to_regenerate`, `validate_sequence`, `merge_proposals`, `parse_analysis` 는 **변경하지 않고 그대로 재사용**한다. `propose_section` 은 Task 5 에서 **기본값이 있는 입력 필드만 추가**하며(기존 호출 동작 불변) 그 밖에는 건드리지 않는다.
- Python 엔드포인트는 모두 `X-Internal-Key` 필수 (§4.2).
- 결정적 로직은 **TDD로 처음부터** 작성한다: 실패하는 테스트 → 실패 확인 → 최소 구현 → 통과 확인 (§12 제약 5).
- `interruptId = f"{thread_id}:{revision}:{kind}"` (§6.3). `revision` 은 interrupt 노드가 resume 을 받을 때마다 1씩 오른다.
- 낡은 resume·더블 클릭은 409 (§4.2, §8). `waiting_input` 이면서 `interruptId` 가 현재 것과 같을 때만 받는다.
- 부분 재생성 불변식: 피드백이 지정한 `targets` 밖의 `proposals[idx]` 는 바이트 단위로 같아야 한다 (§6.3).
- 구간 경계는 interrupt #1 에서만 바뀐다. interrupt #1 resume 은 연속 덮음·최소 길이를 검증한 뒤 받는다 (§6.3).
- `jobs` 상태: `running / waiting_input / done / error`. 전이는 반드시 조건부 UPDATE 로 한다 (§6.1).
- 보존 정책 (§6.4): 승인된 스레드는 `done` 24시간 후 **체크포인트만** 삭제, 승인되지 않은 스레드는 `updated_at` 7일 방치 시 **체크포인트와 jobs 행 모두** 삭제. 서비스 시작 시와 주기 스크립트에서 실행.
- 전용 Postgres 는 Python 외 접근하지 않는다 (§6.1).

## Review Focus

- 사람이 고친 구간이 빈틈·겹침·최소 길이 미만·12개 초과·곡 길이 불일치·빈 라벨이면 **422 로 거절하고 interrupt 는 그대로 남아야** 한다 → Task 4(검증 함수), Task 7(HTTP 422·상태 유지)
- 더블 클릭·낡은 화면에서 같은 resume 이 두 번 오면 **그래프는 정확히 한 번만 돌고** 나머지는 409 여야 한다 → Task 7
- 프로세스가 죽거나 LLM 이 실패하면 작업이 `running` 으로 영원히 남지 않고 `error` 가 되며, 다시 시도하면 **이미 끝난 구간의 LLM 호출을 반복하지 않고** 마지막 체크포인트에서 이어진다 → Task 2, 7, 9
- 무드 해석이 깨져도(JSON 아님·개수 불일치·비문자열·과대 파일·http URL·네트워크 실패) 시퀀스 생성이 **막히지 않아야** 하고, 사용자가 입력한 무드·피드백 문구가 프롬프트에서 지시로 취급되지 않아야 한다 → Task 3, 4, 5
- 피드백 턴에서 게이트가 targets 밖 구간을 문제 삼아도 그 구간을 **다시 만들지 않는다**(바이트 동일). 남은 위반은 `issues` 로 사람에게 보인다 → Task 6

## 이 계획이 정한 값 (스펙이 정하지 않은 것)

| 값 | 초기값 | 위치 |
| --- | --- | --- |
| 피드백 문구 최대 길이 | `MAX_FEEDBACK_CHARS = 500` | `models.py` |
| 구간 무드 최대 길이(사용자 입력 포함) | `SECTION_MOOD_MAX = 200` | `models.py` |
| 모델이 낸 무드 최대 길이 | `MOOD_OUTPUT_MAX = 100` | `mood.py` |
| 구간 라벨 최대 길이 | `MAX_LABEL_CHARS = 40` | `analysis/sections.py` |
| 음원 내려받기 상한 | `MAX_AUDIO_BYTES = 15 MiB`(Gemini 인라인 요청 한도 20MB 아래), `AUDIO_TIMEOUT_SEC = 30`, https 만 허용 | `audio.py` |
| 동시 그래프 실행 수 | `MAX_CONCURRENT_RUNS = 2`(스레드 풀 크기). 구간 단위 동시 호출은 기존 `MAX_CONCURRENT_PROPOSALS = 3` | `runner.py` |
| 승인 후 체크포인트 보존 | `DONE_CHECKPOINT_TTL = 24시간` (§6.4 규칙 1) | `retention.py` |
| 초안 방치 보존 | `DRAFT_TTL = 7일` (§6.4 규칙 2) | `retention.py` |
| `jobs` 의 `progress` 컬럼 | 만들지 않는다. 분석 작업(5단계)이 추가 | `jobs.py` |
| resume 오류 코드 | 409: `not_waiting_input` · `stale_interrupt` · `kind_mismatch` / 422: `invalid_sections` · `invalid_targets` | `runner.py`, `api.py` |

## 파일 구조

| 파일 | 책임 | 태스크 |
| --- | --- | --- |
| `src/stage_director/models.py`(수정) | `Section.mood`, `SequenceRequest.audio_url`, `RunCreate`, `RunStatus`, resume 모델, `ProposeRequest.feedback/previous`, `SequenceResponse` 삭제 | 1, 5, 7 |
| `src/stage_director/jobs.py` | `Job`, `JobStore`, `InMemoryJobStore`, `PostgresJobStore` | 2 |
| `src/stage_director/llm/client.py`, `llm/fake.py`, `llm/gemini.py`(수정) | `generate_json_with_audio` | 3 |
| `src/stage_director/audio.py` | `fetch_audio`, `AudioError` | 3 |
| `src/stage_director/mood.py` | `interpret_moods` | 3 |
| `src/stage_director/analysis/sections.py`(수정) | `validate_section_edit` | 4 |
| `src/stage_director/graph_state.py`(수정) | `revision`, `feedback_log`, `feedback_targets`, `review_action`, `ProposeTask` 확장 | 4, 6 |
| `src/stage_director/graph_nodes.py`(수정) | `make_mood_node`, `confirm_sections_node`, `review_node`, `decide_review`, `_propose_task` | 4, 6 |
| `src/stage_director/graph.py`(수정) | 노드·엣지 재배선, `checkpointer` 필수 | 4, 6 |
| `src/stage_director/prompts.py`, `propose.py`(수정) | 무드·피드백 프롬프트 | 5 |
| `src/stage_director/runner.py` | `Runner`, `RunNotFound`, `RunConflict`, `InvalidResume` | 7 |
| `src/stage_director/api.py`(수정) | `/runs` 3개, `/sequence` 삭제, `lifespan`(보존 정책·기동 복구) | 7, 8 |
| `src/stage_director/retention.py` | `purge`, `main`(CLI) | 8 |
| `tests/graph_helpers.py` | `drive`, `default_answer` | 4, 6 |
| `tests/conftest.py`(수정) | `InlineExecutor`, `DeferredExecutor` | 7 |
| `tests/test_models.py`, `test_jobs.py`, `test_llm.py`, `test_audio.py`, `test_mood.py`, `analysis/test_sections.py`, `test_graph.py`, `test_propose.py`, `test_runner.py`, `test_api.py`, `test_retention.py`, `test_checkpointer.py`(수정) | 위 파일 각각의 테스트 | 1~8 |
| `docs/superpowers/specs/stage-director-agent-design.md`(수정) | §4.2, §6.1, §6.3, §13 갱신 | 9 |

---

### Task 1: 모델 — 무드, 음원 URL, `/runs` 요청·응답·resume

**Files:**
- Modify: `src/stage_director/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces (`stage_director.models`): `SECTION_MOOD_MAX`, `MAX_FEEDBACK_CHARS`, `Section.mood`, `SequenceRequest.audio_url`, `RunCreate`, `RunStatus`, `SectionsPayload`, `FeedbackPayload`, `SectionsResume`, `FeedbackResume`, `ApproveResume`, `ResumeRequest`(`kind` 판별 유니온)

- [ ] **Step 1: 브랜치를 만들고 시작 상태를 확인한다**

```bash
git switch main && git pull --ff-only
git switch -c feat/human-in-the-loop
git add docs/superpowers/plans/stage-director-agent-human-in-the-loop.md
git commit -m "docs: add human-in-the-loop plan"
uv sync && uv run pytest -q
```

Expected: `186 passed, 7 deselected`. 다르면 멈추고 원인을 확인한다.

- [ ] **Step 2: 실패하는 테스트를 쓴다** — `tests/test_models.py`: 무드 기본값·길이 상한, `audioUrl` 선택 필드, `RunCreate` 파싱·`threadId` 길이 검증, 세 `kind` 의 resume 파싱, 피드백 문구 공백 제거·빈 문구/501자/빈 targets/음수 targets 거절, `RunStatus` camelCase 직렬화

_(코드 본문 생략 — 상세본 Task 1 Step 2 참고)_

- [ ] **Step 3: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'ResumeRequest'`

- [ ] **Step 4: 구현한다** — `models.py`: `Section.mood`(기본 `""`, 최대 200자), `SequenceRequest.audio_url`, `RunCreate`, `RunStatus`, resume 모델 3종과 `Field(discriminator="kind")` 유니온

_(코드 본문 생략 — 저장소의 `src/stage_director/models.py` 참고)_

- [ ] **Step 5: 통과하는 것을 확인한다**

Run: `uv run pytest -q`
Expected: `201 passed, 7 deselected`

- [ ] **Step 6: 커밋한다**

```bash
git add src/stage_director/models.py tests/test_models.py
git commit -m "feat: add section mood, audio url and run protocol models"
```

---

### Task 2: `jobs` 테이블 — `JobStore`

**Files:**
- Create: `src/stage_director/jobs.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Produces (`stage_director.jobs`): `Job`, `JobStore`(`create`, `get`, `transition`, `fail_running`, `stale`, `delete`), `InMemoryJobStore()`, `PostgresJobStore(pool)`(생성 시 `CREATE TABLE IF NOT EXISTS jobs`)

- [ ] **Step 1: 실패하는 테스트를 쓴다** — 같은 계약 테스트를 메모리·Postgres 구현에 모두 돌린다(Postgres 쪽은 `postgres` 마커): 생성의 멱등, 없는 작업 조회, 허용된 상태에서만 전이, 결과·에러 저장, `fail_running` 은 `running` 만 `error("interrupted")` 로, `stale` 의 상태·나이 필터, 삭제, **동시 8개 전이 중 승자는 정확히 하나**

_(코드 본문 생략 — 상세본 Task 2 Step 1 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_jobs.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.jobs'`

- [ ] **Step 3: 구현한다** — 메모리 구현은 락 하나, Postgres 구현은 `UPDATE … WHERE status = ANY(…)` 의 `rowcount` 로 조건부 전이

_(코드 본문 생략 — 저장소의 `src/stage_director/jobs.py` 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_jobs.py -q`
Expected: `8 passed, 8 deselected`

```bash
docker compose up -d checkpointer-db && sleep 3
uv run pytest tests/test_jobs.py -m postgres -q
docker compose down
```

Expected: `8 passed`

Run: `uv run pytest -q`
Expected: `209 passed, 15 deselected`

- [ ] **Step 5: 커밋한다**

```bash
git add src/stage_director/jobs.py tests/test_jobs.py
git commit -m "feat: add jobs table store with conditional status transitions"
```

---

### Task 3: 오디오 입력 무드 해석

**Files:**
- Modify: `src/stage_director/llm/client.py`, `llm/fake.py`, `llm/gemini.py`
- Create: `src/stage_director/audio.py`, `src/stage_director/mood.py`
- Test: `tests/test_llm.py`(수정), `tests/test_audio.py`, `tests/test_mood.py`

**Interfaces:**
- Produces: `LLMClient.generate_json_with_audio(*, system, user, schema, audio, mime_type)`(`FakeLLM`·`GeminiClient` 구현), `audio.fetch_audio(url, *, opener) -> (bytes, mime)`·`AudioError`, `mood.interpret_moods(llm, audio, mime_type, sections, track) -> list[str]`(항상 구간 수만큼, 예외 없음)

- [ ] **Step 1: 실패하는 테스트를 쓴다 — LLM 오디오 입력**(`tests/test_llm.py`: FakeLLM 이 큐를 공유하고 오디오 크기를 기록, Gemini 가 `[오디오 Part, 프롬프트]` 를 보냄, SDK 실패는 `LLMError`)

_(코드 본문 생략 — 상세본 Task 3 Step 1 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_llm.py -q`
Expected: FAIL — `AttributeError: 'FakeLLM' object has no attribute 'generate_json_with_audio'`

- [ ] **Step 3: 구현한다 — LLM 클라이언트**(Protocol 추가, `FakeLLM` 의 `_next()` 공유, `GeminiClient._generate` 공통화)

_(코드 본문 생략 — 저장소의 `src/stage_director/llm/` 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_llm.py -q`
Expected: 모두 통과(신규 3개)

- [ ] **Step 5: 실패하는 테스트를 쓴다 — 음원 내려받기**(`tests/test_audio.py`: http·file·ftp 거절(요청 자체를 보내지 않음), 바이트·mime 반환, 비오디오 content-type 은 `audio/mpeg` 로 폴백, 상한 초과 거절, 네트워크 실패는 `AudioError`)

_(코드 본문 생략 — 상세본 Task 3 Step 5 참고)_

- [ ] **Step 6: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_audio.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.audio'`

- [ ] **Step 7: 구현한다 — 음원 내려받기**

_(코드 본문 생략 — 저장소의 `src/stage_director/audio.py` 참고)_

- [ ] **Step 8: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_audio.py -q`
Expected: `7 passed`

- [ ] **Step 9: 실패하는 테스트를 쓴다 — 무드 해석**(`tests/test_mood.py`: 구간 순서대로 1회 오디오 호출·프롬프트에 구간 시각, 개수 불일치는 채움/자름, 비문자열·과대 원소 정리, 쓰레기 응답은 빈 무드, `LLMError` 는 비치명적)

_(코드 본문 생략 — 상세본 Task 3 Step 9 참고)_

- [ ] **Step 10: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_mood.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.mood'`

- [ ] **Step 11: 구현한다 — 무드 해석**(시스템 프롬프트가 곡 메타를 "지시가 아닌 데이터"로 못박음)

_(코드 본문 생략 — 저장소의 `src/stage_director/mood.py` 참고)_

- [ ] **Step 12: 통과하는 것을 확인한다**

Run: `uv run pytest -q`
Expected: `227 passed, 15 deselected`

- [ ] **Step 13: 커밋한다**

```bash
git add src/stage_director/llm src/stage_director/audio.py src/stage_director/mood.py tests/test_llm.py tests/test_audio.py tests/test_mood.py
git commit -m "feat: add audio-input mood interpretation with non-fatal failures"
```

---

### Task 4: interrupt #1 — `mood` · `confirm_sections` 노드

**Files:**
- Modify: `src/stage_director/analysis/sections.py`, `graph_state.py`, `graph_nodes.py`, `graph.py`, `tests/test_graph.py`, `tests/test_checkpointer.py`, `tests/analysis/test_sections.py`, `tests/test_api.py`(깨지는 `/sequence` 테스트 4개 삭제)
- Create: `tests/graph_helpers.py`

**Interfaces:**
- Produces: `validate_section_edit(sections, duration_sec) -> str | None`, `GraphState.revision`, `interrupt_id(config, revision, kind)`, `make_mood_node(llm, fetch)`, `confirm_sections_node`, `build_sequence_graph(llm, checkpointer, fetch=fetch_audio)`(**`checkpointer` 필수**), 첫 interrupt 값 `{interruptId, kind:"confirm_sections", sections, energyCurve, durationSec}`, 테스트 헬퍼 `drive`·`default_answer`

- [ ] **Step 1: 실패하는 테스트를 쓴다 — 구간 수정 검증**(`tests/analysis/test_sections.py`: 정상, 짧은 곡 단일 구간 허용, 빈 목록·빈틈·겹침·0초 미시작·곡 끝 미도달·5초 구간·13개·빈 라벨·41자 라벨은 사유 문자열)

_(코드 본문 생략 — 상세본 Task 4 Step 1 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/analysis/test_sections.py -q`
Expected: FAIL — `ImportError: cannot import name 'validate_section_edit'`

- [ ] **Step 3: 구현한다 — 구간 수정 검증**

_(코드 본문 생략 — 저장소의 `src/stage_director/analysis/sections.py` 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest tests/analysis/test_sections.py -q`
Expected: 기존 9개 + 신규 11개 통과

- [ ] **Step 5: 테스트 헬퍼를 만든다** — `tests/graph_helpers.py`: interrupt 마다 기본 답(구간 그대로 확인, 리뷰는 승인)으로 재개하며 끝까지 돌리고 최종 상태 값을 돌려줌

_(코드 본문 생략 — 저장소의 `tests/graph_helpers.py` 참고)_

- [ ] **Step 6: 기존 그래프 테스트의 헬퍼를 바꾸고 새 테스트를 쓴다** — `run()` 이 `InMemorySaver` + `drive` 로 끝까지 돌리게 바꾸고(기존 테스트 본문은 수정하지 않는다), 신규: ① 첫 interrupt 에서 멈추고 LLM 미호출·`interruptId=t1:0:confirm_sections` ② 수정한 경계대로 제안 ③ 무드가 interrupt 전에 채워짐 ④ 음원 실패해도 진행. `test_checkpointer.py` 의 첫 테스트도 `drive` 로 바꾼다

_(코드 본문 생략 — 상세본 Task 4 Step 6 참고)_

- [ ] **Step 7: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_graph.py -q`
Expected: FAIL — `fetch=`/`confirm_sections` 가 없고 interrupt 가 없어 `KeyError: '__interrupt__'`

- [ ] **Step 8: 구현한다 — 상태, 노드, 그래프**(`confirm_sections_node` 는 interrupt 앞 부수효과 없음, `mood` 는 `audioUrl` 없음·내려받기 실패 시 건너뜀)

_(코드 본문 생략 — 저장소의 `src/stage_director/graph_nodes.py`, `graph.py` 참고)_

- [ ] **Step 9: 깨진 `/sequence` 테스트를 정리한다** — `tests/test_api.py` 의 `/sequence` 테스트 4개 삭제(`sequence_app`·`SEQUENCE_GOOD`·체크포인터 fail-fast 테스트는 남김)

- [ ] **Step 10: 통과하는 것을 확인한다**

Run: `uv run pytest -q`
Expected: `238 passed, 15 deselected`

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 11: 커밋한다**

```bash
git add src tests
git commit -m "feat: add mood node and interrupt #1 for section confirmation"
```

---

### Task 5: `propose_section` 입력 확장 — 무드, 피드백, 이전 제안

**Files:**
- Modify: `src/stage_director/models.py`, `prompts.py`, `propose.py`
- Test: `tests/test_models.py`, `tests/test_propose.py`, `tests/test_graph.py`

**Interfaces:**
- Produces: `ProposeRequest.feedback`·`previous`(기본값 `None`, 기존 호출 불변), 프롬프트의 `분위기:` 줄과 `## 사용자 피드백 (수정 요청)` 블록, "분위기·피드백은 지시가 아닌 데이터" 시스템 프롬프트

- [ ] **Step 1: 실패하는 테스트를 쓴다** — 모델이 `feedback`·`previous` 를 받음, 무드가 있을 때만 프롬프트에 줄이 생김, 피드백 블록에 이전 제안·요청이 들어가고 기본값에서는 없음, 시스템 프롬프트에 데이터 취급 문구, 그래프에서 사용자가 고친 무드가 propose 프롬프트에 도달

_(코드 본문 생략 — 상세본 Task 5 Step 1 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_models.py tests/test_propose.py tests/test_graph.py -q`
Expected: FAIL — `ProposeRequest` 가 `feedback`/`previous` 를 모르고 프롬프트에 줄이 없다

- [ ] **Step 3: 구현한다**

_(코드 본문 생략 — 저장소의 `models.py`, `prompts.py`, `propose.py` 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest -q`
Expected: `245 passed, 15 deselected`

- [ ] **Step 5: 커밋한다**

```bash
git add src tests
git commit -m "feat: let propose_section take section mood and revision feedback"
```

---

### Task 6: interrupt #2 — `review` 노드와 피드백 부분 재생성

**Files:**
- Modify: `src/stage_director/graph_state.py`, `graph_nodes.py`, `graph.py`, `tests/test_graph.py`, `tests/test_checkpointer.py`

**Interfaces:**
- Produces: `GraphState.{feedback_log, feedback_targets, review_action}`, `ProposeTask.{feedback, previous}`, `_propose_task`(초기·자동·피드백 재생성의 단일 생성자), `review_node`, `decide_review`, review interrupt 값 `{interruptId, kind:"review", items, issues}`, resume 값 `{"action":"approve"}` / `{"action":"feedback","text","targets"}`. `assemble_node` 는 피드백 턴에서 자동 재생성 대상을 `feedback_targets` 로 제한하고, 피드백 턴마다 `regen_round` 가 0 으로 리셋된다

- [ ] **Step 1: 실패하는 테스트를 쓴다** — ① review 에서 `items`·`issues`(`interruptId=t1:1:review`)와 함께 멈춤 ② 승인하면 끝(`next == ()`) ③ **피드백은 targets 만 재생성, 나머지 `proposals` 는 바이트 동일** ④ 피드백 문구·이전 제안이 프롬프트에 도달 ⑤ interrupt id 가 `t1:0:confirm_sections` → `t1:1:review` → `t1:2:review` ⑥ **게이트가 targets 밖 구간을 문제 삼아도 재생성하지 않고 남은 위반이 `issues` 에 보임**. Postgres: 새 연결에서 대기 중 interrupt 를 읽고 재개(스펙 §9 재시작 후 재개)

_(코드 본문 생략 — 상세본 Task 6 Step 1 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_graph.py -q`
Expected: FAIL — `review` interrupt 가 없어 `KeyError: '__interrupt__'`

- [ ] **Step 3: 구현한다 — 상태**

_(코드 본문 생략 — 저장소의 `src/stage_director/graph_state.py` 참고)_

- [ ] **Step 4: 구현한다 — 노드와 라우팅**(`decide_regen` 은 `END` 대신 `"review"`, `review` → `decide_review` → `propose`/`END`)

_(코드 본문 생략 — 저장소의 `graph_nodes.py`, `graph.py` 참고)_

- [ ] **Step 5: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_graph.py -q`
Expected: 모두 통과(21개)

Run: `uv run pytest -q`
Expected: `251 passed, 16 deselected`

- [ ] **Step 6: Postgres 재시작 재개 테스트를 돌린다**

```bash
docker compose up -d checkpointer-db && sleep 3
uv run pytest tests/test_checkpointer.py -m postgres -q
docker compose down
```

Expected: `3 passed`

- [ ] **Step 7: 커밋한다**

```bash
git add src tests
git commit -m "feat: add interrupt #2 review with targeted feedback regeneration"
```

---

### Task 7: `Runner` 와 `/runs` 엔드포인트 — `/sequence` 교체

**Files:**
- Create: `src/stage_director/runner.py`
- Modify: `src/stage_director/api.py`, `models.py`(`SequenceResponse` 삭제), `tests/conftest.py`, `tests/test_models.py`, `tests/test_api.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Produces: `Runner(graph, jobs, executor)` — `start`(멱등·`error` 면 재개)·`status`·`resume`; `RunNotFound`·`RunConflict(code)`·`InvalidResume(code, message)`; `create_app(settings, llm, checkpointer_cm, job_store, executor)`; `POST /runs`(202)·`GET /runs/{id}`·`POST /runs/{id}/resume`(202/404/409/422). `/sequence` 삭제

- [ ] **Step 1: 테스트 인프라를 만든다** — `tests/conftest.py` 에 `InlineExecutor`(바로 실행)·`DeferredExecutor`(`run_all()` 때만 실행, 더블 클릭·프로세스 사망 시나리오용)

- [ ] **Step 2: 실패하는 테스트를 쓴다 — `Runner`**(`tests/test_runner.py`): 시작이 첫 interrupt 까지 감, 시작의 멱등(LLM 재호출 없음), 모르는 스레드, 전체 흐름, 피드백 턴 후 새 `interruptId`, 낡은 id 는 409 이고 interrupt 유지, 종료 후 resume 은 `not_waiting_input`, **더블 클릭은 그래프를 정확히 한 번만 돌림**, 잘못된 구간은 422 이고 interrupt 유지, 범위 밖 targets, kind 불일치, **LLM 실패 → `error` → 다시 시도하면 마지막 체크포인트에서 이어짐(LLM 4회 호출)**, 예기치 않은 에러는 상세를 노출하지 않음(`internal_error`), 시작 전에 죽은 작업은 `interrupted` 로 복구 후 재시작

_(코드 본문 생략 — 상세본 Task 7 Step 2 참고)_

- [ ] **Step 3: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_runner.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.runner'`

- [ ] **Step 4: 구현한다 — `Runner`**(`start` 의 `create`/`error→running` 분기, `_run` 의 상태 전이, `_resume_value` 의 검증 순서: 404 → 409 not_waiting → 409 stale → 409 kind → 422 payload → **그 뒤에야** `waiting_input→running` 전이)

_(코드 본문 생략 — 저장소의 `src/stage_director/runner.py` 참고)_

- [ ] **Step 5: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_runner.py -q`
Expected: `14 passed`

- [ ] **Step 6: 실패하는 테스트를 쓴다 — HTTP**(`tests/test_api.py`: `sequence_app` 을 `runs_app` 으로 교체, 세 엔드포인트의 인증 401, 202 와 첫 interrupt, 시작의 멱등, 404 `thread_not_found`, HTTP 전체 흐름(결과가 `validate_sequence` 통과), 낡은 resume 409, 잘못된 구간 422 + 대기 유지, 알 수 없는 kind 422, 잘못된 본문 422)

_(코드 본문 생략 — 상세본 Task 7 Step 6 참고)_

- [ ] **Step 7: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_api.py -q`
Expected: FAIL — `create_app() got an unexpected keyword argument 'job_store'`

- [ ] **Step 8: 구현한다 — API**(예외 → 404/409/422 핸들러, `lifespan` 에서 `fail_running` 후 `Runner` 생성, 기본 `ThreadPoolExecutor(MAX_CONCURRENT_RUNS)`, `SequenceResponse`·`/sequence` 삭제)

_(코드 본문 생략 — 저장소의 `src/stage_director/api.py` 참고)_

- [ ] **Step 9: 통과하는 것을 확인한다**

Run: `uv run pytest -q`
Expected: `275 passed, 16 deselected`

Run: `uv run ruff check .`
Expected: `All checks passed!`

```bash
grep -rn "SequenceResponse\|/sequence\b" src tests || echo "no leftovers"
```

Expected: `no leftovers`

- [ ] **Step 10: 커밋한다**

```bash
git add src tests
git commit -m "feat: replace provisional /sequence with the jobs-backed /runs protocol"
```

---

### Task 8: 체크포인트 보존 정책 (§6.4)

**Files:**
- Create: `src/stage_director/retention.py`
- Modify: `src/stage_director/api.py`(`lifespan` 에서 `purge` 호출)
- Test: `tests/test_retention.py`

**Interfaces:**
- Produces: `DONE_CHECKPOINT_TTL`, `DRAFT_TTL`, `purge(jobs, saver, now=None) -> (규칙 1 삭제 수, 규칙 2 삭제 수)`, `main()`(`python -m stage_director.retention`, `DATABASE_URL` 필요)

- [ ] **Step 1: 실패하는 테스트를 쓴다** — 승인 24시간 뒤 체크포인트만 사라지고 `jobs.result` 는 남음(23시간에는 유지), 7일 방치된 `waiting_input` 은 체크포인트와 행이 모두 사라짐(6일에는 유지), `running`·`error` 도 7일 규칙, 반복 실행 안전, **서비스 시작 시 방치 작업 삭제가 `fail_running` 보다 먼저 일어남**, Postgres 에서 `updated_at` 기준 삭제(`postgres` 마커)

_(코드 본문 생략 — 상세본 Task 8 Step 1 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_retention.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.retention'`

- [ ] **Step 3: 구현한다** — `purge`, CLI `main`, `lifespan` 에서 `purge` → `fail_running` 순서

_(코드 본문 생략 — 저장소의 `src/stage_director/retention.py` 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_retention.py -q`
Expected: `6 passed, 1 deselected`

```bash
docker compose up -d checkpointer-db && sleep 3
uv run pytest tests/test_retention.py -m postgres -q
docker compose down
```

Expected: `1 passed`

Run: `uv run pytest -q`
Expected: `281 passed, 17 deselected`

- [ ] **Step 5: 커밋한다**

```bash
git add src tests
git commit -m "feat: add checkpoint retention policy and run it at startup"
```

---

### Task 9: 통합 검증 · 스펙 갱신 · 실제 Gemini 오디오 + 실제 Neon

**Files:**
- Modify: `docs/superpowers/specs/stage-director-agent-design.md`(§4.2, §6.1, §6.3, §13)

**Interfaces:**
- Consumes: 이 계획의 모든 산출물
- Produces: 없음(검증·문서 작업)

- [ ] **Step 1: 전체 테스트를 돌린다**

```bash
uv run pytest -q
```

Expected: `281 passed, 17 deselected`

```bash
docker compose up -d checkpointer-db && sleep 3
uv run pytest -m postgres -q
docker compose down
```

Expected: `12 passed` (체크포인터 3 + jobs 8 + retention 1)

```bash
uv run ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 2: 실제 프로세스로 재시작·재개를 확인한다** — docker Postgres + 가짜 Gemini 키로 서버를 띄워 `POST /runs` → 서버 종료 → 재기동 후 `GET` 이 같은 `interruptId` 의 `waiting_input` 을 돌려주는지, 낡은 id 409·빈틈 구간 422·올바른 resume 202, 가짜 키라 `error(llm_failed)` 가 된 뒤 같은 `threadId` 로 `POST /runs`(다시 시도)가 202 인지 확인

_(명령 본문 생략 — 상세본 Task 9 Step 2 참고)_

- [ ] **Step 3: 스펙 §4.2 를 갱신한다** — 잠정 `POST /sequence` 문단을 Python `/runs` 프로토콜 표(엔드포인트별 동작·409/410(404)/422 코드)와 `context.audioUrl` 설명으로 교체

- [ ] **Step 4: 스펙 §6.1, §6.3 을 갱신한다** — §6.1 상태 판정을 "Runner 가 전이 시점에 조건부 UPDATE 로 `jobs.status` 기록, interrupt 는 체크포인트 `tasks` 에서 읽음, `progress` 컬럼 없음"으로, §6.3 interrupt·resume 페이로드를 새 모양(`feedback` 의 `targets` 는 사용자 지정, 피드백 턴의 자동 재생성 제한·예산 리셋 포함)으로

- [ ] **Step 5: 스펙 §13 의 무드 행을 확정한다** — 곡 전체 오디오 + 구간 시각 목록 1회 호출, 비치명적 실패, `Section.mood` 로 propose 프롬프트 반영

- [ ] **Step 6: 커밋한다**

```bash
git add docs/superpowers/specs/stage-director-agent-design.md
git commit -m "docs: record the /runs protocol, resume payloads and mood decision in the spec"
```

- [ ] **Step 7: (사람이 하는 단계) 실제 Gemini 오디오 무드 해석을 들어보며 확인한다**

에이전트가 들을 수 없는 단계다. `demo-tracks/` 의 두 곡에 `detect_sections` → `interpret_moods`(실제 `GeminiClient`)를 돌려 구간별 무드가 들리는 분위기와 맞는지 메모한다(`docs/superpowers/notes/mood-spike.md`): 구간 시각에 맞춘 해석인가, 구간 간 차이가 실제와 맞는가, 전부 빈 문자열이면 파일 크기 상한·모델의 오디오 거절을 의심. 프롬프트를 고쳤다면 수정 커밋을 하나 만든다.

- [ ] **Step 8: (사람이 하는 단계) 실제 Gemini + 실제 Neon 으로 `/runs` 전 과정을 확인한다**

에이전트가 할 수 없는 단계다. `.env` 에 Neon 연결 문자열을 채우고 `uv run --env-file .env pytest -m postgres -q`(Expected: `12 passed`, `jobs` 테이블 생성 확인) 후 서버를 띄워 눈으로 확인한다: ① `confirm_sections` 의 구간·`energyCurve`·`mood` ② 경계 수정 resume 후 `review` 의 `items`·`issues` ③ 한 구간 피드백 시 그 구간만 바뀌는지(JSON diff) ④ `approve` 후 `done` 과 연속 구간 ⑤ Neon 의 `jobs`·`checkpoints` 행 ⑥ 서버 재기동 후 `waiting_input` 작업의 `GET` 이 같은 `interruptId`. 결과(통과 여부, Neon 연결 이슈, 무드 품질 메모)는 PR 설명에 적는다.

_(명령 본문 생략 — 상세본 Task 9 Step 8 참고)_

---

## 스펙 대응

| 스펙 | 이 계획의 태스크 |
| --- | --- |
| §6.3 interrupt #1 페이로드와 resume `{sections}` 검증 | Task 4, 7 |
| §6.3 interrupt #2 페이로드, resume approve/feedback(`targets` 는 사용자 지정으로 스펙 수정) | Task 6, 7, 9 |
| §6.3 `revision`, `interruptId`, 낡은 resume 거부, `feedback_log`, 부분 재생성 불변식 | Task 6, 7 |
| §6.3 `sections.mood` (오디오 입력 무드 해석) | Task 1, 3, 4, 5 |
| §4.2 `POST /runs`(멱등), `GET /runs/{id}`, `POST /runs/{id}/resume`(409), 404→Next.js 410 | Task 7 |
| §6.1 `jobs` 테이블, 기동 시 `running` → `error(interrupted)`, "다시 시도" = 마지막 체크포인트 재개 | Task 2, 7 |
| §6.4 보존 정책 규칙 1·2, 서비스 시작 시 + 주기 스크립트 | Task 8 |
| §7 2층 "위반 구간은 최대 2회 자동 재생성 후 경고로 interrupt #2 에 노출" | Task 6 |
| §8 노드 단위 실패 → `jobs.status=error`, 낡은 resume·더블 클릭 409 | Task 7 |
| §9 interrupt/재개 통합 테스트(프로세스 재시작 후 재개), 프로토콜 테스트 | Task 6, 9 |
| §13 오디오 입력 무드 해석 확정 | Task 3, 9 |
| 이 계획 밖 (5단계) | 분석 작업(`kind=analysis`, `progress`), 시드 곡 분석, 배포, Python 쪽 RPM 토큰 버킷 |

## 알려진 한계와 5단계로 넘기는 것

| 한계 | 넘기는 곳 |
| --- | --- |
| 프로세스 하나를 가정한다. 인스턴스가 여러 개면 한 인스턴스가 죽었을 때 다른 인스턴스가 그 `running` 작업을 알아채지 못한다(`jobs` 잠금은 중복 시작만 막는다) | 5단계(heartbeat 또는 별도 워커) |
| `MAX_CONCURRENT_RUNS=2` 스레드 풀이 가득 차면 새 작업은 대기열에서 `running` 으로 보인다. 분당 요청 수(RPM) 기반 속도 제한·백오프는 여전히 없다(3단계에서 이월) | 5단계 |
| 피드백 턴 횟수 상한이 없다. 공유 데모 계정의 남용은 Next.js 레이트 리밋(스펙 §8)에 맡긴다 | on-stage 쪽 |
| interrupt #1 에서 사람이 경계를 바꿔도 무드를 다시 해석하지 않는다. 경계가 바뀐 구간의 무드는 사람이 고친다 | 필요성이 확인되면 별도 작업 |
| `done` 작업의 `jobs` 행과 `result` 는 영구 보관된다(체크포인트만 24시간 뒤 삭제) | 5단계 |
| `fetch_audio` 는 리다이렉트를 따라가며 SSRF 를 직접 막지 않는다(Next.js 가 만든 서명 URL 만 온다는 신뢰에 의존) | 5단계(배포 시 재검토) |
| 무드 해석 정확도는 사람 청취로만 검증한다(자동 평가 세트 없음) | 필요하면 `llm` 마커 평가 추가 |
| `jobs` 의 `progress`, 분석 작업(`kind=analysis`), 그 결과 7일 보관은 만들지 않았다 | 5단계 |
