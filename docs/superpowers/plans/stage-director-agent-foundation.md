# 무대 연출 디렉터 에이전트 — 기반 구현 계획 (contracts · 결정적 코어 · 수치 분석)

> 이 문서는 태스크 순서, 검증 절차, 명령, 기대 결과를 담는다. 코드·테스트·문서의 본문은 싣지 않고, 저장소의 해당 파일이 원본이다.

**Goal:** on-stage 계약을 `contracts/`로 추출하고, 그 위에 LLM·그래프·서버 없이 테스트 가능한 결정적 코어(StageState 검증, 시퀀스 불변식, 검증 게이트 규칙, 부분 재생성 병합, 음원 수치 분석)를 만든다.

**Architecture:** 이 계획은 스펙의 MVP 1~2단계 기반 작업이다. `contracts/`(on-stage 계약의 파이썬 포트와 문서)와 `src/stage_director/`(우리 코드)를 분리한다. 우리 코드는 `contracts/`만 보고 작성하며 LangGraph·FastAPI·LLM에 의존하지 않는 순수 함수로 둔다. 이후 계획(그래프, 프로토콜)이 이 함수들을 노드와 리듀서에 그대로 꽂는다.

**Tech Stack:** Python 3.12, uv, pydantic 2, pytest, librosa 1.0 + numpy + soundfile, hatchling(editable 설치)

**Spec:** [docs/superpowers/specs/stage-director-agent-design.md](../specs/stage-director-agent-design.md) — 실행자는 이 계획과 스펙을 함께 읽는다.

## 이 계획의 범위

| 포함 | 스펙 위치 |
| --- | --- |
| Task 1: `contracts/` 추출 + 프로젝트 골격 | §10, §12 제약 1~3 |
| Task 2: 시퀀스 항목 모델과 불변식 | §5 |
| Task 3: 검증 게이트 규칙(1층 후처리 + 2층) | §7 |
| Task 4: 부분 재생성용 제안 병합 | §6.3 |
| Task 5: 음원 수치 측정 + 분석 결과 파서 + 스파이크용 CLI | §5, §6.3, §9(분석 테스트), 기획서 §5 1층 |

**이 계획에 없는 것과 이유.** 스펙 §13 이 "1단계 스파이크 결과로 확정"한다고 둔 항목(LLM provider, 구간 구조 분석 모델, 무드 해석 방식)이 뒤 단계의 설계를 바꾼다. 그래서 스파이크 이전에 계획할 수 있는 것까지만 담았다. 아래는 별도 계획으로 이어 쓴다.

| 후속 계획 | 내용 | 스펙 위치 |
| --- | --- | --- |
| 2. 싱글 제안 | LLM 클라이언트 인터페이스, 구간 하나 연출 노드(가짜 LLM 테스트), FastAPI 골격, `X-Internal-Key` | §3, §7, §9 |
| 3. 시퀀스 그래프 | LangGraph 상태와 리듀서 연결, 무드 해석·구간별 생성(Send)·검증 노드, `jobs` | §6.3 |
| 4. 사람 개입 | 전용 Postgres 체크포인터, interrupt #1·#2, 실행·재개 프로토콜(409/410/멱등) | §4, §6 |
| 5. 업로드 분석·배포 | 분석 작업 엔드포인트, 보존 정책 스크립트, 배포 | §4.1, §6.4 |

## Global Constraints

모든 태스크의 요구사항에 아래가 암묵적으로 포함된다. 값은 스펙에서 그대로 옮겼다.

