# 배포 후 확인 — 직접 실행하는 명령 모음

대상: `https://stage-director-agent-815384828055.asia-southeast1.run.app`
기준: 요약 plan(`docs/superpowers/plans/stage-director-agent-realtime-deploy.md`) Task 9 Step 7의 확인 12항목. zsh와 bash 모두에서 동작한다.
각 항목은 **명령 → 기대 결과** 순서이고, curl로 안 되는 항목(DB·gcloud·콘솔)은 그렇다고 표시했다.

> 이 점검은 **운영 서비스와 운영 DB(Neon)를 실제로 쓴다.** 3·4·5·6번은 Gemini를 실제로 호출해 쿼터를 쓰고, 5번은 운영 `jobs`·체크포인트 행을 만든다(`check-` 로 시작하는 이름이라 구별되고, 방치된 초안은 7일 뒤 보존 정책이 지운다).

## 0. 준비 — 한 번만 (시크릿은 화면에 나오지 않는다)

```bash
URL=https://stage-director-agent-815384828055.asia-southeast1.run.app

# 키는 입력할 때 화면에 보이지 않고, 셸 기록에도 남지 않는다 (bash/zsh 공용)
printf 'INTERNAL_API_KEY: '; read -rs KEY; echo

# 키를 헤더로 붙여 주는 함수. 이 함수는 키를 출력하지 않는다
api() { curl -sS -H "X-Internal-Key: $KEY" -H "Content-Type: application/json" "$@"; }

# 아래 예시는 jq 로 JSON 을 다룬다 (없으면 `brew install jq`)
```

지켜야 할 것:
- `echo "$KEY"`, `set -x`, `curl -v`(요청 헤더를 출력한다)를 쓰지 않는다.
- 끝나면 `unset KEY AUDIO` 로 지운다.
- 서명 URL에 든 `token=` 값도 비밀에 가깝다. 채팅·이슈·화면 공유에 그대로 붙여 넣지 않는다.
- 아래 `gcloud` 출력에는 시크릿 **값**이 나오지 않는다(이름 참조만 나온다).

## 1. 상태 확인과 인증

```bash
# (a) 키 없이 /health, /ready — 둘 다 {"status":"ok"} 만 나와야 한다
curl -sS -w '\nHTTP %{http_code}\n' $URL/health
curl -sS -w '\nHTTP %{http_code}\n' $URL/ready

# (b) 키 없이 보호된 주소 → 401, 틀린 키 → 401
curl -sS -w '\nHTTP %{http_code}\n' $URL/runs/x
curl -sS -w '\nHTTP %{http_code}\n' -H "X-Internal-Key: wrong" $URL/runs/x

# (c) 올바른 키 → 인증은 통과하고 "모르는 스레드" 404
api -w '\nHTTP %{http_code}\n' $URL/runs/does-not-exist

# (d) probe 가 /health 로 들어갔는지 (gcloud)
gcloud run services describe stage-director-agent --region asia-southeast1 | grep -iE -B1 -A6 "startupProbe|livenessProbe"
```

기대 결과:
- (a) `{"status":"ok"}` / `HTTP 200` 두 번. `/ready` 가 `503 {"status":"db_unavailable"}` 이면 Neon 연결(DATABASE_URL 시크릿)을 의심한다.
- (b) `{"detail":"unauthorized"}` / `HTTP 401` 두 번.
- (c) `{"detail":"thread_not_found"}` / `HTTP 404`.
- (d) 두 probe 의 `path: /health` 가 보인다.

## 2. Neon `jobs` 표 (curl 아님 — Neon 콘솔 SQL 편집기)

서비스는 시작할 때 `jobs` 표에 `kind`·`progress` 칸을 더한다. Neon 콘솔 → 운영 브랜치 → SQL Editor:

```sql
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name = 'jobs' ORDER BY ordinal_position;

SELECT kind, status, count(*) FROM jobs GROUP BY kind, status;
```

기대 결과: 칸 7개 `id, status, result, error, updated_at, kind, progress`. 4단계에서 만든 기존 행이 있었다면 개수가 그대로이고 `kind` 는 전부 `graph`.
curl로 간접 확인하려면, 4단계에서 만든 `threadId` 를 알 때 `api $URL/runs/<그 threadId>` 가 404가 아닌 응답을 주는지 본다.

