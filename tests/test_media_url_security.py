import socket

import httpcore

_ORIGINAL_CONNECT = socket.socket.connect

import httpx
import pytest

from songseek.errors import SongSeekError
from songseek.media_url import PublicNetworkBackend, secure_media_client, validate_url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a", "http://10.0.0.1/a", "http://169.254.169.254/a",
    "http://[::1]/a", "http://[::ffff:192.168.1.1]/a", "http://224.0.0.1/a",
    "http://[fe80::1%25en0]/a", "http://[64:ff9b::a00:1]/a", "file:///tmp/a", "http://user@host/a",
])
def test_rejects_unsafe_url_literals(url):
    with pytest.raises(SongSeekError):
        validate_url(url)


def test_redirect_is_checked_before_next_request():
    seen = []
    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        secure_media_client(client)
        with pytest.raises(SongSeekError):
            client.get("https://public.example/audio")
    assert seen == ["https://public.example/audio"]


class Stream:
    def __init__(self, peer):
        self.peer = peer
        self.closed = False
    def get_extra_info(self, name):
        return (self.peer, 443) if name == "server_addr" else None
    def close(self):
        self.closed = True


class Backend:
    def __init__(self, peer):
        self.peer = peer
        self.calls = []
        self.stream = Stream(peer)
    def connect_tcp(self, host, port, *args):
        self.calls.append(host)
        return self.stream


def addresses(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in ips]


def test_dns_rebinding_is_pinned_and_peer_checked(monkeypatch):
    calls = []
    def resolve(host, port, **kwargs):
        calls.append(host)
        return addresses("8.8.8.8") if len(calls) == 1 else addresses("127.0.0.1")
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    backend = Backend("8.8.8.8")
    PublicNetworkBackend(backend).connect_tcp("example.test", 443)
    assert calls == ["example.test"]
    assert backend.calls == ["8.8.8.8"]


def test_connected_peer_mismatch_is_closed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses("8.8.8.8"))
    backend = Backend("127.0.0.1")
    with pytest.raises(SongSeekError):
        PublicNetworkBackend(backend).connect_tcp("example.test", 443)
    assert backend.stream.closed


def test_dns_private_addresses_never_connected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses("10.0.0.1", "169.254.169.254"))
    backend = Backend("10.0.0.1")
    with pytest.raises(SongSeekError):
        PublicNetworkBackend(backend).connect_tcp("example.test", 443)
    assert not backend.calls


def test_proxy_transport_is_rejected():
    with httpx.Client(proxy="http://127.0.0.1:7897", trust_env=False) as client:
        with pytest.raises(SongSeekError):
            secure_media_client(client)


def test_actual_direct_transport_uses_guard():
    with httpx.Client(trust_env=False) as client:
        secure_media_client(client)
        assert isinstance(client._transport._pool._network_backend, PublicNetworkBackend)


def test_real_http_transport_blocks_dns_to_loopback(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses("127.0.0.1"))
    with httpx.Client(trust_env=False) as client:
        secure_media_client(client)
        with pytest.raises(SongSeekError):
            client.get("http://rebound.example/audio")


@pytest.mark.parametrize("url", [
    "http://r1.googlevideo.com/audio", "https://127.0.0.1/audio",
    "https://169.254.169.254/audio", "https://googlevideo.com.evil.test/audio",
    "https://evilgooglevideo.com/audio", "https://r1.googlevideo.com:8443/audio",
    "https://localhost/audio", "https://[::1]/audio",
])
def test_youtube_proxy_rejects_untrusted_targets(url):
    from songseek.media_url import validate_youtube_proxy_url
    with pytest.raises(SongSeekError):
        validate_youtube_proxy_url(url)


@pytest.mark.parametrize("url", ["https://r1.googlevideo.com/audio", "https://i.ytimg.com/image", "https://youtube.com/media"])
def test_youtube_proxy_allows_official_https(url):
    from songseek.media_url import validate_youtube_proxy_url
    assert validate_youtube_proxy_url(url) == url


