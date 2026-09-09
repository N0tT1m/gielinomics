"""Wise Old Man: history that predates this bot.

:mod:`reldo.progress` records what it has watched. That is exact and
minute-granular, and it starts the day you first ask a question -- so for the
first week it can say almost nothing. WOM has been tracking OSRS accounts for
years, and answers "what have you actually done this month" on day one.

They complement rather than compete, and the split is deliberate:

* **progress.py** -- what happened since you last asked. Local, no third party,
  fine-grained enough to notice a session.
* **wom.py** -- weeks and months, plus EHP and EHB, which are the community's
  own efficiency measures and exactly the vocabulary a min-maxing answer wants.

An account has to be tracked before there is anything to read. :meth:`track`
registers it, which anybody may do for any public account -- the data is the
hiscores, which are public regardless.

API: https://docs.wiseoldman.net
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

log = logging.getLogger(__name__)

WOM_API = "https://api.wiseoldman.net/v2"

# WOM asks for a descriptive agent so they can contact you about a misbehaving
# client rather than just blocking it. Same courtesy wiki.py already pays.
DEFAULT_AGENT = "reldo/0.1 (github.com/N0tT1m/nieve)"

PERIODS = ("day", "week", "month", "year")


# Their rate limit is generous but real; this is a coaching aside, not a hot path.
DEFAULT_TIMEOUT = 20.0


class WomError(RuntimeError):
    """WOM failed, or answered in a shape we can't use."""