## 3. 분석 `/analyze` — 음원 URL 준비가 핵심

### 음원 URL은 어떻게 준비하나

서버가 **인터넷에서 직접 내려받는다.** 내 컴퓨터의 `demo-tracks/` 경로는 쓸 수 없다. 서버 설정 `AUDIO_URL_ALLOWED_HOSTS=htmfbhgjxgxbhuujfvwm.supabase.co` 때문에 다음 조건을 **모두** 지켜야 한다. 하나라도 어기면 큐에 넣기 전에 **422 `invalid_audio_url`** 이다.

| 조건 | 이유 |
| --- | --- |
| `https://` 이고 포트 443 | `http://`, 다른 포트 거절 |
| 호스트가 `htmfbhgjxgxbhuujfvwm.supabase.co` (또는 그 하위 도메인) | 허용 목록. 다른 사이트(예: `example.com`)는 거절 |
| 사용자 정보 없음(`user:pw@`), 공인 IP로 풀림 | SSRF 방어 |
| 길이 2048자 이하 | 요청 모델 제한(서명 URL 은 토큰 때문에 길다) |
| 파일 30MiB 이하, 길이 300초 이하, mp3/wav/flac/ogg | 분석 한도. m4a/aac 는 `decode_failed` |

준비 방법(Supabase 대시보드):
1. Storage → 버킷(비공개여도 됨)에 `demo-tracks/` 의 mp3 하나를 올린다(4MB 안팎의 곡이 적당하다).
2. 올린 파일에서 **Create signed URL** 로 유효기간 1시간짜리 URL 을 만든다.
   형태: `https://htmfbhgjxgxbhuujfvwm.supabase.co/storage/v1/object/sign/<버킷>/<파일>?token=…`
   (공개 버킷이면 `…/object/public/<버킷>/<파일>` 도 같은 호스트라 된다.)
3. 화면에 보이지 않게 변수에 담는다:

```bash
printf 'AUDIO signed URL: '; read -rs AUDIO; echo
```

### 요청 본문과 확인

본문은 `{"jobId": "…", "audioUrl": "…"}` 두 필드다. `jobId` 는 1~64자 아무 문자열이다(실제 서비스에서는 `audio_tracks.id`).

```bash
JOB=check-analyze-$(date +%s)

# 시작 → 202 와 {jobId,status:"queued"|"running",progress,result:null,error:null}
api -X POST $URL/analyze -w '\nHTTP %{http_code}\n' \
  -d "$(jq -n --arg id "$JOB" --arg u "$AUDIO" '{jobId:$id, audioUrl:$u}')"

# 폴링 (3초마다, done/error 면 멈춘다)
while :; do
  R=$(api $URL/analyze/$JOB); echo "$R" | jq -c '{status,progress,error}'
  case $(echo "$R" | jq -r .status) in done|error) break;; esac
  sleep 3
done

# 결과 요약 (done 일 때)
echo "$R" | jq '{fileHash: .result.fileHash, durationSec: .result.analysis.durationSec, bpm: .result.analysis.bpm, energyCount: (.result.analysis.energyCurve|length)}'
```

기대 결과:
- 첫 응답 `HTTP 202`. 폴링에서 `queued`(대기가 있을 때) → `running` 진행률 `0.1` → `0.4` → `done` 진행률 `1.0`.
- `done` 의 `fileHash` 는 64자리 16진수, `bpm`·`durationSec` 이 곡과 맞다(예: `burn it up.mp3` 면 약 173.8초, BPM 99.38). `energyCurve` 개수가 초 단위 길이와 비슷하다.
- 같은 `jobId` 로 한 번 더 POST → 다시 분석하지 않고 `done` 상태를 그대로 돌려준다(멱등).

실패 경로(각각 한 번씩):

```bash
# 허용되지 않은 호스트 → 422 {"detail":"invalid_audio_url", ...}
api -X POST $URL/analyze -w '\nHTTP %{http_code}\n' -d '{"jobId":"check-bad-host","audioUrl":"https://example.com/a.mp3"}'
# http 주소 → 422
api -X POST $URL/analyze -w '\nHTTP %{http_code}\n' -d '{"jobId":"check-http","audioUrl":"http://htmfbhgjxgxbhuujfvwm.supabase.co/a.mp3"}'
# 모르는 jobId → 404 {"detail":"job_not_found"}
api $URL/analyze/nope -w '\nHTTP %{http_code}\n'
```

