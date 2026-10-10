# on-stage 인수 문서 — Python 서비스(stage-director-agent) API 계약

이 문서는 on-stage(Next.js) 쪽 작업자가 **이 저장소를 열지 않고** Python 서비스를 호출하는 코드를 쓸 수 있게 만든 인수 문서다.
모양과 값은 실제 코드와 테스트에서 가져왔고, 응답 예시는 서비스를 실행해 얻은 실제 출력이다. 이 문서와 코드가 다르면 코드가 맞고, 이 문서를 고쳐야 한다.

- 작성 기준: 브랜치 `feat/realtime-deploy` (2026-10-09), 서비스는 Google Cloud Run(asia-southeast1)에 배포됨
- 상세 설계는 `docs/superpowers/specs/stage-director-agent-design.md` (§4.1 분석, §4.2 시퀀스, §5 데이터 모델, §6 상태·보존, §7 검증, §8 오류)

## 1. 먼저 알아 둘 것

| 항목 | 내용 |
| --- | --- |
| 서비스 주소(Base URL) | `https://stage-director-agent-815384828055.asia-southeast1.run.app` (환경변수 예: `PYTHON_SERVICE_URL`) |
| 인증 | 모든 요청에 헤더 `X-Internal-Key: <값>` (환경변수 예: `INTERNAL_API_KEY`). 서버 환경변수에만 두고 브라우저로 내보내지 않는다. 키 값은 이 문서에 없고 안전한 경로로 따로 전달한다. 예외: `GET /health`, `GET /ready` 는 키 없이 호출된다 |
| 호출 주체 | Next.js **API Route(서버)** 만 부른다. 브라우저가 직접 부르지 않는다 |
| 형식 | 요청·응답 모두 JSON, 키는 **camelCase**. 요청에 `Content-Type: application/json` |
| Python 은 Supabase 를 읽지 않는다 | 곡 메타·아티스트·프리셋·분석 결과·음원 URL 은 전부 **요청 본문에 실어 보낸다**. 소유권 검증(RLS)은 Next.js 가 하고, Python 은 id 의 주인이 누군지 모른다 |
| 비동기 | 모든 작업은 **접수(202) 후 폴링**이다. 요청이 길게 열려 있지 않는다 |
| 콜드 스타트 | 평소 인스턴스가 0대로 줄어 있어 **유휴 뒤 첫 요청은 약 10초**(실측) 걸린다. 서버 쪽 요청 타임아웃을 30초 이상으로 잡는다 |
| 쓰지 말 것 | `POST /propose` — 동기로 최악 약 6분 걸려 서버리스 시간 제한에 걸린다. 시퀀스 생성은 `/runs` 를 쓴다 |
| 인스턴스 | 항상 최대 1대. 동시에 돌 수 있는 작업: 시퀀스(그래프) 2건, 분석 1건. 초과분은 `queued` 로 줄을 선다 |

## 2. 상태값

작업 상태는 두 종류의 응답에 나온다.

| 응답 | `status` 값 |
| --- | --- |
| `/analyze` | `queued` · `running` · `done` · `error` |
| `/runs` | `queued` · `running` · `waiting_input` · `done` · `error` |

| 값 | 뜻 | 프론트가 할 일 |
| --- | --- | --- |
| `queued` | 처리 슬롯이 없어 순서를 기다리는 중 | `running` 처럼 진행 중으로 보이되 "대기 중" 문구를 쓸 수 있다 |
| `running` | 처리 중 | 스피너. 계속 폴링 |
| `waiting_input` | (`/runs` 만) 사람의 응답을 기다림. `interrupt` 에 화면에 보여 줄 내용이 있다 | 해당 화면을 보여 주고 사람이 답하면 `resume` |
| `done` | 끝남. `result` 에 결과 | 결과 저장 후 폴링 종료 |
| `error` | 실패. `error` 에 코드 문자열 | §5 의 에러 코드 표대로 안내. 대부분 **같은 id 로 다시 POST 하면 재시도** |

폴링 권장: 2~3초 간격. `done`/`error`, `/runs` 는 `waiting_input` 에서도 멈춘다. `queued` 는 분석 풀이 1건이라 앞 작업이 길면(수십 초~) 오래 갈 수 있다.

`progress`(분석만): 0~1 실수 또는 `null`. **단계 단위**로만 움직인다(시작 0.1 → 내려받음 0.4 → 끝 1.0). 퍼센트 바로 쓰면 오래 멈춘 것처럼 보이니 `status` 문구(대기 중/분석 중)와 함께 쓴다. **`status` 가 `error` 일 때 `progress` 는 무시한다** — 끊긴 분석이 `1.0` 같은 마지막으로 기록된 값을 남길 수 있다(에러인데 100% 로 보이는 것은 정상 동작이다).

## 3. 음원 분석 — `/analyze`

### 3.1 `POST /analyze` → 202

요청 본문:

```json
{ "jobId": "9b2f...(audio_tracks.id)", "audioUrl": "https://htmfbhgjxgxbhuujfvwm.supabase.co/storage/v1/object/sign/<버킷>/<경로>?token=..." }
```

