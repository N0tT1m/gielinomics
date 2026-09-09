"""Official OSRS hiscores lookup.

This is what turns "what should I do" from a generic wiki answer into a specific
one. "Am I ready for Dragon Slayer II" is unanswerable without the asker's stats;
with them it is a comparison.

Uses ``index_lite.json``, which returns skills by *name* rather than by position.
The older CSV endpoint returns bare rows whose meaning depends on a positional
mapping that changes whenever Jagex adds a skill or activity -- a decode step that
silently corrupts every stat when it drifts. Nothing here needs that.

Unranked skills come back as ``-1`` for rank, level and xp. That is not zero and
must not be rendered as zero: it means the player is outside the ranked
population for that skill, which for a low-level account is most of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

HISCORES_URL = "https://secure.runescape.com/m=hiscore_oldschool/index_lite.json"

# Jagex returns -1 for every field of a skill the player isn't ranked in.
UNRANKED = -1

# Hitpoints is the one skill that does not start at 1: every account is created
# with 10 and 1,154 XP already banked. That matters precisely because unranked
# is the common case for the accounts this feature exists to help -- the
# hiscores rank only a slice of the population, so a low-level player comes back
# unranked in Hitpoints, and reading that as level 1 understated their combat
# level by 3 and reported them as short of a 10 Hitpoints requirement every
# account has always met. A map rather than a branch: if Jagex ever ships
# another skill that starts above 1, it belongs here.
STARTING_LEVELS = {"Hitpoints": 10}


class HiscoresError(RuntimeError):
    """The lookup failed, or the player doesn't exist."""


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    rank: int
    level: int
    xp: int

    @property
    def ranked(self) -> bool:
        return self.level != UNRANKED


# Counters whose score is a rating, not a tally of things done. Verified against
# a live account: "PvP Arena - Rank" comes back rank=-1, score=2500, and 2,500 is
# a rating rather than 2,500 wins. Sorted in with the kill counts it outranked
# everything -- so a stat block opened with "done: PvP Arena - Rank 2,500" for a
# player whose largest genuine number was eleven collection log slots.
_RATING_SUFFIX = " - Rank"

# The sum of the six tiers. Listed beside them it counts every clue twice and,
# being the largest, leads the list -- the same shape as WOM's "overall", which
# wom.py already drops for the same reason.
CLUE_TOTAL = "Clue Scrolls (all)"

# Easiest first, which is the order they are earned and the order they read in.
CLUE_TIERS = (
    "Clue Scrolls (beginner)", "Clue Scrolls (easy)", "Clue Scrolls (medium)",
    "Clue Scrolls (hard)", "Clue Scrolls (elite)", "Clue Scrolls (master)",
)


@dataclass(frozen=True, slots=True)
class Activity:
    """One boss, clue tier, raid or minigame counter.

    **Unranked reads differently here than it does for a skill.** Jagex sends
    ``rank: -1`` with ``score: 0`` for everything a player has never done, so a
    fresh account comes back with all 91 activities present and zeroed. Testing
    ``score != -1`` -- the shape that works for skills -- reports 91 completed
    activities for somebody who has killed nothing. The count is what matters.

    **And the score does not always mean the same thing.** Most of the 91 are
    counts, two are ratings, and one is the total of six others. Reporting all
    of them as "things you have done N times" is how a model ends up telling
    somebody they have completed the PvP Arena two and a half thousand times.
    """

    name: str
    rank: int
    score: int

    @property
    def is_rating(self) -> bool:
        """A score that measures how good you are, not how often you did it."""
        return self.name.endswith(_RATING_SUFFIX)

    @property
    def is_clue(self) -> bool:
        return self.name.startswith("Clue Scrolls")

    @property
    def done(self) -> bool:
        """Actually did this, this many times.

        False for a rating however high it is: a rating is not a tally, and the
        whole value of this property is that a caller can total, sort and quote
        what it returns without checking what each number means.
        """
        return self.score > 0 and not self.is_rating


# Combat level, from the formula on the wiki's "Combat level" page. Verified
# there rather than recalled, because it lives in <math> markup that page_text
# strips -- the same table-blindness documented in wiki.py:
#
#   Base  = 1/4 * (Defence + Hitpoints + floor(Prayer / 2))
#   Melee = 13/40 * (Attack + Strength)
#   Range = 13/40 * floor(Ranged * 3/2)
#   Mage  = 13/40 * floor(Magic * 3/2)
#   Final = floor(Base + max(Melee, Range, Mage))
COMBAT_RATIO = 13 / 40


