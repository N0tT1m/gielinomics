"""Conversation memory and the follow-up rewrite.

The rewrite is the part with teeth: it sits in front of an ``ask()`` whose every
guarantee assumes a self-contained question, so a rewrite that drops the subject
or imports a stale number is worse than not rewriting at all. These pin the
fallbacks, because each one is a case where doing nothing is the correct answer.
"""

from __future__ import annotations

import pytest

from reldo.conversation import (
    MAX_REWRITE_CHARS,
    ConversationStore,
    Turn,
    _launders_numbers,
    resolve,
)
from reldo.llm import LLMError


class FakeChat:
    """A ChatClient stand-in that returns a canned rewrite."""

    def __init__(self, content: str | None = "", *, error: Exception | None = None):
        self._content = content
        self._error = error
        self.calls: list[list[dict]] = []

    async def complete(self, messages, *, tools=None, max_tokens=None):
        self.calls.append(messages)
        if self._error:
            raise self._error
        return {"role": "assistant", "content": self._content}


WHIP = [
    Turn(question="is the abyssal whip worth it at 70 attack", answer="Yes, at 70 Attack it is.")
]


# -- the store -------------------------------------------------------------


def test_a_fresh_conversation_has_no_history():
    assert ConversationStore().history(("c", "u")) == []


def test_turns_come_back_oldest_first():
    store = ConversationStore()
    store.remember(("c", "u"), "first", "one")
    store.remember(("c", "u"), "second", "two")
    assert [t.question for t in store.history(("c", "u"))] == ["first", "second"]


def test_only_the_last_few_turns_are_kept():
    store = ConversationStore(max_turns=2)
    for n in range(5):
        store.remember(("c", "u"), f"q{n}", f"a{n}")
    assert [t.question for t in store.history(("c", "u"))] == ["q3", "q4"]


def test_two_users_in_one_channel_do_not_share_a_thread():
    store = ConversationStore()
    store.remember(("c", "alice"), "whip?", "yes")
    assert store.history(("c", "bob")) == []


def test_a_stale_conversation_is_forgotten():
    clock = iter([0.0, 0.0, 5000.0, 5000.0])
    store = ConversationStore(ttl=900.0, clock=lambda: next(clock))
    store.remember(("c", "u"), "whip?", "yes")
    assert store.history(("c", "u")) == []


def test_replying_to_an_answer_resumes_that_conversation():
    store = ConversationStore()
    store.remember(("c", "alice"), "whip?", "yes", message_id=999)
    assert store.key_for_message(999) == ("c", "alice")


def test_a_reply_to_an_unknown_message_resumes_nothing():
    assert ConversationStore().key_for_message(4242) is None


def test_a_reply_to_an_expired_conversation_resumes_nothing():
    """The message id outlives the turns; resolving it anyway would key a live
    conversation to history that has already been dropped."""
    clock = iter([0.0, 0.0, 5000.0, 5000.0])
    store = ConversationStore(ttl=900.0, clock=lambda: next(clock))
    store.remember(("c", "u"), "whip?", "yes", message_id=999)
    assert store.key_for_message(999) is None


def test_forgetting_clears_the_thread():
    store = ConversationStore()
    store.remember(("c", "u"), "whip?", "yes")
    store.forget(("c", "u"))
    assert store.history(("c", "u")) == []


def test_conversations_are_capped_so_a_busy_server_cannot_grow_it():
    store = ConversationStore(max_conversations=3)
    for n in range(10):
        store.remember(("c", n), "q", "a")
    assert len(store.history(("c", 0))) == 0  # evicted, least recently used
    assert len(store.history(("c", 9))) == 1


# -- the number guard ------------------------------------------------------


def test_numbers_the_user_typed_may_carry_into_the_rewrite():
    assert _launders_numbers("is the whip worth it at 80 attack", "what about at 80?", []) == []


def test_numbers_from_an_earlier_question_may_carry_too():
    assert (
        _launders_numbers("is the tentacle worth it at 70 attack", "what about a tentacle?", WHIP)
        == []
    )


def test_a_number_only_the_bot_said_is_caught():
    """The failure this exists for: 70 came from the previous answer, and putting
    it in the question would make _ungrounded_numbers treat it as a given."""
    history = [Turn(question="whip requirement?", answer="You need 70 Attack.")]
    assert _launders_numbers(
        "is the whip, which needs 70 attack, worth it", "worth it?", history
    ) == ["70"]


# -- the rewrite -----------------------------------------------------------


async def test_a_first_question_is_never_rewritten():
    """No history means nothing to resolve, and the model call would be waste."""
    chat = FakeChat("something else entirely")
    assert await resolve(chat, [], "how do I kill Vorkath") == "how do I kill Vorkath"
    assert chat.calls == []


async def test_a_follow_up_is_resolved_against_its_history():
    chat = FakeChat("is the abyssal whip worth using at 80 attack")
    got = await resolve(chat, WHIP, "what about at 80?")
    assert got == "is the abyssal whip worth using at 80 attack"


async def test_the_earlier_exchange_is_shown_to_the_rewriter():
    chat = FakeChat("is the abyssal whip worth using at 80 attack")
    await resolve(chat, WHIP, "what about at 80?")
    prompt = chat.calls[0][-1]["content"]
    assert "abyssal whip" in prompt
    assert "what about at 80?" in prompt


async def test_a_model_that_is_down_falls_back_to_the_raw_question():
    chat = FakeChat(error=LLMError("connection refused"))
    assert await resolve(chat, WHIP, "what about at 80?") == "what about at 80?"


@pytest.mark.parametrize("content", ["", None, "   ", "\n\n"])
async def test_an_empty_rewrite_falls_back(content):
    assert await resolve(FakeChat(content), WHIP, "what about at 80?") == "what about at 80?"


async def test_a_rambling_rewrite_falls_back():
    assert await resolve(FakeChat("x" * (MAX_REWRITE_CHARS + 1)), WHIP, "at 80?") == "at 80?"


async def test_a_preamble_is_discarded_and_the_question_kept():
    chat = FakeChat("Here is the rewritten question:\nis the abyssal whip worth it at 80 attack")
    assert await resolve(chat, WHIP, "at 80?") == "is the abyssal whip worth it at 80 attack"


async def test_surrounding_quotes_are_stripped():
    chat = FakeChat('"is the abyssal whip worth it at 80 attack"')
    assert await resolve(chat, WHIP, "at 80?") == "is the abyssal whip worth it at 80 attack"


async def test_a_rewrite_that_launders_a_number_is_rejected():
    """Better to ask the vague question than a precise one built on a stale fact:
    ask() can recover from vagueness and cannot see a smuggled premise."""
    history = [Turn(question="whip requirement?", answer="You need 70 Attack.")]
    chat = FakeChat("is the abyssal whip, which needs 70 attack, worth it")
    assert await resolve(chat, history, "worth it?") == "worth it?"