def _moment(value) -> datetime | None:
    """One of WOM's ISO timestamps, or None if it is missing or unparseable.

    A timestamp we cannot read must not cost the whole lookup: everything it
    feeds is a nicety, and the efficiency figures beside it are the point.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        log.warning("Could not read WOM timestamp %r", value)
        return None


@dataclass(frozen=True, slots=True)
class Efficiency:
    """The community's own measures of how well an account is played.

    EHP is "efficient hours played": the time your XP would have taken at the
    best known rates. Well below your actual played hours means the methods are
    the problem, not the effort -- which is the single most useful thing a
    min-maxing coach can say, and it is not derivable from the hiscores.
    """

    name: str
    ehp: float
    ehb: float
    exp: int
    combat_level: int
    account_type: str
    build: str
    # Hours to max and to 200m all, as WOM projects them.
    ttm: float
    tt200m: float
    # When the account last actually *changed*. Nothing in the hiscores says
    # this -- they are a snapshot with no history attached -- and it answers
    # "have you been playing?" without waiting for progress.py to have watched
    # for long enough to know.
    last_changed_at: datetime | None = None
    # Efficient hours per skill, from the snapshot WOM already sends. This is
    # the thing the hiscores genuinely cannot say: not what you have, but what
    # it cost. Fishing at 72.9 of an account's 82.3 total hours is a different
    # account from one that spread the same hours across ten skills.
    skill_hours: dict[str, float] = field(default_factory=dict)

    def idle_for(self, now: datetime | None = None) -> timedelta | None:
        """How long since the account last gained anything."""
        if self.last_changed_at is None:
            return None
        return (now or datetime.now(UTC)) - self.last_changed_at

    def summary(self, *, now: datetime | None = None) -> str:
        lines = [
            f"{self.name} ({self.account_type}, {self.build}): "
            f"combat {self.combat_level}, {self.exp:,} XP"
        ]
        lines.append(
            f"  {self.ehp:,.0f} efficient hours played, {self.ehb:,.0f} efficient hours bossed"
        )
        spent = sorted(self.skill_hours.items(), key=lambda kv: -kv[1])[:4]
        if spent:
            lines.append(
                "  where those hours went: "
                + ", ".join(f"{skill.title()} {hours:,.0f}" for skill, hours in spent)
            )
        if self.ttm > 0:
            lines.append(f"  {self.ttm:,.0f} hours to max at efficient rates")
        idle = self.idle_for(now)
        if idle is not None and idle.days >= 1:
            lines.append(f"  nothing gained in the last {idle.days} day(s)")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Gains:
    """XP gained per skill over a period, plus what it cost in EHP."""

    period: str
    skills: dict[str, int]
    ehp_gained: float

    def summary(self, *, limit: int = 6) -> str:
        if not self.skills:
            return f"No XP gained in the last {self.period}."
        ranked = sorted(self.skills.items(), key=lambda kv: -kv[1])[:limit]
        body = ", ".join(f"{s.title()} +{x:,}" for s, x in ranked)
        line = f"XP in the last {self.period}: {body}."
        if self.ehp_gained:
            line += f" ({self.ehp_gained:,.1f} efficient hours)"
        return line


class WomClient:
    """Async client for the Wise Old Man API."""

    def __init__(
        self,
        user_agent: str = DEFAULT_AGENT,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=WOM_API,
            headers={"User-Agent": user_agent},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> WomClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params) -> dict:
        try:
            response = await self._http.get(path, params=params or None)
        except httpx.HTTPError as exc:
            raise WomError(f"Could not reach Wise Old Man: {exc!r}") from exc
        if response.status_code == 404:
            raise WomError(
                "Not tracked on Wise Old Man yet. Track it first and come back "
                "in a day or two -- gains need two snapshots to exist."
            )
        if response.status_code == 429:
            raise WomError("Wise Old Man is rate-limiting; try again shortly.")
        if response.status_code != 200:
            raise WomError(f"Wise Old Man returned HTTP {response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise WomError(f"Wise Old Man sent malformed JSON: {exc!r}") from exc

    async def lookup(self, username: str) -> Efficiency:
        """Efficiency figures for a tracked account.

        The snapshot rides along in the same response, so the per-skill hours
        below cost nothing extra -- they were being parsed and dropped.
        """
        data = await self._get(f"/players/{username.strip()}")
        snapshot = ((data.get("latestSnapshot") or {}).get("data") or {})
        hours = {
            metric: float(row.get("ehp") or 0)
            for metric, row in (snapshot.get("skills") or {}).items()
            # "overall" is the sum of the rest; beside them it is every hour
            # counted twice and always the largest. Same call gains() makes.
            if metric != "overall" and (row or {}).get("ehp")
        }
        return Efficiency(
            last_changed_at=_moment(data.get("lastChangedAt")),
            skill_hours=hours,
            name=str(data.get("displayName") or username),
            ehp=float(data.get("ehp") or 0),
            ehb=float(data.get("ehb") or 0),
            exp=int(data.get("exp") or 0),
            combat_level=int(data.get("combatLevel") or 0),
            account_type=str(data.get("type") or "regular"),
            build=str(data.get("build") or "main"),
            ttm=float(data.get("ttm") or 0),
            tt200m=float(data.get("tt200m") or 0),
        )

    async def gains(self, username: str, *, period: str = "week") -> Gains:
        """XP gained per skill over a period.

        Raises:
            WomError: unknown period, or the account is not tracked. Checked
                here rather than passed through, because WOM answers an
                unrecognised period with an empty result that reads exactly like
                a player who did nothing.
        """
        if period not in PERIODS:
            raise WomError(f"Period must be one of {', '.join(PERIODS)}; got {period!r}.")
        payload = await self._get(f"/players/{username.strip()}/gained", period=period)
        skills = (payload.get("data") or {}).get("skills") or {}

        gained: dict[str, int] = {}
        ehp = 0.0
        for metric, row in skills.items():
            amount = int(((row or {}).get("experience") or {}).get("gained") or 0)
            if metric == "overall":
                # Overall is the sum of the others; listing it alongside them
                # would double every total and top every ranking.
                ehp = float(((row or {}).get("ehp") or {}).get("gained") or 0)
                continue
            if amount > 0:
                gained[metric] = amount
        return Gains(period=period, skills=gained, ehp_gained=ehp)

    async def track(self, username: str) -> Efficiency:
        """Register or refresh an account, then return its figures.

        WOM pulls the hiscores itself; this only asks it to look. Anybody may do
        this for any account, because the underlying data is public either way.
        """
        name = username.strip()
        try:
            response = await self._http.post(f"/players/{name}")
        except httpx.HTTPError as exc:
            raise WomError(f"Could not reach Wise Old Man: {exc!r}") from exc
        if response.status_code == 429:
            raise WomError("Wise Old Man is rate-limiting; try again shortly.")
        if response.status_code not in (200, 201):
            raise WomError(
                f"Wise Old Man would not track {name!r} (HTTP {response.status_code}). "
                "Check the spelling -- it has to match the hiscores exactly."
            )
        return await self.lookup(name)