| 필드 | 규칙 |
| --- | --- |
| `jobId` | 1~64자. `audio_tracks.id` 를 그대로 쓴다. 같은 id 로 다시 POST 하면 **멱등**(이미 있으면 현재 상태를 돌려주고, `error` 일 때만 처음부터 재시도) |
| `audioUrl` | 최대 2048자. https, 포트 443(생략), **호스트가 `htmfbhgjxgxbhuujfvwm.supabase.co`(허용 목록)**, 사용자 정보(`user:pw@`) 없음, 공인 IP 로 풀림. 하나라도 어기면 큐에 넣지 않고 422 `invalid_audio_url` |

`audioUrl` 준비: Supabase Storage 의 음원 파일에 대한 **서명 URL**(또는 공개 버킷 URL)을 서버에서 만들어 넘긴다. 서버가 인터넷에서 직접 내려받으므로 (a) 로컬 경로·다른 사이트 주소는 안 되고, (b) 유효기간이 지나기 전에 받을 수 있어야 한다. 분석 풀이 1건이라 앞 작업 뒤에 줄 설 수 있으니 **유효기간 30분 이상**을 권장한다. 서명 URL 의 `token=` 은 비밀에 가깝다 — 로그에 남기지 않는다.

응답 본문 (`GET` 과 같은 모양):

```json
{ "jobId": "…", "status": "queued", "progress": null, "result": null, "error": null }
```

### 3.2 `GET /analyze/{jobId}` → 200

`status` 가 `done` 이면 `result` 가 채워진다:

```json
{
  "jobId": "…", "status": "done", "progress": 1.0, "error": null,
  "result": {
    "fileHash": "<sha256 16진수 64자>",
    "analysis": {
      "durationSec": 173.819, "bpm": 99.38,
      "beatsSec": [0.5, 1.1, "..."],
      "energyCurve": [0.0213, "... 초당 1개"],
      "onsetDensity": [3.0, "... 초당 1개"]
    }
  }
}
```

저장 방법(제안): `analysis` 객체를 그대로 `audio_tracks.analysis`(jsonb)에, `fileHash` → `file_hash`, `analysis.durationSec` → `duration_sec`. 저장은 **멱등**하게 한다 — 탭을 닫아도 결과는 Python 쪽에 **마지막 갱신 후 7일** 남아 다음 폴링 때 받을 수 있지만, 7일이 지나면 404 이다.
`analysis_status` 대응(제안): `queued` → `pending`, `running` → `running`, `done` → `done`, `error` → `error`.

### 3.3 분석 제한

| 제한 | 값 | 어기면 |
| --- | --- | --- |
| 파일 크기 | **30MiB** 이하 | 내려받다가 중단 → `error: audio_unavailable` |
| 길이 | **300초(5분)** 이하(1초 오차 허용, 즉 301초까지) | `error: too_long` |
| 형식 | **mp3 · wav · flac · ogg** | 그 밖의 형식(m4a·aac 등)은 읽지 못해 `error: decode_failed` |
| 동시 처리 | 분석 1건 | 나머지는 `queued` |

음원 버킷도 같은 제한(30MiB, 위 4가지 MIME)으로 만들어 두면 업로드 단계에서 걸러져 사용자가 일찍 알 수 있다. Storage 는 **길이**를 제한하지 못하므로 300초 초과는 Python 이 분석 때 `too_long` 으로 알려 준다.

**wav 는 길이보다 크기에 먼저 걸린다.** 44.1kHz 스테레오 16비트 wav 는 약 178초가 30MiB 라서, 5분 곡은 mp3·ogg·flac 같은 압축 형식이거나 모노·저샘플레이트 wav 여야 한다(5분 mp3 는 보통 3~12MB). 업로드 화면 안내에 "wav 는 파일이 커서 3분 안팎까지만 올릴 수 있다"를 함께 적어 두면 사용자가 헷갈리지 않는다.

## 4. 시퀀스 생성 — `/runs`

### 4.1 흐름

```
POST /runs ─▶ queued/running ─▶ waiting_input  [kind: confirm_sections]   ← 구간 확인·수정 화면
                                      │ POST /runs/{id}/resume  {kind:"sections"}
                                      ▼
                                queued/running ─▶ waiting_input  [kind: review]   ← 구간별 제안 리뷰 화면
                                      │ resume {kind:"feedback"} ─▶ (선택한 구간만 다시 생성) ─▶ review 로 돌아옴
                                      │ resume {kind:"approve"}
                                      ▼
                                    done  (result 에 최종 시퀀스)
```

`threadId` = `stage_sequences.id`(uuid 문자열). 시퀀스 생성 전제: 곡의 `analysis_status` 가 `done` (스펙 §4.2, Next.js 가 409 `analysis_not_ready` 로 막는다).

### 4.2 `POST /runs` → 202