만료된 서명 URL이나 없는 파일이면 큐에는 들어가지만 `error` 로 끝나고 `error` 값이 `audio_unavailable` 이다(서버가 받지 못함).

## 4. 새 리비전 도중 확인 — 분석 + 그래프 + 초안 유지 (4번과 5번의 "도중 새 리비전"을 한 번에)

재배포(빌드 포함)를 **먼저** 끝낸 뒤에 한다. 이 장은 새 리비전을 **빌드 없이 환경변수만 바꿔서** 만든다(20~40초). 필요한 것: 0장의 `URL`·`KEY`·`api`, 3장의 `AUDIO`와 **`done` 이 된 분석의 `JOB`**. 저장소 루트에서 실행한다.

왜 이렇게 하나: 분석은 몇 초면 끝나서 새 인스턴스가 켜지는 순간에 겹치기 어렵다. 그래프의 제안 단계는 무료 키(분당 4회)에서 1분 안팎 걸려 확실히 겹친다. 그래서 **긴 그래프 작업을 걸어 두고** 그 사이에 새 리비전을 만들고, 분석은 여러 건을 줄 세워 같이 본다.

**Step 1 — 끊기지 않아야 할 초안 만들기** (구간 확인 화면까지만 진행해 둔다)

```bash
# 3장의 분석 결과로 진짜 곡 길이의 요청 본문을 만든다 (구간 4개 안팎)
ANALYSIS=$(api $URL/analyze/$JOB | jq -c .result.analysis)
CTX=$(jq -c --argjson a "$ANALYSIS" --arg u "$AUDIO" '. + {analysis:$a, durationSec:$a.durationSec, audioUrl:$u}' tests/fixtures/sequence_request.json)

KEEP=check-keep-$(date +%s)
api -X POST $URL/runs -d "$(jq -n --arg t "$KEEP" --argjson c "$CTX" '{threadId:$t, context:$c}')" | jq -c '{status}'
while :; do R=$(api $URL/runs/$KEEP); echo "$R" | jq -c '{status, kind: .interrupt.kind, id: .interrupt.interruptId}'
  [ "$(echo "$R" | jq -r .status)" = waiting_input ] && break; sleep 3; done
```

`id` 값(`…:0:confirm_sections`)을 기억해 둔다. 무드 해석(오디오 Gemini 호출)이 먼저 돌아서 수십 초 걸릴 수 있다.

**Step 2 — 끊길 작업 준비** (같은 방식으로 구간 확인 화면까지)

```bash
TID=check-rev-run-$(date +%s)
api -X POST $URL/runs -d "$(jq -n --arg t "$TID" --argjson c "$CTX" '{threadId:$t, context:$c}')" | jq -c '{status}'
while :; do R=$(api $URL/runs/$TID); echo "$R" | jq -c '{status, kind: .interrupt.kind}'
  [ "$(echo "$R" | jq -r .status)" = waiting_input ] && break; sleep 3; done
```

**Step 3~5 — 한 번에 붙여 넣는다** (분석 3건을 줄 세우고 → 제안 단계를 시작하고 → 곧바로 새 리비전을 만든다)

```bash
A1=check-rev-a1-$(date +%s); A2=check-rev-a2-$(date +%s); A3=check-rev-a3-$(date +%s)
for A in $A1 $A2 $A3; do
  api -X POST $URL/analyze -d "$(jq -n --arg id "$A" --arg u "$AUDIO" '{jobId:$id, audioUrl:$u}')" | jq -c '{jobId,status}'
done
api -X POST $URL/runs/$TID/resume \
  -d "$(echo "$R" | jq -c '{interruptId: .interrupt.interruptId, kind:"sections", payload:{sections: .interrupt.sections}}')" | jq -c '{status}'
gcloud run services update stage-director-agent --region asia-southeast1 --update-env-vars REVISION_BUMP=$(date +%s)
```

gcloud 가 끝나면(20~40초) 바로 아래로 상태를 본다.

**Step 6 — 상태 보기** (5초 간격, 약 2분)

