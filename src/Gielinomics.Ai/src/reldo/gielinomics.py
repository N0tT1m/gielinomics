"""Reading the game's data from this platform instead of from upstream.

Every client in this package was written against a public API -- the wiki's
real-time prices, Jagex's hiscores, Wise Old Man. Each of those answers exactly
one question: *what is true right now*. None of them keeps yesterday.

Gielinomics does. The ingest workers have been writing five-minute price bars,
hourly bars and hiscore snapshots into TimescaleDB since the first commit, which
means the platform can answer a question none of the upstreams can: **what has
this been doing.** "Is the whip worth buying" is a different question from "is
the whip trending up", and until now only the first one was answerable here.

So this module is not a rewrite. It is three subclasses that change *where the
bytes come from* and nothing else:

======================  =========================  ============================
class                   overrides                  everything else
======================  =========================  ============================
:class:`GEClient`       ``_get``                   ``find``, ``prices``,
                                                   ``lookup``, ``exactly``,
                                                   ranking, tax, liquidity
:class:`HiscoresClient` ``lookup``                 combat level, ``meets``,
                                                   activities, ``summary``
:class:`WomClient`      ``gains``                  ``lookup``, ``track``
======================  =========================  ============================

**Why subclass rather than write new clients.** ``ge.py`` is 730 lines, and
about six hundred of them are judgement rather than transport: the four-tier
name resolution that stops "granite" pricing a granite hammer, the spread sanity
check, the liquidity bands, the tax rules, the buy-versus-sell estimate split.
Reimplementing that against a second set of field names would be writing the
same reasoning twice and getting it subtly different in one of them. The
platform serves the upstream *shape* (see ``PriceMirrorEndpoints`` on the C#
side) precisely so that this file only has to move a base URL.

**What is deliberately not routed through the platform.** WOM's efficiency
model -- EHP, EHB, time-to-max -- is a community-maintained ratings system, not
an observation. Gielinomics does not compute it and proxying the call through
the platform would add a hop and a failure mode for no data anybody gained. So
:meth:`WomClient.lookup` stays pointed at Wise Old Man; only :meth:`gains`,
which is a question about observed history, moves.

**Failure is a fallback, not an error.** The platform is one more service that
can be down or not yet backfilled, and an assistant that answers "I could not
reach the database" to "how much is a whip" is worse than one that asks the
wiki. Every method here falls back to its upstream implementation and logs the
reason. The cost of the fallback is the history, which the caller was not
guaranteed anyway.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from . import ge as _ge
from . import hiscores as _hiscores
from . import wom as _wom

log = logging.getLogger(__name__)

# The platform's own routes, not upstream's. Kept here rather than inlined so
# the whole surface this module depends on is readable in one screen -- if the
# C# side renames a route, this list is the blast radius.
MAPPING_PATH = "/api/prices/mapping"
LATEST_PATH = "/api/prices/latest"
WINDOW_PATH = "/api/prices/{window}"
SNAPSHOT_PATH = "/api/players/{name}/snapshot"
GAINS_PATH = "/api/players/{name}/gains"
TRACK_PATH = "/api/players/{name}/track"
SERIES_PATH = "/api/items/{item_id}/prices"
STATS_PATH = "/api/items/{item_id}/stats"

# Windows the mirror aggregates. Upstream serves the same four names, and
# GEClient only ever asks for "latest" and "24h" -- the other two are here
# because trend() reaches for them and a caller may.
WINDOWS = ("5m", "1h", "6h", "24h")


class GielinomicsError(RuntimeError):
    """The platform could not be reached, or sent something unusable."""


@dataclass(frozen=True, slots=True)
class Bar:
    """One retained price bar.

    Named for what it is rather than mirroring ``PricePoint`` on the C# side,
    because the two carry different types: the wire sends decimals as strings
    to avoid float rounding, and this is after they have been parsed.
    """

    at: datetime
    avg_high: float | None
    avg_low: float | None
    high_volume: int
    low_volume: int

    @property
    def mid(self) -> float | None:
        """Midpoint of the two sides, or whichever side traded."""
        sides = [side for side in (self.avg_high, self.avg_low) if side]
        return sum(sides) / len(sides) if sides else None


@dataclass(frozen=True, slots=True)
class Trend:
    """What a price has been doing over a window.

    The thing none of the upstream APIs can answer, stated in the form a person
    asked it: not a list of bars, but a direction and a size.
    """

    item: _ge.Item
    window: str
    start: float | None
    end: float | None
    low: float | None
    high: float | None
    volume: int
    samples: int

    @property
    def change(self) -> float | None:
        """Absolute move over the window."""
        if self.start is None or self.end is None:
            return None
        return self.end - self.start

    @property
    def change_percent(self) -> float | None:
        """Move as a percentage of where it started."""
        if not self.start or self.end is None:
            return None
        return (self.end - self.start) / self.start * 100

    @property
    def direction(self) -> str:
        """A word for the move, banded so noise does not read as a trend.

        The bands are deliberately wide. Grand Exchange prices wander a percent
        or two on nothing, and an assistant that calls every wander a trend is
        an assistant nobody should trade on.
        """
        pct = self.change_percent
        if pct is None:
            return "unknown"
        if pct >= 10:
            return "rising sharply"
        if pct >= 3:
            return "rising"
        if pct <= -10:
            return "falling sharply"
        if pct <= -3:
            return "falling"
        return "flat"

    def summary(self) -> str:
        """One line, in the vocabulary the rest of the package prints in."""
        if self.end is None or self.samples == 0:
            return f"{self.item.name}: no retained history over the last {self.window}."
        line = f"{self.item.name}: {self.direction} over {self.window}"
        pct = self.change_percent
        if pct is not None and self.start is not None:
            line += f", {self.start:,.0f} -> {self.end:,.0f} gp ({pct:+.1f}%)"
        else:
            line += f", now {self.end:,.0f} gp"
        if self.low is not None and self.high is not None and self.high > self.low:
            line += f"; ranged {self.low:,.0f}-{self.high:,.0f}"
        if self.volume:
            line += f"; {self.volume:,} traded"
        return line


class _PlatformMixin:
    """Shared transport against the platform.

    Not a base class with an ``__init__`` of its own on purpose: each client
    below already has a constructor with its own upstream arguments, and this
    only adds the two things they share.
    """

    _base_url: str
    _platform: httpx.AsyncClient

    def _configure(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._base_url = base_url.rstrip("/")
        self._platform = httpx.AsyncClient(
            base_url=self._base_url, headers=headers, timeout=timeout, transport=transport
        )

    async def _platform_get(self, path: str, **params: Any) -> Any:
        """One GET against the platform, with its failures named.

        Raises:
            GielinomicsError: unreachable, non-2xx, or not JSON. Callers treat
                all three the same -- fall back to upstream -- so they are one
                exception type rather than three the caller would have to
                enumerate to do the same thing with each.
        """
        try:
            wanted = {k: v for k, v in params.items() if v is not None}
            response = await self._platform.get(path, params=wanted)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise GielinomicsError(
                f"Gielinomics returned HTTP {exc.response.status_code} for {path}."
            ) from exc
        except httpx.HTTPError as exc:
            raise GielinomicsError(f"Could not reach Gielinomics: {exc!r}") from exc
        except ValueError as exc:
            raise GielinomicsError(f"Gielinomics sent malformed JSON for {path}: {exc!r}") from exc


class GEClient(_PlatformMixin, _ge.GEClient):
    """Prices from the platform's retained history.

    Overrides transport and nothing else. ``find``, ``prices``, ``lookup``,
    ``exactly``, ``rank``, ``verdict`` and ``compare`` are inherited unchanged
    and keep working because the platform serves upstream's field names.

    Args:
        base_url: The platform's API root.
        user_agent: Used only by the upstream fallback path.
        timeout: Per-request timeout in seconds.
        fallback: Ask the wiki when the platform cannot answer. Off makes a
            misconfigured base URL loud instead of silently slow.
        transport: Test seam for the platform client.
    """

    def __init__(
        self,
        base_url: str,
        *,
        user_agent: str = "",
        timeout: float = 20.0,
        fallback: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        upstream_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # An empty agent must never reach upstream -- the wiki 403s it outright -- so an
        # unset value leaves the inherited default in place rather than overwriting it.
        if user_agent:
            _ge.GEClient.__init__(self, user_agent, timeout=timeout, transport=upstream_transport)
        else:
            _ge.GEClient.__init__(self, timeout=timeout, transport=upstream_transport)
        self._configure(base_url, timeout=timeout, transport=transport)
        self._fallback = fallback

    async def aclose(self) -> None:
        await self._platform.aclose()
        await _ge.GEClient.aclose(self)

    async def _get(self, path: str) -> Any:
        """Serve one of upstream's three paths from the platform.

        ``GEClient`` only ever asks for ``mapping``, ``latest`` and a window
        name, which is what makes this the whole integration: one method, three
        cases, and everything built on top of them is untouched.
        """
        route = MAPPING_PATH if path == "mapping" else (
            LATEST_PATH if path == "latest" else (
                WINDOW_PATH.format(window=path) if path in WINDOWS else None
            )
        )
        if route is None:
            log.debug("No platform route for GE path %r; asking upstream.", path)
            return await _ge.GEClient._get(self, path)

        try:
            return await self._platform_get(route)
        except GielinomicsError as exc:
            if not self._fallback:
                raise _ge.GEError(str(exc)) from exc
            log.warning("Gielinomics could not serve %r (%s); falling back to the wiki.", path, exc)
            return await _ge.GEClient._get(self, path)

    async def series(self, item_id: int, *, window: str = "7d", interval: str = "1h") -> list[Bar]:
        """Retained price bars for one item, oldest first.

        Raises:
            GielinomicsError: the platform could not answer. No fallback here,
                deliberately -- upstream has no history to fall back *to*, and
                returning an empty list would read as "this item never traded".
        """
        to = datetime.now(UTC)
        payload = await self._platform_get(
            SERIES_PATH.format(item_id=item_id),
            **{
                "from": (to - _parse_window(window)).isoformat(),
                "to": to.isoformat(),
                "interval": interval,
            },
        )
        points = payload.get("points") if isinstance(payload, dict) else None
        if not isinstance(points, list):
            raise GielinomicsError(f"Gielinomics sent no price points for item {item_id}.")
        return [
            Bar(
                at=_moment(row.get("bucketTs")),
                avg_high=_number(row.get("avgHigh")),
                avg_low=_number(row.get("avgLow")),
                high_volume=int(row.get("highVolume") or 0),
                low_volume=int(row.get("lowVolume") or 0),
            )
            for row in points
            if isinstance(row, dict) and row.get("bucketTs")
        ]

    async def trend(self, query: str, *, window: str = "7d", interval: str = "1h") -> Trend | None:
        """What the thing a person named has been doing, or None if no such item.

        Resolves the name through the inherited four-tier :meth:`find`, so the
        same question that prices correctly also trends correctly -- "granite"
        does not trend a granite hammer here either.
        """
        found = await self.find(query, limit=1)
        if not found:
            return None
        item = found[0]
        bars = await self.series(item.id, window=window, interval=interval)
        mids = [bar.mid for bar in bars if bar.mid is not None]
        return Trend(
            item=item,
            window=window,
            start=mids[0] if mids else None,
            end=mids[-1] if mids else None,
            low=min(mids) if mids else None,
            high=max(mids) if mids else None,
            volume=sum(bar.high_volume + bar.low_volume for bar in bars),
            samples=len(bars),
        )


class HiscoresClient(_PlatformMixin, _hiscores.HiscoresClient):
    """Player stats from the platform's retained snapshots.

    The platform stores each hiscores response verbatim, so this reads the same
    shape :class:`reldo.hiscores.HiscoresClient` parses from Jagex -- activity
    counters included. Nothing about combat level, ``meets`` or ``summary``
    changes.

    Args:
        base_url: The platform's API root.
        user_agent: Used by the upstream fallback path.
        token: API token, needed only when ``track`` is on.
        timeout: Per-request timeout in seconds.
        track: Ask the platform to start tracking an account it has never seen.
            The first answer still comes from Jagex; the point is that the
            *second* one, next week, can come with history attached.
        transport: Test seam for the platform client.
    """

    def __init__(
        self,
        base_url: str,
        *,
        user_agent: str = "",
        token: str = "",
        timeout: float = 20.0,
        track: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        upstream_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # An empty agent must never reach upstream -- the wiki 403s it outright -- so an
        # unset value leaves the inherited default in place rather than overwriting it.
        if user_agent:
            _hiscores.HiscoresClient.__init__(
                self, user_agent, timeout=timeout, transport=upstream_transport
            )
        else:
            _hiscores.HiscoresClient.__init__(
                self, timeout=timeout, transport=upstream_transport
            )
        self._configure(base_url, token=token, timeout=timeout, transport=transport)
        self._track = track and bool(token)

    async def aclose(self) -> None:
        await self._platform.aclose()
        await _hiscores.HiscoresClient.aclose(self)

    async def lookup(self, username: str) -> _hiscores.Player:
        """A player's stats, from the platform when it has them.

        Falls through to Jagex for anybody untracked, which is most people the
        first time they are asked about. That is the expected path rather than
        an error case: the platform polls accounts it knows, and it learns
        about an account by somebody asking.
        """
        name = username.strip()
        if not name:
            raise _hiscores.HiscoresError("Username is empty.")

        try:
            payload = await self._platform_get(SNAPSHOT_PATH.format(name=name))
            snapshot = payload.get("payload") if isinstance(payload, dict) else None
            if isinstance(snapshot, dict) and snapshot.get("skills"):
                return _parse_hiscores(payload.get("player") or name, snapshot)
            log.debug("Gielinomics has no usable snapshot for %r; asking Jagex.", name)
        except GielinomicsError as exc:
            log.debug("Gielinomics has no snapshot for %r (%s); asking Jagex.", name, exc)

        player = await _hiscores.HiscoresClient.lookup(self, name)
        if self._track:
            await self._start_tracking(player.name)
        return player

    async def _start_tracking(self, name: str) -> None:
        """Register an account so the platform starts keeping its history.

        Best-effort and silent on failure. Tracking is a side benefit of having
        been asked, and a caller who wanted a stat block should not get an
        error because the enrolment 401'd.
        """
        try:
            response = await self._platform.post(TRACK_PATH.format(name=name))
            if response.status_code >= 400:
                log.debug("Could not track %r: HTTP %s", name, response.status_code)
        except httpx.HTTPError as exc:
            log.debug("Could not track %r: %r", name, exc)


class WomClient(_PlatformMixin, _wom.WomClient):
    """Gains from the platform's own polling; efficiency still from WOM.

    The split is on purpose and it is not arbitrary. ``gains`` asks what an
    account actually did over a window, which is an observation the platform
    has been recording; ``lookup`` asks for EHP and EHB, which are a community
    ratings model the platform does not implement. Routing the second one
    through here would add a hop to reach the same Wise Old Man response.

    Args:
        base_url: The platform's API root.
        user_agent: Used by the WOM path.
        timeout: Per-request timeout in seconds.
        transport: Test seam for the platform client.
    """

    def __init__(
        self,
        base_url: str,
        *,
        user_agent: str = "",
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        upstream_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # An empty agent must never reach upstream -- the wiki 403s it outright -- so an
        # unset value leaves the inherited default in place rather than overwriting it.
        if user_agent:
            _wom.WomClient.__init__(self, user_agent, timeout=timeout, transport=upstream_transport)
        else:
            _wom.WomClient.__init__(self, timeout=timeout, transport=upstream_transport)
        self._configure(base_url, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._platform.aclose()
        await _wom.WomClient.aclose(self)

    async def gains(self, username: str, *, period: str = "week") -> _wom.Gains:
        """XP gained per skill, from the platform's snapshots.

        ``ehp_gained`` comes back zero: efficient hours are WOM's model and the
        platform does not compute them. Zero is what ``Gains.summary`` already
        treats as "do not mention it", so the line reads correctly rather than
        claiming an efficiency figure that was never measured.
        """
        try:
            payload = await self._platform_get(
                GAINS_PATH.format(name=username.strip()), period=_period_to_window(period)
            )
        except GielinomicsError as exc:
            log.warning(
                "Gielinomics could not serve gains for %r (%s); asking WOM.", username, exc
            )
            return await _wom.WomClient.gains(self, username, period=period)

        rows = payload.get("skills") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            log.warning(
                "Gielinomics sent no gains for %r; asking WOM.", username
            )
            return await _wom.WomClient.gains(self, username, period=period)

        skills = {
            str(row["name"]).lower(): int(row.get("gainedXp") or 0)
            for row in rows
            if isinstance(row, dict) and row.get("name") and int(row.get("gainedXp") or 0) > 0
        }
        return _wom.Gains(period=period, skills=skills, ehp_gained=0.0)


# ---------------------------------------------------------------------------
# Parsing helpers. Small, and separate from the classes because the tests for
# them are about wire shapes rather than about clients.
# ---------------------------------------------------------------------------

def _parse_hiscores(name: str, payload: dict[str, Any]) -> _hiscores.Player:
    """Build a Player from a stored hiscores payload.

    Deliberately the same field names ``hiscores.py`` reads, because it is the
    same document -- the platform stored what Jagex sent without reshaping it.
    """
    try:
        skills = {
            s["name"]: _hiscores.Skill(s["name"], s["rank"], s["level"], s["xp"])
            for s in payload["skills"]
        }
        activities = {
            a["name"]: _hiscores.Activity(a["name"], a["rank"], a["score"])
            for a in payload.get("activities", [])
        }
    except (KeyError, TypeError) as exc:
        raise _hiscores.HiscoresError(f"Stored snapshot has an unexpected shape: {exc!r}") from exc
    return _hiscores.Player(name=name, skills=skills, activities=activities)


def _moment(value: Any) -> datetime:
    """Parse an ISO-8601 instant from the wire, defaulting to UTC."""
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _number(value: Any) -> float | None:
    """Parse a decimal that may arrive as a JSON string, or None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Suffixes the platform's window parser accepts, which is what these strings
# are ultimately handed to.
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def _parse_window(window: str) -> timedelta:
    """Turn "7d" into a timedelta.

    Raises:
        ValueError: unparseable. Loud rather than defaulted: a silently wrong
            window produces a trend over the wrong period, which reads as a
            confident answer to a question nobody asked.
    """
    text = window.strip().lower()
    if len(text) < 2 or text[-1] not in _UNITS or not text[:-1].isdigit():
        raise ValueError(f"Unparseable window {window!r}. Use forms like 6h, 7d, 2w.")
    return timedelta(**{_UNITS[text[-1]]: int(text[:-1])})


# WOM names its periods; the platform takes a duration. The two vocabularies
# meet here rather than at either call site.
_PERIODS = {"day": "1d", "week": "7d", "month": "30d", "year": "365d"}


def _period_to_window(period: str) -> str:
    """Translate a WOM period name to the platform's window syntax."""
    return _PERIODS.get(period.strip().lower(), "7d")