@dataclass(frozen=True, slots=True)
class Player:
    """A player's ranked skills, keyed by skill name."""

    name: str
    skills: dict[str, Skill]
    activities: dict[str, Activity] = field(default_factory=dict)

    @property
    def combat_level(self) -> int:
        """Combat level from the wiki's formula. Exact, so never estimated."""
        import math

        base = 0.25 * (
            self.level("Defence") + self.level("Hitpoints") + self.level("Prayer") // 2
        )
        melee = COMBAT_RATIO * (self.level("Attack") + self.level("Strength"))
        ranged = COMBAT_RATIO * (self.level("Ranged") * 3 // 2)
        magic = COMBAT_RATIO * (self.level("Magic") * 3 // 2)
        return math.floor(base + max(melee, ranged, magic))

    def xp(self, skill: str) -> int:
        """XP in a skill, or 0 if unranked."""
        found = self.skills.get(skill.title())
        return found.xp if found and found.ranked and found.xp > 0 else 0

    def done(self) -> list[Activity]:
        """Activities the player has actually done, highest count first.

        Ratings are excluded because they are not counts, and the clue-scroll
        total because it is the sum of six entries that are already here.
        Everything this returns is a tally of the same kind, which is what makes
        it safe to sort and quote.
        """
        return sorted(
            (a for a in self.activities.values() if a.done and a.name != CLUE_TOTAL),
            key=lambda a: -a.score,
        )

    def ratings(self) -> list[Activity]:
        """Minigame ratings, which are scores rather than tallies."""
        return sorted(
            (a for a in self.activities.values() if a.is_rating and a.score > 0),
            key=lambda a: a.name,
        )

    def clues(self) -> list[Activity]:
        """Completed clue scrolls per tier, easiest first, skipping the total.

        Tier order rather than count order: clues are earned in that sequence,
        and "master 3" means something quite different beside "beginner 200"
        than it does at the top of a list sorted by volume.
        """
        found = [self.activities.get(tier) for tier in CLUE_TIERS]
        return [a for a in found if a is not None and a.score > 0]

    def level(self, skill: str) -> int:
        """Level in a skill, or its starting level if unranked.

        Not 0, and not always 1: an unranked account still *has* whatever the
        skill starts at, and callers comparing against a requirement want the
        in-game truth rather than the API's sentinel. Hitpoints starts at 10 --
        see :data:`STARTING_LEVELS`.
        """
        name = skill.title()
        found = self.skills.get(name)
        if found is None or not found.ranked:
            return STARTING_LEVELS.get(name, 1)
        return found.level

    def meets(self, requirements: dict[str, int]) -> dict[str, tuple[int, int, bool]]:
        """Compare levels against a requirement map.

        Returns skill -> (has, needs, ok), so a caller can report what's missing
        rather than just pass/fail.
        """
        return {
            skill: (self.level(skill), needed, self.level(skill) >= needed)
            for skill, needed in requirements.items()
        }

    def summary(self) -> str:
        """Compact, model-readable stat block."""
        overall = self.skills.get("Overall")
        lines = [f"{self.name} -- total level {overall.level if overall else '?'}"]
        if overall and overall.xp > 0:
            lines[0] += f", {overall.xp:,} XP"
        ranked = [
            s for name, s in self.skills.items() if name != "Overall" and s.ranked
        ]
        lines.append(
            "  " + ", ".join(f"{s.name} {s.level}" for s in sorted(ranked, key=lambda s: s.name))
        )
        unranked = [
            name for name, s in self.skills.items() if name != "Overall" and not s.ranked
        ]
        if unranked:
            # Spelled with the level rather than as bare names. "unranked (level
            # 1 or close)" put the same wrong number in front of the model that
            # `level()` used to return -- Hitpoints reads 10 here, so an answer
            # about whether they can start something does not deduct nine levels
            # they have.
            lines.append(
                "  unranked, at their starting level: "
                + ", ".join(f"{name} {self.level(name)}" for name in sorted(unranked))
            )
        lines[0] += f", combat level {self.combat_level}"
        done = self.done()
        if done:
            # Only what they have actually done. Listing 91 zeroed counters
            # buries the handful that carry information.
            lines.append(
                "  done: " + ", ".join(f"{a.name} {a.score:,}" for a in done[:12])
            )
        clues = self.clues()
        if clues:
            lines.append(
                "  clues: "
                + ", ".join(f"{a.name[13:-1]} {a.score:,}" for a in clues)
            )
        ratings = self.ratings()
        if ratings:
            # Labelled, and on their own line. These are the numbers most likely
            # to be read back as achievements, and the label is the only thing
            # stopping "PvP Arena 2,500" from becoming two and a half thousand
            # wins in an answer.
            lines.append(
                "  minigame ratings (a score, not a number of games): "
                + ", ".join(
                    f"{a.name.removesuffix(_RATING_SUFFIX)} {a.score:,}" for a in ratings
                )
            )
        return "\n".join(lines)


class HiscoresClient:
    """Async lookup against the official hiscores.

    Args:
        user_agent: Sent on every request. Jagex is less explicit than the wiki
            about blocking default agents, but identifying yourself on a public
            endpoint you are polling is the same courtesy either way.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        user_agent: str = "reldo/0.1 (github.com/N0tT1m/nieve)",
        *,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            headers={"User-Agent": user_agent}, timeout=timeout, transport=transport
        )

    async def __aenter__(self) -> HiscoresClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def lookup(self, username: str) -> Player:
        """Fetch a player's stats.

        Raises:
            HiscoresError: no such player, or the endpoint misbehaved. A 404 here
                means "not on the hiscores", which covers a typo, a brand-new
                account, and a name change alike -- the API can't distinguish
                them, so neither do we.
        """
        name = username.strip()
        if not name:
            raise HiscoresError("Username is empty.")

        try:
            response = await self._http.get(HISCORES_URL, params={"player": name})
        except httpx.HTTPError as exc:
            raise HiscoresError(f"Could not reach the hiscores: {exc!r}") from exc

        if response.status_code == 404:
            raise HiscoresError(
                f"No hiscores entry for {name!r}. Check the spelling, or the account "
                "may be too new, renamed, or not ranked yet."
            )
        if response.status_code != 200:
            raise HiscoresError(f"Hiscores returned HTTP {response.status_code}.")

        try:
            payload = response.json()
            skills = {
                s["name"]: Skill(s["name"], s["rank"], s["level"], s["xp"])
                for s in payload["skills"]
            }
            activities = {
                a["name"]: Activity(a["name"], a["rank"], a["score"])
                for a in payload.get("activities", [])
            }
        except (ValueError, KeyError, TypeError) as exc:
            raise HiscoresError(f"Unexpected hiscores response shape: {exc!r}") from exc

        return Player(
            name=payload.get("name") or name, skills=skills, activities=activities
        )