```bash
for i in $(seq 1 24); do
  echo "── $(date +%H:%M:%S)"
  echo "  그래프:   $(api $URL/runs/$TID | jq -c '{status,error}')"
  for A in $A1 $A2 $A3; do echo "  분석 ${A##*-}: $(api $URL/analyze/$A | jq -c '{status,progress,error}')"; done
  echo "  유지용:   $(api $URL/runs/$KEEP | jq -c '{status, id: .interrupt.interruptId}')"
  sleep 5
done
```

기대 결과:
- **유지용 초안(`KEEP`)**: 끝까지 `waiting_input` 이고 `id` 가 Step 1 과 **똑같다.** 배포·새 리비전에도 사람이 응답하던 초안은 남는다는 가장 중요한 확인이다.
- **그래프(`TID`)**: `error` / `interrupted`. 새 인스턴스가 켜질 때 돌고 있던 작업이 정리된다.
- **분석 3건**: 먼저 끝난 것은 `done`, 새 인스턴스가 켜질 때 `queued`·`running` 이던 것은 `error` / `interrupted`.
- 전부 `done` 이거나 `running` 이었다면 타이밍이 어긋난 것이다(검증 불가). Step 2부터 다시 한다.

**Step 7 — 복구 확인**

```bash
# (a) 그래프: 같은 threadId 로 다시 POST → 마지막 체크포인트에서 이어져 waiting_input(review) 까지
api -X POST $URL/runs -d "$(jq -n --arg t "$TID" --argjson c "$CTX" '{threadId:$t, context:$c}')" | jq -c '{status}'
while :; do R=$(api $URL/runs/$TID); echo "$R" | jq -c '{status, kind: .interrupt.kind, error}'
  case $(echo "$R" | jq -r .status) in waiting_input|done|error) break;; esac; sleep 3; done

# (b) 분석: interrupted 였던 jobId 하나(여기서는 A3)를 같은 id 로 다시 POST → 처음부터 다시 done
api -X POST $URL/analyze -d "$(jq -n --arg id "$A3" --arg u "$AUDIO" '{jobId:$id, audioUrl:$u}')" | jq -c '{status}'
while :; do R=$(api $URL/analyze/$A3); echo "$R" | jq -c '{status,progress,error}'
  case $(echo "$R" | jq -r .status) in done|error) break;; esac; sleep 3; done
```

기대 결과: (a) `queued → running → waiting_input(kind review)`. "이미 끝난 구간을 다시 호출하지 않는다"는 앱 로그에 호출 단위 기록이 없어서 로그로는 못 센다. Google AI Studio 의 요청 수 그래프로 보거나, 재시작 후 걸리는 시간이 처음 제안 단계보다 눈에 띄게 짧은지로 본다. (b) 처음부터 다시 분석해 `done`.
정리: 이 장에서 만든 `check-` 작업은 직접 지우지 않는다(보존 정책이 정리한다). 끝나면 `unset ANALYSIS CTX`.

## 5. 그래프 `/runs` — 구간 확인 → 제안 → 승인, 그리고 도중 새 리비전

`POST /runs` 본문은 `{"threadId": "…", "context": {…}}` 이고 `context` 는 `track`, `artist`, `presets`, `analysis`, `durationSec` (그리고 선택적으로 `audioUrl`) 이다. 저장소의 테스트 입력을 그대로 쓸 수 있다(60초짜리 가짜 곡, 구간 2개). **저장소 루트에서 실행한다.**

```bash
TID=check-run-$(date +%s)

# 시작 → 202, status "waiting_input", interrupt.kind "confirm_sections" (구간 확인 대기)
api -X POST $URL/runs -w '\nHTTP %{http_code}\n' \
  -d "$(jq -n --arg t "$TID" --slurpfile c tests/fixtures/sequence_request.json '{threadId:$t, context:$c[0]}')" \
  | jq -c '{status, kind: .interrupt.kind, sections: (.interrupt.sections|length)}'
```

(`context` 에 `audioUrl` 을 넣고 싶으면 `.context.audioUrl = "<서명 URL>"` 로 더한다 — 넣으면 무드 해석에도 Gemini 를 쓴다. 안 넣으면 무드는 건너뛴다.)

