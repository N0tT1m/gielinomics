"""Discord layer tests. No token, no gateway connection.

bot.py had zero coverage and is the only module that has never run live. These
pin the things that are invisible until a user hits them: a command that isn't
actually a slash command, an embed that exceeds Discord's limits and 400s, and a
sync that silently goes global when it should be instant.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import discord
import pytest

from reldo.agent import Answer
from reldo.bot import (
    ANSWER_BUDGET,
    ReldoClient,
    _strip_mention,
    build_client,
    render_answer,
)
from reldo.conversation import ConversationStore

BOT_ID = 1
ALICE = 20
BOB = 21


class FakeAgent:
    """Records what it was asked, so a rewritten follow-up is visible."""

    def __init__(self, text: str = "an answer"):
        self.asked: list[str] = []
        self.player = None
        self.persona = ""
        self._text = text

    async def ask(self, question: str, *, player=None, persona="", **kwargs):
        self.asked.append(question)
        self.player = player
        self.persona = persona
        return Answer(text=self._text, pages_read=["Abyssal whip"])


class FakeChat:
    def __init__(self, content: str = ""):
        self._content = content

    async def complete(self, messages, *, tools=None, max_tokens=None):
        return {"role": "assistant", "content": self._content}


def fake_message(
    content: str,
    *,
    author: int = ALICE,
    channel: int = 100,
    guild: object | None = object(),
    mentions: tuple[int, ...] = (),
    reply_to: SimpleNamespace | None = None,
    bot: bool = False,
):
    """Enough of a discord.Message for the routing logic under test."""
    sent: list[dict] = []

    async def reply(content=None, *, embed=None, mention_author=None):
        sent.append({"content": content, "embed": embed})
        return SimpleNamespace(id=900 + len(sent))

    return SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=author, bot=bot),
        channel=SimpleNamespace(id=channel, typing=contextlib.nullcontext),
        guild=guild,
        mentions=[SimpleNamespace(id=m) for m in mentions],
        reference=reply_to,
        reply=reply,
        sent=sent,
    )


async def _resolved(value):
    return value


def _asking(agent, *, pages):
    async def ask(question, *, player=None, persona="", **kwargs):
        agent.asked.append(question)
        return Answer(text="an answer", pages_read=list(pages))

    return ask


def bot_client(monkeypatch, **kwargs) -> ReldoClient:
    client = ReldoClient(**kwargs)
    monkeypatch.setattr(
        type(client), "user", property(lambda self: SimpleNamespace(id=BOT_ID))
    )
    return client


EXPECTED_COMMANDS = {
    "wiki", "ge", "trend", "goal", "unlocks", "map", "stats", "xp", "train",
    "search", "help", "link", "unlink", "next", "say", "progress", "wom",
    "womtrack",
}


def test_every_command_is_registered():
    client = build_client(agent=None)
    assert {c.name for c in client.tree.get_commands()} == EXPECTED_COMMANDS


def test_wiki_is_a_real_slash_command_with_a_described_parameter():
    """A prefix command would look fine here and never appear in Discord's UI."""
    client = build_client(agent=None)
    commands = {c.name: c for c in client.tree.get_commands()}

    wiki = commands["wiki"]
    assert isinstance(wiki, discord.app_commands.Command)
    assert wiki.description  # Discord rejects a command with no description
    (question,) = wiki.parameters
    assert question.name == "question"
    assert question.required
    assert question.description  # shown as placeholder text in the client


def test_default_intents_only():
    """message_content is privileged and would gate the bot behind review; slash
    commands do not need it."""
    client = ReldoClient()
    assert client.intents.message_content is False


async def test_guild_sync_is_scoped_to_that_guild(monkeypatch):
    seen: dict = {}

    async def fake_sync(self, *, guild=None):
        seen["guild"] = guild
        return list(self.get_commands())

    monkeypatch.setattr(discord.app_commands.CommandTree, "sync", fake_sync)
    client = build_client(agent=None, guild_id=42)
    await client.setup_hook()

    assert seen["guild"] is not None and seen["guild"].id == 42
    synced = {c.name for c in client.tree.get_commands(guild=discord.Object(id=42))}
    assert synced == EXPECTED_COMMANDS


async def test_global_sync_when_no_guild_is_configured(monkeypatch):
    seen: dict = {}

    async def fake_sync(self, *, guild=None):
        seen["guild"] = guild
        return list(self.get_commands())

    monkeypatch.setattr(discord.app_commands.CommandTree, "sync", fake_sync)
    client = build_client(agent=None)
    await client.setup_hook()
    assert seen["guild"] is None


