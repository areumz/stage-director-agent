# 무대 연출 디렉터 에이전트 — 실시간 분석 + 배포 구현 계획 (분석 작업 · 대기열 · RPM 제한 · SSRF · 보존 정책 · 배포)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> 이 문서는 태스크 순서, 검증 절차, 명령, 기대 결과를 담는다.

**Goal:** 업로드한 음원을 백그라운드에서 분석하는 작업(`/analyze`)과 시드 곡 오프라인 분석을 만들고, 스레드 풀이 찰 때의 대기열 상태·Gemini 분당 요청 수 제한·재시작으로 죽은 작업 정리·`jobs` 행 보존·`fetch_audio` SSRF 방어를 갖춘 뒤, Python 서비스를 컨테이너로 배포해 "배포 환경에서 업로드부터 저장까지 동작"(기획서 §10 5단계)하게 한다.

**Architecture:** 기존 `jobs` 테이블에 `kind`(graph/analysis)·`progress` 컬럼을 `ALTER TABLE … ADD COLUMN IF NOT EXISTS` 로 더하고(기존 DB 의 행은 보존), 상태에 `queued` 를 추가한다. 분석 작업은 그래프와 같은 `jobs`·같은 조건부 전이를 쓰되 별도 스레드 풀(동시 1건)에서 `AnalysisRunner` 가 돌린다. 죽은 작업은 별도 워커나 heartbeat 없이, 서비스가 시작될 때 `queued`·`running` 으로 남은 작업을 `error(interrupted)` 로 바꾸는 기존 방식(`fail_running`)을 `queued` 까지 넓혀 처리한다(인스턴스 1대 전제). Gemini 호출은 `GeminiClient` 안에서 모델마다 슬라이딩 윈도우 제한기를 거친다. 배포는 Dockerfile 을 Google Cloud Run(최대 1대, CPU 항상 할당, 유휴 시 0대로 자동 중지, 시크릿은 Secret Manager)에 올리고, 전용 Postgres 는 Neon 을 그대로 쓴다.

**Tech Stack:** Python 3.12, uv, FastAPI, LangGraph(변경 없음), psycopg 3 pool, librosa, google-genai, pytest, Docker, Google Cloud Run + Secret Manager + Cloud Build

## 이 계획의 범위

