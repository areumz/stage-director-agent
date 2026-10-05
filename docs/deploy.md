# 배포 가이드 (Google Cloud Run)

Python 서비스(FastAPI + LangGraph + librosa) 한 대를 Cloud Run 에 올린다. 전용 Postgres 는 Neon 을 그대로 쓴다.
배포는 `gcloud run deploy --source .` 한 줄이다: 저장소의 `Dockerfile` 로 Cloud Build 가 이미지를 만들고 바로 배포한다
(이미지를 직접 푸시하는 방식보다 단계가 적다. 이미지를 직접 관리해야 하면 `docker build` → Artifact Registry 푸시 → `--image` 로 바꾼다).

## 지켜야 할 조건

| 조건 | 이유 | 설정 |
| --- | --- | --- |
| **`--no-cpu-throttling`**(CPU 항상 할당) | 기본값은 요청을 처리하는 동안에만 CPU 를 준다. 그러면 요청이 끝난 뒤 백그라운드 스레드(그래프·분석)가 멈춘다 | `--no-cpu-throttling` |
| 최대 인스턴스 1대 | RPM 제한·resume 락이 프로세스 단위다. 요금이 폭주하는 것도 막는다 | `--max-instances 1` |
| 워커 프로세스 1개 | 위와 같은 이유 | `Dockerfile` 의 `--workers 1` |
| 메모리 2GiB, CPU 1 | librosa 분석 한 건이 수백 MB | `--memory 2Gi --cpu 1` |
| 리전 asia-southeast1(싱가포르) | Neon 과 같은 지역. 체크포인트 읽기·쓰기가 그래프 한 단계마다 일어난다 | `--region asia-southeast1` |
| 유휴 시 인스턴스 0대(자동 중지) | 비용 최소화. 대신 첫 요청이 콜드 스타트를 겪는다(아래 "비용과 콜드 스타트") | `--min-instances 0` |
| `$PORT` 로 뜸 | Cloud Run 이 정해 주는 포트(기본 8080)로 서버가 떠야 한다 | `Dockerfile` 의 `--port ${PORT:-8080}` |

## 환경변수와 시크릿

| 이름 | 종류 | 값 |
| --- | --- | --- |
| `INTERNAL_API_KEY` | **Secret Manager** | Next.js 가 `X-Internal-Key` 로 보내는 값과 같아야 한다 |
| `GEMINI_API_KEY` | **Secret Manager** | Gemini API 키 |
| `DATABASE_URL` | **Secret Manager** | Neon 연결 문자열(`sslmode=require` 포함) |
| `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL` | 일반 | `--set-env-vars` |
| `GEMINI_RPM` | 일반 | 모델당 분당 요청 수. 실제 계정 한도에 맞춘다 |
| `AUDIO_URL_ALLOWED_HOSTS` | 일반 | Supabase 프로젝트 호스트. 비워 두고 배포하지 않는다 |

시크릿은 이미지·저장소에 넣지 않고 `--set-secrets` 로만 넣는다. `.env` 는 `.dockerignore` 로 이미지에서 빠진다.

## 처음 배포

아래 `PROJECT_ID`·`BILLING_ACCOUNT_ID`(형식 `XXXXXX-XXXXXX-XXXXXX`)는 본인 값으로 바꾼다. 음원 호스트는 이 프로젝트의 Supabase 호스트 `htmfbhgjxgxbhuujfvwm.supabase.co` 로 채워 두었다.

### 1. GCP 프로젝트와 결제

```bash
gcloud auth login
gcloud projects create PROJECT_ID --name="stage-director-agent"
gcloud config set project PROJECT_ID
gcloud billing projects link PROJECT_ID --billing-account=BILLING_ACCOUNT_ID   # 결제 계정 연결: 이것이 없으면 Cloud Run 을 못 쓴다
```

### 2. API 활성화

```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com billingbudgets.googleapis.com
```

### 3. 월 $5 예산 알림 (먼저 만든다)

```bash
gcloud billing budgets create --billing-account=BILLING_ACCOUNT_ID \
  --display-name="stage-director-agent 월 5달러" --budget-amount=5USD \
  --threshold-rule=percent=0.5 --threshold-rule=percent=1.0
```

예산 알림은 **메일로 알려 줄 뿐 요금을 막지 않는다.** 요금 폭주를 실제로 막는 것은 `--max-instances 1` 이다.

