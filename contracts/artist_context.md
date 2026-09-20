# 아티스트 컨텍스트 [계약]

출처: `../on-stage/src/lib/types.ts`(`Artist`, `ShaderPattern`), `src/app/api/artists/route.ts`.

Python 서비스는 Supabase 를 읽지 않는다. 아티스트 정보는 Next.js 가 요청 본문에 실어 보낸다.

## Python 이 쓰는 부분집합

```json
{
  "slug": "aurora",
  "name": "AURORA",
  "nameKo": "오로라",
  "color": "#9F77DD",
  "shader": { "pattern": "wave", "freq": 9, "falloff": 0.75, "speed": 0.5 }
}
```

| 필드 | 타입 | 용도 |
| --- | --- | --- |
| `slug` | string | 아티스트 식별 |
| `name` | string | 영문 대문자 표기 |
| `nameKo` | string | 한글 표기 |
| `color` | string(hex) | 시그니처 컬러. 연출의 기본 색이며 벗어나면 이유를 기록한다 |
| `shader.pattern` | `"wave"` \| `"ripple"` \| `"grain"` | 아티스트 셰이더 테마(연출 무드 근거) |
| `shader.freq` / `falloff` / `speed` | number | 셰이더 파라미터 |

## GET /api/artists (참고)

| 요청 | 200 응답 |
| --- | --- |
| `GET /api/artists` | `{ "artists": Artist[] }` |
| `GET /api/artists?slug=<slug>` | `{ "artist": Artist }` |

| 상태 | 본문 | 조건 |
| --- | --- | --- |
| 404 | `{ "error": "not found" }` | slug 에 해당하는 아티스트가 없음 |

## `Artist` 전체 필드 (에이전트는 위 부분집합만 쓴다)

`slug`, `name`, `nameKo`, `color`, `initials`(노드 아바타용 3글자), `orbit`(0–2), `angle`(deg), `size`, `news`, `tour { badge, titleKo, year }`, `stats { cities, countries, tracks }`, `cities [{ code, name, date }]`, `tracks [{ no, title, duration, cover { from, to } }]`, `gallery [{ src, creator, license, origin }]`, `shader { pattern, freq, falloff, speed }`.
