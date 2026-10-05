"""Gemini 어댑터. google-genai SDK 의 실패를 모두 LLMError 로 바꾼다.

주 모델이 실패하면(503 혼잡, 404 퇴역 등) 예비 모델로 한 번 더 시도. 테스트 결과 모델이 번갈아 혼잡해지는 일이 잦아서 추가.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

from google import genai
from google.genai import types

from stage_director.llm.client import LLMError
from stage_director.llm.ratelimit import RateLimiter

log = logging.getLogger(__name__)

TIMEOUT_MS = 60_000


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        client: Any = None,
        fallback_model: str | None = None,
        rpm: int = 0,
        limiter_factory: Callable[[int], Any] = RateLimiter,
    ):
        """client : 테스트에서 SDK 대신 스텁을 넣는 자리. 운영에서는 api_key 로 만듦.

        fallback_model : 주 모델이 실패했을 때 쓸 모델. 비어 있거나 주 모델과 같으면 예비 시도를 하지 않는다.
        rpm : 모델당 분당 요청 수 상한. 0 이면 제한하지 않는다. Gemini 한도는 모델별이라 모델마다 따로 센다.
        """
        self._models = [model, *([fallback_model] if fallback_model and fallback_model != model else [])]
        self._limiters = {m: limiter_factory(rpm) for m in self._models} if rpm > 0 else {}
        self._client = client if client is not None else genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=TIMEOUT_MS))

    def generate_json(self, *, system: str, user: str, schema: dict[str, Any]) -> Any:
        return self._generate(user, system, schema)

    def generate_json_with_audio(self, *, system: str, user: str, schema: dict[str, Any], audio: bytes, mime_type: str) -> Any:
        return self._generate([types.Part.from_bytes(data=audio, mime_type=mime_type), user], system, schema)

    def _generate(self, contents: Any, system: str, schema: dict[str, Any]) -> Any:
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema,
        )
        for i, model in enumerate(self._models):
            if (limiter := self._limiters.get(model)) is not None:
                limiter.acquire()  # 실패해서 예비 모델로 넘어가는 시도도 주 모델의 한 자리를 쓴다
            try:
                response = self._client.models.generate_content(model=model, contents=contents, config=config)
                return json.loads(response.text)
            except Exception as e:  # SDK·네트워크·JSON 파싱 실패를 한 종류로 묶어 노드가 재시도하게 함
                if i + 1 < len(self._models):
                    log.warning("Gemini %s 호출 실패, 예비 모델 %s 로 다시 시도: %s: %s", model, self._models[i + 1], type(e).__name__, e)
                    continue
                raise LLMError(f"{type(e).__name__}: {e}") from e