구간 확인을 그대로 승인하고(= 제안 단계 시작) 결과를 기다린다:

```bash
R=$(api $URL/runs/$TID)
api -X POST $URL/runs/$TID/resume -w '\nHTTP %{http_code}\n' \
  -d "$(echo "$R" | jq -c '{interruptId: .interrupt.interruptId, kind:"sections", payload:{sections: .interrupt.sections}}')" | jq -c '{status}'

# 폴링: queued → running → waiting_input(리뷰). 모델 호출이 있어 수십 초 걸릴 수 있다
while :; do R=$(api $URL/runs/$TID); echo "$R" | jq -c '{status, kind: .interrupt.kind, error}'
  case $(echo "$R" | jq -r .status) in waiting_input|done|error) break;; esac; sleep 3; done

# 리뷰 화면에서 전체 승인 → done, 결과 items
api -X POST $URL/runs/$TID/resume \
  -d "$(echo "$R" | jq -c '{interruptId: .interrupt.interruptId, kind:"approve"}')" | jq -c '{status}'
api $URL/runs/$TID | jq '{status, sectionCount: (.result.items|length), issueCount: (.result.issues|length)}'
```

기대 결과: `202` → `waiting_input`(confirm_sections) → resume `202` → `queued/running` → `waiting_input`(kind `review`, items 있음) → approve `202` → `done`, `items` 가 구간 수(2)만큼, 각 항목의 `state` 가 StageState 모양.
낡은 interruptId 로 resume → `409 stale_interrupt`, 이미 처리된 뒤 같은 resume 을 다시 보내면 `409 not_waiting_input` 이다.

**도중 새 리비전**(끊김과 이어서 하기)은 위 **4장**에서 분석·초안 유지 확인과 함께 한다.

## 6. 대기열 (분석 풀 = 동시 1건)

```bash
A=check-q-a-$(date +%s); B=check-q-b-$(date +%s)
api -X POST $URL/analyze -d "$(jq -n --arg id "$A" --arg u "$AUDIO" '{jobId:$id, audioUrl:$u}')" | jq -c '{jobId,status}'
api -X POST $URL/analyze -d "$(jq -n --arg id "$B" --arg u "$AUDIO" '{jobId:$id, audioUrl:$u}')" | jq -c '{jobId,status}'
for i in $(seq 1 12); do echo "A: $(api $URL/analyze/$A | jq -c '{status,progress}')   B: $(api $URL/analyze/$B | jq -c '{status,progress}')"; sleep 3; done
```

기대 결과: B의 첫 응답이 `queued`(진행률 null), A가 끝나는 순간 B가 `running` 으로 바뀌고 이어서 `done`.

## 7. 유휴 후 0대로 줄고 첫 요청에 깨어남 (콜드 스타트 측정)

1. 요청을 보내지 않고 15분쯤 기다린다.
2. Cloud Run 콘솔 → 서비스 → **Metrics** 의 *Container instance count* 가 0 으로 내려갔는지 본다.
3. 그 뒤 첫 요청의 시간을 잰다:

```bash
curl -sS -o /dev/null -w "콜드 스타트: 총 %{time_total}s (연결 %{time_connect}s, 첫 바이트 %{time_starttransfer}s) HTTP %{http_code}\n" $URL/health
curl -sS -o /dev/null -w "다음 요청: 총 %{time_total}s\n" $URL/health
```

기대 결과: 첫 요청이 둘째보다 확연히 길다(수 초~수십 초 — 이 값을 `docs/deploy.md` "비용과 콜드 스타트" 에 기록한다). 발표 당일용 명령 확인:

```bash
gcloud run services update stage-director-agent --region asia-southeast1 --min-instances 1   # 켜 두기
gcloud run services update stage-director-agent --region asia-southeast1 --min-instances 0   # 반드시 되돌리기
```

## 8. 메모리 부족 종료가 없는지

```bash
gcloud run services logs read stage-director-agent --region asia-southeast1 --limit 200 | grep -iE "memory limit|exceeded" || echo "메모리 관련 종료 로그 없음"
```

기대 결과: `메모리 관련 종료 로그 없음`. 3·4번 분석 직후에 돌린다. 콘솔 Metrics 의 *Memory utilization* 이 100% 에 붙지 않는지도 본다. 나오면 `--memory 4Gi`.