```json
{
  "threadId": "<stage_sequences.id>",
  "context": {
    "track":  { "title": "나만의 작은 우주", "genre": "K-pop", "moodKeywords": ["몽환", "벅찬"] },
    "artist": { "slug": "aurora", "name": "AURORA", "nameKo": "오로라", "color": "#9F77DD",
                "shader": { "pattern": "wave", "freq": 9, "falloff": 0.75, "speed": 0.5 } },
    "presets": [ { "name": "기본", "state": { /* 저장된 StageState jsonb 그대로 */ } } ],
    "analysis": { /* audio_tracks.analysis 그대로 (§3.2 의 analysis) */ },
    "durationSec": 173.819,
    "audioUrl": "https://htmfbhgjxgxbhuujfvwm.supabase.co/storage/v1/object/sign/..."
  }
}
```

| 필드 | 규칙 |
| --- | --- |
| `threadId` | 1~64자. 같은 id 로 다시 POST 하면 **멱등**: 새 스레드면 시작, `error` 면 **마지막 체크포인트에서 이어서**, 그 밖에는 현재 상태 반환 |
| `context.track` | `title` 필수, `genre`·`moodKeywords` 선택 |
| `context.artist` | `slug, name, nameKo, color(#RRGGBB), shader{pattern: wave\|ripple\|grain, freq, falloff, speed}` 필수. 모양은 `contracts/artist_context.md`(on-stage 의 `Artist` 부분집합) |
| `context.presets` | 선택. 아티스트의 기존 프리셋 `[{name, state}]`. 범위 밖 값이 있어도 Python 이 clamp 해서 쓴다 |
| `context.analysis` | `audio_tracks.analysis` 를 그대로. 필드가 깨져도 필드 단위로 방어한다 |
| `context.durationSec` | 필수, 0 초과. 곡의 실제 길이(`duration_sec`). 최종 시퀀스의 마지막 구간이 여기서 끝나야 한다 |
| `context.audioUrl` | 선택. 주면 곡 전체 오디오로 구간별 **분위기(mood)** 를 해석한다. 조건은 §3.1 의 `audioUrl` 과 같고, **조건을 어기거나 내려받기에 실패하거나 15MiB 를 넘으면 요청을 거절하지 않고 무드 해석만 건너뛴다**(무드는 선택 정보). 이 URL 은 요청 직후 첫 단계에서 쓰이므로 10분 유효면 충분 |

응답은 `RunStatus` (아래 4.3). 처음 응답은 보통 `queued` 또는 `running`, 곧 `waiting_input`.

### 4.3 `GET /runs/{threadId}` → 200, `RunStatus`

```json
{ "threadId": "…", "status": "waiting_input", "interrupt": { /* 아래 */ }, "result": null, "error": null }
```

| 필드 | 설명 |
| --- | --- |
| `interrupt` | `waiting_input` 일 때만. 아래 두 종류 중 하나 |
| `result` | `done` 일 때만 `{ sections, items, issues }` |
| `error` | `error` 일 때만 코드 문자열 (§5) |

**interrupt 1 — 구간 확인** (`kind: "confirm_sections"`):

```json
{
  "interruptId": "t1:0:confirm_sections", "kind": "confirm_sections",
  "sections": [ { "label": "intro", "startSec": 0.0, "endSec": 62.0, "mood": "긴장감을 고조시키는 에너제틱한 비트" },
                { "label": "chorus", "startSec": 62.0, "endSec": 114.0, "mood": "" } ],
  "energyCurve": [0.0213, "… 초당 1개 (구간 경계 편집 UI 용)"],
  "durationSec": 173.819
}
```

구간 경계는 에너지 곡선 기반 자동 휴리스틱이라 **틀릴 수 있다**(미세한 벌스↔코러스 전환을 놓친다). 그래서 사람이 이 화면에서 경계·라벨·무드를 고친다. `mood` 는 비어 있을 수 있다.

**interrupt 2 — 리뷰** (`kind: "review"`):

```json
{
  "interruptId": "t1:1:review", "kind": "review",
  "items": [ {
    "sectionLabel": "intro", "startSec": 0.0, "endSec": 30.0, "transitionMs": 2000,
    "state": { "color": "#9F77DD",
               "spots": { "left":   { "on": true, "intensity": 300.0, "angle": 0.5, "penumbra": 0.6 },
                          "center": { "on": true, "intensity": 300.0, "angle": 0.5, "penumbra": 0.6 },
                          "right":  { "on": true, "intensity": 300.0, "angle": 0.5, "penumbra": 0.6 } },
               "camera": "front", "smoke": { "density": 0.3, "color": "#ffffff" } },
    "rationale": "측정값에 맞춘 연출"
  } ],
  "issues": [ { "rule": "calm_too_bright", "message": "에너지 비 0.40인 잔잔한 구간의 밝기가 600 (> 500)", "idx": 1 } ]
}
```

- `items[i].state` 는 on-stage 의 `StageState` 모양(`contracts/enums_and_constants.md`). `rationale` 은 구간별 연출 이유 문장.
- `issues` 는 자동 재생성 후에도 남은 **경고**(비어 있을 수 있다). `idx` 는 해당 구간 번호이고 곡 전체 경고는 `null`. `rule` 값: `calm_too_bright`(잔잔한 구간이 너무 밝음), `energy_brightness_direction`(에너지와 밝기 변화 방향이 반대), `color_deviation_without_rationale`(시그니처 컬러에서 벗어났는데 이유 없음), `clamped`(범위 밖 값을 보정함), `invalid_state`(상태가 깨져 기본값으로 대체). 경고가 있어도 승인할 수 있다 — 화면에 경고를 보여 준다.
- `interruptId = "<threadId>:<revision>:<kind>"`. 화면이 바뀔 때마다 revision 이 올라가므로 **resume 은 항상 방금 받은 interruptId 로** 보낸다.

