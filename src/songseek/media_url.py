"""Guard network connections made for untrusted audio and cover URLs."""
import ipaddress
import socket
import threading
import time
from urllib.parse import urlsplit

import httpcore
import httpx

from .errors import SongSeekError


def invalid_url():
    return SongSeekError("媒体地址不安全或无法安全连接", "invalid_audio_url")


def validate_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.port == 0 or "%" in parts.hostname):
            raise ValueError
        # Reject unsafe literals even when a test transport performs no DNS lookup.
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            pass
        else:
            if not public_ip(address):
                raise ValueError
    except (ValueError, TypeError):
        raise invalid_url() from None
    return url


def public_ip(address):
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return public_ip(address.ipv4_mapped)
        # Translation/tunneling prefixes can hide a private IPv4 destination.
        if (address.sixtofour or address.teredo
                or address in ipaddress.ip_network("64:ff9b::/96")
                or address in ipaddress.ip_network("64:ff9b:1::/48")):
            return False
    return address.is_global and not (address.is_multicast or address.is_unspecified or address.is_loopback or address.is_link_local)


_dns_slots = threading.BoundedSemaphore(16)


def resolve_addresses(host, port, timeout):
    # getaddrinfo has no portable timeout; cap outstanding abandoned lookups.
    if not _dns_slots.acquire(blocking=False):
        raise httpcore.ConnectTimeout("DNS resolver busy")
    done = threading.Event()
    result = []

    def resolve():
        try:
            result.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as error:
            result.append(error)
        finally:
            _dns_slots.release()
            done.set()

    try:
        threading.Thread(target=resolve, daemon=True).start()
    except BaseException:
        _dns_slots.release()
        raise
    if not done.wait(timeout):
        raise httpcore.ConnectTimeout("DNS resolution timed out")
    if isinstance(result[0], OSError):
        raise httpcore.ConnectError(str(result[0])) from result[0]
    return result[0]


