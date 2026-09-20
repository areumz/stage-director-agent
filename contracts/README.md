# contracts/

`../on-stage`에서 이 프로젝트가 실제로 쓰는 계약만 옮겨 둔 폴더다.
**구현은 이 폴더만 보고 진행하고 `../on-stage`를 열지 않는다.**

## 출처

| 항목 | 값 |
| --- | --- |
| 저장소 | on-stage (읽기 전용 참고. 이 저장소에서 수정하지 않는다) |
| 추출일 | 2026-09-19 |
| 브랜치 | `feat/responsive-design` (작업 트리 깨끗함. `main`과 계약 관련 파일에 차이 없음을 확인) |
| 커밋 | `dc14d342d656812064a425c2a984ac821970e79c` |

## 파일

각 파일은 세 성격 중 하나 이상이다. 한 파일 안에서 섞일 때는 섹션으로 구분해 둔다.

- **[계약]** on-stage 의 실제 동작. 그대로 따른다.
- **[우리 규칙]** 이 프로젝트가 덧붙인 규칙. on-stage 에는 없다.
- **[요구사항]** on-stage 가 앞으로 구현해야 하는 것. 아직 없다.

| 파일 | 성격 | on-stage 출처 |
| --- | --- | --- |
| `stage_state.py` | [계약] 모델·기본값·`merge_stage_state` / [우리 규칙] `clamp_stage_state` | `src/lib/stageState.ts`, `docs/design-v2.md` §5.1·§5.3 |
| `fixtures/merge_stage_state_cases.json` | [계약] 골든 벡터 | `src/lib/stageState.test.ts`, `src/lib/stageState.ts` |
| `api_stage_presets.md` | [계약] | `src/app/api/stage-presets/route.ts`, `src/app/api/stage-presets/[id]/route.ts`, `src/lib/routeHelpers.ts` |
| `enums_and_constants.md` | [계약] | `src/lib/stageState.ts`, `src/lib/types.ts`, `src/data/artists.json`, `docs/design-v2.md` §5.3 |
| `artist_context.md` | [계약] | `src/lib/types.ts`, `src/app/api/artists/route.ts` |
| `supabase_required.md` | [계약] 기존 사실 / [요구사항] 신규 테이블·버킷 | `supabase/migrations/20260813120014_init.sql`, `src/app/api/gallery/upload-url/route.ts`, `docs/superpowers/specs/stage-director-agent-design.md` §5 |

## 규칙

1. 계약에 없는 정보가 필요하면 **추측하지 않는다.** 구현을 멈추고 "계약 갱신" 태스크를 따로 만든다.
2. 계약 갱신 태스크만 `../on-stage`를 읽을 수 있다. 읽은 뒤 이 README 의 브랜치·커밋 행과 해당 파일을 함께 고친다.
3. `fixtures/`의 골든 벡터는 on-stage 테스트와 코드 동작에서 옮긴 것이다. 임의로 고치지 않는다.

## 알려진 공백 (on-stage 의 현재 동작)

| 사실 | 영향 |
| --- | --- |
| `mergeStageState` 는 숫자 범위를 clamp 하지 않고 hex 형식도 검증하지 않는다 | 범위 방어는 `stage_state.py` 의 `clamp_stage_state`([우리 규칙])가 유일한 실효 방어선이다 |
| `POST /api/stage-presets` 는 `state` 를 검증 없이 저장한다 | 에이전트 출력은 저장 요청 전에 반드시 자체 검증을 거친다 |
| `penumbra` 는 타입·기본값·씬(SpotLight)에 남아 있고 UI 슬라이더만 제거됐다 | 모델에 유지하고(기본 0.6) 에이전트는 값을 바꾸지 않는다 |
| 기존 `tracks` 테이블은 디스코그래피용이고 쓰기가 오너 전용이다 | 새 음원 테이블은 `audio_tracks` 로 이름을 달리한다 |