### 4.4 `POST /runs/{threadId}/resume` → 202

본문은 `{ "interruptId": "...", "kind": "...", "payload": ... }` 이고 `kind` 별로 세 가지다. 응답은 `RunStatus` (보통 `queued`).

| `kind` | 언제 | `payload` |
| --- | --- | --- |
| `"sections"` | `confirm_sections` 화면에서 확정 | `{ "sections": [ {label, startSec, endSec, mood?}, … ] }` — 사람이 고친 전체 목록 |
| `"feedback"` | `review` 화면에서 **선택한 구간만** 다시 생성 | `{ "text": "수정 요청 (1~500자)", "targets": [구간 번호, …] }` — `targets` 는 사용자가 고른 0부터의 구간 번호(최소 1개). 대상 밖 구간은 **바이트 단위로 그대로** 유지된다 |
| `"approve"` | `review` 화면에서 전체 승인 | 없음 |

`sections` 검증 규칙(어기면 422 `invalid_sections`, **interrupt 는 그대로 남아 다시 보낼 수 있다**):
- 구간 1~12개, 첫 구간 `startSec` = 0, 마지막 `endSec` = `durationSec`(오차 0.001초), 앞 구간 `endSec` = 다음 `startSec`
- 구간이 2개 이상이면 각 구간 8초 이상
- 라벨 1~40자(공백만 불가), `mood` 는 200자 이하

종류가 맞아야 한다: `sections` ↔ `confirm_sections`, `feedback`·`approve` ↔ `review`. 틀리면 409 `kind_mismatch`.

**피드백 이후:** 선택한 구간만 다시 생성한 뒤 `review` 로 **다시 돌아온다**(새 `interruptId`, 같은 `kind: "review"`). 피드백은 횟수 제한이 없으니 남용 방지는 Next.js 레이트 리밋 몫이다.

### 4.5 최종 결과 (`done`)

```json
{ "status": "done", "result": { "sections": [ … ], "items": [ … ], "issues": [ … ] } }
```

`result.items` 가 승인된 시퀀스다. **저장 전에 Next.js 가 다시 검증**한다(스펙 §7 3층): 항목 순서 = 시간 순서, 첫 `startSec` = 0, 마지막 `endSec` = `duration_sec`, `items[i].endSec == items[i+1].startSec`, `0 <= transitionMs <= (endSec-startSec)*1000`, 그리고 각 `state` 를 `mergeStageState` 로 정리. 그 뒤 `stage_sequences.items` 에 저장, `status = approved`, `approved_at = now()`.

### 4.6 완료 후·만료 규칙 (중요)

- **승인된 시퀀스(`approved`)는 Python 을 부르지 않는다.** 이후 조회는 Supabase 행만 읽는다. `done` 작업의 Python 쪽 행은 7일 뒤 삭제되고 체크포인트는 24시간 뒤 삭제되어, 그 id 로 `GET /runs/{id}` 를 부르면 404 가 된다.
- **초안(`draft`)에 대해서만** `GET /runs/{id}` 가 404 `thread_not_found` 이면 "만료됨"으로 보고 **410 을 돌려주며 `draft` 행을 지운다**(UI: "세션이 만료되었습니다. 다시 시작하세요"). Python 은 "존재한 적 없음"과 "7일 방치로 삭제됨"을 구별하지 못한다.
- 승인 직후 저장이 실패하면 `GET /runs/{id}` 로 같은 결과를 다시 받아 저장할 수 있다. 결과(`result`)는 `done` 후 7일까지 남지만 설계상 여유는 24시간이니, 실패 재시도는 그 안에 한다.

## 5. 에러 코드 표

에러 본문은 항상 `{ "detail": "<코드>" }` (설명이 있으면 `"message": "…"` 가 붙는다). 입력 모양 오류는 §5.3.

### 5.1 HTTP 에러

| HTTP | `detail` | 발생 | 프론트/라우트 처리 |
| --- | --- | --- | --- |
| 401 | `unauthorized` | `X-Internal-Key` 없음·틀림 | 서버 설정 오류. 사용자에게는 일반 오류 |
| 404 | `thread_not_found` | `/runs/*` 에서 모르는 `threadId`(분석 id 포함) | 초안이면 410 + 초안 행 삭제 (§4.6) |
| 404 | `job_not_found` | `/analyze/*` 에서 모르는 `jobId`(그래프 id 포함) | 7일 지난 분석이면 다시 POST 해서 새로 분석 |
| 409 | `not_waiting_input` | resume 인데 `waiting_input` 이 아님(이미 처리 중·끝남·더블 클릭) | 상태를 다시 조회해 화면 갱신 |
| 409 | `stale_interrupt` | `interruptId` 가 현재 것이 아님(낡은 화면) | 최신 상태를 다시 불러 화면 갱신 |
| 409 | `kind_mismatch` | resume `kind` 가 현재 interrupt 와 안 맞음 | 프론트 버그. 화면 종류와 kind 확인 |
| 422 | `invalid_sections` (+`message`) | 구간 수정이 규칙 위반 (§4.4) | `message` 를 사용자에게 보여 주고 수정하게 함. 대기는 유지 |
| 422 | `invalid_targets` (+`message`) | 피드백 `targets` 가 구간 범위 밖 | 선택 UI 확인 |
| 422 | `invalid_audio_url` (+`message`) | `/analyze` 의 `audioUrl` 조건 위반 (§3.1) | 서버에서 만든 URL 의 호스트·만료 확인 |
| 422 | (배열) | 요청 본문 모양 오류 (§5.3) | 프론트/라우트 버그 |
| 5xx / 연결 실패 | — | Cloud Run 콜드 스타트·재시작·장애 | Next.js 가 502 로 변환. 시드 곡은 캐시된 예시 시퀀스로 폴백(스펙 §8) |

