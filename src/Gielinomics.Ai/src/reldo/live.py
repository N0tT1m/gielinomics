"""Live game state, pushed in from a RuneLite plugin.

The hiscores say what you have; :mod:`reldo.progress` says what you gained this
week. Neither can say what you are doing *right now*, and that is the difference
between advice and coaching: "mine granite" is a guide, "you have been at the
quarry forty minutes for twelve thousand XP, that is half rate" is a coach.

Jagex exposes nothing for this, and no third party can. The only legitimate
source is the player's own client, so the flow is inbound: a RuneLite plugin
POSTs state here, and reldo never reaches into the game. See ``runelite-plugin/``.

**This reads. It never writes.** Nothing here, and nothing in the plugin, sends
input to the game -- reading your own client's state and getting advice is what
RuneLite's plugin API is for and what Wise Old Man and the XP tracker already do.
Sending input would be botting, and it is the line this stops at.

The server is aiohttp, which discord.py already depends on, so it adds nothing
to install.

**Off loopback, the token is not optional.** This module's own default is
``127.0.0.1``, but the deployment it was written for runs RuneLite and the bot
on different machines, so ``RELDO_LIVE_HOST`` is ``0.0.0.0`` -- every interface
on the box. An empty token disables the check altogether, and those two defaults
together are an unauthenticated endpoint that folds whatever it is sent into a
player's prompt. :func:`serve` refuses the combination rather than starting.
"""

from __future__ import annotations

import hmac
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from aiohttp import web

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8099

# Addresses only this machine can reach. Anything else is the network, and the
# token stops being optional there. "0.0.0.0" is deliberately absent: it is not
# a loopback address, it is *all* of them.
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

# State older than this is history, not "what you are doing". A tick is 0.6s and
# the plugin throttles, so anything past a minute means the client is closed,
# logged out, or the machine went to sleep.
FRESH_SECONDS = 60.0

# A gap longer than this starts a new session. Two hours of granite with a lunch
# break in the middle is two sessions, and averaging across the break reports a
# rate nobody achieved.
SESSION_GAP = 600.0

# Refuse absurd payloads outright rather than storing them.
MAX_INVENTORY = 28
MAX_FIELD = 200
# A real bank runs to hundreds of distinct items. Capped so one POST cannot put
# an unbounded list in memory, and generous enough that the cap is not what
# somebody hits.
MAX_BANK = 400
MAX_EQUIPMENT = 14
MAX_QUESTS = 300

# Players held at once, least-recently-reported evicted first. The per-payload
# caps above bound one POST; nothing bounded how many *players* could be in
# here, and the dicts only ever grew -- one entry per distinct name, for the
# life of the process. A profile runs to tens of kilobytes with a bank in it, so
# this is the difference between a bounded few megabytes and a slow leak that
# needs a restart to clear.
#
# Generous against the real population: this is a Discord bot's linked accounts,
# which is dozens. It is a ceiling on what a client posting names in a loop can
# cost, not a limit anybody should reach.
#
# A count rather than an age, because ageing is what the two halves disagree
# about. State is perishable and LiveState.fresh already refuses to report it
# after a minute; a profile is durable on purpose -- a quest you finished is
# still finished with the client closed -- so expiring it on a timer would throw
# away the one thing here no public API can replace. Evicting the coldest player
# instead costs nothing that does not come back: the plugin re-POSTs, and the
# gap reads as "the plugin has not reported yet", which the tools already say.
MAX_TRACKED_PLAYERS = 200


@dataclass(frozen=True, slots=True)
class LiveState:
    """What the client last reported."""

    player: str
    at: float
    skills: dict[str, int] = field(default_factory=dict)
    inventory: list[str] = field(default_factory=list)
    location: tuple[int, int, int] | None = None
    region: int | None = None
    hitpoints: int | None = None
    prayer: int | None = None
    energy: int | None = None
    activity: str = ""

    def age(self, now: float) -> float:
        return now - self.at

    def fresh(self, now: float, *, ttl: float = FRESH_SECONDS) -> bool:
        return self.age(now) <= ttl

    @property
    def total_xp(self) -> int:
        return sum(self.skills.values())


