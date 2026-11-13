import socket

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
