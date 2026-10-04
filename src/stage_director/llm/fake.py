"""테스트용 결정적 가짜 LLM. 미리 넣어 둔 응답을 순서대로 돌려줌."""

from typing import Any


class FakeLLM:
    def __init__(self, *responses: Any):
        """응답이 Exception 이면 돌려주는 대신 던짐 (재시도 테스트용)."""
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate_json(self, *, system: str, user: str, schema: dict[str, Any]) -> Any:
        self.calls.append({"system": system, "user": user, "schema": schema})
        return self._next()

    def generate_json_with_audio(self, *, system: str, user: str, schema: dict[str, Any], audio: bytes, mime_type: str) -> Any:
        self.calls.append({"system": system, "user": user, "schema": schema, "audio_bytes": len(audio), "mime_type": mime_type})
        return self._next()

    def _next(self) -> Any:
        assert self._responses, "FakeLLM 에 넣어 둔 응답이 다 떨어졌다"
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
