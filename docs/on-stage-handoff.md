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

`progress`(분석만): 0~1 실수 또는 `null`. **단계 단위**로만 움직인다(시작 0.1 → 내려받음 0.4 → 끝 1.0). 퍼센트 바로 쓰면 오래 멈춘 것처럼 보이니 `status` 문구(대기 중/분석 중)와 함께 쓴다.

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
