"""Outbound-request guard against server-side request forgery.

The console fetches URLs that come from a form field (the client's website) and from the open web
(search results, links on fetched pages, redirects). Without a check, `localhost:5432`, a private
network address, or a page that redirects to the cloud metadata endpoint (169.254.169.254) would be
fetched from inside our network and its text shown back to whoever typed the URL.

Every request, including each redirect hop, is checked here: http(s) only, ports 80/443 only, and every
address the host name resolves to must be public.
"""

import asyncio
import contextvars
import ipaddress
import socket

import httpcore
import httpx

ALLOWED_PORTS = frozenset({80, 443})
# The request hook resolves a name and stores the selected public address in
# this request task.  The network backend consumes that exact address for the
# TCP connection, closing the DNS-rebinding gap between validation and connect.
_PINNED_ADDRESSES: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "pinned_addresses", default=None
)


class BlockedDestinationError(httpx.RequestError):
    """Raised for a destination we refuse to contact. An httpx.RequestError, so callers that already
    treat network failures as 'page not available' handle it the same way."""


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _public_addresses(url: httpx.URL) -> list[str]:
    if url.scheme not in ("http", "https"):
        raise BlockedDestinationError(f"scheme {url.scheme!r} not allowed")
    port = url.port or (443 if url.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise BlockedDestinationError(f"port {port} not allowed")
    host = url.host
    if not host:
        raise BlockedDestinationError("no host")
    try:  # a literal address needs no lookup
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise httpx.ConnectError(f"cannot resolve {host}") from exc
        addresses = [info[4][0] for info in infos]
    if not addresses or not all(_is_public(a) for a in addresses):
        raise BlockedDestinationError(f"{host} resolves to a non-public address")
    return addresses


async def check_url(url: httpx.URL) -> None:
    await _public_addresses(url)


async def _guard(request: httpx.Request) -> None:
    addresses = await _public_addresses(request.url)
    host = request.url.host
    if host:
        # Copy rather than mutate the default/context inherited from a sibling
        # task: concurrent fetches must not overwrite one another's pins.
        _PINNED_ADDRESSES.set((_PINNED_ADDRESSES.get() or {}) | {host.lower(): addresses[0]})


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect by the address approved by `_guard`, while HTTP core retains the
    original hostname for HTTP Host and TLS SNI/certificate verification."""

    def __init__(self) -> None:
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's signature
        local_address: str | None = None,
        socket_options=None,
    ):
        address = (_PINNED_ADDRESSES.get() or {}).get(host.lower())
        if address is None:
            raise BlockedDestinationError(f"no validated address pinned for {host}")
        return await self._backend.connect_tcp(address, port, timeout, local_address, socket_options)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options=None,
    ):
        raise BlockedDestinationError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def guarded_client(**kwargs) -> httpx.AsyncClient:
    """An AsyncClient whose every request is checked and whose TCP peer is DNS-pinned.

    Redirects run request hooks independently, so every hop obtains a fresh,
    validated pin. Environment proxies are disabled: a proxy would otherwise be
    the actual unvalidated network destination.
    """
    hooks = kwargs.pop("event_hooks", {}) or {}
    hooks = {**hooks, "request": [_guard, *hooks.get("request", [])]}
    if "transport" not in kwargs:
        transport = httpx.AsyncHTTPTransport()
        # HTTPX does not yet expose httpcore's network backend as a public
        # constructor argument. Its pool is the supported httpcore integration
        # point; the backend preserves TLS SNI because only connect_tcp receives
        # the substituted address.
        transport._pool._network_backend = _PinnedNetworkBackend()  # type: ignore[attr-defined]
        kwargs["transport"] = transport
    kwargs.setdefault("trust_env", False)
    return httpx.AsyncClient(event_hooks=hooks, **kwargs)
