"""음원 서명 URL 에서 오디오를 내려받는다 (무드 해석 입력). URL 은 Next.js 가 만든 것만 온다 (스펙 §3)."""

import http.client
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

MAX_AUDIO_BYTES = 15 * 1024 * 1024  # Gemini 인라인 요청 한도(20MB) 아래. 3분 곡 mp3 는 3~6MB
AUDIO_TIMEOUT_SEC = 30
DEFAULT_MIME = "audio/mpeg"


class AudioError(Exception):
    """음원을 내려받을 수 없다. 무드 해석은 비치명적이라 호출자가 삼킨다."""


def fetch_audio(url: str, *, opener: Callable[..., Any] = urllib.request.urlopen) -> tuple[bytes, str]:
    """(바이트, mime) 을 돌려준다. https 만 받는다.

    ponytail: urlopen 의 리다이렉트는 따라간다. URL 이 신뢰하는 Next.js 가 만든 서명 URL 이라 SSRF 방어는 여기서 안 한다.
    """
    if urlparse(url).scheme != "https":
        raise AudioError("https URL 만 받는다")
    try:
        with opener(url, timeout=AUDIO_TIMEOUT_SEC) as response:
            data = response.read(MAX_AUDIO_BYTES + 1)
            content_type = response.headers.get("Content-Type", "")
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise AudioError(f"{type(e).__name__}: {e}") from e
    if len(data) > MAX_AUDIO_BYTES:
        raise AudioError(f"음원이 {MAX_AUDIO_BYTES} 바이트를 넘는다")
    mime = content_type.split(";")[0].strip().lower()
    return data, mime if mime.startswith("audio/") else DEFAULT_MIME
