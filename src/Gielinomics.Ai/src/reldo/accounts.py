"""Which RuneScape account belongs to which Discord user.

Without this, every stats-aware answer starts by asking who you are, and "how do
I train mining" gets the generic level-1 answer for somebody who is already 54.
The whole value of the hiscores integration is that the advice starts from where
you actually are, and that needs a name the bot already knows.

**No verification, deliberately.** Claiming somebody else's RSN gets you their
public hiscores, which you could already read on Jagex's own website -- there is
nothing to protect. Verification schemes for this cost a round trip through a
profile field and buy nothing.

Stored as a flat JSON file rather than a database: it is one string per user, it
has to survive a restart, and a service to run for that would be absurd.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .jsonfile import write_json

log = logging.getLogger(__name__)

# Jagex allows 1-12 characters: letters, digits, spaces and underscores.
MAX_RSN = 12


class AccountStore:
    """Discord user id -> RuneScape name, persisted to JSON.

    Args:
        path: File to keep them in. Created on first write; a missing or corrupt
            file reads as empty rather than raising, because losing the mapping
            should degrade the bot to asking for a username, not stop it booting.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._names: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            self._names = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._names = {}
        except (ValueError, OSError) as exc:
            log.warning("Could not read %s (%s); starting with no links", self._path, exc)
            self._names = {}

    def _save(self) -> None:
        # Atomic, because _load above treats an unreadable file as no links at
        # all -- so a save interrupted halfway does not degrade the mapping, it
        # silently discards every one of them. See reldo.jsonfile.
        write_json(self._path, self._names, indent=2)

    def get(self, user_id: int) -> str | None:
        """The RSN linked to a Discord user, if any."""
        return self._names.get(str(user_id))

    def names(self) -> list[str]:
        """Every linked RuneScape name, once each, in link order.

        Deduplicated because two Discord users can hold the same account -- a
        shared ironman, or one person with a second Discord -- and the scheduled
        sample in :func:`reldo.progress.poll` would otherwise look it up twice a
        round for one player's worth of history.
        """
        return list(dict.fromkeys(self._names.values()))

    def link(self, user_id: int, rsn: str) -> str:
        """Link a name, returning it as stored.

        Raises:
            ValueError: the name cannot be a RuneScape name, so linking it would
                only produce a confusing 404 from the hiscores later.
        """
        name = " ".join(rsn.split())
        if not name:
            raise ValueError("Give me a username.")
        if len(name) > MAX_RSN:
            raise ValueError(
                f"{name!r} is {len(name)} characters; RuneScape names are at most {MAX_RSN}."
            )
        if not all(c.isalnum() or c in " _-" for c in name):
            raise ValueError(
                f"{name!r} has characters a RuneScape name cannot: letters, digits, "
                "spaces, hyphens and underscores only."
            )
        self._names[str(user_id)] = name
        self._save()
        return name

    def unlink(self, user_id: int) -> bool:
        """Forget a user's name. True if there was one."""
        if self._names.pop(str(user_id), None) is None:
            return False
        self._save()
        return True
