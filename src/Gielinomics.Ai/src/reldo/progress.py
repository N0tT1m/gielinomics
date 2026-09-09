"""XP over time, so the coaching can be about what you actually did.

The hiscores are a snapshot: they say you have 95 Fishing, never that you got
there this week and have not touched anything else since. That difference is the
whole of coaching. "Train Slayer" is advice anyone could give; "you have put
400k into Fishing since Tuesday and your Slayer is still 1" is about you.

**The question path costs nothing extra.** :meth:`WikiAgent._player_preamble`
already looks the asker up on every question, so that data is arriving anyway
and was being thrown away. This keeps it.

**But that path alone samples by conversation, not by time**, and the difference
is the whole feature. History only advanced when somebody asked something, so a
week of playing without talking to the bot left nothing to compare against and
:func:`summarise` had one snapshot and nothing to say -- while asking twice in an
hour gave you an hour's window and called it a week. :func:`poll` closes that by
sampling every linked account on a timer, whether or not anyone is talking.

Written to JSON rather than a database for the same reason as
:mod:`reldo.accounts`: it is a few hundred integers per player, it has to survive
a restart, and standing up a service for that would be silly. Identical snapshots
are not stored at all, so a day of asking questions without playing adds nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .jsonfile import write_json
from .skills import SKILLS

log = logging.getLogger(__name__)

# Per player. At one snapshot per real change this is months of history, and it
# bounds a file that would otherwise grow for as long as the bot runs.
MAX_SNAPSHOTS = 400

DAY = 86400.0

# How often :func:`poll` samples a linked account. Half an hour is fine-grained
# enough to tell an evening's play from a break, and gentle enough that a dozen
# linked accounts is well under one hiscores request a minute.
#
# It interacts with MAX_SNAPSHOTS, and that is the number to move if you want
# more history rather than this one: only *changed* XP is stored, so somebody
# playing four hours a day writes roughly eight rows a day and 400 rows is about
# fifty days. Polling twice as often does not double the rows, but it does halve
# the horizon for anyone who plays continuously.
DEFAULT_POLL_SECONDS = 1800.0


@dataclass(frozen=True, slots=True)
class Snapshot:
    """XP per skill at a moment."""

    at: float
    xp: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.xp.values())


def _humanise(seconds: float) -> str:
    """'3 days', '5 hours'. Rough on purpose -- nobody wants 2.83 days."""
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} minutes"
    if seconds < DAY:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = int(seconds // DAY)
    return f"{days} day{'s' if days != 1 else ''}"


class ProgressStore:
    """XP snapshots per player, persisted to JSON.

    Args:
        path: File to keep them in. A missing or corrupt file reads as empty --
            losing history should cost the coaching, not stop the bot booting.
        clock: Injectable so tests can age a snapshot without sleeping.
    """

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        self._players: dict[str, list[dict]] = {}
        self._load()

    def _load(self) -> None:
        try:
            self._players = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._players = {}
        except (ValueError, OSError) as exc:
            log.warning("Could not read %s (%s); starting with no history", self._path, exc)
            self._players = {}

    def _save(self) -> None:
        # Atomic, for the reason _load above makes necessary: it reads an
        # unparseable file as no history, so an interrupted save would not lose
        # the last snapshot, it would lose every snapshot. See reldo.jsonfile.
        write_json(self._path, self._players)

    def record(self, rsn: str, xp: dict[str, int]) -> bool:
        """Store a snapshot unless it is identical to the last one.

        The dedupe is what keeps this honest and small. Every question triggers a
        hiscores lookup, so without it a day of asking questions while not
        playing would write hundreds of rows and make "you have been at this for
        6 hours" true of the conversation rather than of the game.
        """
        if not xp:
            return False
        rows = self._players.setdefault(rsn, [])
        if rows and rows[-1].get("xp") == xp:
            return False
        rows.append({"at": self._clock(), "xp": dict(xp)})
        del rows[:-MAX_SNAPSHOTS]
        self._save()
        return True

    def snapshots(self, rsn: str) -> list[Snapshot]:
        return [Snapshot(at=r["at"], xp=r["xp"]) for r in self._players.get(rsn, [])]

    def gains(self, rsn: str, *, since: float = 7 * DAY) -> tuple[dict[str, int], float] | None:
        """XP gained per skill over a window, and how long that window really is.

        Returns the *actual* elapsed time rather than the requested window,
        because they are rarely the same: ask for a week of history from someone
        tracked since yesterday and the honest answer is "in 1 day", not "this
        week". Reporting the requested window would quietly overstate how long
        somebody has been stuck.

        None when there is nothing to compare against yet.
        """
        rows = self.snapshots(rsn)
        if len(rows) < 2:
            return None
        now = self._clock()
        # Oldest snapshot still inside the window, else the oldest we have.
        baseline = next((r for r in rows if now - r.at <= since), rows[0])
        latest = rows[-1]
        if baseline.at >= latest.at:
            return None
        gained = {
            skill: latest.xp[skill] - baseline.xp.get(skill, 0)
            for skill in latest.xp
            if latest.xp[skill] - baseline.xp.get(skill, 0) > 0
        }
        return gained, latest.at - baseline.at


def snapshot_of(player) -> dict[str, int]:
    """The XP map a snapshot stores, for one hiscores lookup.

    Shared by both callers rather than spelled out at each. :meth:`record`
    dedupes on exact equality, so a scheduled sample that built this dict even
    slightly differently from the conversational one would read as a change
    every time the two alternated -- filling the file with rows in which nothing
    happened, and reporting gains of zero as though they were gains.
    """
    return {skill: player.xp(skill) for skill in SKILLS if player.xp(skill)}


async def sample_once(accounts, hiscores, store) -> int:
    """Look up every linked account once, storing whatever moved.

    Returns how many players had actually changed, which is what the log line
    is worth reporting -- a round where nobody gained is the common case and
    costs nothing but the requests.

    One failed lookup never stops the round. A renamed account 404s forever and
    would otherwise take everybody else's history down with it.
    """
    changed = 0
    for rsn in accounts.names():
        try:
            player = await hiscores.lookup(rsn)
        except Exception as exc:
            log.warning("Scheduled hiscores sample for %r failed: %s", rsn, exc)
            continue
        try:
            if store.record(player.name, snapshot_of(player)):
                changed += 1
        except Exception as exc:  # a full disk should not kill the loop
            log.warning("Could not store a snapshot for %r: %s", player.name, exc)
    return changed


async def poll(
    accounts,
    hiscores,
    store,
    *,
    interval: float = DEFAULT_POLL_SECONDS,
    sleep=asyncio.sleep,
) -> None:
    """Sample linked accounts forever, on a timer. Never raises.

    Runs one round immediately. A restart is the moment you most want a
    baseline -- otherwise the first half-hour after every deploy is a hole in
    the history, and :meth:`ProgressStore.record` drops the row anyway if
    nothing moved since the last one.

    Cancellation propagates, so the caller can stop this with the task it
    created; everything else is swallowed, because a background sampler that
    dies silently leaves a feature that looks enabled and does nothing, which is
    the failure this whole function exists to remove.
    """
    while True:
        try:
            changed = await sample_once(accounts, hiscores, store)
            if changed:
                log.info("Progress poll: %d account(s) moved", changed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Progress poll round failed; continuing")
        await sleep(interval)


def summarise(gains: tuple[dict[str, int], float] | None, *, limit: int = 6) -> str:
    """One line the model can quote and a human can read.

    Says so plainly when nothing moved. "No XP in 3 days" is the single most
    useful thing a coach can notice, and an empty string would hide it.
    """
    if gains is None:
        return ""
    gained, elapsed = gains
    window = _humanise(elapsed)
    if not gained:
        return f"No XP gained at all in the last {window}."
    ranked = sorted(gained.items(), key=lambda kv: -kv[1])[:limit]
    body = ", ".join(f"{skill} +{amount:,}" for skill, amount in ranked)
    return f"XP in the last {window}: {body}."
