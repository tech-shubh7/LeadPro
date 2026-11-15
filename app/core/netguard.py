"""Outbound-request guard against server-side request forgery.

The console fetches URLs that come from a form field (the client's website) and from the open web
(search results, links on fetched pages, redirects). Without a check, `localhost:5432`, a private
network address, or a page that redirects to the cloud metadata endpoint (169.254.169.254) would be
fetched from inside our network and its text shown back to whoever typed the URL.

Every request, including each redirect hop, is checked here: http(s) only, ports 80/443 only, and every
address the host name resolves to must be public.
"""
import asyncio
import ipaddress
import socket

import httpx

ALLOWED_PORTS = frozenset({80, 443})


class BlockedDestination(httpx.RequestError):
    """Raised for a destination we refuse to contact. An httpx.RequestError, so callers that already
    treat network failures as 'page not available' handle it the same way."""


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def check_url(url: httpx.URL) -> None:
    if url.scheme not in ("http", "https"):
        raise BlockedDestination(f"scheme {url.scheme!r} not allowed")
    port = url.port or (443 if url.scheme == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise BlockedDestination(f"port {port} not allowed")
    host = url.host
    if not host:
        raise BlockedDestination("no host")
    try:  # a literal address needs no lookup
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise httpx.ConnectError(f"cannot resolve {host}") from exc
        addresses = [info[4][0] for info in infos]
    if not addresses or not all(_is_public(a) for a in addresses):
        raise BlockedDestination(f"{host} resolves to a non-public address")


async def _guard(request: httpx.Request) -> None:
    await check_url(request.url)


def guarded_client(**kwargs) -> httpx.AsyncClient:
    """An AsyncClient whose every request (redirects included: httpx runs request hooks per hop) is checked."""
    hooks = kwargs.pop("event_hooks", {}) or {}
    hooks = {**hooks, "request": [_guard, *hooks.get("request", [])]}
    return httpx.AsyncClient(event_hooks=hooks, **kwargs)
