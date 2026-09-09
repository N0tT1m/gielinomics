"""Slash commands, invoked without a gateway.

Two classes of failure live here and neither shows up locally any other way.
Discord *rejects* a command whose description is too long or whose option has no
description, at sync time, for the whole tree -- so one bad string takes every
command down. And it rejects an embed over its size limits at send time, which
surfaces as a user seeing nothing at all.

The commands are called through their ``.callback``, which is the plain coroutine
underneath the decorator, so none of this needs a connection.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from reldo.agent import Answer
from reldo.bot import build_client
from reldo.commands import MAX_CHOICES, _block
from reldo.ge import GEError, Item, Price
from reldo.skills import MAX_LEVEL, SKILLS


class FakeInteraction:
    """Captures whatever the command sends back."""

    def __init__(self, channel_id: int = 100, user_id: int = 20):
        self.channel_id = channel_id
        self.user = SimpleNamespace(id=user_id)
        self.sent: list[dict] = []
        self.deferred = False
        interaction = self

        async def defer(*, thinking=False):
            interaction.deferred = True

        async def send(content=None, *, embed=None, wait=False):
            interaction.sent.append({"content": content, "embed": embed})
            return SimpleNamespace(id=900)

        self.response = SimpleNamespace(defer=defer, send_message=send)
        self.followup = SimpleNamespace(send=send)

    @property
    def embed(self):
        return self.sent[0]["embed"]

    @property
    def text(self):
        return self.sent[0]["content"]


def price(name: str, item_id: int, value: int, volume: int) -> Price:
    item = Item(id=item_id, name=name, limit=None, high_alch=None, members=True)
    return Price(
        item=item, instant_sell=value, instant_buy=value,
        avg_sell=value, avg_buy=value, volume=volume,
    )


class FakeGE:
    def __init__(self, results=None, *, error=None):
        self._results = results or {}
        self._error = error
        self.looked_up: list[str] = []

    async def lookup(self, name, *, limit=12):
        self.looked_up.append(name)
        if self._error:
            raise self._error
        return self._results.get(name, [])

    async def find(self, query, *, limit=12):
        if self._error:
            raise self._error
        return [p.item for p in self._results.get(query, [])][:limit]


class FakeRetriever:
    def __init__(self, hits=()):
        self._hits = list(hits)

    async def shortlist(self, query, *, k=8, pool=20):
        return self._hits[:k]


def hit(title, summary="a summary", score=0.03, found_by=("semantic", "keyword")):
    return SimpleNamespace(title=title, summary=summary, score=score, found_by=found_by)


def build(**kwargs):
    agent = SimpleNamespace(**kwargs)
    client = build_client(agent=agent, wiki=kwargs.get("wiki"))
    return {c.name: c for c in client.tree.get_commands()}


# -- what Discord will accept at sync time ---------------------------------


def test_every_command_has_a_description_within_the_limit():
    """Discord rejects the whole tree over this, not the one command."""
    for command in build_client(agent=None).tree.get_commands():
        assert command.description, command.name
        assert len(command.description) <= 100, command.name


def test_every_option_is_described():
    for command in build_client(agent=None).tree.get_commands():
        for parameter in command.parameters:
            assert parameter.description, f"{command.name}.{parameter.name}"
            assert len(parameter.description) <= 100, f"{command.name}.{parameter.name}"


def test_the_skill_dropdown_fits_discords_choice_ceiling():
    """24 skills against a limit of 25. Adding a 26th silently drops one."""
    assert len(SKILLS) <= MAX_CHOICES
    (skill,) = [p for p in build(retriever=None)["unlocks"].parameters if p.name == "skill"]
    assert {c.value for c in skill.choices} == set(SKILLS)


def test_the_level_option_is_bounded():
    """Range keeps 'level 500' from reaching the scan at all."""
    (level,) = [p for p in build(retriever=None)["unlocks"].parameters if p.name == "level"]
    assert (level.min_value, level.max_value) == (1, MAX_LEVEL)


# -- /trend -----------------------------------------------------------------
# The one question the upstream APIs cannot answer, so the command has to cope
# with a GE client that cannot answer it either.


class FakeTrend:
    """What GEClient.trend returns, reduced to what the embed reads."""

    def __init__(self, *, direction="rising", start=100.0, end=150.0, samples=168):
        self.item = SimpleNamespace(name="Abyssal whip")
        self.window = "7d"
        self.direction = direction
        self.start = start
        self.end = end
        self.low = 95.0
        self.high = 155.0
        self.volume = 12_345
        self.samples = samples
        self.change_percent = None if start is None else (end - start) / start * 100


# A sentinel, because None is a meaningful result here -- it is what trend()
# returns for a name that matches no tradeable item -- so it cannot double as
# "caller did not pass one".
_DEFAULT = object()


class PlatformGE(FakeGE):
    """A GE client that can answer history, the way the platform subclass can."""

    def __init__(self, result=_DEFAULT, *, error=None, **kwargs):
        super().__init__(**kwargs)
        self._trend = FakeTrend() if result is _DEFAULT else result
        self._trend_error = error
        self.asked: list[tuple[str, str]] = []

    async def trend(self, query, *, window="7d", interval="1h"):
        self.asked.append((query, window))
        if self._trend_error:
            raise self._trend_error
        return self._trend


async def test_trend_reports_the_direction_before_the_numbers():
    ge = PlatformGE()
    interaction = FakeInteraction()
    await build(ge=ge)["trend"].callback(interaction, "abyssal whip", "7d")

    assert ge.asked == [("abyssal whip", "7d")]
    assert "rising" in interaction.embed.description
    values = {f.name: f.value for f in interaction.embed.fields}
    assert values["Was"] == "100 gp"
    assert values["Now"] == "150 gp"
    assert values["Change"] == "+50.0%"


async def test_trend_defaults_to_a_week():
    ge = PlatformGE()
    await build(ge=ge)["trend"].callback(FakeInteraction(), "abyssal whip")
    assert ge.asked == [("abyssal whip", "7d")]


async def test_trend_says_so_when_the_platform_is_not_configured():
    """Reading from the wiki means there is no history to read at all.

    The plain GEClient has no trend method, so the command must say why rather
    than raising AttributeError at the user.
    """
    interaction = FakeInteraction()
    await build(ge=FakeGE())["trend"].callback(interaction, "abyssal whip", "7d")

    assert "keeps no history" in interaction.text
    assert "RELDO_GIELINOMICS_URL" in interaction.text


async def test_trend_on_an_unknown_item_says_which_item():
    interaction = FakeInteraction()
    await build(ge=PlatformGE(result=None))["trend"].callback(
        interaction, "not a real thing", "7d"
    )
    assert "not a real thing" in interaction.text


async def test_trend_with_no_retained_history_does_not_claim_a_direction():
    """A platform running for a day cannot answer a question about a month."""
    interaction = FakeInteraction()
    ge = PlatformGE(result=FakeTrend(samples=0, end=None, start=None))
    await build(ge=ge)["trend"].callback(interaction, "abyssal whip", "30d")

    assert "No retained history" in interaction.embed.description
    assert not interaction.embed.fields


async def test_an_unparseable_window_is_reported_rather_than_defaulted():
    """Silently falling back would answer about a period nobody asked for."""
    interaction = FakeInteraction()
    ge = PlatformGE(error=ValueError("Unparseable window 'last tuesday'."))
    await build(ge=ge)["trend"].callback(interaction, "abyssal whip", "last tuesday")

    assert "Unparseable window" in interaction.text


async def test_a_platform_that_is_down_does_not_traceback_at_the_user():
    interaction = FakeInteraction()
    ge = PlatformGE(error=RuntimeError("connection refused"))
    await build(ge=ge)["trend"].callback(interaction, "abyssal whip", "7d")

    assert "could not answer" in interaction.text


# -- /wiki ------------------------------------------------------------------
# The only command that reaches the model, and the one people will use after
# /link tells them their answers now start from their actual level.


class FakeAskAgent:
    """Records the context /wiki hands to ask()."""

    def __init__(self):
        self.player = None
        self.persona = ""
        self.live = ""

    async def ask(self, question, *, player=None, persona="", live="", **kwargs):
        self.player, self.persona, self.live = player, persona, live
        return Answer(text="an answer", pages_read=["Mining"])


def wiki_client(**kwargs):
    agent = FakeAskAgent()
    client = build_client(agent=agent, **kwargs)
    return agent, {c.name: c for c in client.tree.get_commands()}["wiki"]


async def test_wiki_answers_as_the_asker_rather_than_as_a_stranger():
    """The ids reach ask() at all. Omitting them did not fail, it degraded:
    `_answer` defaults both to None, so /link would take a username, confirm it,
    promise answers from the level you actually have -- and then /wiki returned
    the level-1 answer, with no error anywhere to say why."""
    agent, wiki = wiki_client(
        accounts=SimpleNamespace(get=lambda user_id: "Zezima" if user_id == 20 else None),
        persona="plain",
        persona_channels={100: "plain"},
        live=SimpleNamespace(
            summary=lambda rsn: f"Right now, live from {rsn}'s client",
            profile=lambda rsn: None,
        ),
    )
    await wiki.callback(FakeInteraction(channel_id=100, user_id=20), "how do I train mining")

    assert agent.player == "Zezima"
    assert "live from Zezima's client" in agent.live


async def test_wiki_from_an_unlinked_user_is_still_answered_generically():
    agent, wiki = wiki_client(
        accounts=SimpleNamespace(get=lambda user_id: None), persona="plain"
    )
    await wiki.callback(FakeInteraction(channel_id=100, user_id=99), "how do I train mining")

    assert agent.player is None
    assert agent.live == ""


async def test_two_slash_wiki_answers_cannot_overlap():
    """`/wiki` answers under the conversation lock, exactly as a mention does.

    It used to reach past the lock into `_answer` and `_store` directly. That
    key is the same one `conversation_key` falls back to, so it was not merely
    racing other `/wiki` calls -- it raced mentions in the same channel, over
    the same history, which is the whole thing the lock in `bot.py` exists for.
    """
    inside: list[str] = []
    overlaps: list[list[str]] = []

    class SlowAgent:
        async def ask(self, question, **kwargs):
            inside.append(question)
            overlaps.append(list(inside))
            await asyncio.sleep(0.05)  # the wiki call
            inside.remove(question)
            return Answer(text=f"answered {question}", pages_read=[])

    client = build_client(agent=SlowAgent())
    wiki = {c.name: c for c in client.tree.get_commands()}["wiki"]

    await asyncio.gather(
        wiki.callback(FakeInteraction(channel_id=100, user_id=20), "A"),
        wiki.callback(FakeInteraction(channel_id=100, user_id=20), "B"),
    )

    assert overlaps == [["A"], ["B"]], f"answers overlapped: {overlaps}"
    # Both turns remembered, and the lock cleaned up behind them.
    assert [t.question for t in client._store.history((100, 20))] == ["A", "B"]
    assert client._locks == {} and client._lock_users == {}


async def test_a_slash_wiki_turn_is_remembered_before_the_next_is_let_in():
    """A follow-up resolves against history, so history has to be current by the
    time the waiter wakes -- which means remembering inside the lock."""
    seen: list[int] = []

    class RecordingAgent:
        def __init__(self, client):
            self._client = client

        async def ask(self, question, **kwargs):
            seen.append(len(self._client._store.history((100, 20))))
            await asyncio.sleep(0.02)
            return Answer(text="an answer", pages_read=[])

    client = build_client(agent=None)
    client._agent = RecordingAgent(client)
    wiki = {c.name: c for c in client.tree.get_commands()}["wiki"]

    await asyncio.gather(
        *(
            wiki.callback(FakeInteraction(channel_id=100, user_id=20), f"q{n}")
            for n in range(3)
        )
    )
    assert seen == [0, 1, 2], f"a question saw a gap in history: {seen}"


async def test_wiki_keeps_the_persona_out_of_a_channel_it_was_not_given():
    """A character configured for one room must not follow the bot into #help;
    passing the channel id is what makes that check possible at all."""
    agent, wiki = wiki_client(persona="plain", persona_channels={100: "plain"})
    await wiki.callback(FakeInteraction(channel_id=555, user_id=20), "how do I train mining")

    assert agent.persona == ""


# -- /ge --------------------------------------------------------------------


async def test_ge_ranks_without_touching_the_model():
    ge = FakeGE({
        "sandstone": [price("Sandstone (10kg)", 1, 2700, 146)],
        "granite": [price("Granite (5kg)", 2, 713, 354)],
    })
    interaction = FakeInteraction()
    await build(ge=ge)["ge"].callback(interaction, "sandstone granite")

    assert ge.looked_up == ["sandstone", "granite"]
    assert "Sandstone (10kg) is the best of these to sell" in interaction.embed.description


async def test_ge_deduplicates_overlapping_queries():
    """'granite' and 'granite maul' would otherwise list the same row twice."""
    same = price("Granite (5kg)", 2, 713, 354)
    ge = FakeGE({"granite": [same], "granite rock": [same]})
    interaction = FakeInteraction()
    await build(ge=ge)["ge"].callback(interaction, "granite granite rock")
    # One surviving row means the single-item layout, where the name is the
    # title. Two would have fallen through to the table.
    assert interaction.embed.title == "Granite (5kg)"
    assert "Granite (5kg)" not in (interaction.embed.description or "")


async def test_ge_one_item_is_fields_not_a_monospace_paragraph():
    """Price.summary is written for the model: one long labelled line per fact,
    nothing aligned. In a Discord code block those lines wrap mid-number and the
    block buys nothing, because it has no columns to align."""
    ge = FakeGE({"marlin": [price("Marlin", 1, 3480, 435433)]})
    interaction = FakeInteraction()
    await build(ge=ge)["ge"].callback(interaction, "marlin")
    assert "```" not in (interaction.embed.description or "")
    assert interaction.embed.title == "Marlin"
    assert {f.name for f in interaction.embed.fields} & {"Traded", "You receive"}


async def test_ge_reads_commas_as_the_separator():
    maul = price("Granite maul", 3, 40000, 900)
    stone = price("Sandstone (10kg)", 1, 2700, 146)
    ge = FakeGE({"granite maul": [maul], "sandstone": [stone]})
    interaction = FakeInteraction()
    await build(ge=ge)["ge"].callback(interaction, "granite maul, sandstone")

    assert ge.looked_up == ["granite maul", "sandstone"]


async def test_ge_still_takes_space_separated_names_without_commas():
    """What the command has always accepted. "sandstone granite" names nothing
    on its own, so it is two items and stays two items."""
    ge = FakeGE({
        "sandstone": [price("Sandstone (10kg)", 1, 2700, 146)],
        "granite": [price("Granite (5kg)", 2, 713, 354)],
    })
    interaction = FakeInteraction()
    await build(ge=ge)["ge"].callback(interaction, "sandstone granite")

    assert ge.looked_up == ["sandstone", "granite"]


async def test_a_multi_word_item_name_is_one_lookup_not_two():
    """The catalogue settles the ambiguity rather than a guess: "Granite maul"
    is an item, so it is one lookup even with no comma to say so."""
    maul = price("Granite maul", 3, 40000, 900)
    ge = FakeGE({"granite maul": [maul]})
    interaction = FakeInteraction()
    await build(ge=ge)["ge"].callback(interaction, "granite maul")

    assert ge.looked_up == ["granite maul"]
    assert interaction.embed.title == "Granite maul"


async def test_a_catalogue_failure_does_not_change_how_the_input_is_read():
    """find() is consulted to disambiguate; when it raises, the lookup below
    still has to report the failure rather than this swallowing it."""
    ge = FakeGE(error=GEError("prices API is down"))
    interaction = FakeInteraction()
    await build(ge=ge)["ge"].callback(interaction, "granite maul")
    assert "prices API is down" in interaction.text


async def test_ge_says_so_when_nothing_matches():
    interaction = FakeInteraction()
    await build(ge=FakeGE())["ge"].callback(interaction, "notanitem")
    assert "No tradeable item" in interaction.text


async def test_ge_reports_an_api_failure_rather_than_crashing():
    interaction = FakeInteraction()
    await build(ge=FakeGE(error=GEError("prices API is down")))["ge"].callback(interaction, "coal")
    assert "prices API is down" in interaction.text


async def test_ge_defers_because_the_lookup_outlives_the_ack_window():
    interaction = FakeInteraction()
    await build(ge=FakeGE())["ge"].callback(interaction, "coal")
    assert interaction.deferred


# -- /ge autocomplete -------------------------------------------------------


async def test_autocomplete_completes_only_the_last_entry():
    """The option holds several items; replacing the whole value would throw
    away what the user already typed."""
    ge = FakeGE({"gran": [price("Granite (5kg)", 2, 713, 354)]})
    choices = await build(ge=ge)["ge"]._params["items"].autocomplete(
        FakeInteraction(), "sandstone, gran"
    )
    assert [c.value for c in choices] == ["sandstone, Granite (5kg)"]


async def test_autocomplete_can_suggest_an_item_whose_name_has_a_space():
    """Completing per word could not reach these at all -- it would have been
    matching on "maul" with "granite" stranded in front of it."""
    ge = FakeGE({"granite ma": [price("Granite maul", 3, 40000, 900)]})
    choices = await build(ge=ge)["ge"]._params["items"].autocomplete(
        FakeInteraction(), "granite ma"
    )
    assert [c.value for c in choices] == ["Granite maul"]


async def test_what_autocomplete_offers_is_what_the_command_can_parse():
    """The bug this pair exists to stop: the dropdown offered real item names,
    and the command then split them on whitespace and looked up the pieces. A
    suggestion its own command cannot read is worse than no suggestion."""
    maul = price("Granite maul", 3, 40000, 900)
    ge = FakeGE({"granite ma": [maul], "Granite maul": [maul]})
    command = build(ge=ge)["ge"]

    (choice,) = await command._params["items"].autocomplete(
        FakeInteraction(), "granite ma"
    )
    interaction = FakeInteraction()
    await command.callback(interaction, choice.value)

    # One item resolved, not "granite" and "maul" as two.
    assert ge.looked_up == ["Granite maul"]
    assert interaction.embed.title == "Granite maul"


async def test_autocomplete_stays_quiet_until_there_is_something_to_match():
    ge = FakeGE({"g": [price("Granite (5kg)", 2, 713, 354)]})
    assert await build(ge=ge)["ge"]._params["items"].autocomplete(FakeInteraction(), "g") == []


async def test_autocomplete_never_raises_into_the_client():
    """A failed suggestion must degrade to no suggestions, not an error toast."""
    ge = FakeGE(error=GEError("down"))
    assert await build(ge=ge)["ge"]._params["items"].autocomplete(FakeInteraction(), "coal") == []


# -- /xp --------------------------------------------------------------------


async def test_xp_computes_the_gap_exactly():
    interaction = FakeInteraction()
    await build()["xp"].callback(interaction, 1, 99, None, None)
    assert "13,034,431" in interaction.embed.description


async def test_xp_turns_a_rate_into_hours():
    interaction = FakeInteraction()
    await build()["xp"].callback(interaction, 45, 99, 126000, None)
    assert "hours" in interaction.embed.description


async def test_xp_answers_without_deferring():
    """A table lookup and two divisions land well inside the 3s window; an ack
    would only add a round trip."""
    interaction = FakeInteraction()
    await build()["xp"].callback(interaction, 1, 99, None, None)
    assert not interaction.deferred


async def test_xp_refuses_a_backwards_range_in_words():
    interaction = FakeInteraction()
    await build()["xp"].callback(interaction, 99, 45, None, None)
    assert "Cannot compute" in interaction.text


# -- /stats -----------------------------------------------------------------


def _player(name="Lynx Titan", **levels):
    """A player shaped like hiscores.Player, for the layout to render."""
    levels = levels or {"Attack": 99, "Construction": 99, "Fishing": 99}
    skills = {
        skill: SimpleNamespace(name=skill, level=level, xp=level * 1000, ranked=True)
        for skill, level in levels.items()
    }
    skills["Overall"] = SimpleNamespace(
        name="Overall", level=2277, xp=4_600_000_000, ranked=True
    )
    return SimpleNamespace(
        name=name, skills=skills, combat_level=126, done=lambda: [],
        summary=lambda: f"{name} -- total level 2277",
    )


async def test_stats_reports_the_hiscores():
    hiscores = SimpleNamespace(lookup=lambda name: _resolved(_player()))
    interaction = FakeInteraction()
    await build(hiscores=hiscores)["stats"].callback(interaction, "Lynx Titan")
    assert "2,277" in interaction.embed.description


async def test_stats_lays_levels_out_as_a_grid():
    """Player.summary joins 23 skills onto one 291-character line -- right for
    the model, and in a code block a paragraph of numbers you cannot scan."""
    hiscores = SimpleNamespace(lookup=lambda name: _resolved(_player()))
    interaction = FakeInteraction()
    await build(hiscores=hiscores)["stats"].callback(interaction, "Lynx Titan")
    body = interaction.embed.description
    assert "Attack" in body and "Fishing" in body
    # Alphabetical, three to a row, and no row wide enough to wrap.
    grid = body.split("```")[1].strip().splitlines()
    assert grid[0].startswith("Attack")
    assert max(len(row) for row in grid) <= 50


async def test_stats_passes_a_lookup_failure_through_in_words():
    async def missing(name):
        raise RuntimeError("No such player 'nosuchplayer'.")

    interaction = FakeInteraction()
    await build(hiscores=SimpleNamespace(lookup=missing))["stats"].callback(
        interaction, "nosuchplayer"
    )
    assert "No such player" in interaction.text


# -- /map -------------------------------------------------------------------


async def test_map_falls_past_hits_that_have_no_map_of_their_own():
    """Vorkath outranks Ungael for 'where is Vorkath' and has no map itself."""
    wiki = SimpleNamespace(
        wikitext=lambda titles: _resolved(
            {"Ungael": "{{Map|name=Ungael|x=2272|y=4064|zoom=2}}"}
        )
    )
    agent = SimpleNamespace(retriever=FakeRetriever([hit("Vorkath"), hit("Ungael")]))
    client = build_client(agent=agent, wiki=wiki)
    interaction = FakeInteraction()
    await {c.name: c for c in client.tree.get_commands()}["map"].callback(
        interaction, "where is vorkath"
    )
    assert "#2/0/0/2272/4064" in interaction.embed.description


async def test_map_says_so_when_nothing_found_has_one():
    wiki = SimpleNamespace(wikitext=lambda titles: _resolved({"Abyssal whip": "{{Infobox Item}}"}))
    agent = SimpleNamespace(retriever=FakeRetriever([hit("Abyssal whip")]))
    client = build_client(agent=agent, wiki=wiki)
    interaction = FakeInteraction()
    await {c.name: c for c in client.tree.get_commands()}["map"].callback(interaction, "whip")
    assert "no page in the results has a map" in interaction.text


# -- /search ----------------------------------------------------------------


async def test_search_shows_which_half_of_the_hybrid_found_each_hit():
    """The flags are the reason this exists rather than being a worse /wiki."""
    agent = SimpleNamespace(
        retriever=FakeRetriever([hit("Nylocas", found_by=("semantic",))])
    )
    interaction = FakeInteraction()
    await build_commands(agent)["search"].callback(interaction, "boss that heals itself")
    assert "semantic" in interaction.embed.description
    assert "Nylocas" in interaction.embed.description


async def test_search_results_are_not_a_fixed_width_block():
    """Titles are long and vary wildly in width. In a monospace block every
    second row wraps on a normal-width client and the columns stop lining up,
    which is the only reason to use one."""
    agent = SimpleNamespace(
        retriever=FakeRetriever([hit("Money making guide/Catching trout & salmon")])
    )
    interaction = FakeInteraction()
    await build_commands(agent)["search"].callback(interaction, "fishing money")
    assert "```" not in interaction.embed.description
    # Each hit links to the page, so a result is one click from being read.
    assert (
        "https://oldschool.runescape.wiki/w/Money_making_guide/Catching_trout_&_salmon"
        in interaction.embed.description
    )


async def test_search_escapes_markdown_in_titles():
    """Wiki titles carry _ and * often enough to matter; unescaped, one of them
    silently italicises every row after it."""
    agent = SimpleNamespace(retriever=FakeRetriever([hit("Dragon_scimitar *test*")]))
    interaction = FakeInteraction()
    await build_commands(agent)["search"].callback(interaction, "scim")
    assert r"Dragon\_scimitar \*test\*" in interaction.embed.description


# -- rendering --------------------------------------------------------------


def test_a_long_block_is_cut_on_a_line_boundary():
    """Truncating a fixed-width table mid-row yields a line that looks like data
    and is not."""
    block = _block("\n".join(f"row {n} " + "x" * 50 for n in range(500)), budget=200)
    assert block.startswith("```") and block.endswith("```")
    assert "…" in block
    assert len(block) <= 200 + 20


@pytest.mark.parametrize(
    "name",
    ["wiki", "ge", "unlocks", "map", "stats", "xp", "search", "help", "link",
     "unlink", "next", "say", "progress", "wom", "womtrack"],
)
def test_no_command_can_exceed_discords_embed_ceiling(name):
    assert len(build_client(agent=None).tree.get_command(name).description) <= 100


async def test_help_lists_every_other_command():
    interaction = FakeInteraction()
    await build()["help"].callback(interaction)
    listed = " ".join(f.value for f in interaction.embed.fields)
    for command in ("/ge", "/unlocks", "/xp", "/stats", "/map", "/search", "/wiki"):
        assert command in listed
    # The conversational paths are the ones the slash UI cannot advertise.
    assert "mention me" in interaction.embed.description


async def _resolved(value):
    return value


def build_commands(agent):
    return {c.name: c for c in build_client(agent=agent).tree.get_commands()}
