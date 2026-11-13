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


def secure_media_client(client: httpx.Client, provider_id: str = "") -> httpx.Client:
    """Install per-hop validation and guard real direct connections, retaining mocks."""
    proxied = False
    transports = [client._transport, *client._mounts.values()]
    for transport in transports:
        if transport is None or isinstance(transport, httpx.MockTransport):
            continue
        if not isinstance(transport, httpx.HTTPTransport):
            raise invalid_url()
        pool = transport._pool
        if type(pool) is not httpcore.ConnectionPool:
            if provider_id != "youtube" or type(pool) not in (httpcore.HTTPProxy, httpcore.SOCKSProxy):
                raise SongSeekError("代理媒体连接无法验证目标地址，请关闭代理后重试", "unsafe_media_proxy")
            proxied = True
        else:
            pool._network_backend = PublicNetworkBackend(pool._network_backend)
    validator = validate_youtube_proxy_url if proxied else validate_url
    client.event_hooks["request"].append(lambda request: validator(str(request.url)))
    return client