시작 상태: `origin/main`(PR #4 "사람 개입" 병합 후, Gemini 예비 모델 폴백 포함), 테스트 293개 통과(17개는 `llm`·`postgres` 마커로 제외).

| 포함 | 스펙 위치 |
| --- | --- |
| Task 1: `jobs` 에 `kind`·`progress` 컬럼과 `queued` 상태, ALTER 기반 마이그레이션, `fail_running` 이 `queued` 까지 정리 | §6.1 |
| Task 2: 스레드 풀이 찰 때 대기열 `queued` 표시, 대기 중 정리된 작업은 실행하지 않음 | §6.1, §8 |
| Task 3: Gemini 모델별 분당 요청 수 제한(RPM), 폴백 모델의 배포 환경 점검 | §8 |
| Task 4: `fetch_audio` SSRF 방어(허용 호스트·공인 IP 고정 연결·리다이렉트 금지) | §3, §4.1 |
| Task 5: 분석 작업 — `POST /analyze` · `GET /analyze/{jobId}`, 진행률 | §4.1 |
| Task 6: 보존 정책 확장(완료 행 7일 삭제, 분석 결과 7일 보관, 실행 중 주기 실행) | §6.4 |
| Task 7: 시드 곡 오프라인 분석 스크립트 | §4.1 5번 |
| Task 8: 상태 확인(`/healthz`·`/readyz`), Dockerfile(`$PORT`), Cloud Run 배포 가이드(`docs/deploy.md`: GCP 프로젝트·결제·API·Secret Manager·예산 알림·첫 배포·비용), 환경변수·시크릿 | §13, 기획서 §9 |
| Task 9: 통합 검증, 스펙 갱신(API 계약), 사람 단계(실제 배포·재시작·폴백·한도) | §9, §11 |

**이 계획 밖.** Next.js 라우트(`/api/audio-tracks*`, `/api/sequences*`)와 프론트 진행 상태 UI, on-stage 마이그레이션(`audio_tracks`, 음원 버킷, RLS)과 시드 곡을 DB 에 넣는 스크립트는 on-stage 저장소 몫이다. 이 계획은 on-stage 가 부를 API 계약(아래 표)만 정해 스펙에 반영한다.

## 확정한 결정 사항

| 항목 | 결정 | 이유 |
| --- | --- | --- |
| 호스팅 | **Google Cloud Run**, 리전 `asia-southeast1`(싱가포르), 1vCPU·메모리 2GiB. `--no-cpu-throttling`, `--min-instances 0`(유휴 시 자동 중지), `--max-instances 1`, `$PORT`(기본 8080)로 기동, 시크릿은 Secret Manager(`--set-secrets`), 상태 확인 `/healthz`(시작·생존)·`/readyz`(사람·모니터링용), 배포는 `gcloud run deploy --source .` | 비용 최소화 + GCP 경험. CPU 항상 할당이 아니면 요청 밖에서 도는 그래프·분석 스레드와 heartbeat 가 멈춘다. 최대 1대는 RPM 제한·resume 락이 프로세스 단위이기 때문이고 요금 폭주도 막는다. **리전:** Neon 에 서울 리전이 없어 가장 가까운 곳이 싱가포르이고, Python↔Neon 쿼리가 가장 많아 이 구간을 가깝게 둔다(Vercel·Supabase 는 도쿄. 도쿄는 Tier 1 이라 단가가 약 17% 싸지만 무료 한도 안에서는 차이가 없어 대안으로만 둔다). 요금은 2026-10-05 에 가격 페이지로 확인(서울·싱가포르 Tier 2, 시간당 약 $0.095, 무료 한도 $5.22/월 ≈ 55시간). 기획서 §9 의 "콜드 스타트 없는 플랜 우선" 을 비용 때문에 의도적으로 접는다 — 평소 첫 요청은 콜드 스타트를 겪고, 필요한 경우에만 `--min-instances 1` 로 올린다(`docs/deploy.md`) |
| 대기열 상태 알림 | `status` 값에 `queued` 를 추가한다(API 계약 변경). on-stage 는 `queued` 를 `running` 과 같은 진행 중으로 처리하고 "대기 중" 문구만 더하면 된다 | 한 곳(`status`)만 보면 되고 보존 규칙도 단순하다 |
| 죽은 작업 처리 | **heartbeat·별도 워커 없이 시작 시 정리.** 서비스가 시작될 때 `queued`·`running` 으로 남은 작업(그래프·분석)을 `error(interrupted)` 로 바꾼다(4단계의 `fail_running` 을 `queued` 까지 확장). 사용자가 "다시 시도"하면 그래프는 마지막 체크포인트에서, 분석은 처음부터 이어진다 | 인스턴스 1대에서는 이것으로 충분하다. 배포 때 옛 인스턴스가 잠시 살아 있어도 곧 내려가므로 그 작업이 함께 `interrupted` 로 정리되는 것은 어차피 일어날 일을 앞당길 뿐이고, 줄 서 있던 작업은 `queued → running` 전이가 실패해 실행되지 않는다. heartbeat(`owner`·`heartbeat_at` 컬럼, 주기 스레드, 45초 복구 지연)는 인스턴스가 여러 대가 될 때 필요하다 |
| `GEMINI_RPM` | 모델당 분당 10회로 시작하고 Task 9 에서 실제 계정 한도를 확인해 조정한다 | 실제 한도를 모르는 상태의 보수적 시작값 |
| `/propose` | 엔드포인트는 유지하되 스펙에 "배포 환경에서 Next.js 는 호출하지 않는다"고 적는다 | 동기 요청이라 최악 약 6분(아래 폴백 점검 표)이라 프록시·서버리스 시간 제한에 걸린다. 시퀀스 생성은 `/runs` 를 쓴다 |
| 스키마 변경 방식 | `CREATE TABLE IF NOT EXISTS`(4단계 모양) 뒤에 컬럼마다 `ALTER TABLE jobs ADD COLUMN IF NOT EXISTS …` 를 이어 실행. 서비스 시작 시, `pg_advisory_xact_lock` 으로 인스턴스 동시 기동을 직렬화. 버전 관리 테이블은 만들지 않는다 | 새 DB 와 기존 DB 가 같은 경로를 타서 분기가 없고, 기존 행이 보존된다. `jobs` 는 임시 데이터라 되돌림(down) 마이그레이션이 필요 없다 |
| 대기열 | 상태 `queued` 추가. 만들거나 재개할 때 `queued`, 스레드가 시작하는 순간 `running`. 전이가 실패하면(대기 중 다른 인스턴스의 시작 정리가 `error` 로 바꿈) 실행하지 않는다 | 풀이 찬 것을 사용자가 구별할 수 있고, 이미 정리된 작업이 늦게 깨어나 실행되는 일이 없다 |
| 분당 요청 수 제한 | **모델마다 슬라이딩 윈도우** `RateLimiter`, `GeminiClient` 안에서 요청 직전에 `acquire()`. 토큰 버킷이 아니다 | 서버 한도는 "어느 60초를 잘라도 N회 이하"라서, 버스트를 허용하는 토큰 버킷은 그 보장을 못 준다. Gemini 한도는 모델별이라 예비 모델도 따로 센다. 폴백·오디오 호출이 모두 이 한 곳을 지나간다 |
| 분석 풀 | 그래프 풀(동시 2건)과 **별도 풀, 동시 1건** | 분석 한 건이 수백 MB 를 쓰고, 오래 걸리는 분석이 그래프 작업을 굶기지 않게 한다 |
| 진행률 | 단계 단위(`0.1` 시작, `0.4` 내려받음, `1.0` 측정 끝). 퍼센트가 아니다 | librosa 가 중간 진행을 알려 주지 않는다. 진행 바가 멈춘 것처럼 보이면 `status` 로 판단한다 |
| SSRF 방어 | ① https·443·계정 정보 없음 ② 호스트 허용 목록(`AUDIO_URL_ALLOWED_HOSTS`) ③ 이름 풀이 결과가 **전부 공인 IP** 일 때만, **검증한 그 IP 로 직접 연결**(DNS rebinding 방지, TLS 는 원래 호스트 이름 기준) ④ 리다이렉트는 따라가지 않음 ⑤ 크기·전체 시간 상한 | 서명 URL 은 신뢰하는 Next.js 가 만들지만 배포 환경에서는 내부망·클라우드 메타데이터 주소(`169.254.169.254`)를 가리키는 URL 이 섞일 수 있다고 가정한다. 리다이렉트를 따라가면 허용 호스트가 내부 주소로 튕겨 낼 수 있다 |
| `jobs` 행 보존 | 승인(`done`)된 그래프 작업의 행은 7일 뒤 삭제(체크포인트는 24시간 규칙 그대로), 분석 작업은 마지막 갱신 후 7일 뒤 삭제 | 완료된 행이 영원히 쌓이고 보존 정책이 매번 같은 행을 훑는 문제(4단계 한계 표)를 푼다 |
| 보존 정책 실행 | 시작 시 + 실행 중 6시간마다(인-프로세스) + 기존 CLI | 오래 떠 있는 인스턴스는 시작 시에만 도는 정책으로는 정리되지 않는다 |
| 시드 곡 분석 | `python -m stage_director.analysis.seed` 가 곡마다 JSON 을 내보낸다(`seed-analysis/`, gitignore). 업로드 분석과 같은 `build_result` 를 쓴다 | Python 은 Supabase 에 쓰지 않는다(§3). 곡 약관 확인 전이라 결과 파일도 저장소에 올리지 않고 on-stage 에 파일로 넘긴다 |
| 상태 확인 | `/healthz`(키 없음, DB 안 봄) = Cloud Run 시작·생존 확인용, `/readyz`(키 없음, DB 한 번 왕복) = 사람·모니터링용 | DB 가 느려졌다고 플랫폼이 인스턴스를 내리면 장애가 커진다. 두 엔드포인트 모두 내부 정보를 노출하지 않는다 |
| 호스팅과 이 설계의 맞물림 | Cloud Run `--no-cpu-throttling` + 최대 1대 + 유휴 시 0대. 0대로 줄었다 켜지면 **시작 시 보존 정책**이 정기 실행 역할을 하고, 유휴 중에는 주기 작업이 돌지 않는다(맡은 작업이 없으니 문제없음). `waiting_input` 작업은 DB 에만 있어 0대가 돼도 안전하다 | CPU 항상 할당이 아니면 요청이 끝난 뒤 그래프·분석 스레드가 멈춘다. 최대 1대는 RPM 제한·resume 락이 프로세스 단위이기 때문이다 |

**실행 전에 on-stage 에서 받을 값.** ① Supabase 프로젝트 호스트(`abc.supabase.co`) — `AUDIO_URL_ALLOWED_HOSTS` 에 넣는다. 이 값이 없으면 **배포하지 않는다**(SSRF 방어가 공인 IP 검사만 남는다). ② 음원 버킷이 파일 크기 30MiB 이하·길이 180초 이하만 받는지 — on-stage 마이그레이션(스펙 §5)에서 맞춘다. (Task 4, 5, 8, 9)

## 추가 항목

| 발견 | 처리 |
| --- | --- |
| 서비스 시작 시 `fail_running()` 이 `running` 만 정리해, 풀 대기 중이던 `queued` 작업이 정리되지 않는다(`queued` 를 새로 도입하므로) | `fail_running` 이 `queued` 도 정리(Task 1). heartbeat 는 만들지 않는다(결정 사항) |
| 보존 정책이 서비스 시작 시에만 돈다. 항상 켜진 인스턴스는 재시작 전까지 정리되지 않는다 | 6시간 주기 실행(Task 6) |
| `purge` 가 `done` 행을 지우지 않아 매번 다시 훑는다(코드의 `ponytail:` 주석) | 규칙 3 으로 행 삭제(Task 6) |
| `logging` 설정이 없어 `INFO` 로그가 나오지 않는다. 배포 로그에서 예비 모델이 켜져 있는지 알 수 없다 | `create_app` 이 로그를 설정하고 기동 시 모델·RPM 을 한 줄 남긴다(Task 3) |
| `graph_nodes.py` 의 `MAX_CONCURRENT_PROPOSALS` 주석이 "RPM 제한은 없다"고 말한다 | 주석 갱신(Task 3) |
| `fetch_audio` 의 15MiB 상한은 무드 해석(Gemini 인라인 한도)용이다. 분석은 wav 도 받아야 한다 | `max_bytes` 인자 추가, 분석은 30MiB(Task 4, 5) |
| 이미지에는 개발 의존성·테스트·`.env`·`demo-tracks` 가 들어가면 안 되고, `contracts/` 패키지가 빠지면 기동에 실패한다 | `--no-editable` 설치와 `.dockerignore`, Task 8 에서 컨테이너 안 확인 |
| `/propose` 는 동기 요청이고 최악 약 6분이다 | 결정 사항의 `/propose` 행, 스펙 문구(Task 9) |
| `Dockerfile` 이 포트를 8080 으로 고정했다. Cloud Run 은 `$PORT` 를 넘긴다 | `--port ${PORT:-8080}`(Task 8, 컨테이너에서 `PORT=9000` 으로 확인) |
| 유휴 시 0대로 줄면 6시간 주기 보존 정책이 돌지 않고, 요청이 끊긴 채 돌던 작업은 인스턴스 회수로 사라질 수 있다 | 시작 시 보존 정책이 정기 실행 역할(스펙 §6.4), 사라진 작업은 다음 기동의 시작 정리 + 다시 시도(알려진 한계 표) |
| `GEMINI_RPM=""` 처럼 배포 환경이 빈 값을 넘길 수 있다 | 빈 값은 기본값, 숫자가 아니면 기동 실패(Task 3 `test_settings.py`) |

## Global Constraints

모든 태스크의 요구사항에 아래가 암묵적으로 포함된다. 값은 스펙과 선행 계획에서 그대로 옮겼다.

- Python 서비스는 Supabase 에 접근하지 않는다. 필요한 컨텍스트는 요청 본문으로 받는다 (§3).
- **`../on-stage`를 열지 않는다** (§12 제약 2). 필요한 모양은 모두 `contracts/`에 있다.
- Python 엔드포인트는 모두 `X-Internal-Key` 필수 (§4.2). **예외는 `/healthz`·`/readyz` 둘뿐**이며 `{"status": …}` 외에는 아무것도 노출하지 않는다.
- 결정적 로직은 **TDD로 처음부터** 작성한다: 실패하는 테스트 → 실패 확인 → 최소 구현 → 통과 확인 (§12 제약 5).
- `jobs` 전이는 반드시 조건부 UPDATE 로 한다 (§6.1). 분석 작업도 같은 규칙이다.
- 보존 정책 (§6.4): 승인된 스레드는 `done` 24시간 후 **체크포인트만**, 승인되지 않은 스레드는 `updated_at` 7일 방치 시 **체크포인트와 jobs 행 모두** 삭제. 이 계획은 여기에 규칙 3(완료 행 7일)·규칙 4(분석 7일)를 **더할 뿐** 앞의 두 규칙은 바꾸지 않는다.
- `audio_tracks.duration_sec` 최대 180 (§5) — 분석 작업이 이 상한을 강제한다.
- 전용 Postgres 는 Python 외 접근하지 않는다 (§6.1).
- 그래프(`graph.py`·`graph_nodes.py`)의 동작은 바꾸지 않는다. 이 계획은 그 위의 작업 실행·저장·호출 계층만 건드린다(`graph_nodes.py` 는 주석 한 군데).
- 시크릿(`INTERNAL_API_KEY`, `GEMINI_API_KEY`, `DATABASE_URL`)은 이미지·저장소에 넣지 않고 Secret Manager(`--set-secrets`)로만 넣는다.

## Review Focus

- **재시작·배포로 죽은 작업이 `queued`·`running` 으로 영원히 남지 않고, 다시 시도하면 이어진다. 줄 서 있던 작업이 정리된 뒤 늦게 깨어나도 실행되지 않는다** → Task 1(`test_fail_running_marks_queued_and_running_jobs_but_not_waiting_or_finished_ones`), Task 2(`test_a_run_killed_before_it_started_is_marked_interrupted_and_can_be_restarted`·`test_a_job_failed_while_queued_never_runs`), Task 5(분석의 같은 테스트)
- **음원 URL 이 내부 주소·메타데이터 주소·리다이렉트·느린 응답으로 서버를 속일 수 없다**(사설/루프백/링크로컬/CGNAT/IPv4 매핑 IPv6, 허용 목록 우회 `supabase.co.evil.com`, 계정 정보 URL, 비표준 포트, DNS 가 검증 뒤에 바뀌는 경우) → Task 4
- **분석할 수 없는 음원(텍스트를 `.mp3` 로 바꾼 파일, 빈 파일, 180초 초과, 30MiB 초과, 같은 `jobId` 재요청)이 서버를 죽이거나 `running` 으로 멈추지 않고 짧은 에러 코드로 끝난다** → Task 5(`test_build_result_rejects_text_pretending_to_be_an_mp3` 외)
- **완료된 작업의 행을 지운 뒤에도 승인본이 사라지지 않는다**: Python 이 404 를 내는 `threadId` 를 Next.js 가 410 으로 오해해 승인본 행을 지우는 일이 없어야 한다 → Task 6(`test_approved_job_rows_are_deleted_after_7_days_but_kept_before`), 계약은 Task 9 스펙 문구
- **기존 DB(4단계 스키마, 행이 있는 Neon)에서 기동해도 행이 보존되고, 마이그레이션을 반복·동시에 실행해도 깨지지 않는다** → Task 1(`test_migrate_upgrades_the_stage_4_table_without_losing_rows`·`test_two_instances_migrating_at_once_do_not_collide`, Postgres 마커)

## 폴백 모델 점검 결과

"폴백 로직이 배포 환경에서도 문제없는가"를 코드와 테스트로 점검한 표다. 고친 것은 Task 3 에 있다.

| 점검 항목 | 결과 | 처리 |
| --- | --- | --- |
| 폴백이 오디오 호출(무드 해석)에도 적용되는가 | 적용된다. 기존 `test_gemini_audio_calls_also_fall_back` 이 고정 | 변경 없음 |
| 분당 한도가 모델별인데 폴백 시도가 한도를 몰래 초과하지 않는가 | **초과할 수 있었다**(제한이 아예 없었다). 주 모델이 실패해 예비 모델로 넘어가는 시도도 각 모델의 한도를 써야 한다 | 모델별 `RateLimiter`, `test_each_model_gets_its_own_limiter_and_waits_before_every_request` (Task 3) |
| 배포 환경이 `GEMINI_FALLBACK_MODEL=""` 를 넘기면? | 빈 값은 기본 예비 모델로 대체된다(끌 수 없음). 끄려면 주 모델과 같은 값을 넣는다 — `.env.example`·`docs/deploy.md` 에 이미/함께 적혀 있다 | 문서 확인(Task 8) |
| 배포 로그에서 예비 모델이 실제로 켜져 있는지 보이는가 | **안 보였다**(`INFO` 로그 미설정, 폴백 경고만 `WARNING` 으로 보임) | 기동 시 모델·RPM 한 줄 + 로그 설정(Task 3) |
| 최악 지연 | 한 호출 = 주 60초 + 예비 60초(`TIMEOUT_MS`). 노드 재시도 3회면 약 6분. 그래프는 비동기 작업이라 HTTP 시간 제한과 무관하지만 **`/propose` 는 동기**다 | 결정 사항의 `/propose` 행, 스펙 문구(Task 9) |
| 주 모델이 장시간 죽어 있으면 | 모든 호출이 먼저 주 모델을 시도(지연 + 주 모델 한도 소모)한 뒤 예비 모델로 간다 | 회로 차단기는 만들지 않는다(알려진 한계 표). 필요하면 `GEMINI_MODEL` 을 예비 모델 값으로 바꿔 재배포 |
| 오류 메시지가 사용자나 DB 로 새는가 | 새지 않는다. `LLMError` 상세는 로그에만, `jobs.error` 는 `llm_failed` 코드만. 기존 `test_unexpected_errors_do_not_leak_details` 가 고정 | 변경 없음 |
| 예비 모델이 오디오 입력과 구조화 출력을 실제로 지원하는가, 모델 ID 가 유효한가 | **자동 테스트로 확인할 수 없다**(실제 API 필요) | Task 9 테스트 단계: 주 모델 이름을 일부러 틀려 예비 모델로 무드 해석이 도는지 확인 |

## 이 계획이 정한 값 (스펙이 정하지 않은 것)

| 값 | 초기값 | 위치 |
| --- | --- | --- |
| 보존 정책 주기 | `RETENTION_INTERVAL_SEC = 6시간` | `retention.py` |
| 완료 행 보존 / 분석 결과 보존 | `DONE_ROW_TTL = 7일` / `ANALYSIS_TTL = 7일` | `retention.py` |
| 분석 동시 실행 수 | `MAX_CONCURRENT_ANALYSES = 1` | `analyzer.py` |
| 분석 입력 상한 | 파일 `MAX_ANALYSIS_AUDIO_BYTES = 30 MiB`, 길이 `MAX_DURATION_SEC = 180`(+`DURATION_TOLERANCE_SEC = 1.0`) | `analyzer.py` |
| 음원 내려받기 전체 시간 | `AUDIO_TOTAL_TIMEOUT_SEC = 60`(소켓 한 번은 기존 `AUDIO_TIMEOUT_SEC = 30`) | `audio.py` |
| Gemini 분당 요청 수 | `DEFAULT_GEMINI_RPM = 10`(모델당, `GEMINI_RPM` 으로 변경, 0 이면 끔) | `settings.py` |
| 분석 에러 코드 | `audio_unavailable` · `decode_failed` · `too_long` · `internal_error` · `interrupted` | `analyzer.py`, `jobs.py` |
| 분석 API 에러 | 422 `invalid_audio_url` · 404 `job_not_found` | `analyzer.py` |

## API 계약 변경 (on-stage 가 알아야 할 것)

Task 9 에서 스펙에 반영한다. 모든 엔드포인트는 기존처럼 `X-Internal-Key` 필수(`/healthz`·`/readyz` 제외).

| 변경 | 내용 | on-stage 가 할 일 |
| --- | --- | --- |
| `POST /analyze {jobId, audioUrl}` → 202 | 신규. `jobId` = `audio_tracks.id`. 응답 `{jobId, status, progress, result?, error?}`. 멱등, `error` 면 처음부터 재시도. 422 `invalid_audio_url`(https·443·허용 호스트·공인 IP 가 아님) | 업로드 후 호출 |
| `GET /analyze/{jobId}` → 200 | 신규. `status`: `queued / running / done / error`, `progress`: 0~1(단계 단위), `result`: `{analysis, fileHash}`(`done`), `error`: 코드. 404 `job_not_found`(그래프 작업 id 포함) | 폴링해서 `done` 이면 `analysis` 저장(멱등) |
| `RunStatus.status` 에 `queued` 추가 | `POST /runs`·`GET /runs/{id}`·`POST …/resume` 응답에 나온다 | `running` 처럼 처리하고 "대기 중" 문구 선택 |
| `error` 값 `interrupted` 의 범위 확대 | 분석 작업도 서비스 재시작 때 `interrupted` 가 된다(그래프는 4단계부터). 같은 id 로 다시 `POST` 하면 재시도 | 기존 "다시 시도" 경로 그대로 |
| 분석 작업 id 로 `/runs`, 그래프 id 로 `/analyze` | 서로 404 (`thread_not_found` / `job_not_found`) | 영향 없음 |
| **`done` 그래프 작업의 `GET /runs/{id}` 는 7일 뒤 404** | 행이 삭제된다(체크포인트는 24시간 뒤) | **`approved` 시퀀스 행에 대해 Python 을 호출하지 않는다.** 404→410→행 삭제 경로는 `draft` 행에만 쓴다 |
| `context.audioUrl` | https·443·허용 호스트·공인 IP 가 아니면 **요청을 거절하지 않고** 무드 해석만 건너뛴다 | 영향 없음(무드는 비치명적) |
| `GET /healthz`, `GET /readyz` | 신규. 키 없이 `{"status": …}` | 호출 안 함(호스팅·사람용) |

## 파일 구조

| 파일 | 책임 | 태스크 |
| --- | --- | --- |
| `src/stage_director/jobs.py`(수정) | `Job` 확장, `migrate`, `ACTIVE`, `JobStore.set_progress`, `create(kind, status)`, `stale(kind)`, `fail_running` 이 `queued` 까지 | 1 |
| `src/stage_director/background.py` | `Periodic`(주기 작업 데몬 스레드, 보존 정책 주기 실행용) | 6 |
| `src/stage_director/runner.py`, `models.py`(수정) | `queued`, 대기 중 정리된 작업은 실행 안 함, 그래프 작업 종류 확인 | 2 |
| `src/stage_director/llm/ratelimit.py`, `llm/gemini.py`, `settings.py`(수정) | `RateLimiter`, 모델별 제한, `GEMINI_RPM` | 3 |
| `src/stage_director/audio.py`(재작성), `settings.py`(수정) | SSRF 방어, `AUDIO_URL_ALLOWED_HOSTS` | 4 |
| `src/stage_director/analyzer.py`, `models.py`, `api.py`(수정) | `AnalysisRunner`, `build_result`, `/analyze` | 5 |
| `src/stage_director/retention.py`(수정) | 규칙 3·4, `PurgeCounts`, 주기 실행 | 6 |
| `src/stage_director/analysis/seed.py` | 시드 곡 분석 CLI | 7 |
| `Dockerfile`, `.dockerignore`, `docs/deploy.md`, `.env.example` | 컨테이너 이미지와 Cloud Run 배포 가이드 | 8 |
| `src/stage_director/api.py`(수정) | `/healthz`, `/readyz`, 분석 풀·보존 주기 실행 연결 | 3, 4, 5, 6, 8 |
| `docs/superpowers/specs/stage-director-agent-design.md`(수정) | §4.1, §4.2, §6.1, §6.4, §8, §13 | 9 |
| `tests/` | 각 파일의 테스트(`test_background.py`, `test_ratelimit.py`, `test_settings.py`, `test_analyzer.py`, `analysis/test_seed.py` 신규) | 1~8 |

---


### Task 1: `jobs` 스키마 확장 — `kind` · `progress` · `queued`, ALTER 기반 마이그레이션

**Files:**
- Modify: `src/stage_director/jobs.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Consumes: 4단계의 `JobStore`(`create`, `get`, `transition`, `fail_running`, `stale`, `delete`), `postgres_checkpointer`
- Produces (`stage_director.jobs`): `ACTIVE = {"queued", "running"}`, `migrate(conn)`, `Job.{kind, progress}`, `JobStore.create(job_id, *, kind="graph", status="running")`, `set_progress(job_id, progress)`, `fail_running()`(이제 `queued` 도 `error("interrupted")` 로), `stale(statuses, before, kind="graph")`, `PostgresJobStore(pool)`(생성 시 `migrate`)

- [ ] **Step 1: 브랜치를 만들고 시작 상태를 확인한다**

```bash
git switch main && git pull --ff-only
git switch -c feat/realtime-deploy
git add docs/superpowers/plans/stage-director-agent-realtime-deploy.md
git commit -m "docs: add realtime analysis and deploy plan"
uv sync && uv run pytest -q
```

Expected: `293 passed, 17 deselected`. 다르면 멈추고 원인을 확인한다.

- [ ] **Step 2: 실패하는 테스트를 쓴다**

`tests/test_jobs.py`: 같은 계약 테스트를 메모리·Postgres 구현에 모두 돌린다 — `queued` 로 시작·`kind` 기록, 진행률 저장, `kind` 별 `stale`, `fail_running` 이 `queued`·`running` 을 `error(interrupted)` 로 바꾸고 `waiting_input`·`done` 은 그대로 둠. Postgres 전용: **4단계 스키마 테이블을 행 보존하며 업그레이드**, 인스턴스 4개가 동시에 `migrate` 해도 충돌 없음

_(코드 본문 생략 — 상세본 Task 1 Step 2 참고)_

- [ ] **Step 3: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_jobs.py -q`
Expected: FAIL — `ImportError: cannot import name 'migrate' from 'stage_director.jobs'`

- [ ] **Step 4: 구현한다**

`jobs.py`: 모듈 설명, `ACTIVE`, `_DDL`(4단계 `CREATE TABLE` + 컬럼마다 `ADD COLUMN IF NOT EXISTS` 한 줄)과 `migrate`(advisory lock 안에서 실행), `Job` 에 `kind`·`progress`, 두 구현에 `set_progress`·`create(kind, status)`·`stale(kind)`, `fail_running` 이 `queued` 까지 정리

_(코드 본문 생략 — 상세본 Task 1 Step 4 참고)_

- [ ] **Step 5: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_jobs.py -q`
Expected: `11 passed, 13 deselected`

Run: `uv run pytest -q`
Expected: `296 passed, 22 deselected`

Run: `uv run ruff check .`
Expected: `All checks passed!`

Postgres (이 단계가 새 SQL 을 **처음 실행**한다):

```bash
docker compose up -d checkpointer-db && sleep 3
uv run pytest tests/test_jobs.py -m postgres -q
docker compose down
```

Expected: `13 passed, 11 deselected`. 실패하면 SQL(`create` 의 `kind`, `fail_running` 의 `status = ANY(%s)`, `migrate` 의 advisory lock)부터 확인한다. **Neon 에 대고 이 마커를 돌리지 않는다** — 로컬 docker 만.

- [ ] **Step 6: 커밋한다**

```bash
git add src/stage_director/jobs.py tests/test_jobs.py
git commit -m "feat: add kind, progress and queued status to the jobs table with an ALTER-based migration"
```

---

### Task 2: 대기열(`queued`) 표시 — 스레드 풀이 찰 때 상태를 구분한다

**Files:**
- Modify: `src/stage_director/runner.py`, `models.py`, `api.py`(주석 두 줄)
- Test: `tests/test_runner.py`, `tests/test_models.py`

**Interfaces:**
- Consumes: Task 1 의 `JobStore.create(status=)`, `Job.kind`, `fail_running`
- Produces: `RunStatus.status` 에 `"queued"`, `Runner` 의 상태 흐름 `queued → running → waiting_input / done / error`(풀 대기 중이 `queued`, 재개·재시도도 `queued` 를 거침), 스레드가 `queued → running` 전이에 실패하면 실행하지 않음, `Runner._graph_job`(분석 작업 id 는 404 `thread_not_found`)

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_runner.py`(풀 대기 중 `queued` 후 실행, 재개도 `queued`, **정리된 작업은 늦게 깨어나도 실행되지 않음**, 분석 작업 id 는 404, 기존 "시작 전에 죽은 작업" 테스트의 기대 상태를 `queued` 로), `tests/test_models.py`(`queued` 허용)

_(코드 본문 생략 — 상세본 Task 2 Step 1 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_runner.py tests/test_models.py -q`
Expected: FAIL — 5개 실패(`test_a_job_waiting_for_a_worker_thread_is_queued_then_runs`, `test_resume_is_queued_until_a_thread_picks_it_up`, `test_a_run_killed_before_it_started_is_marked_interrupted_and_can_be_restarted`, `test_analysis_job_ids_are_unknown_threads`, `test_run_status_accepts_queued`)

- [ ] **Step 3: 구현한다**

`models.py`(`queued`), `runner.py`(생성·재개·재시도는 `queued`, `_run` 이 `queued → running` 전이에 실패하면 실행하지 않음, `_graph_job` 의 종류 확인, 모듈 설명), `api.py`(시작 시 정리 주석에 `queued` 반영)

_(코드 본문 생략 — 상세본 Task 2 Step 3 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest -q`
Expected: `301 passed, 22 deselected`

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: 커밋한다**

```bash
git add src tests
git commit -m "feat: show a queued state while the thread pool is full"
```

---

### Task 3: Gemini 분당 요청 수(RPM) 제한과 폴백 모델 점검

**Files:**
- Create: `src/stage_director/llm/ratelimit.py`
- Modify: `src/stage_director/llm/gemini.py`, `settings.py`, `api.py`, `graph_nodes.py`(주석), `.env.example`
- Test: `tests/test_ratelimit.py`(신규), `tests/test_settings.py`(신규), `tests/test_llm.py`

**Interfaces:**
- Produces: `RateLimiter(rpm, *, window=60.0, clock=time.monotonic, sleep=time.sleep).acquire()`(슬라이딩 윈도우, 락을 잡고 자지 않음), `GeminiClient(api_key, model, client=None, fallback_model=None, rpm=0, limiter_factory=RateLimiter)`(모델마다 제한기 하나, `rpm=0` 이면 제한 없음), `Settings.gemini_rpm`(`GEMINI_RPM`, 기본 `DEFAULT_GEMINI_RPM = 10`, 빈 값은 기본값, 숫자가 아니거나 음수면 기동 실패)

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_ratelimit.py`(한도 아래는 대기 없음, 한도 초과는 가장 오래된 요청이 윈도우를 벗어날 때까지 대기, **어느 60초 조각도 rpm 을 넘지 않음**, 스레드 여럿이 모두 통과하되 윈도우를 넘겨 밀림, rpm<1 거절), `tests/test_llm.py`(**모델마다 제한기가 따로 있고 요청 직전에 기다림**, 오디오 호출도 제한, `rpm=0` 이면 제한기 없음), `tests/test_settings.py`(기본값·빈 값·변환·잘못된 값)

_(코드 본문 생략 — 상세본 Task 3 Step 1 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_ratelimit.py tests/test_llm.py tests/test_settings.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.llm.ratelimit'`

- [ ] **Step 3: 구현한다**

`ratelimit.py`(슬라이딩 윈도우), `gemini.py`(모델마다 제한기, 시도 직전에 `acquire` — 예비 모델로 넘어가는 시도도 각 모델의 한도를 씀), `settings.py`(`GEMINI_RPM`), `api.py`(로그 설정 + 기동 시 모델·RPM 한 줄, `rpm` 전달), `graph_nodes.py`(오래된 `ponytail:` 주석을 현재 상태로 갱신), `.env.example`

_(코드 본문 생략 — 상세본 Task 3 Step 3 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest -q`
Expected: `314 passed, 22 deselected`

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: 커밋한다**

```bash
git add src tests .env.example
git commit -m "feat: limit Gemini requests per minute per model and log the effective model settings"
```

---

### Task 4: `fetch_audio` SSRF 방어 — 허용 호스트 · 공인 IP 고정 연결 · 리다이렉트 금지

**Files:**
- Modify: `src/stage_director/audio.py`(재작성), `settings.py`, `api.py`
- Test: `tests/test_audio.py`(재작성), `tests/test_settings.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: `Settings`, `build_sequence_graph(llm, checkpointer, fetch=…)`(변경 없음 — 무드 노드가 `fetch(url)` 만 부름)
- Produces (`stage_director.audio`): `check_url(url, allowed_hosts=()) -> (host, port, path_with_query)`(DNS 를 풀지 않는 빠른 검사, 실패하면 `AudioError`), `public_address(host, port, resolve=socket.getaddrinfo) -> str`(모든 답이 공인 IP 일 때만), `fetch_audio(url, *, max_bytes=MAX_AUDIO_BYTES, allowed_hosts=(), connect=_connect) -> (bytes, mime)`, `AUDIO_TOTAL_TIMEOUT_SEC = 60`. `Settings.audio_allowed_hosts`(`AUDIO_URL_ALLOWED_HOSTS`, 쉼표 구분·소문자·접미사 일치). 호출 쪽은 `functools.partial(fetch_audio, allowed_hosts=…)`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_audio.py` 전체를 새로 쓴다: 안전하지 않은 URL 14종(http·file·ftp·계정 정보·8443 포트·허용 목록 밖·`supabase.co.evil.com`·루프백·사설·메타데이터·IPv6 루프백·IPv4 매핑·zone id·빈 호스트)이 **연결 시도 없이** 거절됨, 접미사 일치는 라벨 경계에서만, 이름 풀이 결과가 하나라도 공인 IP 가 아니면 거절(사설·루프백·링크로컬·CGNAT·매핑 IPv6·섞인 답), **검증한 IP 로 직접 연결**하고 사설 IP 면 소켓을 열지 않음, 3xx/4xx/5xx 는 따라가지 않고 실패(두 번째 요청 없음), 스트리밍 중·`Content-Length` 로 크기 초과 거절, 느리게 흘리는 서버는 전체 시간 상한으로 포기, 네트워크 오류도 연결을 닫음. `tests/test_api.py`: 설정한 허용 목록이 그래프의 음원 내려받기에 전달됨

_(코드 본문 생략 — 상세본 Task 4 Step 1 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_audio.py -q`
Expected: FAIL — `ImportError: cannot import name '_PinnedHTTPSConnection' from 'stage_director.audio'`

- [ ] **Step 3: 구현한다**

`audio.py` 를 새로 쓴다(`urlopen` 대신 `http.client.HTTPSConnection` 하위 클래스가 풀이·검증한 IP 로 직접 연결하고 TLS 는 원래 호스트 이름으로 검증, 200 이외는 모두 실패라 리다이렉트를 따라가지 않음). `settings.py`, `api.py`(`partial(fetch_audio, allowed_hosts=…)`)

_(코드 본문 생략 — 상세본 Task 4 Step 3 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest tests/test_audio.py -q`
Expected: `43 passed`

Run: `uv run pytest -q`
Expected: `352 passed, 22 deselected`

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: 실제 TLS 로 한 번 확인한다 (자동 테스트가 아니다)**

가짜 연결 테스트는 `_PinnedHTTPSConnection` 의 실제 TLS(SNI·인증서 검증)를 거치지 않는다. 한 번 직접 돌려 본다.

```bash
uv run python - <<'EOF'
from stage_director.audio import fetch_audio, AudioError
data, mime = fetch_audio("https://www.python.org/static/img/python-logo.png", max_bytes=1_000_000)
print(len(data), mime)
for u in ["https://python.org/", "https://localhost/", "https://www.python.org:444/"]:
    try:
        print(u, fetch_audio(u)[1])
    except AudioError as e:
        print(u, "->", e)
EOF
```

Expected:

```text
15770 audio/mpeg        # 바이트 수는 달라질 수 있다. 오디오가 아니라 audio/mpeg 폴백이 나오는 게 정상
https://python.org/ -> HTTP 301 (리다이렉트는 따라가지 않는다)
https://localhost/ -> 공인 주소가 아닌 곳으로 풀리는 호스트다
https://www.python.org:444/ -> 443 포트만 받는다
```

첫 줄이 `AudioError` 로 실패하면(인증서·SNI 문제) 멈추고 원인을 확인한다.

- [ ] **Step 6: 커밋한다**

```bash
git add src tests
git commit -m "fix: harden fetch_audio against SSRF (host allowlist, pinned public IP, no redirects, total deadline)"
```

---

### Task 5: 분석 작업 — `POST /analyze` · `GET /analyze/{jobId}`

**Files:**
- Create: `src/stage_director/analyzer.py`
- Modify: `src/stage_director/models.py`, `api.py`
- Test: `tests/test_analyzer.py`(신규), `tests/test_api.py`

**Interfaces:**
- Consumes: Task 1~4 의 `JobStore`(`kind`·`progress`·`queued`·`fail_running`), `fetch_audio(url, *, max_bytes, allowed_hosts)`, `check_url`, 기존 `measure_file`, `RunError`
- Produces: `analyzer.build_result(data, mime, measure=measure_file) -> {"analysis": …camelCase, "fileHash": sha256}`(Task 7 도 사용), `AnalysisError(code)`(`decode_failed` / `too_long`), `AnalysisRunner(jobs, executor, *, fetch, build, allowed_hosts).start(job_id, audio_url) -> AnalysisStatus`·`.status(job_id)`, `MAX_CONCURRENT_ANALYSES = 1`, `models.AnalyzeCreate{job_id, audio_url}`, `models.AnalysisStatus{job_id, status, progress, result, error}`, `create_app(..., analysis_executor=None)`, 엔드포인트 `POST /analyze`(202)·`GET /analyze/{job_id}`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_analyzer.py`: 시작→`done`(결과·진행률 1.0·분석 풀 상한 전달), 멱등(재분석 없음), 풀 대기 중 `queued`, 대기 중 복구된 작업은 실행 안 됨, 잘못된 URL 은 큐에 넣기 전에 422, 실패는 상세 없는 짧은 코드(`audio_unavailable`·`too_long`·`decode_failed`·`internal_error`), 에러 뒤 재시작은 처음부터, 그래프 작업 id 는 404, `build_result`(camelCase+sha256, 바이트를 파일로 건넴, 디코딩 실패·0초·초과 길이·허용 오차, **실제 wav 를 librosa 로 측정**, **텍스트를 mp3 로 위장한 파일**). `tests/test_api.py`: 인증 401, 202 와 결과, 허용 밖 호스트 422, 모르는 작업 404, `/runs`·`/analyze` id 비교차

_(코드 본문 생략 — 상세본 Task 5 Step 1 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_analyzer.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.analyzer'`

- [ ] **Step 3: 구현한다**

`analyzer.py`(`build_result`, `AnalysisRunner` — 만들기·재시도는 `queued`, 스레드가 `queued → running` 에 성공해야 실행, 단계별 진행률, 실패를 코드로 변환), `models.py`(`AnalyzeCreate`, `AnalysisStatus`), `api.py`(분석 풀·`AnalysisRunner` 연결, `/analyze` 두 엔드포인트, 종료 시 풀 닫기)

_(코드 본문 생략 — 상세본 Task 5 Step 3 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest -q`
Expected: `379 passed, 22 deselected`

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: 커밋한다**

```bash
git add src tests
git commit -m "feat: add the analysis job with /analyze endpoints, queue state and progress"
```

---

### Task 6: 보존 정책 확장 — 완료 행 7일 삭제, 분석 결과 7일 보관, 실행 중 주기 실행

**Files:**
- Modify: `src/stage_director/retention.py`, `api.py`
- Test: `tests/test_retention.py`

**Interfaces:**
- Consumes: `JobStore.stale(statuses, before, kind=)`, `delete`
- Produces: `background.Periodic(fn, interval_sec, name)`(`start()`, `stop()`, `fn` 예외에도 계속 돎), `retention.DONE_ROW_TTL = 7일`, `ANALYSIS_TTL = 7일`, `RETENTION_INTERVAL_SEC = 6시간`, `PurgeCounts(done_checkpoints, drafts, done_rows, analyses)`(`NamedTuple`), `purge(jobs, saver, now=None) -> PurgeCounts`. 규칙 2 의 대상 상태에 `queued` 추가

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_background.py`(주기 호출·예외 후 계속·정지), `tests/test_retention.py`: 승인 작업의 행은 6일에는 남고(체크포인트는 24시간에 이미 없음) 8일에 사라짐, 분석 행은 상태와 무관하게 7일 뒤 사라지고 6일에는 남음, 그래프 규칙은 분석 행을 건드리지 않고 분석 규칙은 그래프 행을 건드리지 않음(분석 결과는 24시간이 지나도 남음), **실행 중인 서비스가 주기적으로 `purge` 를 부름**. 기존 반복 실행 테스트의 기대값은 `(1, 0, 1, 0)` 으로 바꾼다

_(코드 본문 생략 — 상세본 Task 6 Step 1 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_retention.py tests/test_background.py -q`
Expected: FAIL — `ImportError: cannot import name 'ANALYSIS_TTL' from 'stage_director.retention'` 와 `ModuleNotFoundError: No module named 'stage_director.background'`

- [ ] **Step 3: 구현한다**

`background.py`(`Periodic`), `retention.py`(규칙 3·4, `PurgeCounts`, CLI 출력 갱신, 모듈 설명에 새 규칙과 "승인된 시퀀스는 Python 을 부르지 않는다" 계약을 적음), `api.py`(`Periodic` 으로 6시간마다 `purge`)

_(코드 본문 생략 — 상세본 Task 6 Step 3 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest -q`
Expected: `385 passed, 22 deselected`

Run: `uv run ruff check .`
Expected: `All checks passed!`

Postgres:

```bash
docker compose up -d checkpointer-db && sleep 3
uv run pytest tests/test_retention.py -m postgres -q
docker compose down
```

Expected: `1 passed, 10 deselected`

- [ ] **Step 5: 커밋한다**

```bash
git add src tests
git commit -m "feat: expire finished job rows and analysis results, and run retention periodically"
```

---

### Task 7: 시드 곡 오프라인 분석 스크립트

**Files:**
- Create: `src/stage_director/analysis/seed.py`
- Modify: `.gitignore`
- Test: `tests/analysis/test_seed.py`(신규)

**Interfaces:**
- Consumes: `analyzer.build_result`, `AnalysisError`, `detect_sections`, `parse_analysis`
- Produces: `seed.analyze_seed(path) -> {"fileName", "fileHash", "durationSec", "analysis"}`(분석 불가면 `AnalysisError`), `seed.main(argv) -> int`(`python -m stage_director.analysis.seed <파일…> --out seed-analysis`, 곡마다 `<stem>.json`, 실패한 파일이 있으면 나머지는 처리하고 종료 코드 1)

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/analysis/test_seed.py`: **오프라인 결과가 업로드 분석 작업의 `build_result` 와 같음**(분석·sha256), 곡마다 JSON 한 개와 요약 출력, 디코딩 불가 파일이 있어도 나머지를 처리하고 종료 코드 1, 한글·공백 파일 이름

_(코드 본문 생략 — 상세본 Task 7 Step 1 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/analysis/test_seed.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stage_director.analysis.seed'`

- [ ] **Step 3: 구현한다**

`seed.py`, `.gitignore`(`seed-analysis/` — 곡 약관 확인 전이라 결과 파일도 저장소에 올리지 않는다)

_(코드 본문 생략 — 상세본 Task 7 Step 3 참고)_

- [ ] **Step 4: 통과하는 것을 확인한다**

Run: `uv run pytest -q`
Expected: `389 passed, 22 deselected`

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 5: 실제 곡 2개로 돌려 본다**

```bash
uv run python -m stage_director.analysis.seed demo-tracks/*.mp3 --out seed-analysis
ls seed-analysis
```

Expected (각 곡 1~2초):

```text
burn it up.mp3: 173.819초, BPM 99.38, 구간 4개 → seed-analysis/burn it up.json
나만의_작은_우주.mp3: 160.522초, BPM 86.13, 구간 6개 → seed-analysis/나만의_작은_우주.json
```

길이·BPM 이 `docs/superpowers/notes/sections-spike.md` 의 값(160.5초/BPM 86, 173.8초/BPM 99)과 맞아야 한다. 다르면 멈춘다. 이 JSON 을 on-stage 의 시드 스크립트 담당에게 파일로 넘긴다(저장소에는 올리지 않는다).

- [ ] **Step 6: 커밋한다**

```bash
git add src tests .gitignore
git commit -m "feat: export seed-track analysis as JSON files with the same code path as upload analysis"
```

---

### Task 8: 상태 확인 · Dockerfile · 배포 설정과 문서

**Files:**
- Create: `.dockerignore`, `Dockerfile`, `docs/deploy.md`
- Modify: `src/stage_director/api.py`, `.env.example`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: Task 3·4·5 의 환경변수(`GEMINI_RPM`, `AUDIO_URL_ALLOWED_HOSTS`)와 `create_app` 팩토리
- Produces: `GET /healthz`(키 없음, DB 안 봄) → `{"status": "ok"}`, `GET /readyz`(키 없음) → 200 `{"status": "ok"}` / 503 `{"status": "db_unavailable"}`(오류 내용 노출 없음), `app.state.jobs`, 컨테이너 이미지(워커 1개, `$PORT`(기본 8080)로 기동), `docs/deploy.md`(Cloud Run 조건 표·환경변수와 Secret Manager·GCP 프로젝트 생성·결제 연결·API 활성화·월 $5 예산 알림·시크릿 등록·`gcloud run deploy --source .`·**배포 후 오래된 이미지 정리 한 줄**·설정/시크릿 교체·**리전 비교(싱가포르/서울/도쿄)와 Neon 왕복 시간 재는 방법**·**확인된 요금과 월 사용량 시나리오**·콜드 스타트·`--min-instances 1` 올리고 되돌리기·새 리비전 때 일어나는 일·음원 15MiB/30MiB·로그에서 볼 것)

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_api.py`: `/healthz` 는 키 없이 200 이고 다른 정보를 노출하지 않음, `/readyz` 는 저장소가 답하면 200, **DB 가 죽어도 503 이고 오류 문구(호스트 이름 등)를 노출하지 않음**

_(코드 본문 생략 — 상세본 Task 8 Step 1 참고)_

- [ ] **Step 2: 실패하는 것을 확인한다**

Run: `uv run pytest tests/test_api.py -q`
Expected: FAIL — `test_healthz_needs_no_key_and_reveals_nothing_else` (`404 != 200`)

- [ ] **Step 3: 구현한다 — 상태 확인 엔드포인트**

`api.py`: `app.state.jobs` 저장, `/healthz`, `/readyz`

_(코드 본문 생략 — 상세본 Task 8 Step 3 참고)_

- [ ] **Step 4: 구현한다 — 컨테이너와 Cloud Run 배포 가이드**

`Dockerfile`(의존성 레이어 분리, `--no-dev --no-editable`, 워커 1개, `--port ${PORT:-8080}`, 시크릿 없음), `.dockerignore`(`.env`·`tests`·`docs`·`demo-tracks` 제외), `docs/deploy.md`(Cloud Run 가이드. `<Supabase 호스트>` 는 on-stage 에서 받은 값으로 채워 배포한다), `.env.example`

_(코드 본문 생략 — 상세본 Task 8 Step 4 참고)_

- [ ] **Step 5: 통과하는 것을 확인한다**

Run: `uv run pytest -q`
Expected: `392 passed, 22 deselected`

Run: `uv run ruff check .`
Expected: `All checks passed!`

- [ ] **Step 6: 컨테이너를 빌드해 실제로 확인한다 (Docker 필요)**

```bash
docker build -t stage-director-agent .
docker compose up -d checkpointer-db && sleep 3
docker run --rm -d --name sda -p 8080:8080 \
  -e INTERNAL_API_KEY=local-key -e GEMINI_API_KEY=unused \
  -e DATABASE_URL=postgresql://stage_director:stage_director@host.docker.internal:5433/stage_director_checkpoints \
  stage-director-agent
sleep 5
curl -s localhost:8080/healthz                              # {"status":"ok"}
curl -s localhost:8080/readyz                               # {"status":"ok"}
curl -s -o /dev/null -w "%{http_code}\n" localhost:8080/runs/x   # 401 (키 없음)
curl -s -H "X-Internal-Key: local-key" localhost:8080/analyze/x   # {"detail":"job_not_found"}
docker run --rm --entrypoint sh stage-director-agent -c 'ls -a /app'   # .env·tests·demo-tracks·docs 가 없어야 한다
docker run --rm -v "$PWD/demo-tracks:/tracks:ro" stage-director-agent \
  python -m stage_director.analysis.seed "/tracks/burn it up.mp3" --out /tmp/out
docker stop sda
# Cloud Run 처럼 $PORT 가 8080 이 아니어도 그 포트로 뜨는지
docker run --rm -d --name sda2 -p 9000:9000 -e PORT=9000 \
  -e INTERNAL_API_KEY=local-key -e GEMINI_API_KEY=unused \
  -e DATABASE_URL=postgresql://stage_director:stage_director@host.docker.internal:5433/stage_director_checkpoints \
  stage-director-agent
sleep 5
curl -s localhost:9000/healthz                              # {"status":"ok"}
docker stop sda2; docker compose down
```

Expected: 위 주석의 응답(`PORT=9000` 으로 띄운 컨테이너도 9000 에서 `/healthz` 응답), 시드 분석 명령은 `burn it up.mp3: 173.819초, BPM 99.38, 구간 4개 → /tmp/out/burn it up.json`.

마지막 명령이 `decode_failed` 이면 슬림 이미지에 mp3 디코더가 없는 것이다. `Dockerfile` 의 `FROM` 바로 아래에 다음을 넣고 다시 빌드한다:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 ffmpeg && rm -rf /var/lib/apt/lists/*
```

`gcloud` 가 있으면 `gcloud run deploy --help` 로 `--startup-probe`·`--liveness-probe` 플래그 문법이 `docs/deploy.md` 의 명령과 맞는지 본다. 다르면 `docs/deploy.md` 를 고친다.

- [ ] **Step 7: 커밋한다**

```bash
git add Dockerfile .dockerignore docs/deploy.md .env.example src tests
git commit -m "feat: add health endpoints, Dockerfile and Cloud Run deployment guide"
```

---

### Task 9: 통합 검증 · 스펙 갱신 · 실제 배포 확인

**Files:**
- Modify: `docs/superpowers/specs/stage-director-agent-design.md`(§4.1, §4.2, §6.1, §6.4, §8, §13)

**Interfaces:**
- Consumes: 이 계획의 모든 산출물
- Produces: 갱신된 스펙(on-stage 가 읽는 API 계약), PR 설명에 적을 사람 단계 결과

- [ ] **Step 1: 전체 테스트를 돌린다**

```bash
uv run pytest -q
```

Expected: `392 passed, 22 deselected`

```bash
docker compose up -d checkpointer-db && sleep 3
uv run pytest -m postgres -q
docker compose down
```

Expected: `17 passed` (체크포인터 3 + jobs 13 + retention 1)

```bash
uv run ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 2: 스펙을 갱신한다**

§4.1(분석 프로토콜: `/analyze` 요청·응답·에러 코드·진행률·크기 상한, 시드 곡 JSON 내보내기), §4.2(`audioUrl` 조건, `queued` 상태, `interrupted` 의미, **승인된 시퀀스는 Python 을 부르지 않는다**), §6.1(`jobs` 새 컬럼·시작 시 정리·스키마 변경 방식), §6.4(규칙 3·4와 실행 시점), §8(`/propose` 동기 호출·프로세스 사망·Gemini 한도·음원 URL 거절 행), §6.4 실행 시점(0대로 줄었다 켜지는 호스팅에서는 시작 시 실행이 정기 실행)과 §13(호스팅은 Google Cloud Run — 기획서 §9 의 "콜드 스타트 없는 플랜 우선" 을 비용 때문에 의도적으로 접었다는 점 포함, RPM 기본값, 분석 입력 상한)

_(코드 본문 생략 — 상세본 Task 9 Step 2 참고)_

- [ ] **Step 3: 스펙에 낡은 문장이 남지 않았는지 확인한다**

```bash
grep -nE "fail_running|컬럼을 두지 않|컬럼이 없다" docs/superpowers/specs/stage-director-agent-design.md || echo "no stale statements"
```

Expected: `no stale statements`

- [ ] **Step 4: (사람이 하는 단계) 폴백 모델을 실제 API 로 확인한다**

에이전트가 실제 Gemini 를 부를 수 없다. 주 모델 이름을 일부러 틀리게 하고 무드 해석(오디오 입력)까지 예비 모델로 도는지 본다.

```bash
GEMINI_MODEL=gemini-does-not-exist uv run --env-file .env uvicorn --factory stage_director.api:create_app --port 8080
```

다른 터미널에서 실제 음원 서명 URL(`audioUrl`)이 든 `POST /runs`(on-stage 에서 받은 Supabase 호스트를 `AUDIO_URL_ALLOWED_HOSTS` 로 같이 설정)를 보내 확인한다:

1. 서버 로그에 `Gemini 모델 gemini-does-not-exist (예비 …)` 한 줄과 `호출 실패, 예비 모델 … 로 다시 시도` 경고가 보인다
2. 첫 interrupt 의 구간에 `mood` 가 채워져 있다(예비 모델이 오디오 입력을 지원한다는 뜻). 전부 빈 문자열이면 예비 모델의 오디오 지원이나 모델 ID 를 의심한다
3. 끝까지 진행(`approve`)해 `done` 이 된다

- [ ] **Step 5: (사람이 하는 단계) 분당 한도를 실제 계정에 맞춘다**

Google AI Studio 에서 두 모델(주·예비)의 실제 분당 요청 한도를 확인하고 `GEMINI_RPM` 을 정한다. 한도 직전 값이 아니라 여유를 둔다. 5구간 곡 한 건을 처음부터 끝까지 돌려 총 소요 시간을 PR 설명에 적는다(RPM 10 이면 호출이 몰릴 때 느려지는 것이 정상).

정한 값이 10 과 다르면 **다음 Step 의 스펙 커밋에 함께 넣도록** 지금 고친다: 스펙 §13 "Gemini RPM 기본값" 행과 `docs/deploy.md` 의 `--set-env-vars` 예시(`GEMINI_RPM=…`). 코드의 `DEFAULT_GEMINI_RPM` 은 그대로 두고 환경변수로 덮는다.

- [ ] **Step 6: 커밋한다**

```bash
git add docs/superpowers/specs/stage-director-agent-design.md docs/deploy.md
git commit -m "docs: record the analysis, queued and retention contract in the spec"
```

- [ ] **Step 7: (사람이 하는 단계) 배포하고 배포 환경에서 확인한다**

`docs/deploy.md` 의 "처음 배포" 1~6 을 순서대로 따른다: GCP 프로젝트 생성 → 결제 계정 연결 → API 활성화 → **월 $5 예산 알림** → Secret Manager 에 시크릿 3개 등록 → `gcloud run deploy`(`<Supabase 호스트>` 는 on-stage 에서 받은 값). 배포 후:

```bash
URL=$(gcloud run services describe stage-director-agent --region asia-southeast1 --format='value(status.url)')
time curl -s $URL/healthz        # 첫 요청이면 콜드 스타트 시간이 된다. 기록해 둔다
curl -s $URL/readyz
gcloud run services logs read stage-director-agent --region asia-southeast1 --limit 50   # "Gemini 모델 … (예비 …), 모델당 분당 N회" 한 줄
```

확인할 것(결과는 PR 설명에 적는다):

1. `/healthz`·`/readyz` 가 `{"status":"ok"}`, 키 없이 `/runs/x` 는 401. `gcloud run services describe` 출력에 startup·liveness probe 가 `/healthz` 로 들어가 있다
2. Neon 의 `jobs` 테이블에 `kind`·`progress` 컬럼이 생겼고 4단계에서 만든 기존 행이 그대로 있다
3. **실제 Supabase 서명 URL** 로 `POST /analyze` → 폴링에서 `queued`/`running` → `done`, `result.analysis` 와 `fileHash` 확인. 허용 호스트가 아닌 URL 은 422
4. **분석 도중 새 리비전 배포(또는 인스턴스 종료)**: 분석을 시작한 직후 새 리비전을 만든다(`gcloud run services update stage-director-agent --region asia-southeast1 --update-env-vars REVISION_BUMP=$(date +%s)`) → 새 인스턴스가 켜진 뒤 첫 조회에서 `error`/`interrupted`(옛 인스턴스가 내려가고 새 인스턴스가 뜨는 데 걸리는 시간 만큼 `running` 으로 보일 수 있다) → 같은 `jobId` 로 `POST /analyze` 하면 처음부터 다시 `done`
5. **그래프 도중 새 리비전 배포**: `POST /runs` 로 `propose` 가 도는 중에 같은 방법으로 새 리비전을 만든다 → `interrupted` → 다시 `POST /runs` 가 **이미 끝난 구간의 LLM 호출을 반복하지 않고** 이어진다(로그의 호출 수로 확인)
6. **대기열**: 분석 두 건을 연달아 보내면 두 번째가 `queued`(풀 1건)였다가 첫 건이 끝나면 `running`
7. **유휴 후 0대로 줄고 첫 요청에 깨어남**: 요청 없이 15분쯤 두면 Cloud Run 콘솔 Metrics 의 Container instance count 가 0 으로 내려간다. 그 뒤 `time curl -s $URL/healthz` 로 **콜드 스타트 시간**을 재서 PR 설명과 `docs/deploy.md` 의 "비용과 콜드 스타트" 에 기록한다. 발표 당일 쓸 `--min-instances 1` 올리기·되돌리기 명령도 한 번 실행해 본다
8. **메모리 부족 종료가 없다**: 분석(4번)과 재시작 중의 로그에서 `gcloud run services logs read stage-director-agent --region asia-southeast1 --limit 200 | grep -iE "memory limit|exceeded"` 가 아무것도 내지 않고, 콘솔의 Memory utilization 이 한계에 붙지 않는다. 나오면 `--memory 4Gi` 로 올린다
9. `curl -s -o /dev/null -w "%{http_code}\n" $URL/` 이 404 이고 응답이 내부 정보를 노출하지 않는다
10. 예산 알림이 만들어졌는지(`gcloud billing budgets list --billing-account=BILLING_ACCOUNT_ID`)와 서비스가 `--max-instances 1` 인지(`gcloud run services describe` 의 `autoscaling.knative.dev/maxScale`) 확인한다
11. **Neon 왕복 시간을 잰다**: `docs/deploy.md` "리전 선택" 의 반복 명령(`/healthz` 와 `/readyz` 를 번갈아 10번)을 돌려 두 응답 시간의 차이를 기록한다. 이 차이가 대략 Neon 왕복 + 쿼리 한 번이다. 몇 십 ms 이하면 싱가포르 선택이 맞다. 0.3초를 넘으면 원인(Neon 이 잠들어 있었는지, 다른 리전에 배포했는지)을 확인하고, 그래도 크면 PR 설명에 적어 도쿄(Tier 1, Vercel·Supabase 와 같은 도시) 배포를 재검토할 근거로 남긴다
12. 배포를 두세 번 한 뒤 `docs/deploy.md` 의 "오래된 이미지 정리" 를 한 번 실행해 Artifact Registry 에 현재 리비전 이미지만 남는지 본다

- [ ] **Step 8: (사람이 하는 단계) on-stage 에 넘길 것**

PR 설명에 다음을 적고 on-stage 담당에게 전한다. 이 저장소는 on-stage 를 수정하지 않는다.

- 스펙 §4.1·§4.2 의 갱신된 API 계약(위 "API 계약 변경" 표 그대로): `/analyze`, `queued`, `interrupted`, **승인된 시퀀스에 대해 `GET /runs/{id}` 를 부르지 않기**
- 배포된 Python 서비스 주소와 `INTERNAL_API_KEY`(안전한 경로로)
- `seed-analysis/*.json` 파일(Task 7)과 "`file_hash`·`duration_sec`·`analysis` 로 `audio_tracks` 에 넣는다"는 설명
- 음원 버킷 제한: 파일 30MiB 이하, 길이 180초 이하

---

## 스펙 대응

| 스펙 | 이 계획의 태스크 |
| --- | --- |
| §4.1 분석 작업 `POST /analyze`·`GET /analyze/{jobId}`, 진행률, 7일 보관 | Task 5, 6 |
| §4.1 5번 시드 곡 오프라인 분석 | Task 7 |
| §6.1 `jobs` 의 `kind`·`progress` 추가(4단계가 미룬 것) | Task 1, 5 |
| §6.1 프로세스가 죽었을 때 `running` 작업 처리(4단계의 `fail_running` 을 `queued` 까지 확장) | Task 1, 2 |
| §6.4 보존 정책: 분석 결과 7일, 완료 행 정리, 서비스 시작 시 + 주기 | Task 6 |
| §8 오류 처리: 프로세스 사망, Gemini 한도, LLM 호출 실패(예비 모델) | Task 2, 3 |
| §3 Python 은 Supabase 접근 없음 → 음원은 서명 URL 로 받음, SSRF 방어 | Task 4 |
| §13 호스팅(콜드 스타트 없는 플랜, 기획서 §9), 배포·환경변수·시크릿·상태 확인 | Task 8, 9 |
| §10(기획서) 5단계 완료 기준 "배포 환경에서 업로드부터 저장까지 동작"의 Python 쪽 | Task 9 사람 단계(저장은 on-stage 몫) |
| 4단계 "알려진 한계" 표의 5단계 항목 7개 | 아래 표 |

4단계 "알려진 한계와 5단계로 넘기는 것"의 항목별 처리:

| 4단계가 넘긴 항목 | 처리 |
| --- | --- |
| 프로세스 하나 가정, 한 인스턴스가 죽으면 다른 인스턴스가 모름 | **해결하지 않고 1대 전제를 유지**한다(결정 사항의 "죽은 작업 처리"·호스팅 행). 시작 시 정리를 `queued` 까지 넓혔다(Task 1). 여러 대가 필요해지면 heartbeat 를 그때 만든다 |
| 스레드 풀이 차면 대기열이 `running` 으로 보임, RPM 속도 제한 없음 | `queued` 상태(Task 2, 5), 모델별 RPM 제한(Task 3) |
| `done` 작업의 `jobs` 행이 영구 보관 | 7일 뒤 삭제(Task 6) |
| `fetch_audio` 가 리다이렉트를 따라가며 SSRF 를 막지 않음 | Task 4 |
| `jobs.progress`·분석 작업 `kind=analysis`·결과 7일 보관 | Task 1, 5, 6 |
| 시드 곡 오프라인 분석 | Task 7 |
| 피드백 턴 횟수 상한 없음 / 경계 변경 후 무드 재해석 없음 / 무드 정확도 자동 평가 없음 | 5단계 대상이 아니다(on-stage 레이트 리밋 / 필요성 확인 후 / 필요하면 `llm` 마커). 그대로 둔다 |

## 알려진 한계

| 한계 | 비고 |
| --- | --- |
| 인스턴스 1대(`--max-instances 1`)·워커 1개를 전제한다. 두 대가 되면 분당 요청 수 한도와 `resume` 락이 인스턴스마다 따로 적용된다(조건부 전이는 여전히 중복 실행을 막는다) | 스케일 아웃이 필요해지면 제한기와 락을 DB 로 옮긴다 |
| 인스턴스가 여러 대가 되면 죽은 작업을 구별하지 못하고, 새 인스턴스의 시작 정리가 살아 있는 인스턴스의 작업을 `interrupted` 로 만들 수 있다 | heartbeat(`owner`·`heartbeat_at`, 주기 갱신)나 별도 워커를 그때 추가한다 |
| 유휴 시 0대라 평소 첫 요청이 콜드 스타트를 겪는다(이미지가 크고 librosa 를 불러온다). 기획서 §9 의 "콜드 스타트 없는 플랜 우선" 을 비용 때문에 접었다 | 발표·면접 당일 `--min-instances 1`, 끝나면 0 으로 되돌린다(`docs/deploy.md`). 되돌리지 않으면 하루 약 $2.4 |
| 요청이 없을 때 Cloud Run 이 인스턴스를 회수하면 돌던 작업이 사라진다. 폴링 요청이 있는 동안에는 유지되지만 탭을 닫으면 보장되지 않는다 | 다음에 인스턴스가 켜질 때 `interrupted` 로 정리되고 다시 시도하면 이어진다. 잦으면 `--min-instances 1` 을 검토 |
| Cloud Run 요금은 2026-10-05 에 가격 페이지로 확인했다(무료 한도 $5.22/월 ≈ 55시간, 시간당 약 $0.095). **월 사용량(접속 한 번에 인스턴스가 켜져 있는 시간 약 25분)은 가정**이고, Cloud Build·Artifact Registry·Secret Manager·외부 네트워크 요금은 확인하지 못했다 | 배포 전에 가격 페이지를 다시 보고, 별도 요금 항목은 각 가격 페이지에서 확인 |
| Python↔Neon 왕복이 작업 지연에 얼마나 영향을 주는지는 추정(쿼리당 같은 도시 1~2ms, 도쿄↔싱가포르 약 70ms)이다 | Task 9 에서 `/healthz` 와 `/readyz` 응답 시간 차이로 실측 |
| 분석 진행률은 4단계(0.1/0.4/1.0)뿐이다 | librosa 가 중간 진행을 주지 않는다 |
| 주 모델이 장시간 죽으면 모든 호출이 먼저 주 모델을 시도한다(지연 + 한도 소모). 회로 차단기는 없다 | 필요하면 `GEMINI_MODEL` 을 예비 모델 값으로 바꿔 재배포 |
| `/propose` 는 동기 요청이다 | 배포 환경에서 Next.js 는 부르지 않는다 |
| 분석 대기열은 사용자별 공정성이 없다(선착순). 공유 데모 계정의 남용은 Next.js 레이트 리밋에 맡긴다 | on-stage 쪽 |
| `GEMINI_RPM` 기본값 10 은 추정이다 | Task 9 점검 단계에서 실제 한도로 |
| 비밀 교체(`INTERNAL_API_KEY`) 중 짧은 401 구간이 있다 | `docs/deploy.md`. 이중 키 허용은 필요해지면 |
| 시드 곡 분석 결과를 DB 에 넣는 것은 on-stage 몫이다 | Task 9 마지막 단계 |