### 5.2 작업 `error` 값 (HTTP 200 응답 안의 `status: "error"`)

| `error` | 대상 | 뜻 | 처리 |
| --- | --- | --- | --- |
| `llm_failed` | `/runs` | Gemini 호출이 재시도·예비 모델 전환 후에도 실패 | "다시 시도" → 같은 `threadId` 로 `POST /runs`: **마지막 체크포인트에서 이어간다** |
| `llm_quota_exceeded` | `/runs` | Gemini 가 한도 초과(429)를 돌려줘 예비 모델까지 실패. **분당·하루 한도를 구분하지 않고 모두 이 코드**다. 다시 시도해도 같을 수 있다(하루 한도면 다음 날까지) | 사용자에게 “지금은 사용 한도를 초과했습니다”로 안내하고 **잠시 뒤나 내일 다시** 시도하게 한다. 버튼을 연타하게 두지 않는다. 시드 곡(미리 만든 예시 시퀀스) 체험을 안내한다. 한도가 풀린 뒤 같은 `threadId` 로 `POST /runs` 하면 마지막 체크포인트에서 이어간다 |
| `internal_error` | 둘 다 | 예상 못 한 서버 오류(상세는 서버 로그에만) | 재시도 가능. 반복되면 보고 |
| `interrupted` | 둘 다 | 처리 중 서버가 재시작·교체되어 작업이 끊김 | "다시 시도": `/runs` 는 이어서, `/analyze` 는 처음부터 |
| `audio_unavailable` | `/analyze` | 음원을 내려받지 못함(URL 만료·없는 파일·차단·시간 초과) | 새 서명 URL 로 다시 POST |
| `decode_failed` | `/analyze` | 오디오로 읽을 수 없음(형식 미지원·깨진 파일) | 사용자에게 파일 확인 안내. 재시도해도 같다 |
| `too_long` | `/analyze` | 300초 초과 | 사용자에게 안내. 재시도해도 같다 |

### 5.3 422 요청 본문 오류 (FastAPI 형식)

```json
{ "detail": [ { "loc": ["body", "context", "track"], "msg": "Field required", "type": "missing" } ] }
```

입력값은 응답에 되돌려 주지 않는다.

## 6. 호출 순서 예시 (Next.js 라우트 관점)

```
업로드 후
  1. audio_tracks 행 생성 (analysis_status=pending). 같은 file_hash 의 분석이 있으면 재사용하고 끝
  2. POST {PY}/analyze {jobId: audio_tracks.id, audioUrl: <서명 URL>}
  3. 프론트 폴링 → 라우트가 GET {PY}/analyze/{jobId} 를 중계 → done 이면 analysis 저장(멱등)

시퀀스 생성
  1. 인증·레이트 리밋·곡 조회(RLS)·analysis_status=done 확인·승인본 5개 상한 확인
  2. stage_sequences 행 생성(draft) → POST {PY}/runs {threadId: 행 id, context}   (실패하면 행 삭제, 502)
  3. 폴링 GET {PY}/runs/{id} → waiting_input(confirm_sections) → 화면
  4. 사람이 확정 → POST .../resume {kind:"sections"} → 폴링 → waiting_input(review) → 화면
  5. 피드백 반복 또는 approve → done → 검증 후 items 저장, approved
```

## 7. 시간 감각 (UI 문구용)

- **분석:** 5분 곡 기준 측정 자체는 약 2~3초이고, 음원 내려받기와 대기열을 합쳐 대략 수 초~수십 초. 대기열이 없을 때.
- **시퀀스 생성:** 구간마다 Gemini 호출이 한 번씩 필요하고, 현재 무료 키 기준 **모델당 분당 4회**로 속도를 제한한다. 구간이 8개인 5분 곡은 단순 계산으로 약 2분, 자동 재생성(위반 구간 최대 2회)이 붙으면 3~4분까지 걸린다(구간은 곡이 길수록 늘며 최대 12개). 진행 화면은 "생성 중(2~4분 걸릴 수 있음)" 정도로 안내한다.
- **무드 해석(`audioUrl` 을 준 경우):** 첫 구간 확인 화면이 뜨기 전에 오디오 해석이 한 번 돌고, 실패하면 최대 3번(최초 + 2회) 재시도하므로 **최대 40초쯤** 걸릴 수 있다. 그래도 실패하면 무드가 빈 채로 진행한다(에러가 아니다).
- **재시작 직후 1분쯤은 429 가 한 번 날 수 있다.** 분당 요청 카운터가 서버 메모리에 있어서 재시작·새 리비전 때 초기화되기 때문이다. 서버가 재시도하지만 끝까지 실패하면 `llm_quota_exceeded` 로 나오며, 1분쯤 뒤 다시 시도하면 된다.
- **주 모델 혼잡(503):** 가끔 일어난다. 서버가 예비 모델·재시도로 흡수하지만 그만큼 느려진다.
- 유료 키로 바꾸면 `GEMINI_RPM` 환경변수만 올리면 빨라진다(서버 설정, on-stage 변경 없음).