- Python 서비스는 Supabase에 접근하지 않는다. 필요한 컨텍스트는 요청 본문으로 받는다 (§3). 그래서 `supabase` 패키지를 의존성에 넣지 않는다.
- **Task 1 이후에는 `../on-stage`를 열지 않는다** (§12 제약 2). 계약에 없는 정보가 필요하면 구현을 멈추고 "계약 갱신" 태스크를 따로 만든다. 그 태스크만 on-stage 를 읽는다.
- 결정적 로직(모델·merge·불변식·리듀서·상태 판정)은 **TDD로 처음부터** 작성한다: 실패하는 테스트 → 실패 확인 → 최소 구현 → 통과 확인 (§12 제약 5). R3F 등 시각 레이어는 이 저장소 범위 밖이다.
- StageState 범위: `intensity` 0–1000, `angle` 0.1–1.0, `penumbra` 0–1, `smoke.density` 0–1. `color`·`smoke.color` 는 `^#[0-9a-fA-F]{6}$`. 범위 밖 숫자는 **clamp 하고 이슈를 기록**하며 거부하지 않는다. `camera` 가 enum(`front`/`audience`/`top`) 밖이면 기본값 (§7 1층).
- `penumbra` 는 모델에 유지하고(기본 0.6) 에이전트는 값을 바꾸지 않는다 (§2).
- 시퀀스 항목의 JSON 키는 camelCase: `sectionLabel`, `startSec`, `endSec`, `transitionMs`, `state`, `rationale` (§5).
- `items` 불변식 (§5): 배열 순서 = 시간 순서, `startSec < endSec` / 첫 항목 `startSec = 0`, 마지막 `endSec = duration_sec`, `items[i].endSec == items[i+1].startSec` / `0 <= transitionMs <= (endSec - startSec) * 1000`.
- 그래프 상태의 분석 스냅샷 에너지 곡선은 1점/초 이하로 다운샘플한다 (§6.3).
- 부분 재생성 불변식 (§6.3): 피드백 라우팅이 지정한 `targets` 밖의 구간은 바이트 단위로 같아야 한다.

## 이 계획이 정한 값 (스펙이 정하지 않은 것)

스펙이 수치를 정하지 않아 이 계획이 초기값을 정했다. 1단계 스파이크에서 실제 곡을 보고 조정한다.

| 값 | 초기값 | 위치 |
| --- | --- | --- |
| "잔잔한 구간" 기준 (구간 평균 에너지 / 곡 평균 에너지 이하) | `CALM_ENERGY_RATIO = 0.9` (처음 0.7로 정했으나 실제 곡 2개 측정 후 0.9로 조정. 잔잔한 구간 0.08~0.83, 일반 구간 1.05~1.37) | `gate.py` |
| 잔잔한 구간 밝기 상한 | `CALM_MAX_INTENSITY = 500` (스펙 §7 "잔잔한 구간 밝기 ≤ 500") | `gate.py` |
| 에너지-밝기 방향 규칙이 적용되는 최소 에너지 비 변화 | `DIRECTION_EPS = 0.15` | `gate.py` |
| 구간 밝기의 정의 | 켜진 스팟 중 최대 `intensity`(전부 꺼졌으면 0) | `gate.py` |
| 구간 경계 허용 오차 | `TOLERANCE_SEC = 1e-3` | `sequence.py` |
| 분석 JSON 키 | camelCase (`durationSec`, `beatsSec`, `energyCurve`, `onsetDensity`) | `snapshot.py` |
| 에너지 곡선 / 온셋 밀도의 해상도 | 1초 구간. 인덱스 i = [i, i+1)초 | `measure.py` |
| 패키징 | hatchling editable 설치(`contracts`, `stage_director` 두 패키지) | `pyproject.toml` |

## 파일 구조

