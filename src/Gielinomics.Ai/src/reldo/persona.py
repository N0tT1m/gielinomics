"""Delivery, for the Discord bot. Nothing else imports this.

The library, the CLI and :mod:`reldo.agent` stay neutral on purpose -- the agent
is the part with the enforcement passes and the eval set, and it should read the
same to anybody auditing why an answer came out the way it did. Tone is a
front-end concern, so it lives at the front end.

**A persona changes delivery, never facts.** It is appended to the system prompt
rather than applied as a rewrite of the finished answer, and that ordering is the
whole safety property: every enforcement pass in ``ask()`` -- the forced read, the
forced arithmetic, the ungrounded-number excision -- runs on the text the model
actually produces, so a persona cannot smuggle in a number or drop a citation
after the checks have run. Rewriting afterwards would put styling *outside* the
grounding perimeter, which is exactly the mistake this project keeps not making.

**No characters ship with this repository.** The mechanism is here and
:data:`PLAIN` is the only persona defined, so the bot answers in one neutral
voice everywhere. The hook remains because the grounding property above is worth
keeping intact for anyone who does add a voice: a character added here inherits
:data:`GROUNDING_CLAUSE` and therefore cannot license guessing, whereas one
bolted on at the call site would bypass it.

Anything added here should stay suitable for a general audience. This repository
is public and its subject is market data.
"""

from __future__ import annotations

from dataclasses import dataclass

# Every character prompt ends with this. A voice that licensed guessing would
# undo the entire project, so the constraint is attached to the voice rather
# than left to the system prompt further up, where a small model may have
# stopped paying attention by the time it reaches the end.
GROUNDING_CLAUSE = """\

None of this touches the facts. Every level, price, XP figure and requirement \
still comes from the tools, still gets cited, and you still say plainly when the \
wiki does not settle something. Being in character is never a reason to guess, \
soften a number, or skip reading the page -- a wrong answer delivered in \
character is still wrong, and it is the one thing that would actually let them \
down. Keep it friendly and suitable for all ages.\
"""


@dataclass(frozen=True, slots=True)
class Persona:
    """A named voice: how it talks, how it sounds, how it opens."""

    name: str
    prompt: str
    greeting: str
    # performer_id on the XTTS server. Empty means the server's default.
    voice: str = ""
    # How fast it talks, applied to the finished audio by :func:`voice.retime`
    # rather than sent to the server. The server's own ``speed`` is model
    # conditioning and is not smooth -- 1.35 produces thirty percent MORE audio
    # than 1.33 on one reference, deterministically, which sounds broken -- so
    # pace cannot be tuned there. atempo can, in whatever increment you like.
    tempo: float | None = None


PLAIN = Persona(
    name="plain",
    prompt="",
    greeting="Ask me anything about Old School RuneScape.",
)

PERSONAS: dict[str, Persona] = {"plain": PLAIN}


def for_channel(
    default: str, channels: dict[int, str] | set[int], channel_id: int | None
) -> Persona:
    """The voice to use in one channel.

    ``channels`` maps a channel id to a persona name, so two voices can hold
    different rooms; a bare set of ids means "use the default here", which is
    the simpler configuration and still the common one.

    Returns :data:`PLAIN` for anywhere not listed, and for any name that is not
    a persona this build defines -- which, as shipped, is every name. An unknown
    name resolving to the neutral voice rather than raising is deliberate: a
    stale ``.env`` naming a persona that no longer exists should cost the tone,
    not the bot.
    """
    if channel_id is None or channel_id not in channels:
        return PLAIN
    wanted = channels.get(channel_id) if isinstance(channels, dict) else None
    return PERSONAS.get(wanted or default, PLAIN)
