import socket

import pytest

from stage_director.audio import (
    MAX_AUDIO_BYTES,
    AudioError,
    _PinnedHTTPSConnection,
    check_url,
    fetch_audio,
    public_address,
)

ALLOWED = ("supabase.co",)
URL = "https://abc.supabase.co/storage/v1/object/sign/audio/a.mp3?token=t"


# ── URL 검사 (DNS 를 풀지 않는다) ─────────────────────────────


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("http://abc.supabase.co/x.mp3", ALLOWED),
        ("file:///etc/passwd", ALLOWED),
        ("ftp://abc.supabase.co/x.mp3", ALLOWED),
        ("https://user:pw@abc.supabase.co/x.mp3", ALLOWED),
        ("https://abc.supabase.co:8443/x.mp3", ALLOWED),
        ("https://evil.example.com/x.mp3", ALLOWED),
        ("https://supabase.co.evil.com/x.mp3", ALLOWED),
        ("https://127.0.0.1/x.mp3", ()),
        ("https://10.0.0.5/x.mp3", ()),
        ("https://169.254.169.254/latest/meta-data/", ()),
        ("https://[::1]/x.mp3", ()),
        ("https://[::ffff:127.0.0.1]/x.mp3", ()),
        ("https://[fe80::1%25en0]/x.mp3", ()),
        ("https:///x.mp3", ()),
    ],
)
def test_unsafe_urls_are_rejected_without_connecting(url, allowed):
    called = []
    with pytest.raises(AudioError):
        fetch_audio(url, allowed_hosts=allowed, connect=lambda *a: called.append(a))
    assert called == []  # 요청 자체를 보내지 않는다


def test_allowed_hosts_match_on_a_label_boundary():
    assert check_url("https://supabase.co/a", ALLOWED)[0] == "supabase.co"
    assert check_url("https://ABC.Supabase.co/a", ALLOWED)[0] == "abc.supabase.co"
    with pytest.raises(AudioError):
        check_url("https://notsupabase.co/a", ALLOWED)


def test_check_url_returns_host_port_and_path_with_query():
    assert check_url(URL, ALLOWED) == ("abc.supabase.co", 443, "/storage/v1/object/sign/audio/a.mp3?token=t")


def test_without_an_allowlist_any_public_host_passes_the_url_check():
    assert check_url("https://cdn.example.com/a.mp3")[0] == "cdn.example.com"


# ── 이름 풀이 (연결 직전에 한 번 더) ───────────────────────────


def resolving_to(*addresses):
    return lambda host, port, type=None: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]


def test_public_address_returns_the_validated_ip():
    assert public_address("h", 443, resolving_to("93.184.216.34")) == "93.184.216.34"


def test_public_address_prefers_ipv4():
    assert public_address("h", 443, resolving_to("2606:2800:220:1::1", "93.184.216.34")) == "93.184.216.34"


@pytest.mark.parametrize(
    "addresses",
    [["10.0.0.5"], ["127.0.0.1"], ["169.254.169.254"], ["100.64.0.1"], ["::ffff:10.0.0.1"], ["93.184.216.34", "10.0.0.5"], []],
)
def test_public_address_rejects_hosts_that_resolve_to_non_public_ips(addresses):
    with pytest.raises(AudioError):
        public_address("h", 443, resolving_to(*addresses))


def test_public_address_wraps_resolution_failure():
    def boom(*args, **kwargs):
        raise socket.gaierror("nope")

    with pytest.raises(AudioError):
        public_address("h", 443, boom)


def test_pinned_connection_refuses_to_open_a_socket_for_a_private_ip(monkeypatch):
    opened = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: opened.append(a))
    conn = _PinnedHTTPSConnection("evil.example.com", 443, timeout=1, resolve=resolving_to("10.0.0.5"))
    with pytest.raises(AudioError):
        conn.connect()
    assert opened == []


def test_pinned_connection_connects_to_the_validated_ip_not_the_name(monkeypatch):
    opened = []

    def fake_create_connection(address, timeout):
        opened.append(address)
        raise OSError("stop here")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    conn = _PinnedHTTPSConnection("abc.supabase.co", 443, timeout=1, resolve=resolving_to("93.184.216.34"))
    with pytest.raises(OSError, match="stop here"):
        conn.connect()
    assert opened == [("93.184.216.34", 443)]  # 이름을 다시 풀지 않는다


# ── 내려받기 ──────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, data=b"", status=200, headers=None):
        self.status, self._data, self._headers = status, data, headers or {}
        self.read_calls = 0

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, n=-1):
        self.read_calls += 1
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


class FakeConnection:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.requests, self.closed = [], False

    def request(self, method, path, headers=None):
        if self.error:
            raise self.error
        self.requests.append((method, path))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def fetch_with(conn, **kwargs):
    return fetch_audio(URL, allowed_hosts=ALLOWED, connect=lambda *a: conn, **kwargs)


def test_fetch_returns_bytes_and_the_audio_mime():
    conn = FakeConnection(FakeResponse(b"abc", headers={"Content-Type": "audio/wav; charset=x"}))
    assert fetch_with(conn) == (b"abc", "audio/wav")
    assert conn.requests == [("GET", "/storage/v1/object/sign/audio/a.mp3?token=t")] and conn.closed


@pytest.mark.parametrize("headers", [{}, {"Content-Type": "application/octet-stream"}])
def test_fetch_falls_back_to_mpeg_for_non_audio_content_types(headers):
    assert fetch_with(FakeConnection(FakeResponse(b"abc", headers=headers)))[1] == "audio/mpeg"


@pytest.mark.parametrize("status", [301, 302, 307, 403, 404, 500])
def test_fetch_does_not_follow_redirects_or_accept_errors(status):
    conn = FakeConnection(FakeResponse(b"", status=status, headers={"Location": "https://169.254.169.254/"}))
    with pytest.raises(AudioError, match=str(status)):
        fetch_with(conn)
    assert len(conn.requests) == 1 and conn.closed  # Location 으로 두 번째 요청을 보내지 않는다


def test_fetch_rejects_files_over_the_limit_while_streaming():
    conn = FakeConnection(FakeResponse(b"x" * (MAX_AUDIO_BYTES + 1)))
    with pytest.raises(AudioError, match="넘는다"):
        fetch_with(conn)


def test_fetch_rejects_by_content_length_without_reading_the_body():
    response = FakeResponse(b"x", headers={"Content-Length": str(MAX_AUDIO_BYTES + 1)})
    with pytest.raises(AudioError, match="넘는다"):
        fetch_with(FakeConnection(response))
    assert response.read_calls == 0


def test_fetch_honors_a_custom_limit():
    assert fetch_with(FakeConnection(FakeResponse(b"x" * 10)), max_bytes=10)[0] == b"x" * 10
    with pytest.raises(AudioError):
        fetch_with(FakeConnection(FakeResponse(b"x" * 11)), max_bytes=10)


def test_fetch_gives_up_on_a_server_that_drips_data_slowly(monkeypatch):
    clock = iter(range(0, 1000, 30))  # read 한 번마다 30초가 흐른다
    monkeypatch.setattr("stage_director.audio.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("stage_director.audio.CHUNK_BYTES", 1)
    with pytest.raises(AudioError, match="오래"):
        fetch_with(FakeConnection(FakeResponse(b"x" * 100)))


def test_fetch_wraps_network_errors_and_still_closes():
    conn = FakeConnection(error=ConnectionResetError("reset"))
    with pytest.raises(AudioError, match="ConnectionResetError"):
        fetch_with(conn)
    assert conn.closed