| 파일 | 책임 | 태스크 |
| --- | --- | --- |
| `pyproject.toml`, `.python-version`, `uv.lock` | uv 프로젝트, 의존성, pytest 설정 | 1, 5 |
| `contracts/stage_state.py` | StageState 모델, 기본값, `merge_stage_state`([계약]), `clamp_stage_state`([우리 규칙]) | 1 |
| `contracts/fixtures/merge_stage_state_cases.json` | on-stage 동작의 골든 벡터 | 1 |
| `contracts/*.md` | 계약 문서 5개(README, 프리셋 API, enum·상수, 아티스트 컨텍스트, Supabase) | 1 |
| `src/stage_director/sequence.py` | `SequenceItem`, `validate_sequence` | 2 |
| `src/stage_director/gate.py` | 게이트 규칙, `sanitize_state` | 3 |
| `src/stage_director/proposals.py` | `merge_proposals`, `apply_regeneration` | 4 |
| `src/stage_director/analysis/snapshot.py` | `AnalysisSnapshot`, `parse_analysis`, `section_energy_ratio` | 5 |
| `src/stage_director/analysis/measure.py` | librosa 측정 | 5 |
| `src/stage_director/analysis/cli.py` | 스파이크용 CLI | 5 |
| `tests/contracts/`, `tests/`, `tests/analysis/` | 위 파일 각각의 테스트 | 1~5 |

---

### Task 1: 프로젝트 골격과 `contracts/` 추출

이 태스크가 `../on-stage`를 여는 **유일한** 구현 태스크다. 골격은 이 태스크의 테스트를 돌리는 데 필요해서 여기에 포함한다.

**Files:**
- Create: `pyproject.toml`, `.python-version`, `contracts/__init__.py`, `src/stage_director/__init__.py`
- Create: `contracts/stage_state.py`, `contracts/fixtures/merge_stage_state_cases.json`
- Create: `contracts/README.md`, `contracts/api_stage_presets.md`, `contracts/enums_and_constants.md`, `contracts/artist_context.md`, `contracts/supabase_required.md`
- Test: `tests/contracts/test_stage_state.py`
- Modify(사용자 승인 시): `.claude/settings.json`

**Interfaces:**
- Consumes: 없음 (`../on-stage` 원본만)
- Produces (`contracts/stage_state.py`):
  - `Camera = Literal["front", "audience", "top"]`, `CAMERAS: tuple[str, ...]`
  - `SpotState(on: bool, intensity: float, angle: float, penumbra: float)`, `Spots(left, center, right: SpotState)`, `Smoke(density: float, color: str)`, `StageState(color: str, spots: Spots, camera: Camera, smoke: Smoke)` — 모두 pydantic `BaseModel`
  - `default_stage_state(color: str) -> StageState`
  - `merge_stage_state(value: Any, fallback: StageState) -> StageState` — [계약]. 항상 새 객체를 돌려준다
  - `INTENSITY_RANGE`, `ANGLE_RANGE`, `PENUMBRA_RANGE`, `DENSITY_RANGE: tuple[float, float]`, `HEX_COLOR: re.Pattern`
  - `ClampNote(path: str, original: Any, applied: Any)` (NamedTuple)
  - `clamp_stage_state(state: StageState, fallback: StageState) -> tuple[StageState, list[ClampNote]]` — [우리 규칙]

- [ ] **Step 1: 프로젝트 골격을 만든다**

`pyproject.toml`:

_(코드 본문 생략 — 저장소의 `pyproject.toml` 참고)_

`.python-version`:

```
3.12
```

빈 패키지 파일과 디렉터리를 만든다.

```bash
mkdir -p contracts/fixtures src/stage_director tests/contracts
touch contracts/__init__.py src/stage_director/__init__.py
uv sync
uv run pytest -q
```

Expected: `uv sync` 가 `stage-director-agent==0.1.0` 을 editable 로 설치한다. `pytest` 는 `no tests ran` 을 출력하고 종료 코드 5 로 끝난다(테스트가 아직 없으므로 정상).

- [ ] **Step 2: 골격을 커밋한다**

```bash
git add pyproject.toml .python-version uv.lock contracts/__init__.py src/stage_director/__init__.py
git commit -m "chore: set up uv project skeleton"
```

- [ ] **Step 3: 골든 벡터와 실패하는 테스트를 쓴다**

`contracts/fixtures/merge_stage_state_cases.json` (on-stage `stageState.test.ts` 의 케이스와 `stageState.ts` 의 코드 동작을 옮긴 것. 케이스 6~8 은 null·문자열·배열 입력이고, `"expected": "FALLBACK"` 은 "fallback 과 같은 값"을 뜻한다):