## 8. 헬스 체크 (참고)

| 요청 | 응답 | 용도 |
| --- | --- | --- |
| `GET /health` | `200 {"status":"ok"}` | 프로세스가 요청을 받는지. 키 없음 |
| `GET /ready` | `200 {"status":"ok"}` 또는 `503 {"status":"db_unavailable"}` | 전용 DB 에 닿는지. 키 없음. 사람·모니터링용이며 앱 코드에서 호출하지 않는다 |

## 9. on-stage 에서 해야 할 일 체크리스트

- [ ] 환경변수 추가: `PYTHON_SERVICE_URL`, `INTERNAL_API_KEY` (서버 전용)
- [ ] 음원 버킷: 30MiB 상한, MIME 은 mp3·wav·flac·ogg 만, 시드 곡은 읽기 허용(§3.3). `audio_tracks.duration_sec` 에 상한 제약을 둔다면 **300초**, 업로드 화면의 길이 안내도 5분으로
- [ ] `audio_tracks`, `stage_sequences` 마이그레이션 (스펙 §5)
- [ ] 서명 URL 발급: 분석용(30분 이상), 무드 해석용(10분 이상)
- [ ] `/api/audio-tracks*` 라우트: 분석 시작·폴링 중계, 결과 멱등 저장
- [ ] `/api/sequences*` 라우트: 생성·폴링 중계·resume, 410 정리(초안만), 저장 전 §4.5 검증
- [ ] 상태값 `queued` 를 `running` 과 같은 진행 중으로 처리 (+ "대기 중" 문구)
- [ ] `approved` 시퀀스에 대해 Python 을 호출하지 않기
- [ ] 시드 곡: `audio_tracks` 행을 `user_id = NULL` 로 만들고 `file_hash`·`duration_sec`·`analysis` 는 Python 쪽에서 받은 `seed-analysis/*.json` 의 값을 사용 (`{fileName, fileHash, durationSec, analysis}`)
- [ ] 저장소를 공개하면 README 에 "오디오는 AI 생성물이며 MIT 라이선스 대상이 아니다" 고지

---

# 부록 — 추가 (on-stage 요청 답변, 2026-10-09)

아래는 위 본문에 **없던 내용을 추가**한 것이다. 위 본문(§1~§9)은 바꾸지 않았다.

## 추가 A. 스키마 정리 (스펙 §5 + `contracts/supabase_required.md` 2부)

on-stage 가 마이그레이션으로 만들 것. 이 저장소는 DDL 을 갖지 않는다(요구사항만). 원본: `docs/superpowers/specs/stage-director-agent-design.md` §5, `contracts/supabase_required.md` 2부(신규 요구사항). `contracts/*.md` 의 다른 파일(`stage_state`·`api_stage_presets`·`artist_context`·`enums_and_constants`)은 **on-stage 코드에서 가져온 사실의 사본**이라 on-stage 에는 새 정보가 아니다(`StageState` 의 범위·기본값만 아래 추가 D 에 다시 적었다).

### `audio_tracks`

| 컬럼 | 타입 | 규칙 |
| --- | --- | --- |
| `id` | uuid pk | `POST /analyze` 의 `jobId` 로 쓴다 |
| `user_id` | uuid null | **NULL = 시드 곡.** 업로드 곡은 기본값 `auth.uid()` |
| `artist_id` | uuid not null | `artists` 참조 |
| `title` | text not null | |
| `genre` | text | |
| `mood_keywords` | text[] | |
| `storage_path` | text not null | 음원 버킷 경로 |
| `file_hash` | text not null | sha256. 분석 캐시 키. Python 이 돌려주는 `result.fileHash` 와 같은 값 |
| `duration_sec` | numeric not null | **최대 300** |
| `analysis` | jsonb null | `result.analysis` 를 그대로 (§3.2: `durationSec, bpm, beatsSec, energyCurve, onsetDensity`, camelCase) |
| `analysis_status` | text not null | `pending` \| `running` \| `done` \| `error` (Python 상태 대응은 §3.2) |
| `created_at` | timestamptz | |

RLS: 읽기 `user_id IS NULL OR user_id = auth.uid()`(시드 공개), 쓰기 `user_id = auth.uid()`.

### `stage_sequences`

