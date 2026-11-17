"""The interface discovery uses to search the web, and a client that falls back between providers."""

from typing import Protocol

import httpx

from app.exceptions.custom_exceptions import CostCapExceededError
from app.providers.search.serper_client import SearchHit
from app.utils.logger import get_logger

logger = get_logger(__name__)


class SearchClient(Protocol):
    async def query(
        self, q: str, kind: str = "search", gl: str = "uk", num: int = 10, tbs: str | None = None
    ) -> list[SearchHit]: ...

    async def resolve_redirect(self, link: str) -> str: ...

    async def aclose(self) -> None: ...


class FallbackSearchClient:
    """Tries each provider in order; a provider that errors (outage, bad key, exhausted credits) hands
    the query to the next. The spend cap is never 'an error': it stops the run, it is not routed around."""

    def __init__(self, providers: list[SearchClient]):
        if not providers:
            raise ValueError("at least one search provider is required")
        self.providers = providers

    async def query(
        self, q: str, kind: str = "search", gl: str = "uk", num: int = 10, tbs: str | None = None
    ) -> list[SearchHit]:
        last: Exception | None = None
        for provider in self.providers:
            try:
                return await provider.query(q, kind=kind, gl=gl, num=num, tbs=tbs)
            except CostCapExceededError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                logger.warning(
                    "search via %s failed (%s); trying the next provider",
                    type(provider).__name__,
                    type(exc).__name__,
                )
        raise last if last else RuntimeError("no search provider answered")

    async def resolve_redirect(self, link: str) -> str:
        return await self.providers[0].resolve_redirect(link)

    async def aclose(self) -> None:
        for provider in self.providers:
            await provider.aclose()