_(코드 본문 생략 — 저장소의 `contracts/fixtures/merge_stage_state_cases.json` 참고)_

`tests/contracts/test_stage_state.py`:

_(코드 본문 생략 — 저장소의 `tests/contracts/test_stage_state.py` 참고)_

- [ ] **Step 4: 테스트가 실패하는 것을 확인한다**

Run: `uv run pytest tests/contracts -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'contracts.stage_state'` (`1 error during collection`)

- [ ] **Step 5: `contracts/stage_state.py` 를 구현한다**

_(코드 본문 생략 — 저장소의 `contracts/stage_state.py` 참고)_

- [ ] **Step 6: 테스트가 통과하는 것을 확인한다**

Run: `uv run pytest tests/contracts -q`
Expected: `18 passed`

- [ ] **Step 7: on-stage 원본과 대조한다 (`../on-stage`를 여는 유일한 단계)**

아래는 모두 읽기 전용 명령이다. 이 단계에서 `../on-stage` 의 어떤 파일도 수정하지 않는다. `git -C ../on-stage` 는 `.claude/settings.json` 이 막고 있으므로 쓰지 않는다.

(a) 커밋 해시. 아래 명령으로 읽은 값이 Step 8 README 의 브랜치·커밋 행과 다르면 README 를 읽은 값으로 고치고, (b)~(g) 로 계약이 그대로인지 특히 꼼꼼히 확인한다.

```bash
cat ../on-stage/.git/HEAD
cat ../on-stage/.git/refs/heads/feat/responsive-design
```

Expected: `ref: refs/heads/feat/responsive-design` / `dc14d342d656812064a425c2a984ac821970e79c`

(b) `mergeStageState` 에 clamp 가 없다는 사실

```bash
grep -c -E "Math\.(min|max)|clamp" ../on-stage/src/lib/stageState.ts
```

Expected: `0`

(c) 프리셋 라우트의 상태 코드 (`api_stage_presets.md` 와 같아야 한다)

```bash
cd ../on-stage
grep -o -h -E "status: [0-9]+|error\.code === \"[0-9]+\"|new Response\(null, \{ status: [0-9]+" src/app/api/stage-presets/route.ts "src/app/api/stage-presets/[id]/route.ts" src/lib/routeHelpers.ts | sort | uniq -c
cd - > /dev/null
```

Expected:

```
   1 error.code === "23505"
   1 new Response(null, { status: 204
   2 status: 201
   2 status: 400
   2 status: 401
   1 status: 403
   2 status: 404
   1 status: 409
   4 status: 500
```

(d) 서명 URL 업로드 라우트의 상태 코드 (`supabase_required.md` 1부, 200 은 기본 응답)

```bash
grep -o -E "status: [0-9]+" ../on-stage/src/app/api/gallery/upload-url/route.ts | sort | uniq -c
```

Expected: `status: 400`, `401`, `413`, `415`, `500` 이 각 1회

(e) 아티스트 시드 slug·컬러 (`enums_and_constants.md`)

```bash
grep -E '^\s+"(slug|color)"' ../on-stage/src/data/artists.json | tr -s ' ' | paste -sd' ' -
```

Expected: `"slug": "aurora", "color": "#9F77DD", "slug": "velvet", "color": "#7F77DD", "slug": "nova", "color": "#D4537E", "slug": "halo", "color": "#BA7517", "slug": "lumen", "color": "#1D9E75", "slug": "echo", "color": "#378ADD",`

(f) `stage_presets` 테이블 (`supabase_required.md` 1부)

```bash
grep -n "create table stage_presets" -A 8 ../on-stage/supabase/migrations/*_init.sql
```

Expected: `id`, `user_id`(기본값 `auth.uid()`), `artist_id`(cascade), `name`, `state jsonb not null`, `created_at`, `unique (user_id, artist_id, name)` 8줄

(g) 아티스트 API (`artist_context.md`)

