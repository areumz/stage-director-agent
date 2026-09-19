# /api/stage-presets 계약 [계약]

출처: `../on-stage/src/app/api/stage-presets/route.ts`, `[id]/route.ts`, `src/lib/routeHelpers.ts`.

- 인증은 Supabase 쿠키 세션이다. `user_id` 는 항상 서버가 세션(`auth.getUser()`)에서 채우며 요청 본문의 값은 쓰지 않는다.
- 접근 제어는 RLS(`user_id = auth.uid()`)다. 라우트는 소유자를 다시 확인하지 않는다.
- 에러 본문은 항상 `{ "error": <문자열> }` 이다. Supabase 오류는 `error` 에 원문 메시지가 들어간다.
- 프리셋은 **사용자 × 아티스트** 스코프다. 유니크 키는 `(user_id, artist_id, name)`.

## GET /api/stage-presets?artist=<slug>

| 항목 | 값 |
| --- | --- |
| 요청 | 쿼리 `artist`(아티스트 slug). 본문 없음 |
| 200 | `{ "presets": [ { "id": string(uuid), "name": string, "state": object } ] }` — `name` 오름차순 |
| 400 | `{ "error": "bad request" }` — `artist` 쿼리가 없음 |
| 404 | `{ "error": "artist not found" }` |
| 500 | `{ "error": <Supabase 메시지> }` |

- 이 라우트는 로그인 여부를 직접 검사하지 않는다. 사용자별 필터링은 RLS 가 한다. 로그아웃 상태의 결과는 계약으로 보증되지 않는다.
- `state` 는 저장된 jsonb 를 그대로 돌려준 것이다. StageState 모양이라는 보장이 없으므로 소비자가 `merge_stage_state` 를 통과시켜야 한다.

## POST /api/stage-presets

요청 본문

```json
{ "artistSlug": "aurora", "name": "코러스용", "state": { "...": "StageState" }, "overwrite": false }
```

| 필드 | 타입 | 비고 |
| --- | --- | --- |
| `artistSlug` | string | 필수 |
| `name` | string | 필수 |
| `state` | object | 필수. **내용은 검증하지 않고 그대로 jsonb 로 저장된다** |
| `overwrite` | boolean | 선택. 참이면 upsert(`onConflict: user_id,artist_id,name`), 아니면 insert |

| 상태 | 본문 | 조건 |
| --- | --- | --- |
| 201 | `{ "id": string(uuid) }` | 저장됨. `overwrite` 로 덮어써도 201 이다 |
| 400 | `{ "error": "bad request" }` | 본문이 JSON 이 아니거나 `artistSlug`·`name`·`state` 중 하나가 falsy |
| 401 | `{ "error": "unauthorized" }` | 로그인하지 않음 |
| 404 | `{ "error": "artist not found" }` | |
| 409 | `{ "error": "duplicate" }` | `overwrite` 없이 같은 `(user, artist, name)` 을 다시 저장 (Postgres 23505) |
| 500 | `{ "error": <Supabase 메시지> }` | |

on-stage 클라이언트의 덮어쓰기 흐름: `overwrite` 없이 POST → 409 → 사용자 확인 → `overwrite: true` 로 재요청.

## DELETE /api/stage-presets/{id}

| 상태 | 본문 | 조건 |
| --- | --- | --- |
| 204 | 없음 | 삭제됨 |
| 401 | `{ "error": "unauthorized" }` | 로그인하지 않음 |
| 403 | `{ "error": "forbidden" }` | 삭제된 행이 0개 — RLS 가 막았거나 없는 id. RLS 는 예외가 아니라 0행으로 돌려주므로 라우트가 이렇게 판정한다 |
| 500 | `{ "error": <Supabase 메시지> }` | |

## 없는 것

- PATCH/PUT 은 없다. 이름 변경·수정은 같은 이름으로 `overwrite: true` POST 하는 방법뿐이다.
- 단건 조회(`GET /api/stage-presets/{id}`)는 없다. 목록 응답이 `state` 를 통째로 포함한다.
