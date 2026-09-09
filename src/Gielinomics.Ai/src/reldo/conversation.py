"""Multi-turn memory, and the rewrite that keeps a follow-up grounded.

"What about at 80?" is not a question anybody can answer, and it is most of what
people actually type once a bot answers them once. Two ways to handle it:

* Prepend the earlier exchange to the message list and let the model sort it out.
* Rewrite it into a standalone question first, then run :meth:`WikiAgent.ask`
  exactly as it runs today.

The first is simpler and quietly undoes the whole project. Every enforcement pass
in :mod:`reldo.agent` -- the forced read, the XP handback, the ungrounded-number
check -- tests what happened *during one* ``ask()``: did it search, did it read a
page, is every number in the answer in something it was shown. Hand the model the
previous answer as context and it can answer the follow-up from text it is still
holding, having searched nothing and read nothing, and those checks cannot tell
that apart from a fresh question answered from memory. The invariant would be
enforced against an empty conversation while the real one happened above it.

So: rewrite, then ask. ``ask()`` is untouched, sees a self-contained question, and
every guarantee it makes still holds. The cost is one extra model call per
follow-up, with no tools and ~120 tokens of output.

**The rewrite is not allowed to launder facts.** Turning "what about at 80?" into
"is the abyssal whip, which needs 70 attack, worth using at 80" would put 70 into
the question -- and :func:`reldo.agent._ungrounded_numbers` counts the question as
a grounding source, so a stale figure from a previous answer would come back
blessed. Subjects carry forward; numbers do not, and :func:`_launders_numbers`
enforces that in code rather than trusting the rewrite prompt.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .agent import _NUMBER
from .llm import ChatClient, LLMError

log = logging.getLogger(__name__)

# How many previous exchanges the rewriter sees. Two is enough to resolve "what
# about at 80" and "and the legs?"; more mostly adds tokens and lets a subject
# from five minutes ago hijack a genuinely new question.
MAX_TURNS = 3

# A conversation this old is almost certainly a new one that happens to share a
# channel. Fifteen minutes is long enough to go and check something in game.
TTL_SECONDS = 900.0

# Ceiling on tracked conversations, so a busy server cannot grow this without
# bound. Least-recently-used goes first.
MAX_CONVERSATIONS = 500

# The rewriter needs the previous answer to know what "it" was, not to quote it.
ANSWER_CONTEXT_CHARS = 400

# A rewrite longer than this is the model having written an essay instead of a
# question, which is a failure to follow the instruction rather than a rewrite.
MAX_REWRITE_CHARS = 300

REWRITE_SYSTEM = """\
You rewrite a follow-up message into a question that stands alone.

You are given the recent exchange and a new message. Return one question that \
means what the new message means, but names its subject outright, so somebody \
who never saw the earlier exchange could answer it.

Carry over only the SUBJECT -- the item, monster, skill, quest or player being \
discussed.

Never carry over facts from the earlier answer: no levels, no prices, no drop \
rates, no XP figures, no quest names. Those get looked up again from the wiki. \
Repeating one here would smuggle a possibly stale number into a fresh question \
and it would never be checked.

If the new message already stands alone, return it unchanged. If it is not a \
question at all, return it unchanged.