# -- embed limits ----------------------------------------------------------
# Exceeding any of these is a 400 from Discord at send time, i.e. a user-visible
# failure that no local test would otherwise catch.


def test_long_answer_is_truncated_within_budget():
    embed = render_answer("q", Answer(text="x" * 9000, pages_read=["Vorkath"]))
    assert len(embed.description) <= ANSWER_BUDGET + 2
    assert embed.description.endswith("…")


def test_embed_stays_under_discord_limits_with_many_citations():
    answer = Answer(text="y" * 3000, pages_read=[f"Page {i}" for i in range(60)])
    embed = render_answer("q" * 400, answer)
    assert len(embed) <= 6000          # total embed
    assert len(embed.title) <= 256     # title
    assert len(embed.fields[0].value) <= 1024  # field value


def test_empty_answer_still_renders_something_useful():
    embed = render_answer("q", Answer(text="", pages_read=[]))
    assert "couldn't find" in embed.description
    assert embed.fields == []


def test_an_empty_answer_that_read_pages_does_not_claim_it_found_nothing():
    """Asked "Karambwans", this said "I couldn't find anything on the wiki for
    that" directly above a Sources list naming Karambwan, the karambwan
    money-making guide and a live GE price. Failing to write an answer and
    failing to find anything are different failures, and the second is a
    contradiction the reader can see."""
    embed = render_answer("Karambwans", Answer(text="", pages_read=["Karambwan"]))
    assert "couldn't find" not in embed.description
    assert "could not put an answer together" in embed.description
    # The sources stay, because they are true and they are the evidence that
    # the sentence above them is not.
    assert "[Karambwan]" in embed.fields[0].value


def test_citations_render_as_links_paired_with_their_titles():
    embed = render_answer("q", Answer(text="a", pages_read=["Abyssal whip"]))
    value = embed.fields[0].value
    assert "[Abyssal whip]" in value
    assert "https://oldschool.runescape.wiki/w/Abyssal_whip" in value


@pytest.mark.parametrize("title", ["", "x" * 500])
def test_odd_questions_do_not_break_the_embed(title):
    embed = render_answer(title, Answer(text="answer", pages_read=[]))
    assert len(embed.title) <= 256


# -- who the bot answers ---------------------------------------------------
# Every case that returns True must be one Discord delivers content for without
# the privileged intent: a DM, a mention, or a reply. A case that needs the
# intent would work in testing and return empty content in production.


def test_a_mention_is_addressed(monkeypatch):
    client = bot_client(monkeypatch)
    assert client.addressed(fake_message(f"<@{BOT_ID}> whip?", mentions=(BOT_ID,)))


def test_a_dm_is_addressed(monkeypatch):
    client = bot_client(monkeypatch)
    assert client.addressed(fake_message("whip?", guild=None))


def test_a_reply_to_the_bot_is_addressed_even_without_a_ping(monkeypatch):
    """Suppressing the ping on a reply removes it from mentions; the reference
    is still there, and the person plainly meant to talk to us."""
    client = bot_client(monkeypatch)
    replied = SimpleNamespace(
        message_id=900, resolved=SimpleNamespace(author=SimpleNamespace(id=BOT_ID))
    )
    assert client.addressed(fake_message("what about at 80?", reply_to=replied))


def test_an_unaddressed_channel_message_is_ignored(monkeypatch):
    """This is the case that would need message_content, so it must stay off."""
    client = bot_client(monkeypatch)
    assert not client.addressed(fake_message("how do I kill vorkath"))


def test_another_bot_is_ignored(monkeypatch):
    """Two of these in one channel would talk to each other forever."""
    client = bot_client(monkeypatch)
    assert not client.addressed(fake_message(f"<@{BOT_ID}> hi", mentions=(BOT_ID,), bot=True))


def test_someone_elses_mention_is_ignored(monkeypatch):
    client = bot_client(monkeypatch)
    assert not client.addressed(fake_message("<@999> whip?", mentions=(999,)))


@pytest.mark.parametrize(
    "content,expected",
    [
        (f"<@{BOT_ID}> whip?", "whip?"),
        (f"whip? <@!{BOT_ID}>", "whip?"),
        (f"<@{BOT_ID}>  what   about  at 80?", "what about at 80?"),
        (f"<@{BOT_ID}> tell <@999> about whips", "tell <@999> about whips"),
        (f"<@{BOT_ID}>", ""),
    ],
)
def test_our_own_mention_is_stripped_and_others_are_kept(content, expected):
    assert _strip_mention(content, BOT_ID) == expected