### 4. 시크릿 등록 (Secret Manager)

```bash
for NAME in INTERNAL_API_KEY GEMINI_API_KEY DATABASE_URL; do
  read -rs -p "$NAME: " VALUE; echo
  printf %s "$VALUE" | gcloud secrets create "$NAME" --data-file=-
done

# Cloud Run 이 쓰는 서비스 계정(기본: Compute 기본 계정)에 읽기 권한을 준다
PROJECT_NUMBER=$(gcloud projects describe PROJECT_ID --format='value(projectNumber)')
for NAME in INTERNAL_API_KEY GEMINI_API_KEY DATABASE_URL; do
  gcloud secrets add-iam-policy-binding "$NAME" \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor
done
```

### 5. 배포

```bash
gcloud run deploy stage-director-agent \
  --source . --region asia-southeast1 \
  --cpu 1 --memory 2Gi --no-cpu-throttling \
  --min-instances 0 --max-instances 1 \
  --port 8080 --allow-unauthenticated \
  --set-env-vars GEMINI_MODEL=gemini-3.8-flash,GEMINI_FALLBACK_MODEL=gemini-3.6-flash,GEMINI_RPM=10,AUDIO_URL_ALLOWED_HOSTS=htmfbhgjxgxbhuujfvwm.supabase.co \
  --set-secrets INTERNAL_API_KEY=INTERNAL_API_KEY:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,DATABASE_URL=DATABASE_URL:latest \
  --startup-probe httpGet.path=/health,httpGet.port=8080,periodSeconds=5,failureThreshold=12 \
  --liveness-probe httpGet.path=/health,httpGet.port=8080,periodSeconds=30
```

- `--allow-unauthenticated` 인 이유: Next.js(Vercel)가 인터넷으로 부르므로 Cloud Run IAM 인증을 쓰지 않고, 앱이 `X-Internal-Key` 로 인증한다.
- 상태 확인은 `/health`(프로세스가 요청을 받는지만 본다). `/ready`(DB 확인)는 사람·모니터링용이다. DB 가 느려졌다고 플랫폼이 인스턴스를 내리면 장애가 커진다.
- `--startup-probe`·`--liveness-probe` 문법은 gcloud 버전에 따라 다를 수 있다. 오류가 나면 `gcloud run deploy --help` 로 확인하고, 배포 뒤 `gcloud run services describe stage-director-agent --region asia-southeast1` 의 출력에 두 probe 가 `/health` 로 들어갔는지 본다.

### 6. 확인

```bash
URL=$(gcloud run services describe stage-director-agent --region asia-southeast1 --format='value(status.url)')
curl -s $URL/health
curl -s $URL/ready
gcloud run services logs read stage-director-agent --region asia-southeast1 --limit 50   # "Gemini 모델 … (예비 …), 모델당 분당 N회" 한 줄
```

배포 후 오래된 이미지를 정리한다(Artifact Registry 의 `cloud-run-source-deploy` 저장소에서 현재 리비전이 쓰는 이미지 외의 것을 삭제).

## 설정 바꾸기

```bash
# 환경변수 한 개 바꾸기(새 리비전이 만들어진다)
gcloud run services update stage-director-agent --region asia-southeast1 --update-env-vars GEMINI_RPM=30
# 시크릿 값 교체: 새 버전을 추가하면 :latest 를 쓰는 서비스는 다음 리비전부터 새 값을 쓴다
printf %s "$NEW_VALUE" | gcloud secrets versions add INTERNAL_API_KEY --data-file=-
gcloud run services update stage-director-agent --region asia-southeast1 --update-env-vars SECRETS_BUMP=$(date +%s)
```

`INTERNAL_API_KEY` 는 Next.js 쪽 환경변수와 거의 동시에 바꾼다. 그 사이 요청은 401 이 나므로 사용량이 적은 때에 하고, 교체 후 `/runs` 한 건으로 확인한다.

## 리전 선택 (asia-southeast1 싱가포르)

