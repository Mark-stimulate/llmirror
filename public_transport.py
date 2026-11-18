"""Resolve and validate the actual address used by member-supplied requests."""
import asyncio
import ipaddress
import socket

import httpx


class PublicNetworkBackend:
    def __init__(self, backend):
        self.backend = backend

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        name = host.decode() if isinstance(host, bytes) else host
        if name.lower() == "localhost" or name.lower().endswith((".localhost", ".local")):
            raise httpx.ConnectError("测试成员的临时接口只支持公网地址")
        addresses = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, name, port, 0, socket.SOCK_STREAM), timeout=timeout or 15)
        candidates = list(dict.fromkeys(item[4][0] for item in addresses))
        if not candidates or any(not ipaddress.ip_address(value).is_global for value in candidates):
            raise httpx.ConnectError("测试成员的临时接口不能访问本机、内网或保留地址")
        failure = None
        for address in candidates:
            try:
                # Connect to the checked address, keeping HTTP Host and TLS SNI on the original URL.
                return await self.backend.connect_tcp(address, port, timeout, local_address, socket_options)
            except Exception as exc:
                failure = exc
        raise failure or httpx.ConnectError("无法连接临时接口")

    async def connect_unix_socket(self, *args, **kwargs):
        raise httpx.ConnectError("临时接口不支持本地套接字")

    async def sleep(self, seconds):
        await self.backend.sleep(seconds)


def public_transport():
    transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False,
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=16))
    # httpx/httpcore are pinned; enforcing here also covers new pooled connections and DNS rebinding.
    transport._pool._network_backend = PublicNetworkBackend(transport._pool._network_backend)
    return transport
