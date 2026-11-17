"""Retry for transient provider errors (429 / 5xx / network), honouring Retry-After."""

import asyncio

import httpx

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    attempts: int = 3,
    base_delay: float = 1.0,
    retry_status: frozenset[int] = RETRY_STATUS,
    **kwargs,
) -> httpx.Response:
    """`retry_status` lets a provider opt out of retrying a code it uses for a permanent condition
    (Hunter answers 429 when the plan's credits are spent: retrying cannot help)."""
    for attempt in range(attempts):
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
        else:
            if resp.status_code not in retry_status or attempt == attempts - 1:
                return resp
            retry_after = resp.headers.get("retry-after", "")
            if retry_after.isdigit():
                await asyncio.sleep(min(float(retry_after), 30))
                continue
        await asyncio.sleep(base_delay * (3**attempt))
    raise AssertionError("unreachable")
