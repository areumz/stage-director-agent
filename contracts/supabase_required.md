# Supabase 계약

이 문서는 두 부분으로 나뉜다. 섞어 읽지 말 것.

- **1부 [계약] 기존 사실**: on-stage 에 이미 있는 것.
- **2부 [요구사항] 신규**: on-stage 가 앞으로 구현해야 하는 것. 지금은 존재하지 않는다. 출처는 `docs/superpowers/specs/stage-director-agent-design.md` §5 이다.

---

## 1부. 기존 사실 [계약]

출처: `../on-stage/supabase/migrations/20260813120014_init.sql`, `src/app/api/gallery/upload-url/route.ts`, `src/proxy.ts`.

### 인증 모델

- Supabase Auth 이메일·비밀번호 로그인. 세션은 쿠키다. API 라우트는 `createServerSupabase()` 로 사용자 세션 클라이언트를 만들고 `auth.getUser()` 로 신원을 확인한다.
- 키는 서버 전용이다(`NEXT_PUBLIC_` 접두사 없음). service role 키는 시드 스크립트 전용이며 Vercel 에 등록하지 않는다.
- 역할은 JWT 의 `app_metadata.role` 이다. `owner` 만 `artists`·`tracks`·`shows` 에 쓸 수 있다. 데모 계정은 `owner` 가 아니다.

### `stage_presets`

| 컬럼 | 타입 | 비고 |
| --- | --- | --- |
| `id` | uuid pk | 기본값 `gen_random_uuid()` |
| `user_id` | uuid not null | `auth.users` 참조, 기본값 `auth.uid()` |
| `artist_id` | uuid not null | `artists` 참조, 아티스트 삭제 시 cascade |
| `name` | text not null | |
| `state` | jsonb not null | StageState |
| `created_at` | timestamptz | 기본값 `now()` |

- 유니크: `(user_id, artist_id, name)`
- RLS: `own all` — `authenticated` 역할, `user_id = auth.uid()` (using, with check 모두). **완전 사용자 스코프**다.

### `artists`(읽기 전용으로만 쓴다)

`id`(uuid), `slug`(unique), `name`, `name_ko`, `color`(시그니처 hex), `shader_pattern`, `shader_freq`, `shader_falloff`, `shader_speed`, … 읽기는 공개다.

### 이미 있는 `tracks` (이름이 충돌한다)

`id`, `artist_id`, `no`, `title`, `duration`(text), `cover_from`, `cover_to` — 아티스트 디스코그래피용이다. 읽기 공개, **쓰기는 오너 전용**. 음원 분석용 테이블로 재사용할 수 없다(데모 계정이 쓸 수 없다). 그래서 신규 테이블은 `audio_tracks` 다.

### 소유 스코프의 시드 패턴

`gallery_images.created_by` 가 NULL 이면 시드 행이다. 읽기는 공개, 수정·삭제는 `created_by = auth.uid()` 인 행만 가능하다. 시드 행은 아무도 지울 수 없다. `audio_tracks.user_id IS NULL` 도 같은 패턴을 따른다.

### Storage 버킷 `gallery`

- 공개 읽기, 허용 MIME `image/jpeg`·`image/png`·`image/webp`, 10MB 상한. **오디오는 올릴 수 없다.**
- 업로드는 서명 URL 방식이다. 브라우저는 Supabase 키를 보지 않는다.

### `POST /api/gallery/upload-url` (서명 URL 업로드 패턴, 음원 업로드가 복제할 대상)

요청 본문 `{ "artistSlug": string, "filename": string, "contentType": string, "size": number }`

| 상태 | 본문 | 조건 |
| --- | --- | --- |
| 200 | `{ "signedUrl": string, "path": string }` | 경로는 `<artistSlug>/<uuid>.<ext>` |
| 400 | `{ "error": "bad request" }` | 필드 누락, `size` 가 숫자가 아님 |
| 401 | `{ "error": "unauthorized" }` | |
| 413 | `{ "error": "file too large" }` | 10MB 초과 |
| 415 | `{ "error": "unsupported type" }` | 허용 MIME 아님 |
| 500 | `{ "error": <Supabase 메시지> }` | |

라우트의 크기·타입 검사는 사용자에게 빨리 알려주기 위한 것이고, 진짜 신뢰 경계는 버킷 설정이다.

---

## 2부. 신규 요구사항 [요구사항]

on-stage 마이그레이션과 라우트가 구현한다. 이 저장소는 구현하지 않는다.

### `audio_tracks`

| 컬럼 | 타입 | 비고 |
| --- | --- | --- |
| `id` | uuid pk | |
| `user_id` | uuid null | NULL = 시드 곡. 기본값 `auth.uid()` |
| `artist_id` | uuid not null | `artists` 참조 |
| `title` | text not null | |
| `genre` | text | |
| `mood_keywords` | text[] | |
| `storage_path` | text not null | 음원 버킷 경로 |
| `file_hash` | text not null | sha256. 분석 캐시 키 |
| `duration_sec` | numeric not null | 최대 180 |
| `analysis` | jsonb null | `AnalysisSnapshot` (`src/stage_director/analysis/snapshot.py` 의 모양, camelCase 키) |
| `analysis_status` | text not null | `pending` \| `running` \| `done` \| `error` |
| `created_at` | timestamptz | |

RLS: 읽기 `user_id IS NULL OR user_id = auth.uid()`. 쓰기 `user_id = auth.uid()`.

### `stage_sequences`

| 컬럼 | 타입 | 비고 |
| --- | --- | --- |
| `id` | uuid pk | LangGraph `thread_id` 와 같은 값 |
| `user_id` | uuid not null | 기본값 `auth.uid()` |
| `audio_track_id` | uuid not null | `audio_tracks` 참조 |
| `status` | text not null | `draft` \| `approved` |
| `items` | jsonb null | 승인 전 NULL. `SequenceItem` 배열 (`src/stage_director/sequence.py`) |
| `approved_at` | timestamptz null | |
| `created_at` | timestamptz | |

- RLS: `stage_presets` 와 동일한 완전 사용자 스코프(`user_id = auth.uid()`).
- 부분 유니크 인덱스: `(user_id, audio_track_id) WHERE status = 'draft'`.
- 승인본 5개 상한은 `(user_id, audio_track_id)` 단위이며 Next.js 라우트에서 강제한다.

### `items` 불변식 (Python 검증 게이트와 Next.js 저장 직전이 같은 규칙을 검사한다)

- 배열 순서 = 시간 순서, `startSec < endSec`
- 첫 항목 `startSec = 0`, 마지막 `endSec = duration_sec`, `items[i].endSec == items[i+1].startSec`
- `0 <= transitionMs <= (endSec - startSec) * 1000`

### 음원 Storage 버킷

- `gallery` 와 별개의 새 버킷.
- 허용 MIME: audio 계열(예: `audio/mpeg`, `audio/wav`). 크기 상한: 3분 곡이 들어가는 값.
- 정확한 버킷 이름과 상한은 on-stage 가 구현할 때 정하고, 정해지면 이 문서를 고친다(계약 갱신 태스크).
- 업로드는 `POST /api/gallery/upload-url` 의 서명 URL 패턴을 복제한다.