[Cloud Run 가격 페이지](https://cloud.google.com/run/pricing)를 2026-10-05 에 확인한 값이다. 요금은 바뀔 수 있으니 배포 전에 다시 본다.

| | 싱가포르 `asia-southeast1` | 서울 `asia-northeast3` | 도쿄 `asia-northeast1` |
| --- | --- | --- | --- |
| 가격 등급 | Tier 2 | Tier 2 | **Tier 1** |
| CPU(vCPU-초) / 메모리(GiB-초), 인스턴스 기준 | $0.0000216 / $0.0000024 | 같음 | $0.000018 / $0.000002 |
| 1vCPU·2GiB 시간당 | 약 $0.095 | 약 $0.095 | 약 $0.079 |
| 무료 한도로 쓸 수 있는 시간 | 약 55시간 | 약 55시간 | 약 66시간 |

**싱가포르로 정했다.** 전용 Postgres(Neon)에 서울 리전이 없어 가장 가까운 곳이 싱가포르이고, Next.js(Vercel)와 Supabase 는 도쿄다.

- Python 은 그래프 단계마다 체크포인트를 Neon 에 읽고 쓰고, 상태 조회(폴링) 한 번도 Neon 쿼리를 2~5회 부른다. Python↔Neon 이 같은 도시면 쿼리당 1~2ms, 도쿄↔싱가포르면 쿼리당 70ms 안팎(일반적인 추정치, 측정 전)이라 이 구간의 거리가 가장 많이 곱해진다.
- Vercel(도쿄)→Python(싱가포르)은 호출이 적고(요청 1회 + 몇 초 간격 폴링) 호출당 70ms 안팎만 붙는다. Supabase(도쿄)에서 음원을 받는 것도 작업당 1회다.
- 도쿄는 단가가 약 17% 싸지만 무료 한도 안에서는 차이가 없다. Neon 지연이 문제가 아닌 것으로 측정되면 도쿄를 대안으로 검토한다.
- 서울은 가격이 싱가포르와 같고 Neon 과의 거리만 멀어져 이점이 없다.

배포 뒤에 Neon 왕복 시간을 직접 잰다(Task 9). `/health`(DB 안 봄)와 `/ready`(DB 한 번 왕복)의 응답 시간 차이가 거의 Neon 왕복 + 쿼리 시간이다.

```bash
URL=$(gcloud run services describe stage-director-agent --region asia-southeast1 --format='value(status.url)')
curl -s -o /dev/null $URL/ready        # 인스턴스와 Neon 을 먼저 깨운다(첫 요청은 재지 않는다)
for i in 1 2 3 4 5 6 7 8 9 10; do
  echo "health $(curl -s -o /dev/null -w '%{time_total}' $URL/health)  ready $(curl -s -o /dev/null -w '%{time_total}' $URL/ready)"
done
```

## 비용과 콜드 스타트

우리 설정(`--no-cpu-throttling`)은 가격 페이지의 **인스턴스 기준 과금**이다. 인스턴스가 켜져 있는 시간 전체가 과금되고(인스턴스 하나당 최소 1분), 이 표에는 요청당 요금이 없다.

| 항목 | 값 |
| --- | --- |
| 무료 한도(인스턴스 기준) | 매달 CPU 240,000 vCPU-초 + 메모리 450,000 GiB-초. **Tier 1 가격 기준 금액 할인 $5.22/월**로 적용되고 결제 계정 전체에서 합산, 매달 초기화 |
| 싱가포르(Tier 2)에서 | 같은 $5.22 로 약 55시간(1vCPU·2GiB, 시간당 약 $0.095). 메모리를 1GiB 로 줄이면 약 60시간 |
| 초과 시 | 인스턴스가 켜져 있는 시간당 약 $0.095 |
| 평소 | `--min-instances 0` 이라 요청이 없으면 0대로 줄어 요금이 나가지 않는다 |
| 첫 요청 | 콜드 스타트(이미지가 크고 librosa 를 불러온다). 배포 직후와 유휴 뒤 첫 요청 시간을 재서 기록해 둔다 |

월 사용량 추정(가정: 한 번 접속에 인스턴스가 켜져 있는 시간 약 25분 = 작업·폴링 약 10분 + 마지막 요청 뒤 남아 있는 시간 약 15분. 이 가정은 가격 페이지에 근거가 없다):

| 시나리오 | 인스턴스 시간 | 요금(무료 한도 차감 전) | 실제 청구 |
| --- | --- | --- | --- |
| 평소 가볍게(월 20회 접속) | 약 8시간 | $0.79 | $0 |
| 면접 준비·지인 공유(월 60회) | 약 25시간 | $2.38 | $0 |
| 링크가 퍼진 경우(월 150회) | 약 62시간 | $5.94 | 약 $0.7 |
| 발표·면접 당일 `--min-instances 1` 8시간 | 8시간 | $0.76 | 무료 한도에 합산 |
| `--min-instances 1` 을 한 달 방치 | 730시간 | $69 | 약 $64 |

월 약 130회 접속(하루 4~5회)을 넘어야 무료 한도를 넘기 시작한다. `--max-instances 1` 이라 트래픽이 몰려도 시간당 요금은 약 $0.095 를 넘지 않는다.

이 페이지에 없는 비용(Cloud Build, Artifact Registry 저장, Secret Manager, 외부로 나가는 네트워크)은 별도 가격 페이지에서 확인한다. 포트폴리오 규모에서는 센트 단위로 예상하지만 이번에 확인하지 못했다. Gemini API 와 Neon 은 Cloud Run 과 별도 요금이다.

**발표·면접 당일에는 콜드 스타트를 없앤다.** 시작 몇 시간 전에 올리고, 끝나면 반드시 되돌린다.

```bash
gcloud run services update stage-director-agent --region asia-southeast1 --min-instances 1   # 켜 두기 (8시간 기준 약 $0.76)
gcloud run services update stage-director-agent --region asia-southeast1 --min-instances 0   # 끝난 뒤 되돌리기
```

`--min-instances 1` 을 되돌리지 않으면 하루 약 $2.28(24시간 × $0.095)이 계속 나간다. 예산 알림이 오면 먼저 이 값을 확인한다.

## 재시작·새 리비전 때 일어나는 일

- 인스턴스는 유휴 상태가 길어지거나 새 리비전을 배포하면 종료 신호(SIGTERM)를 받고 곧 내려간다. 이때 돌던 그래프·분석 작업은 사라진다.
- 새 인스턴스는 켜질 때 `queued`·`running` 으로 남은 작업을 모두 `error(interrupted)` 로 바꾼다. 새 리비전이 올라오는 동안 옛 인스턴스가 잠시 살아 있어도 그 인스턴스는 곧 내려가므로, 옛 인스턴스의 작업이 함께 중단 처리되는 것은 어차피 일어날 일을 조금 앞당길 뿐이다. 사용자가 "다시 시도"하면 그래프 작업은 마지막 체크포인트에서, 분석 작업은 처음부터 다시 돈다.
- 드물게, 롤아웃 중에 옛 인스턴스에서 만들어진 작업이 새 인스턴스의 시작 정리 이후·트래픽 전환 이전에 생기면, 옛 인스턴스가 내려간 뒤에도 `queued`/`running` 으로 남는다. 다음 인스턴스 시작 때(또는 보존 규칙 2 에 따라 7일 뒤) 정리된다. `running` 으로 보이는 동안은 "다시 시도"가 듣지 않으므로, 이런 작업은 한 번 재배포하거나 서비스를 재시작한다.
- `waiting_input`(사람 응답 대기) 작업은 DB 에만 있어서 영향이 없다.
- 보존 정책(`retention`)은 인스턴스가 켜질 때마다 한 번 돈다. 0대로 줄었다 켜지는 서비스에서는 이것이 사실상 정기 실행이다(실행 중 6시간 주기 실행은 켜져 있는 동안만 돈다).

## 음원 크기와 무드 해석

15MiB 를 넘는 음원은 무드 해석(Gemini 인라인 한도)을 건너뛰고 분석(30MiB 까지)만 한다. 3분 mp3 는 3~6MB 라 보통 해당하지 않는다.

## 로그에서 볼 것

- 기동 직후 `Gemini 모델 … (예비 …), 모델당 분당 N회` 한 줄로 실제 적용된 설정을 확인한다.
- `Gemini … 호출 실패, 예비 모델 … 로 다시 시도` 경고가 자주 보이면 주 모델이 혼잡한 것이다.
- `Memory limit of 2048 MiB exceeded` 같은 메모리 부족 종료가 분석·재시작 중에 없는지 본다: `gcloud run services logs read stage-director-agent --region asia-southeast1 --limit 200 | grep -iE "memory limit|exceeded"`. 나오면 `--memory 4Gi` 로 올린다.
