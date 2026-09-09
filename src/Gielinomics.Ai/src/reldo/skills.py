"""Skill and experience arithmetic, in code rather than in the model.

Every number here is exact and cheap to compute, and a 24B model at Q6 asked to
do it in its head is the least reliable component in the system. "How many yew
logs from 60 to 99 Fletching" is a division; letting the model estimate it is how
you get an answer that is confidently 30% wrong with no way to notice.

The XP table is Jagex's published formula, not a lookup scraped from anywhere:

    xp(L) = floor( (1/4) * sum_{i=1}^{L-1} floor(i + 300 * 2^(i/7)) )
"""

from __future__ import annotations

import math
from functools import lru_cache

MAX_LEVEL = 126  # virtual levels; 99 is the in-game cap for non-combat skills
MAX_XP = 200_000_000

SKILLS = (
    "Attack", "Defence", "Strength", "Hitpoints", "Ranged", "Prayer", "Magic",
    "Cooking", "Woodcutting", "Fletching", "Fishing", "Firemaking", "Crafting",
    "Smithing", "Mining", "Herblore", "Agility", "Thieving", "Slayer",
    "Farming", "Runecraft", "Hunter", "Construction", "Sailing",
)
# Sailing is the 24th skill and was missing here while the live hiscores had
# been returning it for a while -- the same staleness this project exists to
# correct for, found in our own constant rather than the model's weights. The
# hiscores are the check: `len(player.skills) - 1` should equal len(SKILLS).


@lru_cache(maxsize=1)
def _xp_table() -> tuple[int, ...]:
    """Cumulative XP required for each level, index 1..MAX_LEVEL."""
    table = [0, 0]  # level 0 unused, level 1 = 0 xp
    points = 0
    for level in range(1, MAX_LEVEL):
        points += int(level + 300 * (2 ** (level / 7)))
        table.append(points // 4)
    return tuple(table)


def xp_for_level(level: int) -> int:
    """Total XP needed to reach ``level``. Level 99 is 13,034,431."""
    if not 1 <= level <= MAX_LEVEL:
        raise ValueError(f"level must be 1-{MAX_LEVEL}, got {level}")
    return _xp_table()[level]


def level_at_xp(xp: int) -> int:
    """Highest level fully reached at ``xp``."""
    if xp < 0:
        raise ValueError("xp must be non-negative")
    table = _xp_table()
    for level in range(MAX_LEVEL, 0, -1):
        if xp >= table[level]:
            return level
    return 1


def xp_between(from_level: int, to_level: int) -> int:
    """XP needed to go from one level to another."""
    if to_level < from_level:
        raise ValueError(f"to_level {to_level} is below from_level {from_level}")
    return xp_for_level(to_level) - xp_for_level(from_level)


def actions_needed(total_xp: int, xp_per_action: float) -> int:
    """How many actions at a given XP rate, rounded up.

    Rounded up because a partial action doesn't level you: 1,000 XP at 300 XP per
    action is 4 actions, not 3.33.
    """
    if xp_per_action <= 0:
        raise ValueError("xp_per_action must be positive")
    return math.ceil(total_xp / xp_per_action)


def hours_needed(total_xp: int, xp_per_hour: float) -> float:
    """Hours at a given XP rate."""
    if xp_per_hour <= 0:
        raise ValueError("xp_per_hour must be positive")
    return total_xp / xp_per_hour


def plan(
    from_level: int, to_level: int, *, xp_per_action: float | None = None,
    xp_per_hour: float | None = None,
) -> str:
    """A human-readable training plan between two levels."""
    total = xp_between(from_level, to_level)
    lines = [
        f"{from_level} -> {to_level}: {total:,} XP "
        f"({xp_for_level(from_level):,} -> {xp_for_level(to_level):,})"
    ]
    if xp_per_action:
        lines.append(
            f"  at {xp_per_action:,g} XP/action: {actions_needed(total, xp_per_action):,} actions"
        )
    if xp_per_hour:
        lines.append(f"  at {xp_per_hour:,g} XP/hr: {hours_needed(total, xp_per_hour):.1f} hours")
    return "\n".join(lines)


# Milestones people actually train toward. 92 is half the XP of 99 and the point
# most efficiency guides treat as the real halfway mark; 99 is the cape.
MILESTONES = (10, 20, 30, 40, 50, 60, 70, 80, 90, 92, 99)


def next_milestone(level: int) -> int | None:
    """The next round goal above a level, or None at 99."""
    return next((m for m in MILESTONES if m > level), None)


def cheapest_gains(levels: dict[str, int], *, limit: int = 8) -> list[tuple[str, int, int, int]]:
    """Skills sorted by how little XP stands between them and their next goal.

    The min-maxing question underneath "what should I do next" is usually "what
    is nearly done", because a level is a level and the cheap ones move total
    level fastest. Returns (skill, level, target, xp_needed).

    Exact rather than estimated, for the same reason everything else in this
    module is: the model comparing seven-digit XP gaps by eye is where it puts
    a 12M number below a 400k one and sounds certain about it.
    """
    out = []
    for skill, level in levels.items():
        target = next_milestone(level)
        if target is None:
            continue
        out.append((skill, level, target, xp_between(level, target)))
    return sorted(out, key=lambda row: row[3])[:limit]
