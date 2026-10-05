import pytest

from stage_director.audio import MAX_AUDIO_BYTES, AudioError, fetch_audio


class FakeResponse:
    def __init__(self, data: bytes, content_type: str | None = None):
        self._data = data
        self.headers = {"Content-Type": content_type} if content_type else {}

    def read(self, n: int = -1) -> bytes:
        return self._data if n < 0 else self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_returning(data: bytes, content_type: str | None = None):
    return lambda url, timeout: FakeResponse(data, content_type)


@pytest.mark.parametrize("url", ["http://a/x.mp3", "file:///etc/passwd", "ftp://a/x.mp3"])
def test_only_https_urls_are_fetched(url):
    called = []
    with pytest.raises(AudioError):
        fetch_audio(url, opener=lambda u, timeout: called.append(u))
    assert called == []  # 요청 자체를 보내지 않는다


def test_returns_bytes_and_the_audio_mime_type():
    data, mime = fetch_audio("https://x/a.mp3", opener=opener_returning(b"abc", "audio/wav; charset=binary"))
    assert (data, mime) == (b"abc", "audio/wav")


def test_non_audio_content_type_falls_back_to_mpeg():
    _, mime = fetch_audio("https://x/a.mp3", opener=opener_returning(b"abc", "application/octet-stream"))
    assert mime == "audio/mpeg"


def test_oversized_audio_is_rejected():
    with pytest.raises(AudioError):
        fetch_audio("https://x/a.mp3", opener=opener_returning(b"x" * (MAX_AUDIO_BYTES + 1), "audio/mpeg"))


def test_network_failure_becomes_audio_error():
    def broken(url, timeout):
        raise OSError("connection reset")

    with pytest.raises(AudioError, match="connection reset"):
        fetch_audio("https://x/a.mp3", opener=broken)
