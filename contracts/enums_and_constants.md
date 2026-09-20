# enum 값과 상수 [계약]

출처: `../on-stage/src/lib/stageState.ts`, `src/lib/types.ts`, `src/data/artists.json`, `docs/design-v2.md` §5.3, `supabase/migrations/*_init.sql`.

## StageState

| 필드 | 타입 | 허용 값 |
| --- | --- | --- |
| `color` | string | hex 문자열. 씬 전체 조명 색 1개 |
| `spots` | object | 키는 정확히 `left`, `center`, `right` 3개 |
| `spots.<키>.on` | boolean | |
| `spots.<키>.intensity` | number | 슬라이더 범위 0–1000 |
| `spots.<키>.angle` | number | 슬라이더 범위 0.1–1.0 |
| `spots.<키>.penumbra` | number | 슬라이더 범위 0–1. UI 에서는 제거됐지만 타입·씬에는 남아 있다 |
| `camera` | string | `"front"`, `"audience"`, `"top"` 셋 중 하나. 그 밖의 값은 병합 시 기본값 |
| `smoke.density` | number | 슬라이더 범위 0–1 |
| `smoke.color` | string | hex 문자열 (조명 색과 별개) |

슬라이더 범위는 UI 상수이며 **서버·병합 로직이 강제하지 않는다.** 강제는 `stage_state.py` 의 `clamp_stage_state`([우리 규칙])가 한다.

| 필드 | 범위 | step | 기본값 |
| --- | --- | --- | --- |
| `intensity` | 0–1000 | 10 | 300 |
| `angle` | 0.1–1.0 | 0.05 | 0.45 |
| `penumbra` | 0–1 | 0.05 | 0.6 |
| `smoke.density` | 0–1 | 0.05 | 0 |

## 기본 상태 `defaultStageState(color)`

```json
{
  "color": "<color>",
  "spots": {
    "left":   { "on": true,  "intensity": 300, "angle": 0.45, "penumbra": 0.6 },
    "center": { "on": true,  "intensity": 300, "angle": 0.45, "penumbra": 0.6 },
    "right":  { "on": false, "intensity": 300, "angle": 0.45, "penumbra": 0.6 }
  },
  "camera": "front",
  "smoke": { "density": 0, "color": "#ffffff" }
}
```

## 아티스트 시드 (slug → 시그니처 컬러)

`src/data/artists.json` 의 값이다. 런타임의 진실은 DB(`artists.color`)이며 오너가 수정할 수 있다. 에이전트는 이 표가 아니라 Next.js 가 컨텍스트로 넘긴 값을 쓴다.

| slug | 시그니처 컬러 |
| --- | --- |
| aurora | `#9F77DD` |
| velvet | `#7F77DD` |
| nova | `#D4537E` |
| halo | `#BA7517` |
| lumen | `#1D9E75` |
| echo | `#378ADD` |

## 셰이더 (아티스트 테마)

| 항목 | 값 |
| --- | --- |
| `shader.pattern` | `"wave"`, `"ripple"`, `"grain"` |
| `shader.freq` | number. DB 기본값 9 |
| `shader.falloff` | number. DB 기본값 0.75 |
| `shader.speed` | number. DB 기본값 0.5 |
| `pattern` DB 기본값 | `"wave"` |

## 그 밖의 값

| 값 | 의미 |
| --- | --- |
| 갤러리 `license` 값 `"AI"` | AI 생성 이미지. 화면에 "AI로 생성됨" 한 줄을 표기한다 (음원도 같은 표기 원칙을 쓴다) |
