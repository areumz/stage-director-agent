"""LLM 클라이언트 인터페이스. 노드는 이 Protocol 만 알고, 구현(Gemini, 가짜)은 주입"""

from typing import Any, Protocol


class LLMError(Exception):
    """LLM 호출이 실패했거나 JSON 으로 읽을 수 없는 응답을 받음. 재시도 대상."""


class LLMClient(Protocol):
    def generate_json(self, *, system: str, user: str, schema: dict[str, Any]) -> Any:
        """schema(JSON Schema)에 맞는 JSON 하나를 생성해 파싱한 값을 돌려줌. 실패하면 LLMError.

        schema 는 모델을 유도하는 용도. 돌려받은 값이 schema 를 지킨다고 믿지 말고 호출자가 방어한다.
        """
        ...