```bash
grep -o -E "status: [0-9]+|error: \"[a-z ]+\"" ../on-stage/src/app/api/artists/route.ts | sort | uniq -c
```

Expected: `error: "not found"` 1회, `status: 404` 1회

기대값과 다르면 **커밋하지 말고** 어느 계약 파일이 틀렸는지 고친 뒤 Step 6 을 다시 돌린다.

- [ ] **Step 8: 계약 문서 5개를 쓴다**

`contracts/README.md`:

_(코드 본문 생략 — 저장소의 `contracts/README.md` 참고)_

`contracts/api_stage_presets.md`:

_(코드 본문 생략 — 저장소의 `contracts/api_stage_presets.md` 참고)_

`contracts/enums_and_constants.md`:

_(코드 본문 생략 — 저장소의 `contracts/enums_and_constants.md` 참고)_

`contracts/artist_context.md`:

_(코드 본문 생략 — 저장소의 `contracts/artist_context.md` 참고)_

`contracts/supabase_required.md`:

_(코드 본문 생략 — 저장소의 `contracts/supabase_required.md` 참고)_

- [ ] **Step 9: 전체 검증 후 커밋한다**

```bash
uv run pytest -q
ls contracts contracts/fixtures
```

Expected: `18 passed`. `contracts` 에 `README.md`, `__init__.py`, `api_stage_presets.md`, `artist_context.md`, `enums_and_constants.md`, `fixtures`, `stage_state.py`, `supabase_required.md` 가 있고, `fixtures` 에 `merge_stage_state_cases.json` 이 있다.

```bash
git add contracts tests/contracts
git commit -m "feat: extract on-stage contracts into contracts/"
```

- [ ] **Step 10: 사용자에게 물어본 뒤에만: `../on-stage` 읽기를 막는다**

스펙 §12 제약 3 의 권장 사항이다. `.claude/settings.json` 은 사용자의 도구 권한 설정이므로 **사용자가 승인하기 전에는 고치지 않는다.** 승인하면 `deny` 배열 끝에 `"Read(../on-stage/**)"` 한 줄을 추가한다.

- 이 규칙은 Read 도구를 막는다. 셸 명령(`cat` 등)까지 막는 것은 아니므로 완전한 강제는 아니다. 규칙 자체는 `contracts/README.md` 에 있다.
- "계약 갱신" 태스크를 할 때는 이 한 줄을 지웠다가 태스크가 끝나면 다시 넣는다.

```bash
git add .claude/settings.json
git commit -m "chore: block reads of ../on-stage after contract extraction"
```

---

### Task 2: 시퀀스 항목 모델과 불변식

**Files:**
- Create: `src/stage_director/sequence.py`
- Test: `tests/test_sequence.py`

**Interfaces:**
- Consumes: `contracts.stage_state.StageState`
- Produces (`stage_director.sequence`):
  - `TOLERANCE_SEC: float = 1e-3`
  - `SequenceItem(section_label: str, start_sec: float, end_sec: float, transition_ms: int, state: StageState, rationale: str)` — pydantic. JSON 별칭은 camelCase (`sectionLabel`, `startSec`, `endSec`, `transitionMs`). `populate_by_name=True` 라서 파이썬 이름으로도 만들 수 있다. 직렬화는 `model_dump_json(by_alias=True)`
  - `Violation(idx: int | None, code: str, message: str)` (NamedTuple). `code` 는 `empty`, `non_positive_length`, `transition_out_of_range`, `gap_or_overlap`, `start_not_zero`, `end_not_duration`
  - `validate_sequence(items: list[SequenceItem], duration_sec: float) -> list[Violation]` — 위반이 없으면 `[]`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_sequence.py`:

_(코드 본문 생략 — 저장소의 `tests/test_sequence.py` 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_sequence.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.sequence'`

- [ ] **Step 3: 구현한다**

