"""Builds the search client from settings: the configured providers, in order, behind one fallback."""

from app.config.settings import get_settings
from app.providers.search.search_client import FallbackSearchClient, SearchClient
from app.providers.search.serper_client import SerperClient
from app.providers.search.tinyfish_search_client import TinyFishSearchClient
from app.utils.cost_tracker import CostTracker

_PROVIDERS = {
    "serper": (SerperClient, "serper_api_key"),
    "tinyfish": (TinyFishSearchClient, "tinyfish_api_key"),
}


def create_search_client(cost: CostTracker) -> SearchClient:
    settings = get_settings()
    providers = []
    for name in settings.search_provider_order:
        if name not in _PROVIDERS:
            raise ValueError(f"unknown search provider {name!r}; known: {', '.join(_PROVIDERS)}")
        cls, key_setting = _PROVIDERS[name]
        if getattr(settings, key_setting):
            providers.append(cls(cost))
    if not providers:
        raise ValueError("no search provider has an API key: set SERPER_API_KEY or TINYFISH_API_KEY")
    return providers[0] if len(providers) == 1 else FallbackSearchClient(providers)