Return only the question. No preamble, no quotation marks, no explanation.\
"""


@dataclass(frozen=True, slots=True)
class Turn:
    """One question and the answer it got."""

    question: str
    answer: str


class ConversationStore:
    """Recent turns per conversation, with a TTL and a cap.

    Keyed by whatever the caller wants -- the Discord layer uses
    ``(channel_id, user_id)`` so two people asking in the same channel do not
    finish each other's sentences, and resolves a *reply* to whichever
    conversation produced the message being replied to.

    Args:
        clock: Injectable so tests can age a conversation without sleeping.
    """

    def __init__(
        self,
        *,
        max_turns: int = MAX_TURNS,
        ttl: float = TTL_SECONDS,
        max_conversations: int = MAX_CONVERSATIONS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_turns = max_turns
        self._ttl = ttl
        self._max_conversations = max_conversations
        self._clock = clock
        # Ordered by least-recently-used, so eviction is popitem(last=False).
        self._turns: OrderedDict[object, list[Turn]] = OrderedDict()
        self._touched: dict[object, float] = {}
        # Bot message id -> the conversation it belongs to, so replying to an
        # answer resumes that thread even from a different user or channel.
        self._by_message: OrderedDict[int, object] = OrderedDict()

    def history(self, key: object) -> list[Turn]:
        """Recent turns for a conversation, newest last. Empty if it has expired."""
        self._expire()
        turns = self._turns.get(key)
        if not turns:
            return []
        self._turns.move_to_end(key)
        return list(turns)

    def remember(
        self, key: object, question: str, answer: str, *, message_id: int | None = None
    ) -> None:
        """Append a turn, and note which message id can resume this conversation."""
        self._expire()
        turns = self._turns.setdefault(key, [])
        turns.append(Turn(question=question, answer=answer))
        del turns[: -self._max_turns]
        self._touched[key] = self._clock()
        self._turns.move_to_end(key)

        if message_id is not None:
            self._by_message[message_id] = key
            self._by_message.move_to_end(message_id)
            while len(self._by_message) > self._max_conversations * self._max_turns:
                self._by_message.popitem(last=False)

        while len(self._turns) > self._max_conversations:
            evicted, _ = self._turns.popitem(last=False)
            self._touched.pop(evicted, None)

    def key_for_message(self, message_id: int) -> object | None:
        """Which conversation produced this bot message, if it is still known."""
        self._expire()
        key = self._by_message.get(message_id)
        return key if key in self._turns else None

    def forget(self, key: object) -> None:
        """Drop a conversation. Backs the "start over" escape hatch."""
        self._turns.pop(key, None)
        self._touched.pop(key, None)

    def _expire(self) -> None:
        now = self._clock()
        stale = [k for k, at in self._touched.items() if now - at > self._ttl]
        for key in stale:
            self._turns.pop(key, None)
            self._touched.pop(key, None)


def _numbers(text: str) -> set[str]:
    """Multi-digit numbers in some text, comma formatting normalised away."""
    return {m.replace(",", "") for m in _NUMBER.findall(text)}


def _launders_numbers(rewritten: str, question: str, history: Iterable[Turn]) -> list[str]:
    """Figures the rewrite took from a previous *answer* rather than a user.

    Numbers the user typed are fair game -- "what about at 80" is the asker being
    specific, and it carrying into the rewritten question is the whole point.
    Numbers that appear only in something the bot previously said are not: they
    would enter the new question as premises, and the grounding check in
    :mod:`reldo.agent` treats the question as a source it can trust.
    """
    allowed = _numbers(question)
    for turn in history:
        allowed |= _numbers(turn.question)
    return sorted(n for n in _numbers(rewritten) if n not in allowed)


def _prompt(history: list[Turn], question: str) -> str:
    lines = []
    for turn in history:
        lines.append(f"Earlier question: {turn.question}")
        lines.append(f"Your answer: {turn.answer[:ANSWER_CONTEXT_CHARS]}")
    lines.append(f"New message: {question}")
    lines.append("")
    lines.append("Rewrite the new message as a standalone question.")
    return "\n".join(lines)


async def resolve(
    chat: ChatClient, history: list[Turn], question: str, *, max_tokens: int = 120
) -> str:
    """A self-contained version of ``question``, given what came before it.

    Falls back to the raw question on every failure -- no history, a model that
    is down, an empty or rambling rewrite, or one that imported a figure from a
    previous answer. The same call :func:`reldo.agent._keep_best` makes: a
    recovery step is never allowed to leave things worse than it found them.
    """
    if not history:
        return question

    try:
        message = await chat.complete(
            [
                {"role": "system", "content": REWRITE_SYSTEM},
                {"role": "user", "content": _prompt(history, question)},
            ],
            max_tokens=max_tokens,
        )
    except LLMError as exc:
        log.warning("Follow-up rewrite failed, asking as typed: %s", exc)
        return question

    # First line only: a model that adds "Here is the rewritten question:" puts
    # the question on its own line underneath, and the last line is the question
    # more often than the first is.
    lines = [ln.strip().strip('"') for ln in str(message.get("content") or "").splitlines()]
    rewritten = next((ln for ln in reversed(lines) if ln), "")

    if not rewritten or len(rewritten) > MAX_REWRITE_CHARS:
        log.warning("Unusable rewrite %r, asking %r as typed", rewritten[:80], question)
        return question

    laundered = _launders_numbers(rewritten, question, history)
    if laundered:
        # Not a nudge-and-retry: the raw question is right here and is safe, so
        # there is nothing to gain from asking twice.
        log.warning(
            "Rewrite %r carried %s from an earlier answer; asking as typed",
            rewritten,
            ", ".join(laundered),
        )
        return question

    if rewritten != question:
        log.info("Follow-up %r -> %r", question, rewritten)
    return rewritten
