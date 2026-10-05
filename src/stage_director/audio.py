"""음원 서명 URL 에서 오디오 다운 (무드 해석·분석 작업 입력)

SSRF 방어. URL 은 Next.js 가 만든 서명 URL 이지만, 배포 환경에서는 내부망·클라우드 메타데이터 주소를 가리키는
URL 이 섞여 들어올 수 있다고 가정한다.
① https · 443 · 계정 정보 없음 ② 호스트 허용 목록(설정) ③ 이름 풀이 결과가 전부 공인 IP 일 때만, 검증한 그 IP 로 직접 연결
(검증과 연결 사이에 DNS 답이 바뀌는 rebinding 방지) ④ 리다이렉트는 따라가지 않음(3xx 는 실패) ⑤ 크기·전체 시간 상한
"""

import http.client
import ipaddress
import socket
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

MAX_AUDIO_BYTES = 15 * 1024 * 1024  # Gemini 인라인 요청 한도(20MB) 아래. 3분 곡 mp3 는 3~6MB
AUDIO_TIMEOUT_SEC = 30  # 소켓 한 번을 기다리는 시간
AUDIO_TOTAL_TIMEOUT_SEC = 60  # 내려받기 전체. 1바이트씩 천천히 보내는 서버가 스레드를 붙잡지 못하게 한다
CHUNK_BYTES = 64 * 1024
DEFAULT_MIME = "audio/mpeg"


class AudioError(Exception):
    """음원을 내려받을 수 없을 때 에러. 무드 해석은 비치명적이라 호출자가 삼킴."""


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])  # IPv6 zone id(fe80::1%en0) 제거
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 로 사설 주소를 숨기는 수법
    return ip.is_global


def check_url(url: str, allowed_hosts: tuple[str, ...] = ()) -> tuple[str, int, str]:
    """(host, port, path+query). 받을 수 없는 URL 이면 AudioError. DNS 는 풀지 않는다(요청을 받는 순간의 빠른 검사)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise AudioError(f"URL 을 읽을 수 없다: {e}") from e
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        raise AudioError("https URL 만 받는다")
    if parts.username or parts.password:
        raise AudioError("URL 에 계정 정보를 넣을 수 없다")
    if port not in (None, 443):
        raise AudioError("443 포트만 받는다")
    if allowed_hosts and not any(host == a or host.endswith("." + a) for a in allowed_hosts):
        raise AudioError("허용되지 않은 호스트다")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass  # 도메인 이름. 연결할 때 풀어서 검사한다
    else:
        if not _is_public(str(literal)):
            raise AudioError("공인 주소가 아니다")
    path = parts.path or "/"
    return host, 443, f"{path}?{parts.query}" if parts.query else path


def public_address(host: str, port: int, resolve: Callable[..., Any] = socket.getaddrinfo) -> str:
    """host 를 풀어 모든 답이 공인 IP 일 때만 그중 하나(IPv4 우선)를 돌려준다."""
    try:
        addresses = [info[4][0] for info in resolve(host, port, type=socket.SOCK_STREAM)]
    except OSError as e:
        raise AudioError(f"호스트를 찾을 수 없다: {e}") from e
    if not addresses or not all(_is_public(a) for a in addresses):
        raise AudioError("공인 주소가 아닌 곳으로 풀리는 호스트다")
    return next((a for a in addresses if ":" not in a), addresses[0])


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """검증한 IP 로 직접 연결하되, TLS 인증서 검증과 Host 헤더는 원래 호스트 이름 기준으로 한다."""

    def __init__(self, host: str, port: int, *, timeout: float, resolve: Callable[..., Any] = socket.getaddrinfo):
        super().__init__(host, port, timeout=timeout)
        self._resolve = resolve

    def connect(self) -> None:
        address = public_address(self.host, self.port, self._resolve)
        sock = socket.create_connection((address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _connect(host: str, port: int, timeout: float) -> Any:
    return _PinnedHTTPSConnection(host, port, timeout=timeout)


def fetch_audio(
    url: str,
    *,
    max_bytes: int = MAX_AUDIO_BYTES,
    allowed_hosts: tuple[str, ...] = (),
    connect: Callable[[str, int, float], Any] = _connect,
) -> tuple[bytes, str]:
    """(바이트, mime) 을 돌려준다. connect 는 테스트에서 가짜 연결을 넣는 자리."""
    host, port, path = check_url(url, allowed_hosts)
    deadline = time.monotonic() + AUDIO_TOTAL_TIMEOUT_SEC
    conn = None
    timed_out = threading.Event()

    def abort() -> None:  # 읽기는 소켓에서 막혀 있으므로 시계 검사만으로는 못 깨운다. 소켓을 닫아 깨운다
        timed_out.set()
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    watchdog = threading.Timer(AUDIO_TOTAL_TIMEOUT_SEC, abort)
    watchdog.daemon = True
    try:
        conn = connect(host, port, AUDIO_TIMEOUT_SEC)  # 호스트 이름이 이상하면 여기서 InvalidURL
        watchdog.start()
        conn.request("GET", path, headers={"User-Agent": "stage-director-agent"})
        response = conn.getresponse()
        if response.status != 200:
            raise AudioError(f"HTTP {response.status} (리다이렉트는 따라가지 않는다)")
        if int(response.getheader("Content-Length") or 0) > max_bytes:
            raise AudioError(f"음원이 {max_bytes} 바이트를 넘는다")
        content_type = response.getheader("Content-Type") or ""
        chunks: list[bytes] = []
        size = 0
        while chunk := response.read(CHUNK_BYTES):
            size += len(chunk)
            if size > max_bytes:
                raise AudioError(f"음원이 {max_bytes} 바이트를 넘는다")
            if time.monotonic() > deadline:
                raise AudioError("내려받기가 너무 오래 걸린다")
            chunks.append(chunk)
        if timed_out.is_set():  # 소켓이 닫혀 본문이 짧게 끝난 경우
            raise AudioError("내려받기가 너무 오래 걸린다")
    except (OSError, ValueError, http.client.HTTPException) as e:
        if timed_out.is_set():
            raise AudioError("내려받기가 너무 오래 걸린다") from e
        raise AudioError(f"{type(e).__name__}: {e}") from e
    finally:
        watchdog.cancel()
        if conn is not None:
            conn.close()
    mime = content_type.split(";")[0].strip().lower()
    return b"".join(chunks), mime if mime.startswith("audio/") else DEFAULT_MIME