`src/stage_director/sequence.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/sequence.py` 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_sequence.py -q`
Expected: `13 passed`

- [ ] **Step 5: 커밋한다**

```bash
git add src/stage_director/sequence.py tests/test_sequence.py
git commit -m "feat: add SequenceItem model and sequence invariants"
```

---

### Task 3: 검증 게이트 규칙

**Files:**
- Create: `src/stage_director/gate.py`
- Test: `tests/test_gate.py`

**Interfaces:**
- Consumes: `contracts.stage_state.{StageState, merge_stage_state, clamp_stage_state}`, `stage_director.sequence.SequenceItem`
- Produces (`stage_director.gate`):
  - 상수 `CALM_ENERGY_RATIO = 0.9`, `CALM_MAX_INTENSITY = 500.0`, `DIRECTION_EPS = 0.15`
  - `GateIssue(idx: int, rule: str, message: str)` (NamedTuple). `rule` 은 `clamped`, `calm_too_bright`, `color_deviation_without_rationale`, `energy_brightness_direction`
  - `brightness(state: StageState) -> float`
  - `sanitize_state(raw: Any, fallback: StageState, idx: int) -> tuple[StageState, list[GateIssue]]` — 병합 후 clamp. 바뀐 값마다 `clamped` 이슈
  - `run_gate(items: list[SequenceItem], energy_ratios: list[float], signature_color: str) -> list[GateIssue]` — `energy_ratios[i]` 는 `items[i]` 의 구간 평균 에너지 / 곡 평균 에너지. 길이가 다르면 `ValueError`
  - `indices_to_regenerate(issues: list[GateIssue]) -> set[int]` — `clamped` 를 제외한 이슈가 있는 구간

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_gate.py`:

_(코드 본문 생략 — 저장소의 `tests/test_gate.py` 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.gate'`

- [ ] **Step 3: 구현한다**

`src/stage_director/gate.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/gate.py` 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_gate.py -q`
Expected: `18 passed`

- [ ] **Step 5: 커밋한다**

```bash
git add src/stage_director/gate.py tests/test_gate.py
git commit -m "feat: add deterministic validation gate rules"
```

---

### Task 4: 부분 재생성용 제안 병합

**Files:**
- Create: `src/stage_director/proposals.py`
- Test: `tests/test_proposals.py`

**Interfaces:**
- Consumes: `stage_director.sequence.SequenceItem`
- Produces (`stage_director.proposals`):
  - `Proposals = dict[int, SequenceItem]` (키는 구간 idx)
  - `merge_proposals(left: Proposals | None, right: Proposals | None) -> Proposals` — LangGraph 리듀서 시그니처. 같은 idx 는 right 가 이긴다. 입력을 수정하지 않는다
  - `apply_regeneration(existing: Proposals, regenerated: Proposals, targets: set[int]) -> Proposals` — `targets` 만 교체한 새 dict. 나머지는 같은 객체. `regenerated` 의 키 집합이 `targets` 와 다르면 `ValueError`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_proposals.py`:

_(코드 본문 생략 — 저장소의 `tests/test_proposals.py` 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_proposals.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.proposals'`

- [ ] **Step 3: 구현한다**

`src/stage_director/proposals.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/proposals.py` 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_proposals.py -q`
Expected: `9 passed`

- [ ] **Step 5: 커밋한다**

```bash
git add src/stage_director/proposals.py tests/test_proposals.py
git commit -m "feat: add proposals reducer and partial regeneration"
```

---

### Task 5: 음원 수치 측정, 분석 결과 파서, 스파이크용 CLI

기획서 §5 의 1층(수치 측정)만 다룬다. 구간 구조 분석 모델과 무드 해석은 스파이크 결과에 따라 후속 계획에서 다룬다.

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (의존성 추가)
- Create: `src/stage_director/analysis/__init__.py`, `snapshot.py`, `measure.py`, `cli.py`
- Test: `tests/analysis/test_snapshot.py`, `tests/analysis/test_measure.py`

**Interfaces:**
- Consumes: 없음 (Task 2~4 와 독립)
- Produces:
  - `stage_director.analysis.snapshot`:
    - `AnalysisSnapshot(duration_sec: float = 0.0, bpm: float = 0.0, beats_sec: list[float], energy_curve: list[float], onset_density: list[float])` — pydantic. JSON 별칭 camelCase(`durationSec`, `bpm`, `beatsSec`, `energyCurve`, `onsetDensity`). `energy_curve`·`onset_density` 의 인덱스 i 는 [i, i+1)초 구간
    - `parse_analysis(raw: Any) -> AnalysisSnapshot` — 필드별 방어 파서. 예외를 던지지 않는다
    - `section_energy_ratio(energy_curve: list[float], start_sec: float, end_sec: float) -> float` — 구간 평균 / 곡 평균. Task 3 의 `run_gate(energy_ratios=...)` 입력을 만든다
  - `stage_director.analysis.measure`:
    - `SAMPLE_RATE = 22_050`, `HOP_LENGTH = 512`
    - `measure_audio(y: np.ndarray, sr: int) -> AnalysisSnapshot`
    - `measure_file(path: str | Path) -> AnalysisSnapshot`
  - `stage_director.analysis.cli.main(argv: list[str] | None = None) -> int` — 실행: `uv run python -m stage_director.analysis.cli <파일>`

- [ ] **Step 1: 의존성을 추가한다**

```bash
uv add numpy librosa soundfile
mkdir -p src/stage_director/analysis tests/analysis
touch src/stage_director/analysis/__init__.py
```

Expected: `pyproject.toml` 의 `dependencies` 에 `librosa`, `numpy`, `soundfile` 이 추가된다(작성 시점 검증 버전: librosa 1.0.0, numpy 2.5.3, soundfile 0.14.0).

```bash
git add pyproject.toml uv.lock src/stage_director/analysis/__init__.py
git commit -m "chore: add audio analysis dependencies"
```

- [ ] **Step 2: 분석 결과 파서의 실패하는 테스트를 쓴다**

`tests/analysis/test_snapshot.py`:

_(코드 본문 생략 — 저장소의 `tests/analysis/test_snapshot.py` 참고)_

- [ ] **Step 3: 실패하는 것을 확인한다**

Run: `uv run pytest tests/analysis/test_snapshot.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.analysis.snapshot'`

- [ ] **Step 4: `snapshot.py` 를 구현한다**

`src/stage_director/analysis/snapshot.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/analysis/snapshot.py` 참고)_

- [ ] **Step 5: 통과하는 것을 확인하고 커밋한다**

Run: `uv run pytest tests/analysis/test_snapshot.py -q`
Expected: `9 passed`

```bash
git add src/stage_director/analysis/snapshot.py tests/analysis/test_snapshot.py
git commit -m "feat: add analysis snapshot model and defensive parser"
```

- [ ] **Step 6: 측정과 CLI 의 실패하는 테스트를 쓴다**

합성 신호(클릭 트랙, 사인파)로 검증한다. 실제 Lyria 곡은 자동 테스트에 쓰지 않는다.

`tests/analysis/test_measure.py`:

_(코드 본문 생략 — 저장소의 `tests/analysis/test_measure.py` 참고)_

- [ ] **Step 7: 실패하는 것을 확인한다**

Run: `uv run pytest tests/analysis/test_measure.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.analysis.cli'` (collection 오류)

- [ ] **Step 8: `measure.py` 와 `cli.py` 를 구현한다**

`src/stage_director/analysis/measure.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/analysis/measure.py` 참고)_

`src/stage_director/analysis/cli.py`:

_(코드 본문 생략 — 저장소의 `src/stage_director/analysis/cli.py` 참고)_

- [ ] **Step 9: 통과하는 것을 확인한다**

Run: `uv run pytest tests/analysis/test_measure.py -q`
Expected: `9 passed`. 첫 실행은 librosa 의 JIT 컴파일로 30초 안팎 걸린다.

- [ ] **Step 10: 전체 테스트를 돌리고 커밋한다**

Run: `uv run pytest -q`
Expected: `76 passed`