# -- conversation routing --------------------------------------------------


def test_a_reply_resumes_the_conversation_that_produced_it(monkeypatch):
    """Including across users: replying to an answer continues that exchange,
    which is what the person clicking reply meant."""
    store = ConversationStore()
    store.remember((100, ALICE), "whip?", "yes", message_id=900)
    client = bot_client(monkeypatch, store=store)
    replied = SimpleNamespace(message_id=900, resolved=None)
    message = fake_message("what about at 80?", author=BOB, reply_to=replied)
    assert client.conversation_key(message) == (100, ALICE)


def test_an_ordinary_message_is_keyed_per_channel_and_author(monkeypatch):
    client = bot_client(monkeypatch)
    assert client.conversation_key(fake_message("whip?", channel=100)) == (100, ALICE)


# -- answering -------------------------------------------------------------


async def test_a_first_question_is_asked_as_typed_and_remembered(monkeypatch):
    agent = FakeAgent()
    client = bot_client(monkeypatch, agent=agent, chat=FakeChat("rewritten"))
    message = fake_message(f"<@{BOT_ID}> how do I kill Vorkath", mentions=(BOT_ID,))

    await client.on_message(message)

    assert agent.asked == ["how do I kill Vorkath"]  # no history, so no rewrite
    assert message.sent[0]["embed"].description == "an answer"
    assert [t.question for t in client._store.history((100, ALICE))] == [
        "how do I kill Vorkath"
    ]


async def test_a_follow_up_is_asked_in_its_rewritten_form(monkeypatch):
    """The whole point: ask() receives a standalone question, so every
    enforcement pass inside it still applies."""
    agent = FakeAgent()
    store = ConversationStore()
    store.remember((100, ALICE), "is the whip worth it at 70 attack", "Yes.")
    client = bot_client(
        monkeypatch,
        agent=agent,
        chat=FakeChat("is the abyssal whip worth using at 80 attack"),
        store=store,
    )

    await client.on_message(fake_message(f"<@{BOT_ID}> what about at 80?", mentions=(BOT_ID,)))

    assert agent.asked == ["is the abyssal whip worth using at 80 attack"]


async def test_the_resolved_question_is_what_gets_remembered(monkeypatch):
    """Storing 'what about at 80?' would leave the next rewrite resolving a
    pronoun against a pronoun."""
    store = ConversationStore()
    store.remember((100, ALICE), "is the whip worth it at 70 attack", "Yes.")
    client = bot_client(
        monkeypatch,
        agent=FakeAgent(),
        chat=FakeChat("is the abyssal whip worth using at 80 attack"),
        store=store,
    )

    await client.on_message(fake_message(f"<@{BOT_ID}> what about at 80?", mentions=(BOT_ID,)))

    assert [t.question for t in store.history((100, ALICE))][-1] == (
        "is the abyssal whip worth using at 80 attack"
    )


async def test_an_unaddressed_message_is_never_answered(monkeypatch):
    agent = FakeAgent()
    client = bot_client(monkeypatch, agent=agent)
    message = fake_message("how do I kill vorkath")
    await client.on_message(message)
    assert agent.asked == [] and message.sent == []


async def test_a_bare_mention_gets_a_hint_rather_than_a_search(monkeypatch):
    agent = FakeAgent()
    client = bot_client(monkeypatch, agent=agent)
    message = fake_message(f"<@{BOT_ID}>", mentions=(BOT_ID,))
    await client.on_message(message)
    assert agent.asked == []
    assert "Ask me something" in message.sent[0]["content"]


async def test_reset_drops_the_thread(monkeypatch):
    store = ConversationStore()
    store.remember((100, ALICE), "whip?", "yes")
    client = bot_client(monkeypatch, agent=FakeAgent(), store=store)
    await client.on_message(fake_message(f"<@{BOT_ID}> reset", mentions=(BOT_ID,)))
    assert store.history((100, ALICE)) == []


async def test_reset_only_matches_the_word_alone(monkeypatch):
    """'reset' is a command; 'how do I reset my prayer' is a question."""
    agent = FakeAgent()
    client = bot_client(monkeypatch, agent=agent)
    await client.on_message(
        fake_message(f"<@{BOT_ID}> how do I reset my prayer", mentions=(BOT_ID,))
    )
    assert agent.asked == ["how do I reset my prayer"]