class PublicNetworkBackend(httpcore.NetworkBackend):
    def __init__(self, backend):
        self.backend = backend

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        addresses = resolve_addresses(host, port, timeout)
        safe = []
        for family, _, _, _, sockaddr in addresses:
            try:
                ip = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                continue
            if family in (socket.AF_INET, socket.AF_INET6) and public_ip(ip) and str(ip) not in safe:
                safe.append(str(ip))
        if not safe:
            raise invalid_url()
        last_error = None
        for index, ip in enumerate(safe):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise httpcore.ConnectTimeout("Connection timed out")
            per_address = None if remaining is None else remaining / (len(safe) - index)
            try:
                stream = self.backend.connect_tcp(ip, port, per_address, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                last_error = error
                continue
            try:
                peer = stream.get_extra_info("server_addr")
                if not peer or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(ip) or not public_ip(ipaddress.ip_address(peer[0])):
                    raise invalid_url()
            except (ValueError, TypeError, IndexError):
                stream.close()
                raise invalid_url() from None
            except BaseException:
                stream.close()
                raise
            return stream
        raise last_error or invalid_url()

    def connect_unix_socket(self, *args, **kwargs):
        raise invalid_url()

    def sleep(self, seconds):
        self.backend.sleep(seconds)


def validate_youtube_proxy_url(url: str) -> str:
    validate_url(url)
    parts = urlsplit(url)
    host = parts.hostname.lower()
    domains = ("googlevideo.com", "ytimg.com", "youtube.com")
    if (parts.scheme != "https" or parts.port not in (None, 443)
            or not any(host == domain or host.endswith("." + domain) for domain in domains)):
        raise SongSeekError("代理媒体地址不属于允许的 YouTube 域名", "unsafe_media_proxy")
    return url


class PinnedTLSContext:
    """Override only the target tunnel's TLS hostname, retaining its context."""
    def __init__(self, context, hostname):
        self.context = context
        self.hostname = hostname

    def set_alpn_protocols(self, protocols):
        self.context.set_alpn_protocols(protocols)

    def wrap_socket(self, sock, *args, **kwargs):
        kwargs["server_hostname"] = self.hostname
        return self.context.wrap_socket(sock, *args, **kwargs)

    def wrap_bio(self, incoming, outgoing, *args, **kwargs):
        kwargs["server_hostname"] = self.hostname
        return self.context.wrap_bio(incoming, outgoing, *args, **kwargs)


class PinnedProxyTransport(httpx.BaseTransport):
    """Resolve through authenticated DNS-over-HTTPS and tunnel to a public IP."""
    def __init__(self, transport):
        self.transport = transport
        self.pool = transport._pool

    def _make_pool(self, hostname):
        p = self.pool
        options = dict(proxy_url=p._proxy_url, ssl_context=PinnedTLSContext(p._ssl_context, hostname),
                       max_connections=p._max_connections, max_keepalive_connections=p._max_keepalive_connections,
                       keepalive_expiry=p._keepalive_expiry, http1=p._http1, http2=p._http2,
                       network_backend=p._network_backend)
        if type(p) is httpcore.HTTPProxy:
            return httpcore.HTTPProxy(**options, proxy_headers=p._proxy_headers, proxy_ssl_context=p._proxy_ssl_context)
        return httpcore.SOCKSProxy(**options, proxy_auth=p._proxy_auth)

    def _request_ip(self, request, ip, hostname):
        transport = httpx.HTTPTransport()
        transport._pool.close()
        transport._pool = self._make_pool(hostname)
        try:
            url = request.url.copy_with(host=ip)
            extensions = dict(request.extensions, sni_hostname=hostname)
            outgoing = httpx.Request(request.method, url, headers=request.headers, stream=request.stream, extensions=extensions)
            response = transport.handle_request(outgoing)
        except BaseException:
            transport.close()
            raise
        response.stream = ClosingStream(response.stream, transport)
        return response

    def _resolve(self, host, timeout):
        # Bootstrap DNS-over-HTTPS using a fixed public Google DNS address; the
        # TLS certificate is validated for dns.google, not the numeric address.
        target = httpx.Request("GET", "https://dns.google/resolve", params={"name": host, "type": "A"},
                               extensions={"timeout": timeout})
        response = self._request_ip(target, "8.8.8.8", "dns.google")
        try:
            response.read()
            if response.status_code != 200:
                raise invalid_url()
            data = response.json()
        finally:
            response.close()
        if data.get("Status") != 0 or data.get("TC"):
            raise invalid_url()
        safe = []
        for answer in data.get("Answer", []):
            if answer.get("type") != 1:
                continue
            try:
                ip = ipaddress.ip_address(answer.get("data", ""))
            except ValueError:
                continue
            if public_ip(ip) and str(ip) not in safe:
                safe.append(str(ip))
        if not safe:
            raise invalid_url()
        return safe

    def handle_request(self, request):
        validate_youtube_proxy_url(str(request.url))
        timeout = request.extensions.get("timeout", {})
        connect = timeout.get("connect")
        deadline = None if connect is None else time.monotonic() + connect
        addresses = self._resolve(request.url.host, timeout)
        last = None
        for index, ip in enumerate(addresses):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise httpx.ConnectTimeout("Proxy media connection timed out", request=request)
            request.extensions["timeout"] = dict(timeout, connect=None if remaining is None else remaining / (len(addresses) - index))
            try:
                return self._request_ip(request, ip, request.url.host)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError) as error:
                last = error
        raise last or invalid_url()

    def close(self):
        self.transport.close()


class ClosingStream(httpx.SyncByteStream):
    def __init__(self, stream, transport):
        self.stream = stream
        self.transport = transport

    def __iter__(self):
        yield from self.stream

    def close(self):
        try:
            self.stream.close()
        finally:
            self.transport.close()


def secure_media_client(client: httpx.Client, provider_id: str = "") -> httpx.Client:
    """Install per-hop validation and guard real connections, retaining mocks."""
    def guard(transport):
        if transport is None or isinstance(transport, httpx.MockTransport):
            return transport
        if not isinstance(transport, httpx.HTTPTransport):
            raise invalid_url()
        pool = transport._pool
        if type(pool) is httpcore.ConnectionPool:
            pool._network_backend = PublicNetworkBackend(pool._network_backend)
            return transport
        if provider_id != "youtube" or type(pool) not in (httpcore.HTTPProxy, httpcore.SOCKSProxy):
            raise SongSeekError("代理媒体连接无法验证目标地址，请关闭代理后重试", "unsafe_media_proxy")
        return PinnedProxyTransport(transport)

    client._transport = guard(client._transport)
    for pattern, transport in client._mounts.items():
        client._mounts[pattern] = guard(transport)
    client.event_hooks["request"].append(lambda request: validate_url(str(request.url)))
    return client