| 컬럼 | 타입 | 규칙 |
| --- | --- | --- |
| `id` | uuid pk | **= Python 의 `threadId`** |
| `user_id` | uuid not null | 기본값 `auth.uid()` |
| `audio_track_id` | uuid not null | `audio_tracks` 참조 |
| `status` | text not null | `draft` \| `approved` |
| `items` | jsonb null | 승인 전 NULL. `result.items`(아래 `SequenceItem` 배열) |
| `approved_at` | timestamptz null | |
| `created_at` | timestamptz | |

- RLS: `stage_presets` 와 같은 완전 사용자 스코프(`user_id = auth.uid()`).
- 부분 유니크 인덱스: `(user_id, audio_track_id) WHERE status = 'draft'` — 곡당 초안은 하나.
- **승인본 5개 상한**은 `(user_id, audio_track_id)` 단위이며 Next.js 라우트에서 강제한다(DB 제약 아님).

### `SequenceItem` (= `items` 배열의 원소)

```json
{ "sectionLabel": "chorus", "startSec": 62.0, "endSec": 114.0, "transitionMs": 2000,
  "state": { /* StageState */ }, "rationale": "에너지 비 1.12: …" }
```

불변식(Python 게이트와 on-stage 저장 직전이 **같은 규칙**을 쓴다. 경계 비교에는 **±0.001초 허용 오차**를 둔다 — 같은 오차를 안 쓰면 Python 이 통과시킨 시퀀스가 저장 단계에서 거절될 수 있다):

- 배열 순서 = 시간 순서, 각 항목 `startSec < endSec`
- 첫 `startSec = 0`, 마지막 `endSec = duration_sec`, `items[i].endSec == items[i+1].startSec`
- `0 <= transitionMs <= (endSec - startSec) * 1000`

### 음원 Storage 버킷 (신규, `gallery` 와 별개)

- 허용 형식: **mp3 · wav · flac · ogg**, 크기 **30MiB** 이하(§3.3). 시드 곡은 모든 사용자가 읽을 수 있어야 한다(재생용 서명 URL 발급).
- 업로드는 `POST /api/gallery/upload-url` 의 서명 URL 패턴을 복제. 크기·타입 검사는 사용자에게 빨리 알려 주기 위한 것이고 신뢰 경계는 버킷 설정이다.
- 음원 파일은 이 저장소(git)에 없다. **Storage 에만 둔다.**

## 추가 B. 구간 `label` 의 전체 어휘

`sections[].label` 과 `items[].sectionLabel` 은 같은 값이다.

| 출처 | 값 |
| --- | --- |
| 자동 구간 검출이 붙이는 값 (영문 소문자) | `intro`(첫 구간) · `outro`(마지막 구간) · `chorus`(그 사이이면서 곡 평균보다 에너지가 큼) · `verse`(그 사이이면서 평균 이하) |
| 곡이 짧거나(약 16초 이하) 분석이 없어 구간이 하나뿐일 때 | `intro` 하나 |
| 사람이 구간 확인 화면에서 고친 값 | **자유 문자열**(공백만은 불가, 1~40자). 예: `bridge`, `드롭` |

- `bridge`·`pre-chorus` 같은 값은 **자동으로는 나오지 않는다.** 사람이 붙일 때만 생긴다.
- 그러므로 on-stage 는 **네 개를 기본값으로 알되 임의 문자열을 그대로 표시**해야 한다(범위 밖 값이 와도 깨지면 안 된다). 같은 라벨이 여러 번 나올 수 있다(예: `chorus` 3개).
- 자동 라벨은 에너지 휴리스틱이라 실제 곡 구조와 다를 수 있다(벌스를 `chorus` 로 붙이기도 한다).

## 추가 C. `transitionMs` 의 의미

**맞다. "구간 `i` 가 시작될 때 이전 구간 `i-1` 의 `state` 에서 `items[i].state` 로 보간해 가는 데 걸리는 시간(밀리초)"으로 확정해서 쓴다.** 스펙에는 방향이 글로 명시돼 있지 않고, "구간 사이 보간은 ref + `useFrame` 에서 처리하고 구간 경계에서만 state 를 커밋한다"(기획서)와 불변식(`0 <= transitionMs <= 구간 길이`)에서 나오는 해석이다 — 전환이 그 구간 안에서 끝나야 한다는 뜻이 불변식이므로 "구간 시작에서 시작해 이 구간 안에서 끝난다"가 맞다.

- 첫 항목(`i = 0`)은 이전 구간이 없다. 어디서부터 보간할지(현재 씬 상태, 기본 상태 등)는 **on-stage 가 정한다**.
- 현재 Python 이 만드는 값은 **모든 항목이 2000**이다(구간이 2초보다 짧으면 그 구간 길이로 줄임). LLM 이 정하는 값이 아니다. on-stage 가 값을 바꿔 저장하려면 위 불변식 범위 안에서 바꾼다.

## 추가 D. 밝기 값의 범위

