"""Builds the ordered email-finder waterfall from settings. The one place that knows every finder."""

from app.config.settings import get_settings
from app.providers.email.email_finder import EmailFinder
from app.providers.email.generect_provider import GenerectProvider
from app.providers.email.hunter_provider import HunterProvider
from app.utils.cost_tracker import CostTracker
from app.utils.logger import get_logger

logger = get_logger(__name__)
_FINDERS = {"generect": (GenerectProvider, "generect_api_key"), "hunter": (HunterProvider, "hunter_api_key")}


def create_email_finders(cost: CostTracker) -> list[EmailFinder]:
    settings = get_settings()
    finders: list[EmailFinder] = []
    for name in settings.email_finder_order:
        if name not in _FINDERS:
            raise ValueError(f"unknown email finder {name!r}; known: {', '.join(_FINDERS)}")
        cls, key_setting = _FINDERS[name]
        if getattr(settings, key_setting):
            finders.append(cls(cost))
        else:
            logger.info("email finder %s skipped: %s not set", name, key_setting.upper())
    return finders
