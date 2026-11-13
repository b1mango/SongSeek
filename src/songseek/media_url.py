"""Guard network connections made for untrusted audio and cover URLs."""
import ipaddress
import socket
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


class PublicNetworkBackend(httpcore.NetworkBackend):
    def __init__(self, backend):
        self.backend = backend

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as error:
            raise httpcore.ConnectError(str(error)) from error
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
        for ip in safe:
            try:
                stream = self.backend.connect_tcp(ip, port, timeout, local_address, socket_options)
            except httpcore.ConnectError as error:
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


def secure_media_client(client: httpx.Client) -> httpx.Client:
    """Install per-hop validation and guard real direct connections, retaining mocks."""
    client.event_hooks["request"].append(lambda request: validate_url(str(request.url)))
    transports = [client._transport, *client._mounts.values()]
    for transport in transports:
        if transport is None or isinstance(transport, httpx.MockTransport):
            continue
        if not isinstance(transport, httpx.HTTPTransport):
            raise invalid_url()
        pool = transport._pool
        # Proxy pools resolve or connect to the target outside our control.
        if type(pool) is not httpcore.ConnectionPool:
            raise SongSeekError("代理媒体连接无法验证目标地址，请关闭代理后重试", "unsafe_media_proxy")
        pool._network_backend = PublicNetworkBackend(pool._network_backend)
    return client