## 9. 루트와 문서 주소가 정보를 노출하지 않는지

```bash
curl -sS -w '\nHTTP %{http_code}\n' $URL/
curl -sS -o /dev/null -w '/docs → HTTP %{http_code}\n' $URL/docs
curl -sS -o /dev/null -w '/openapi.json → HTTP %{http_code}\n' $URL/openapi.json
```

기대 결과: 세 곳 모두 `404` 이고 본문은 `{"detail":"Not Found"}` 정도로 내부 정보(경로 목록, 버전, 스택)가 없다.

## 10. 예산 알림과 최대 인스턴스 (gcloud)

```bash
gcloud billing budgets list --billing-account=BILLING_ACCOUNT_ID
gcloud run services describe stage-director-agent --region asia-southeast1 --format=yaml | grep -iE "maxScale|minScale|cpu-throttling|memory|cpu:"
```

기대 결과: 월 $5 예산이 목록에 있고, `autoscaling.knative.dev/maxScale: '1'`, `minScale: '0'`, `run.googleapis.com/cpu-throttling: 'false'`, 메모리 `2Gi`, CPU `1`. (`BILLING_ACCOUNT_ID` 는 본인 값, 콘솔 결제 화면에서 확인한다.)

## 11. Neon 왕복 시간

```bash
curl -sS -o /dev/null $URL/ready        # 인스턴스와 Neon 을 먼저 깨운다(이 첫 요청은 재지 않는다)
for i in 1 2 3 4 5 6 7 8 9 10; do
  echo "health $(curl -sS -o /dev/null -w '%{time_total}' $URL/health)  ready $(curl -sS -o /dev/null -w '%{time_total}' $URL/ready)"
done
```

기대 결과: `ready` 가 `health` 보다 대체로 조금 크다. 그 **차이**가 대략 Neon 왕복 + 쿼리 한 번이다. 몇 십 ms 이하면 싱가포르 선택이 맞다. 0.3초를 넘으면 Neon 이 잠들어 있었는지(Neon 자동 일시정지 직후), 다른 리전에 있는지 확인한다. 이 숫자를 PR 설명에 적는다. 내 컴퓨터에서 재므로 인터넷 구간 시간이 두 값에 똑같이 들어가 차이만 의미가 있다.

## 12. 오래된 이미지 정리 (gcloud)

배포를 두세 번 한 뒤 한다. 먼저 **지금 쓰는 이미지가 무엇인지** 확인하고, 그것만 남긴다.

```bash
# 지금 서비스가 쓰는 이미지 (이것은 지우지 않는다)
gcloud run services describe stage-director-agent --region asia-southeast1 --format='value(spec.template.spec.containers[0].image)'

# 저장소에 있는 이미지 목록 (PROJECT_ID 는 본인 값)
gcloud artifacts docker images list asia-southeast1-docker.pkg.dev/PROJECT_ID/cloud-run-source-deploy --include-tags

# 쓰지 않는 이미지만 하나씩 삭제 (digest 를 위 목록에서 복사한다)
gcloud artifacts docker images delete asia-southeast1-docker.pkg.dev/PROJECT_ID/cloud-run-source-deploy/<이미지>@sha256:<쓰지 않는 digest> --delete-tags
```

기대 결과: 정리 후 목록에 **현재 서비스가 쓰는 이미지 하나만** 남는다. 삭제 명령의 문법은 쓰기 전에 `gcloud artifacts docker images delete --help` 로 확인한다. 현재 이미지를 지우면 다음 인스턴스 시작이 실패한다.

## 점검이 끝난 뒤

```bash
unset KEY AUDIO
```

- Supabase 에 올린 점검용 음원은 지운다. `check-` 로 시작하는 `jobs` 행과 체크포인트는 **손으로 지우지 않는다** — 보존 정책이 `jobs` 행을 보고 체크포인트를 지우므로, 행만 먼저 지우면 체크포인트가 고아로 남는다. 분석 행은 7일, 방치된 그래프 초안은 7일 뒤 함께 정리되고, 승인까지 끝낸 것은 체크포인트 24시간 뒤·행 7일 뒤에 정리된다.
- `--min-instances` 를 1 로 올렸다면 반드시 0 으로 되돌렸는지 확인한다.