```bash
git add src/stage_director/analysis/measure.py src/stage_director/analysis/cli.py tests/analysis/test_measure.py
git commit -m "feat: add librosa measurement layer and analysis CLI"
```

- [ ] **Step 11: (사람이 하는 단계) 실제 Lyria 곡으로 스파이크를 실행한다**

에이전트가 할 수 없는 단계다. 곡 파일은 사용자가 제공한다. 이 결과가 후속 계획(구조 분석 모델, 무드 해석, 임계값)을 정한다.

```bash
uv run python -m stage_director.analysis.cli path/to/song.mp3 > /tmp/song-analysis.json
```

JSON 을 보고 아래를 메모한다(`docs/superpowers/notes/analysis-spike.md` 등 편한 곳에).

- `bpm` 이 실제 템포와 맞는가. 반이나 두 배로 잡히지 않았는가
- `beatsSec` 간격이 일정한가
- `energyCurve` 가 곡의 인트로→코러스 흐름을 따라 오르내리는가 (구간 경계로 쓸 만한 변화가 보이는가)
- `onsetDensity` 가 에너지와 같은 방향인가

---

## 스펙 대응

| 스펙 | 이 계획의 태스크 |
| --- | --- |
| §10 `contracts/` 요건, §12 제약 1~3 | Task 1 |
| §5 `items` 불변식, §6 항목 모양 | Task 2 |
| §7 1층(모델·clamp), 2층(게이트 규칙) | Task 1(clamp), Task 3 |
| §6.3 `proposals` 리듀서, 부분 재생성 불변식 | Task 4 |
| §5·§6.3 분석 스냅샷, §9 분석 테스트 | Task 5 |
| §7 3층(저장 시 Next.js 검증), §4 프로토콜, §6 체크포인터, §8 오류 처리, §11 on-stage 후속 작업 | 이 계획 밖 (후속 계획 또는 on-stage) |

---

## 실행 후 변경 (배치 리뷰 반영)

각 태스크의 `Expected` 통과 개수는 그 태스크를 처음 실행했을 때의 값이다. 배치 리뷰(포니테일 + 자체 리뷰)를 반영해 아래를 고쳤고, 그 결과 **전체 89개 통과**가 최종 상태다.

| 변경 | 커밋 | 테스트 변화 |
| --- | --- | --- |
| `CALM_ENERGY_RATIO` 0.7 → 0.9 (실제 곡 2개 측정 반영) | `c376699` | gate +2 |
| `merge_stage_state`: float 로 못 바꾸는 큰 정수는 타입이 틀린 값으로 보고 fallback | `45964ce` | stage_state +1 |
| `parse_analysis`: 큰 정수에서 예외를 던지지 않음. `energyCurve`·`onsetDensity` 의 잘못된 원소는 버리지 않고 0.0 으로 채워 인덱스(=시각)를 유지 | `2e39e46` | snapshot +2 |
| `sanitize_state`: 객체(dict)가 아닌 출력은 `invalid_state` 이슈를 남기고 재생성 대상이 됨 (규칙 이름 `invalid_state` 추가) | `7c0908e` | gate +3 |
| 계약 문서 `supabase_required.md` 에 경계 허용 오차 ±1e-3초(`TOLERANCE_SEC`) 명시 | `e8f9db0` | - |
| `HEX_COLOR` 앵커(`\A…\Z`), `CAMERAS = get_args(Camera)` | `a199cc7` | stage_state +5 |
| `cli.py` `print`, `beat_track(units="time")`, `merge_proposals` 한 줄 (동작 동일 확인) | `9fffe8d` | - |
| `.gitignore`: `demo-tracks/`, `.DS_Store`, 상세 계획 파일 | `b486a04` | - |
| `.claude/settings.json`: `additionalDirectories` 제거 (Step 10 의 deny 규칙은 유지) | `d50c632` | - |

파일별 최종 개수: stage_state 24, sequence 13, gate 23, proposals 9, snapshot 11, measure 9.
