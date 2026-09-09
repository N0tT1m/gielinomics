"""Which data clients to build, given the settings.

One decision, made once. Before the platform existed every caller built its own
``GEClient`` and every one of them was correct; now there are two possible
answers and the question "which" would otherwise be asked, and got subtly wrong,
in four places -- the agent, the bot, the CLI and the web front end.

The rule is a single line: **``gielinomics_url`` set means read from the
platform, empty means read from upstream.** Nothing else in the package tests
that setting, so there is no path where the agent reads platform prices while
the ``/ge`` command reads the wiki's and the two disagree about what a whip
costs in the same conversation.

The functions take a :class:`~reldo.config.Settings` rather than a URL because
the platform clients need three more values from it -- token, fallback,
tracking -- and threading four arguments through every call site is how the
call sites drift apart.
"""

from __future__ import annotations

import logging

from . import gielinomics as _platform
from .config import Settings
from .ge import GEClient
from .hiscores import HiscoresClient
from .wom import WomClient

log = logging.getLogger(__name__)


def using_platform(settings: Settings) -> bool:
    """Whether reads are configured to go through Gielinomics."""
    return bool(settings.gielinomics_url.strip())


def ge_client(settings: Settings) -> GEClient:
    """The Grand Exchange client this configuration calls for.

    Returns a :class:`reldo.gielinomics.GEClient` when the platform is
    configured. That subclass is substitutable for every purpose the rest of
    the package has: it inherits name resolution, ranking, tax and liquidity
    unchanged, and adds :meth:`~reldo.gielinomics.GEClient.trend`.
    """
    if not using_platform(settings):
        return _upstream(GEClient, settings)
    return _platform.GEClient(
        settings.gielinomics_url,
        user_agent=settings.user_agent,
        fallback=settings.gielinomics_fallback,
    )


def hiscores_client(settings: Settings) -> HiscoresClient:
    """The hiscores client this configuration calls for."""
    if not using_platform(settings):
        return _upstream(HiscoresClient, settings)
    return _platform.HiscoresClient(
        settings.gielinomics_url,
        user_agent=settings.user_agent,
        token=settings.gielinomics_token,
        track=settings.gielinomics_track,
    )


def wom_client(settings: Settings) -> WomClient | None:
    """The Wise Old Man client, or None when WOM is switched off.

    None rather than a disabled client because ``bot.py`` already treats the
    absence of a WOM client as "do not offer the WOM commands", and a client
    that raises on every call would turn a configuration choice into an error
    the user sees.
    """
    if not settings.wom_enabled:
        return None
    if not using_platform(settings):
        return _upstream(WomClient, settings)
    return _platform.WomClient(settings.gielinomics_url, user_agent=settings.user_agent)


def _upstream(factory, settings: Settings):
    """Build an upstream client, leaving its default agent alone when unset.

    An empty user agent is worse than a misidentified one -- the wiki 403s it --
    so an unset value is passed as *nothing* rather than as an empty string.
    """
    agent = settings.user_agent.strip()
    return factory(agent) if agent else factory()
