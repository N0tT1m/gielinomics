"""The half that talks first.

Everything else here answers a question. You type, it reads the wiki, it tells
you. That is a reference, and a reference is not a coach -- a coach is watching
while you play and says something *before* you thought to ask, because the whole
value is in the thing you did not know to ask about.

So this polls the live state the plugin is already posting, notices what changed,
and speaks when the change is worth interrupting for. It runs on the machine you
play at, not on the machine the bot is on, because that is where the speakers
are.

**Interruption is the scarce resource, not compute.** A coach that comments every
thirty seconds gets muted within the hour, and a muted coach is worth less than
no coach because you also stopped reading the text. Everything here is built
around saying less: a cooldown between remarks, a rule that the same observation
is never made twice in a session, and one remark at a time rather than a list.
Not a floor on the size of a change -- that was tried and it silenced everything,
for the reason written where it used to be. When in doubt this stays quiet -- silence costs you
nothing, and the alternative is being turned off.

The advice itself comes from the agent, with every grounding pass intact. The
triggers below decide *when* to speak and *what to ask*; they never decide what
is true.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from .skills import level_at_xp

log = logging.getLogger(__name__)

# How often to look. A tick is 0.6s and the plugin posts every ~6s, so anything
# under that is asking the same question twice and getting the same answer.
POLL_SECONDS = 20.0

# The floor on interruption. Two minutes of silence between remarks is long
# enough that a remark still registers as one.
COOLDOWN_SECONDS = 120.0

# Logged in and gaining nothing for this long is the one thing worth saying
# unprompted with no other trigger, and it is invisible to every other source:
# the hiscores cannot tell idle from logged out.
IDLE_SECONDS = 420.0

# There is deliberately no floor on how much XP counts as movement.
#
# There was one, at 500, and it silenced the whole thing. A floor makes sense
# against a session total and none at all against a single poll: at 49,165
# Fishing XP an hour -- an ordinary rate, measured live -- fifteen seconds is
# about 205 XP, so every delta fell under it. Activity was never detected, and a
# level whose crossing drop happened to be one fish was skipped as well. The
# coach could not have fired at any real rate, and looked exactly like a coach
# with nothing to say.
#
# Noise is handled by shape instead of by size: activity takes the largest mover
# so the skills that tick along beside the real one lose on their own, and a
# level crossing is a level crossing however small the drop that crossed it.


@dataclass
class Observation:
    """Something worth saying, and the question that would find out what."""

    key: str
    """Stable identity, so the same observation is never made twice."""

    question: str
    """What to ask the agent. The answer is what actually gets spoken."""

    urgency: int = 0
    """Higher wins when two things happen at once. Ties keep the older one."""

    skill: str = ""
    """The skill this is about, when it is about one. Lets the caller fetch
    facts for it before asking -- a level question answered from recall is how
    "Thieving 17" came back as a paragraph about level 91."""

    level: int = 0
    """The level just reached, for the same reason."""


@dataclass
class CoachState:
    """What the coach has already seen, so it can tell change from state."""

    skills: dict[str, int] = field(default_factory=dict)
    activity: str = ""
    said: set[str] = field(default_factory=set)
    last_spoke: float = 0.0
    idle_since: float | None = None


def _training(before: dict[str, int], now: dict[str, int]) -> str:
    """The skill actually gaining XP, or "" when nothing meaningful moved.

    Largest mover rather than any mover: Hitpoints and Defence tick along beside
    whatever you are really doing, and "you have started Hitpoints" is not a
    thing anybody has ever started.
    """
    moved = {
        skill: xp - before[skill]
        for skill, xp in now.items()
        if skill in before and xp > before[skill]
    }
    if not moved:
        return ""
    return max(moved.items(), key=lambda kv: kv[1])[0]


def observe(
    before: CoachState, live: dict, now: float
) -> list[Observation]:
    """What changed that is worth a sentence.

    Pure, and deliberately so: this is the part with the judgement in it, and
    judgement that needs a network and a clock to test is judgement that does not
    get tested.
    """
    out: list[Observation] = []
    skills: dict[str, int] = live.get("skills") or {}

    # Levels. The one event that is unambiguously worth hearing about, because
    # it changes what you are allowed to do rather than just how far along you
    # are -- and it is the moment a better method usually becomes available.
    for skill, xp in skills.items():
        was = before.skills.get(skill)
        if was is None or xp <= was:
            continue
        old_level, new_level = level_at_xp(was), level_at_xp(xp)
        if new_level > old_level:
            out.append(
                Observation(
                    key=f"level:{skill}:{new_level}",
                    question=(
                        f"I just reached {skill} level {new_level}. What does "
                        f"that level unlock in {skill}, and is there a faster "
                        f"training method available now that was not available "
                        f"at level {old_level}?"
                    ),
                    urgency=3,
                    skill=skill,
                    level=new_level,
                )
            )

    # A change of activity is the moment advice is cheapest to act on: you have
    # just decided what to do and have not sunk an hour into it yet.
    #
    # Derived rather than reported. The plugin has an activity field and never
    # fills it in -- snapshot() writes player, skills, location, vitals and
    # inventory, and nothing sets activity -- so a trigger that waited for one
    # would wait forever. The skill whose XP is moving is the activity, which is
    # also the more honest answer: it is what you are *doing*, not what you
    # meant to be doing.
    activity = live.get("activity") or _training(before.skills, skills)
    if activity and activity != before.activity:
        # Carrying the level and the rate is what makes this answerable. Asked
        # bare -- "I have just started Fishing" -- the agent reads it as a
        # question about a thing called Fishing and comes back with the skill's
        # level requirement, which is not advice and not even about you. The
        # numbers are already in hand; not passing them was the whole failure.
        level = level_at_xp(skills.get(activity, 0)) if activity in skills else None
        # Carried for the same reason a level observation carries them: so the
        # runner can fetch the guide's own bracket for this level before asking,
        # rather than have her recall a method. Recalling produced "pickpocket
        # master farmers" for a level 18 account, when they need 38.
        rate = ((live.get("session") or {}).get("rates") or {}).get(activity, 0)
        where = f" at level {level}" if level else ""
        pace = f", getting about {rate:,} XP an hour" if rate else ""
        out.append(
            Observation(
                key=f"doing:{activity}",
                question=(
                    f"I am training {activity}{where}{pace}. Is that the best "
                    f"way to train {activity} at my level, and what method "
                    f"would be faster?"
                ),
                urgency=1,
                skill=activity if activity in skills else "",
                level=level or 0,
            )
        )

    # Logged in and gaining nothing. Said once per stretch of idleness, not once
    # per poll -- the key carries the minute it started so a later idle stretch
    # is a genuinely new observation.
    session = live.get("session") or {}
    gaining = any(session.get("gains", {}).values())
    if live.get("fresh") and not gaining and session.get("minutes", 0) >= IDLE_SECONDS / 60:
        started = before.idle_since or now
        out.append(
            Observation(
                key=f"idle:{int(started // 60)}",
                question=(
                    "I have been logged in for a while without gaining any XP. "
                    "Given what I am carrying and where I am, what should I do "
                    "next to make progress?"
                ),
                urgency=2,
            )
        )

    return [o for o in out if o.key not in before.said]


def choose(seen: list[Observation]) -> Observation | None:
    """The one thing to say. Never a list -- out loud, a list is a lecture."""
    return max(seen, key=lambda o: o.urgency, default=None)


class Coach:
    """Polls one player's live state and speaks when it is worth it."""

    def __init__(
        self,
        base_url: str,
        player: str,
        *,
        token: str = "",
        poll_seconds: float = POLL_SECONDS,
        cooldown: float = COOLDOWN_SECONDS,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._player = player
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._poll = poll_seconds
        self._cooldown = cooldown
        self.state = CoachState()

    async def fetch(self, http: httpx.AsyncClient) -> dict | None:
        """Current live state, or None if the receiver cannot be reached.

        Unreachable is not fatal: the bot restarts, the network blips, and a
        coach that exits because one poll failed is a coach you have to babysit.
        """
        try:
            response = await http.get(
                f"{self._base}/live/{self._player}", headers=self._headers
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            log.warning("Could not read live state: %r", exc)
            return None

    def update(self, live: dict, now: float) -> Observation | None:
        """Fold in one poll. Returns what to say, if anything."""
        seen = observe(self.state, live, now)
        session = live.get("session") or {}
        if any(session.get("gains", {}).values()):
            self.state.idle_since = None
        elif self.state.idle_since is None:
            self.state.idle_since = now

        # Order matters: the derived activity is a comparison against the
        # previous skills, so it has to be taken before they are replaced.
        skills = dict(live.get("skills") or {})
        doing = live.get("activity") or _training(self.state.skills, skills)
        if skills:
            self.state.skills = skills
        if doing:
            self.state.activity = doing

        if now - self.state.last_spoke < self._cooldown:
            # Inside the cooldown the observation is dropped rather than queued.
            # Advice about what you were doing two minutes ago is worse than
            # nothing: you have already moved on, and being told to reconsider a
            # decision you have finished making is how a coach becomes noise.
            return None
        pick = choose(seen)
        if pick is None:
            return None
        self.state.said.add(pick.key)
        self.state.last_spoke = now
        return pick
