"""Gemini 어댑터. google-genai SDK 의 실패를 모두 LLMError 로 바꾼다."""

import json
from typing import Any

from google import genai
from google.genai import types

from stage_director.llm.client import LLMError

TIMEOUT_MS = 60_000


class GeminiClient:
    def __init__(self, api_key: str, model: str, client: Any = None):
        """client : 테스트에서 SDK 대신 스텁을 넣는 자리. 운영에서는 api_key 로 만듦."""
        self._model = model
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
        try:
            response = self._client.models.generate_content(model=self._model, contents=contents, config=config)
            return json.loads(response.text)
        except Exception as e:  # SDK·네트워크·JSON 파싱 실패를 한 종류로 묶어 노드가 재시도하게 함
            raise LLMError(f"{type(e).__name__}: {e}") from e