@pytest.mark.parametrize("scheme", ["http", "socks5"])
@pytest.mark.parametrize("location,allowed", [
    ("https://127.0.0.1/private", False),
    ("http://r2.googlevideo.com/audio", False),
    ("https://evil.test/audio", False),
    ("https://r2.googlevideo.com/audio", True),
])
def test_youtube_actual_proxy_redirects(tmp_path, monkeypatch, scheme, location, allowed):
    import socketserver
    import ssl
    import subprocess
    import threading

    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=googlevideo.com",
                    "-addext", "subjectAltName=DNS:*.googlevideo.com"],
                   check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    seen = []

    def read_exact(sock, count):
        data = b""
        while len(data) < count:
            part = sock.recv(count - len(data))
            if not part:
                raise EOFError
            data += part
        return data

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            sock = self.request
            if scheme == "http":
                data = b""
                while b"\r\n\r\n" not in data:
                    data += sock.recv(4096)
                target = data.split(b" ")[1].decode()
                sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                version, count = read_exact(sock, 2)
                assert version == 5
                read_exact(sock, count)
                sock.sendall(b"\x05\x00")
                version, command, _, atyp = read_exact(sock, 4)
                assert (version, command, atyp) == (5, 1, 3)
                length = read_exact(sock, 1)[0]
                host = read_exact(sock, length).decode()
                port = int.from_bytes(read_exact(sock, 2), "big")
                target = f"{host}:{port}"
                sock.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            seen.append(target)
            with tls.wrap_socket(sock, server_side=True) as connection:
                request = b""
                while b"\r\n\r\n" not in request:
                    request += connection.recv(4096)
                if target.startswith("r1."):
                    response = f"HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
                else:
                    response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                connection.sendall(response)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def local_connection(address, timeout=None, source_address=None):
            assert address == ("127.0.0.1", server.server_address[1])
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            _ORIGINAL_CONNECT(sock, address)
            return sock
        monkeypatch.setattr(socket, "create_connection", local_connection)
        try:
            context = ssl.create_default_context(cafile=str(cert))
            proxy = f"{scheme}://127.0.0.1:{server.server_address[1]}"
            with httpx.Client(proxy=proxy, verify=context, trust_env=False, follow_redirects=True) as client:
                secure_media_client(client, "youtube")
                if allowed:
                    assert client.get("https://r1.googlevideo.com/audio").text == "ok"
                else:
                    with pytest.raises(SongSeekError):
                        client.get("https://r1.googlevideo.com/audio")
        finally:
            server.shutdown()
            thread.join()
    assert seen == (["r1.googlevideo.com:443", "r2.googlevideo.com:443"] if allowed else ["r1.googlevideo.com:443"])


def test_connect_timeout_falls_back_with_shared_budget(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses("8.8.8.8", "1.1.1.1"))
    class RetryBackend(Backend):
        def connect_tcp(self, host, port, timeout, *args):
            self.calls.append((host, timeout))
            if len(self.calls) == 1:
                raise httpcore.ConnectTimeout("first address timed out")
            return self.stream
    backend = RetryBackend("1.1.1.1")
    PublicNetworkBackend(backend).connect_tcp("example.test", 443, timeout=2)
    assert [call[0] for call in backend.calls] == ["8.8.8.8", "1.1.1.1"]
    assert 0 < backend.calls[0][1] <= 1 and 0 < backend.calls[1][1] <= 2


def test_dns_timeout_is_bounded(monkeypatch):
    import threading
    import time
    release = threading.Event()
    def stalled(*args, **kwargs):
        release.wait(2)
        return addresses("8.8.8.8")
    monkeypatch.setattr(socket, "getaddrinfo", stalled)
    started = time.monotonic()
    try:
        with pytest.raises(httpcore.ConnectTimeout):
            PublicNetworkBackend(Backend("8.8.8.8")).connect_tcp("example.test", 443, timeout=0.05)
        assert time.monotonic() - started < 0.5
    finally:
        release.set()


def test_direct_tls_checks_original_hostname(tmp_path, monkeypatch):
    import socketserver
    import ssl
    import subprocess
    import threading
    from httpcore._backends.sync import SyncBackend

    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=wrong.test",
                    "-addext", "subjectAltName=DNS:wrong.test"], check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                with tls.wrap_socket(self.request, server_side=True):
                    pass
            except ssl.SSLError:
                pass
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    with Server(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def local_connection(address, timeout=None, source_address=None):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            _ORIGINAL_CONNECT(sock, ("127.0.0.1", server.server_address[1]))
            return sock
        monkeypatch.setattr(socket, "create_connection", local_connection)
        monkeypatch.setattr(PublicNetworkBackend, "connect_tcp", lambda self, host, port, timeout=None, local_address=None, socket_options=None: SyncBackend().connect_tcp("8.8.8.8", port, timeout))
        try:
            context = ssl.create_default_context(cafile=str(cert))
            with httpx.Client(verify=context, trust_env=False) as client:
                secure_media_client(client)
                with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                    client.get("https://expected.test/audio")
        finally:
            server.shutdown()
            thread.join()