async def test_a_crash_is_reported_and_nothing_is_remembered(monkeypatch):
    class Broken:
        async def ask(self, question):
            raise RuntimeError("wiki on fire")

    client = bot_client(monkeypatch, agent=Broken())
    message = fake_message(f"<@{BOT_ID}> whip?", mentions=(BOT_ID,))
    await client.on_message(message)

    assert "Something broke" in message.sent[0]["content"]
    assert client._store.history((100, ALICE)) == []


async def test_a_map_is_shown_when_a_page_read_has_one(monkeypatch):
    wiki = SimpleNamespace(
        wikitext=lambda titles: _resolved(
            {"Ungael": "{{Map|name=Ungael|x=2272|y=4064|zoom=2}}"}
        )
    )
    agent = FakeAgent()
    agent.ask = _asking(agent, pages=["Ungael"])
    client = bot_client(monkeypatch, agent=agent, wiki=wiki)
    message = fake_message(f"<@{BOT_ID}> where is Vorkath", mentions=(BOT_ID,))

    await client.on_message(message)

    field = message.sent[0]["embed"].fields[-1]
    assert field.name == "Map"
    assert "maps.runescape.wiki/osrs/#2/0/0/2272/4064" in field.value


async def test_no_map_field_when_nothing_read_has_one(monkeypatch):
    """An item has no location; an empty Map field would be noise on every
    price question."""
    wiki = SimpleNamespace(wikitext=lambda titles: _resolved({"Abyssal whip": "{{Infobox Item}}"}))
    client = bot_client(monkeypatch, agent=FakeAgent(), wiki=wiki)
    message = fake_message(f"<@{BOT_ID}> whip?", mentions=(BOT_ID,))

    await client.on_message(message)

    assert [f.name for f in message.sent[0]["embed"].fields] == ["Sources"]


async def test_locks_do_not_accumulate(monkeypatch):
    client = bot_client(monkeypatch, agent=FakeAgent())
    for channel in range(5):
        await client.on_message(
            fake_message(f"<@{BOT_ID}> whip?", mentions=(BOT_ID,), channel=channel)
        )
    assert client._locks == {}
    assert client._lock_users == {}


async def test_a_third_message_cannot_overtake_a_queued_second(monkeypatch):
    """The lock must not be discarded while somebody is still queued on it.

    `lock.locked()` reads False between a holder releasing and the next waiter
    being scheduled, so pruning on it dropped a lock B was waiting for; C then
    built a fresh one and ran alongside B. Three messages is the shortest
    sequence that reaches it: A holds, B queues, A finishes, C arrives.
    """
    inside: list[str] = []
    overlaps: list[list[str]] = []

    class SlowAgent:
        async def ask(self, question, **kwargs):
            inside.append(question)
            overlaps.append(list(inside))
            await asyncio.sleep(0.05)  # the wiki call
            inside.remove(question)
            return Answer(text="an answer", pages_read=[])

    client = bot_client(monkeypatch, agent=SlowAgent())

    def ask(name):
        return client.on_message(
            fake_message(f"<@{BOT_ID}> {name}", mentions=(BOT_ID,))
        )

    a = asyncio.create_task(ask("A"))
    await asyncio.sleep(0.01)
    b = asyncio.create_task(ask("B"))  # queues behind A
    await asyncio.sleep(0.06)  # A finishes and lets go of the lock here
    c = asyncio.create_task(ask("C"))  # arrives after A is done
    await asyncio.gather(a, b, c)

    assert overlaps == [["A"], ["B"], ["C"]], f"answers overlapped: {overlaps}"
    assert client._locks == {} and client._lock_users == {}


async def test_the_turn_is_remembered_before_the_next_message_is_let_in(monkeypatch):
    """A follow-up resolves against history, so history has to be current by the
    time the waiter wakes -- which means remembering inside the lock, not after
    it."""
    seen: list[int] = []

    class RecordingAgent:
        def __init__(self, client):
            self._client = client

        async def ask(self, question, **kwargs):
            seen.append(len(self._client._store.history((100, ALICE))))
            await asyncio.sleep(0.02)
            return Answer(text="an answer", pages_read=[])

    client = bot_client(monkeypatch, agent=None)
    client._agent = RecordingAgent(client)

    await asyncio.gather(
        *(
            client.on_message(fake_message(f"<@{BOT_ID}> q{n}", mentions=(BOT_ID,)))
            for n in range(3)
        )
    )
    # Each question sees every turn before it, never a gap.
    assert seen == [0, 1, 2]