- **`state.spots.<left|center|right>.intensity` 의 범위는 0~1000** — on-stage 슬라이더와 같다(기본 300, step 10). 범위를 벗어난 값은 Python 이 clamp 하고 `issues` 에 `clamped` 를 남긴다. `angle` 0.1~1.0, `penumbra` 0~1(에이전트가 안 바꾸고 기본 0.6 유지), `smoke.density` 0~1.
- **경고 임계값 500·600 의 정체:** 같은 0~1000 눈금이다. 게이트는 구간의 "밝기"를 **켜져 있는(`on: true`) 스팟 중 가장 높은 `intensity`** 로 정의한다(합이나 평균이 아님, 켜진 스팟이 없으면 0).
  - `calm_too_bright`: 곡 평균 대비 에너지 비가 **0.9 이하**인 잔잔한 구간에서 밝기가 **500 초과**면 경고. 메시지 예시의 `600 (> 500)` 은 "밝기 600 이 상한 500 을 넘었다"는 뜻이다.
  - 500 은 슬라이더 최대값(1000)의 절반이다. 규칙이 아니라 **초기 조정값**이며 실제 곡으로 보고 바뀔 수 있다.
  - `energy_brightness_direction`: 인접 구간의 에너지 비 변화가 0.15 를 넘는데 밝기는 반대 방향으로 변하면 경고.
- 경고는 **승인을 막지 않는다**(구간별 자동 재생성 2회 후에도 남은 것만 `issues` 로 보인다).

## 추가 E. 시드 곡 분석 JSON 과 예시 시퀀스 JSON

저장소(git)에는 올리지 않는다(`seed-analysis/` 는 gitignore). **파일로 따로 전달한다.**

### E-1. 분석 결과 (`/analyze` 와 같은 모양)

위치: 저장소 루트 `seed-analysis/<곡 이름>.json`. 만드는 명령(오프라인, Gemini 안 씀):

```bash
uv run python -m stage_director.analysis.seed demo-tracks/*.mp3 --out seed-analysis
```

```json
{ "fileName": "Burn-it-up.mp3", "fileHash": "<sha256>", "durationSec": 173.819, "analysis": { "durationSec": 173.819, "bpm": 99.38, "beatsSec": [], "energyCurve": [], "onsetDensity": [] } }
```

`audio_tracks` 에는 `fileHash → file_hash`, `durationSec → duration_sec`, `analysis → analysis` 로 넣고, `analysis_status = done`, `user_id = NULL`.
현재 만들어 둔 곡: `Small-universe`(160.5초, BPM 86.13, 구간 6), `Burn-it-up`(173.8초, BPM 99.38, 구간 4), `A_Room_Without_Noise`(184.4초, BPM 89.1, 구간 4). (`demo-tracks/` 에는 길이 시험용 `long-test.mp3`(295초)도 있지만 시드 곡이 아니라 제외했다. 같은 곡의 예전 이름 파일 `burn it up.json`, `나만의_작은_우주.json` 은 이름만 다른 이전 산출물이다.)

### E-2. 승인까지 끝난 예시 시퀀스 (`/runs` 의 `done` 결과와 같은 모양)

위치: `seed-analysis/<곡 이름>.sequence.json`. 만드는 스크립트 `scripts/make_example_sequence.py`:

```bash
uv run python scripts/make_example_sequence.py                    # 기본: 규칙 기반, Gemini 안 씀, 쿼터 0
uv run python scripts/make_example_sequence.py --mode gemini     # 실제 그래프(구간 확인→제안→승인)를 로컬에서 돌림. 곡당 Gemini 5~9회
uv run python scripts/make_example_sequence.py "demo-tracks/Burn-it-up.mp3" --out some-dir
```

```json
{ "fileName": "Burn-it-up.mp3", "fileHash": "…", "durationSec": 173.819, "artistSlug": "aurora",
  "generatedBy": "rule-based example (no LLM)",
  "result": { "sections": [ … ], "items": [ … ], "issues": [ … ] } }
```

- `result` 가 `GET /runs/{id}` 의 `done` 응답의 `result` 와 **같은 모양**이라 화면 개발과 "시드 곡 캐시된 예시 시퀀스 폴백"에 그대로 쓴다. `items` 는 §4.5 의 불변식을 통과한다(스크립트가 확인).
- **`generatedBy`: `"rule-based example (no LLM)"` 는 에너지 비로 밝기를 정한 개발용 예시**이고 Gemini 가 만든 연출이 아니다(rationale 끝에 "(규칙 기반 예시)"가 붙는다). 실제 모델 결과가 필요하면 `--mode gemini`(무드 해석 포함)로 만든다.
- 구간 수·라벨은 §추가 B, 밝기 규칙은 §추가 D 와 같다. 예: `Burn-it-up` 은 `intro·chorus·verse·outro` 4구간, 잔잔한 `verse` 는 가운데 스팟만 켜고 밝기 390(≤ 500).
- 아티스트는 `aurora`(`#9F77DD`)로 고정이다. 다른 아티스트로 보려면 스크립트의 `FIXTURE` 아티스트를 바꾼다.

## 추가 F. 이 부록이 바꾸지 않은 것

- API 계약(§3~§5)은 그대로다. 에러 코드·상태값·제한도 같다.
- 이 부록의 추가 C(`transitionMs` 방향)는 **스펙에 글로 없던 해석을 확정**한 것이다. on-stage 가 다르게 구현해야 한다면 알려 달라.