@dataclass
class Session:
    """Where a play session started, so rates are per-session not lifetime."""

    started: float
    baseline: dict[str, int]
    last_seen: float

    def gains(self, current: dict[str, int]) -> dict[str, int]:
        return {
            skill: current[skill] - self.baseline.get(skill, 0)
            for skill in current
            if current[skill] - self.baseline.get(skill, 0) > 0
        }


def _evict(tracked: OrderedDict, cap: int) -> list[str]:
    """Drop the coldest entries until the cap holds. Returns what went.

    A cap of 0 means no limit, the way every other knob here spells it -- not a
    store that can hold nothing, which would silently disable live state
    entirely for anyone who set it looking for "off".
    """
    if not cap:
        return []
    dropped = []
    while len(tracked) > cap:
        name, _ = tracked.popitem(last=False)
        dropped.append(name)
    return dropped


def _rate(gained: int, seconds: float) -> int:
    """XP/hr, or 0 before there is enough time to mean anything.

    Under a minute the divisor is small enough that one XP drop reads as
    hundreds of thousands per hour, which is worse than saying nothing.
    """
    return int(gained / seconds * 3600) if seconds >= 60 and gained > 0 else 0


class LiveStore:
    """Latest state and current session per player. In memory only.

    Deliberately not persisted: this is what is happening now, and a state
    restored from disk after a restart would describe a session that ended.
    :mod:`reldo.progress` is where the durable history lives.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_players: int = MAX_TRACKED_PLAYERS,
    ) -> None:
        self._clock = clock
        self._max_players = max_players
        # Ordered by least-recently-reported, so eviction is popitem(last=False)
        # -- the same shape ConversationStore uses for the same reason.
        self._states: OrderedDict[str, LiveState] = OrderedDict()
        self._sessions: dict[str, Session] = {}
        # Deliberately outlives freshness. A quest you finished is still finished
        # when the client is closed, unlike "you are at the quarry right now",
        # so this is never aged out the way LiveState.fresh ages state. Bounded
        # by count instead; see MAX_TRACKED_PLAYERS.
        self._profiles: OrderedDict[str, PlayerProfile] = OrderedDict()

    def update(self, state: LiveState) -> LiveState:
        now = state.at
        session = self._sessions.get(state.player)
        if session is None or now - session.last_seen > SESSION_GAP:
            session = Session(started=now, baseline=dict(state.skills), last_seen=now)
            self._sessions[state.player] = session
            log.info("New session for %s", state.player)
        session.last_seen = now
        self._states[state.player] = state
        self._states.move_to_end(state.player)
        # Sessions are created here and only here, so evicting the two together
        # is what keeps _sessions from being the unbounded dict instead. It has
        # no cap of its own precisely because it cannot outlive this one.
        for name in _evict(self._states, self._max_players):
            self._sessions.pop(name, None)
            log.info("Stopped tracking %s: %d players already live", name, self._max_players)
        return state

    def get(self, player: str) -> LiveState | None:
        return self._states.get(player)

    def session(self, player: str) -> Session | None:
        return self._sessions.get(player)

    def update_profile(self, profile: PlayerProfile) -> PlayerProfile:
        self._profiles[profile.player] = profile
        self._profiles.move_to_end(profile.player)
        for name in _evict(self._profiles, self._max_players):
            log.info("Dropped %s's profile: %d already held", name, self._max_players)
        return profile

    def profile(self, player: str) -> PlayerProfile | None:
        """What the client last reported about what this player has done."""
        return self._profiles.get(player)

    def summary(self, player: str, *, now: float | None = None) -> str:
        """Model-readable block, or "" when there is nothing live to report.

        Empty rather than "no data" on purpose: this gets folded into a prompt,
        and a line saying the client is closed is noise on every question asked
        away from the game.
        """
        lines: list[str] = []
        state = self._states.get(player)
        moment = self._clock() if now is None else now
        if state is None or not state.fresh(moment):
            # The perishable half is gone, but the durable half is not: a quest
            # you finished is still finished with the client closed. Returning
            # "" here discarded the profile along with the stale state, so a
            # player who had logged out looked like a player with no quest data
            # at all -- and the model has no reason to reach for a tool it has
            # not been told exists.
            return self._profile_digest(player)

        lines = [f"Right now, live from {state.player}'s client:"]
        if state.activity:
            lines.append(f"  doing: {state.activity}")
        if state.location:
            x, y, plane = state.location
            lines.append(f"  at: {x}, {y}" + (f" (plane {plane})" if plane else ""))
        vitals = []
        if state.hitpoints is not None:
            vitals.append(f"{state.hitpoints} hp")
        if state.prayer is not None:
            vitals.append(f"{state.prayer} prayer")
        if state.energy is not None:
            vitals.append(f"{state.energy}% run")
        if vitals:
            lines.append("  " + ", ".join(vitals))
        if state.inventory:
            lines.append(f"  carrying: {', '.join(state.inventory[:MAX_INVENTORY])}")

        session = self._sessions.get(player)
        if session:
            elapsed = state.at - session.started
            gained = session.gains(state.skills)
            if gained:
                parts = []
                for skill, amount in sorted(gained.items(), key=lambda kv: -kv[1])[:4]:
                    rate = _rate(amount, elapsed)
                    parts.append(
                        f"{skill} +{amount:,}" + (f" ({rate:,}/hr)" if rate else "")
                    )
                lines.append(
                    f"  this session ({int(elapsed // 60)} min): " + ", ".join(parts)
                )
            elif elapsed >= 300:
                # The most actionable thing a coach can notice, and it is
                # invisible to every other source: logged in, gaining nothing.
                lines.append(
                    f"  this session ({int(elapsed // 60)} min): no XP gained at all"
                )
        digest = self._profile_digest(player)
        return "\n".join(lines) + (f"\n{digest}" if digest else "")

    def _profile_digest(self, player: str) -> str:
        """One line saying what the client has reported, not what it contains.

        A digest, because this is folded into the prompt for every question and
        160 quest names would drown everything else in it. Its job is to tell
        the model the data exists so it reaches for check_quest; the tools serve
        the contents.
        """
        profile = self._profiles.get(player)
        if profile is None:
            return ""
        parts = [f"{len(profile.quests_finished)} quests finished"]
        tiers = sum(len(t) for t in profile.diaries.values())
        if tiers:
            parts.append(f"{tiers} diary tiers")
        if profile.equipment:
            parts.append(f"{len(profile.equipment)} items worn")
        parts.append(
            f"{len(profile.bank)} bank items seen" if profile.bank is not None
            else "bank not seen yet"
        )
        return (
            f"From {profile.player}'s client: " + ", ".join(parts)
            + ". Use check_quest and check_inventory for the detail."
        )


@dataclass(frozen=True, slots=True)
class PlayerProfile:
    """What the client knows that no public API does.

    Quests, diaries, worn equipment and bank are readable only from the player's
    own client. Jagex publishes none of them and neither does any third party --
    every tracker that knows your quests learned them from a plugin.

    Separate from :class:`LiveState` because the two change on completely
    different timescales. State moves every few seconds and is a few hundred
    bytes; this is kilobytes and moves when you finish a quest. Bundled together,
    either the big half rides along every six seconds or the small half waits on
    the big one.
    """

    player: str
    at: float
    quests_finished: frozenset[str] = frozenset()
    quests_started: frozenset[str] = frozenset()
    diaries: dict[str, list[str]] = field(default_factory=dict)
    equipment: list[str] = field(default_factory=list)
    # None means "the client has not sent one", which is not the same as an
    # empty bank and must not be reported as one: the bank is unreadable until
    # the player opens it, so absent is the normal state rather than an error.
    bank: list[str] | None = None

    def quest_state(self, quest: str) -> str:
        """'finished', 'started' or 'not started', matched case-insensitively."""
        wanted = " ".join(quest.lower().split())
        if wanted in {q.lower() for q in self.quests_finished}:
            return "finished"
        if wanted in {q.lower() for q in self.quests_started}:
            return "started"
        return "not started"

    def holding(self, item: str) -> list[str]:
        """Where a named item is, across equipment and bank. Substring match,
        because the client sends "Rune platebody" and a question says "platebody".
        """
        needle = item.lower().strip()
        found = []
        for where, items in (("worn", self.equipment), ("bank", self.bank or [])):
            found += [f"{name} ({where})" for name in items if needle in name.lower()]
        return found


def parse_profile(payload: dict, *, at: float) -> PlayerProfile:
    """Turn a plugin profile POST into a profile.

    Everything except the player name is optional, the same posture as
    :func:`parse_state`: a plugin build older than this server should report
    less rather than be rejected.

    Raises:
        ValueError: no usable player name.
    """
    player = _clean(payload.get("player") or "", 12)
    if not player:
        raise ValueError("payload has no player name")

    quests = payload.get("quests") or {}

    def names(key: str) -> frozenset[str]:
        raw = quests.get(key) or []
        return frozenset(_clean(q, 60) for q in raw[:MAX_QUESTS] if str(q).strip())

    diaries: dict[str, list[str]] = {}
    for region, tiers in (payload.get("diaries") or {}).items():
        if isinstance(tiers, list):
            cleaned = [_clean(t, 12) for t in tiers[:4] if str(t).strip()]
            if cleaned:
                diaries[_clean(region, 24)] = cleaned

    equipment = [_clean(i, 40) for i in (payload.get("equipment") or [])][:MAX_EQUIPMENT]
    # Absent and empty are different facts. Only a list that was actually sent
    # becomes a list here.
    raw_bank = payload.get("bank")
    bank = (
        [_clean(i, 40) for i in raw_bank][:MAX_BANK]
        if isinstance(raw_bank, list)
        else None
    )

    return PlayerProfile(
        player=player,
        at=at,
        quests_finished=names("finished"),
        quests_started=names("started"),
        diaries=diaries,
        equipment=equipment,
        bank=bank,
    )


def _clean(value, limit: int = MAX_FIELD) -> str:
    return " ".join(str(value).split())[:limit]


def parse_state(payload: dict, *, at: float) -> LiveState:
    """Turn a plugin POST into a state.

    Everything is optional except the player name, because a plugin build older
    than this server should degrade to reporting less rather than being rejected.

    Raises:
        ValueError: no usable player name.
    """
    player = _clean(payload.get("player") or "", 12)
    if not player:
        raise ValueError("payload has no player name")

    skills = {
        _clean(k, 20): int(v)
        for k, v in (payload.get("skills") or {}).items()
        if isinstance(v, (int, float)) and v >= 0
    }
    inventory = [_clean(i, 40) for i in (payload.get("inventory") or [])][:MAX_INVENTORY]

    location = payload.get("location")
    point = None
    if isinstance(location, dict):
        try:
            point = (int(location["x"]), int(location["y"]), int(location.get("plane", 0)))
        except (KeyError, TypeError, ValueError):
            point = None

    def number(key: str) -> int | None:
        value = payload.get(key)
        return int(value) if isinstance(value, (int, float)) else None

    return LiveState(
        player=player,
        at=at,
        skills=skills,
        inventory=inventory,
        location=point,
        region=number("region"),
        hitpoints=number("hitpoints"),
        prayer=number("prayer"),
        energy=number("energy"),
        activity=_clean(payload.get("activity") or "", 60),
    )


def build_app(store: LiveStore, *, token: str = "") -> web.Application:
    """The receiving endpoint.

    A bearer token is checked when configured. It is not much of a secret --
    anything running as you on this machine can read it out of the .env -- but it
    stops other local software posting nonsense by accident, which is the actual
    risk on loopback.
    """

    def authorised(request: web.Request) -> bool:
        if not token:
            return True
        # Constant time. A plain == returns as soon as two bytes differ, which
        # leaks through timing how much of the token a guess got right -- and
        # off loopback this handler is reachable by anything on the network.
        offered = request.headers.get("Authorization") or ""
        return hmac.compare_digest(offered, f"Bearer {token}")

    async def post_state(request: web.Request) -> web.Response:
        if not authorised(request):
            return web.json_response({"error": "bad token"}, status=401)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "malformed JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "expected an object"}, status=400)
        try:
            state = parse_state(payload, at=store._clock())
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        store.update(state)
        return web.json_response({"ok": True, "player": state.player})

    async def post_profile(request: web.Request) -> web.Response:
        if not authorised(request):
            return web.json_response({"error": "bad token"}, status=401)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "malformed JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "expected an object"}, status=400)
        try:
            profile = parse_profile(payload, at=store._clock())
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        store.update_profile(profile)
        return web.json_response(
            {"ok": True, "player": profile.player,
             "quests": len(profile.quests_finished)}
        )

    async def health(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "tracking": sorted(store._states),
                "profiles": sorted(store._profiles),
                "auth": bool(token),
            }
        )

    async def get_live(request: web.Request) -> web.Response:
        """What is happening to one player, for something that is not this box.

        The store has always been readable by whatever process owns it, which
        made the coach impossible anywhere else: the receiver runs next to the
        Discord bot, and the speakers are on the machine you play at. This is
        the seam. Same token as the posts -- it reads out inventory, location
        and session history, which is not less sensitive than writing it.
        """
        if not authorised(request):
            return web.json_response({"error": "bad token"}, status=401)
        player = request.match_info["player"]
        now = store._clock()
        state = store._states.get(player)
        session = store._sessions.get(player)
        body: dict[str, object] = {
            "player": player,
            "fresh": bool(state and state.fresh(now)),
            # The prose block the model gets, so a caller does not have to
            # reimplement the phrasing and drift from it.
            "summary": store.summary(player, now=now),
        }
        if state is not None:
            body |= {
                "activity": state.activity,
                "skills": state.skills,
                "total_xp": state.total_xp,
                "age_seconds": round(state.age(now), 1),
            }
        if state is not None and session is not None:
            elapsed = state.at - session.started
            body["session"] = {
                "minutes": int(elapsed // 60),
                "gains": session.gains(state.skills),
                "rates": {
                    skill: _rate(amount, elapsed)
                    for skill, amount in session.gains(state.skills).items()
                },
            }
        return web.json_response(body)

    app = web.Application()
    app.add_routes([
        web.post("/state", post_state),
        web.post("/profile", post_profile),
        web.get("/health", health),
        web.get("/live/{player}", get_live),
    ])
    return app


async def serve(
    store: LiveStore,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str = "",
) -> web.AppRunner:
    """Start the receiver. Returns the runner so the caller can clean it up.

    Raises:
        ValueError: asked to listen off loopback with no token. The same call
            :class:`~reldo.wiki.WikiClient` makes about a missing User-Agent --
            refuse at construction rather than let it be discovered later, and
            what would be discovered later here is that anyone who can reach the
            port can put words in a player's prompt.
    """
    if host not in LOOPBACK and not token.strip():
        raise ValueError(
            f"Refusing to listen on {host!r} with no token. That is every "
            "interface on this machine, and an empty token disables the check "
            "entirely, so anything on the network could post state for any "
            "player. Set RELDO_LIVE_TOKEN (and the same value in the plugin), "
            "or set RELDO_LIVE_HOST=127.0.0.1 if the game runs on this machine."
        )
    runner = web.AppRunner(build_app(store, token=token))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Live state receiver listening on http://%s:%d/state", host, port)
    return runner
