"""Agent tests with a stubbed chat endpoint. No model server, no network.

The behaviour these exist for is the read-before-answer invariant. Measured on
qwen3:32b, "how do I kill Vorkath" searched, skipped the page read, and answered
from memory with the wrong quest, island, and attack type -- fluent, uncited, and
wrong. Prompting alone did not fix it; the enforcement in `ask` did. That
enforcement is the thing most likely to get "simplified" away by someone who
hasn't seen it fail.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from reldo.agent import (
    _ASKS_BEST_MONEY,
    _ASKS_DURATION,
    _ASKS_HOW_TO_MAKE,
    _ASKS_QUANTITY,
    _ASKS_REQUIREMENTS,
    _DEAD_END,
    _LEVEL_RANGE,
    Answer,
    Budget,
    WikiAgent,
    _best_title,
    _drop_ungrounded_claims,
    _fewer_invented,
    _grounding_nudge,
    _is_tool_leak,
    _last_assistant_text,
    _list_request,
    _material_candidates,
    _named_quest,
    _quantity_request,
    _render_recipe,
    _render_training_cost,
    _skill_level_question,
    _skill_mismatch,
    _target_page,
    _ungrounded_numbers,
)
from reldo.bucket import BucketError
from reldo.ge import Item, Price
from reldo.hiscores import HiscoresError
from reldo.live import parse_profile
from reldo.llm import ChatClient
from reldo.money import parse_goal
from reldo.retrieval import HybridRetriever
from tests.test_retrieval import FakeIndex, FakeWiki


def reply(content=None, tool_calls=None):
    message: dict = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": message}]})


def call(name, arguments, call_id="c1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def build_agent(responses, bodies=None):
    # Padded rather than a bare iterator: the enforcement passes in `ask` fire
    # conditionally, so the exact number of round trips is not something a test
    # should have to predict. Running out used to surface as an unhelpful
    # "coroutine raised StopIteration" from inside httpx.
    it = iter(responses)
    # Every request body, so a test can assert on what the model was actually
    # shown -- the system prompt, and anything put in front of the question.
    sent: list[dict] = []

    def next_response(request):
        sent.append(json.loads(request.content))
        try:
            return next(it)
        except StopIteration:
            return reply("(stub exhausted)")

    chat = ChatClient(
        "http://stub/v1", "test-model",
        transport=httpx.MockTransport(next_response),
    )
    chat.sent = sent
    retriever = HybridRetriever(
        FakeWiki(["Vorkath"], bodies=bodies or {"Vorkath": "Vorkath is on Ungael."}),
        FakeIndex(["Vorkath"]),
    )
    return WikiAgent(retriever, chat), chat


async def test_search_then_read_produces_a_cited_answer():
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("Vorkath is on Ungael."),
    ])
    async with chat:
        answer = await agent.ask("where is vorkath")

    assert answer.searches == ["vorkath"]
    assert answer.pages_read == ["Vorkath"]
    assert answer.citations == ["https://oldschool.runescape.wiki/w/Vorkath"]
    assert answer.text == "Vorkath is on Ungael."


async def test_answering_without_reading_triggers_a_forced_read():
    """The whole point: searched but never read -> hand it back and demand a read."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply("Vorkath is in the Fremennik Province."),   # ungrounded, wrong
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("Vorkath is on Ungael."),                    # grounded, correct
    ])
    async with chat:
        answer = await agent.ask("where is vorkath")

    assert answer.pages_read == ["Vorkath"]
    assert answer.text == "Vorkath is on Ungael."
    assert "Fremennik" not in answer.text


async def test_no_forced_read_when_a_page_was_already_read():
    """The nudge costs a round trip; it must not fire on a grounded answer."""
    calls = []

    def handler(request):
        calls.append(1)
        return [
            reply(None, [call("read_wiki_page", '{"title": "Vorkath"}')]),
            reply("grounded"),
        ][min(len(calls) - 1, 1)]

    chat = ChatClient("http://stub/v1", "m", transport=httpx.MockTransport(handler))
    retriever = HybridRetriever(
        FakeWiki(["Vorkath"], bodies={"Vorkath": "body"}), FakeIndex(["Vorkath"])
    )
    async with chat:
        answer = await WikiAgent(retriever, chat).ask("q")

    assert answer.pages_read == ["Vorkath"]
    assert len(calls) == 2  # no third call -> no nudge


async def test_answering_with_no_search_at_all_is_left_alone():
    """Nothing to ground against, so the nudge would just burn a round trip."""
    agent, chat = build_agent([reply("I refuse to search.")])
    async with chat:
        answer = await agent.ask("q")
    assert answer.searches == []
    assert answer.pages_read == []
    assert answer.text == "I refuse to search."


async def test_search_output_is_capped_per_hit():
    from reldo.agent import Answer

    retriever = HybridRetriever(FakeWiki(["Vorkath"]), FakeIndex(["Vorkath"]))
    chat = ChatClient("http://stub/v1", "m", transport=httpx.MockTransport(lambda r: reply("x")))
    agent = WikiAgent(retriever, chat)
    search = next(t for t in agent._build_tools(Answer(text="")) if t.name == "search_wiki")
    out = await search.handler(query="anything")
    await chat.aclose()
    # "summary of Vorkath" is short, but the cap is what matters structurally.
    assert all(len(line) < 200 for line in out.splitlines())


async def test_pages_read_are_deduplicated_preserving_order():
    agent, chat = build_agent([
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "a")]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "b")]),
        reply("done"),
    ])
    async with chat:
        answer = await agent.ask("q")
    assert answer.pages_read == ["Vorkath"]


async def test_unknown_page_is_reported_without_recording_a_citation():
    agent, chat = build_agent([
        reply(None, [call("read_wiki_page", '{"title": "Nope"}')]),
        reply("could not find it"),
    ])
    async with chat:
        answer = await agent.ask("q")
    assert answer.pages_read == []
    assert answer.citations == []


# -- the repeated-search guard ---------------------------------------------


async def test_repeating_a_search_is_refused_rather_than_re_run():
    """Retrieval is deterministic, so a repeat cannot return anything new.
    Measured: "how many hours from 45 to 99 mining" issued the identical query
    five times, exhausted max_iterations, and answered off the wrong page."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply(None, [call("search_wiki", '{"query": "Vorkath "}', "c2")]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c3")]),
        reply("Vorkath is on Ungael."),
    ])
    async with chat:
        answer = await agent.ask("where is vorkath")

    # Case and whitespace differences are the same search.
    assert answer.searches == ["vorkath"]
    assert answer.text == "Vorkath is on Ungael."


async def test_the_refusal_still_hands_back_the_original_results():
    """Refusing without the results would strand a model that had forgotten
    them, turning a wasted step into a dead end."""
    seen = []

    def handler(request):
        import json as _json
        body = _json.loads(request.content)
        seen.extend(m for m in body["messages"] if m.get("role") == "tool")
        return next(it)

    it = iter([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply(None, [call("search_wiki", '{"query": "vorkath"}', "c2")]),
        # Read a page so the read-enforcement doesn't fire and consume more
        # responses than this stub provides.
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c3")]),
        reply("done"),
    ])
    chat = ChatClient("http://stub/v1", "m", transport=httpx.MockTransport(handler))
    retriever = HybridRetriever(
        FakeWiki(["Vorkath"], bodies={"Vorkath": "x"}), FakeIndex(["Vorkath"])
    )
    async with chat:
        await WikiAgent(retriever, chat).ask("where is vorkath")

    repeat = [m for m in seen if "already searched" in m["content"]]
    assert repeat, "the second identical search should have been refused"
    assert "Vorkath" in repeat[0]["content"], "refusal must still carry the results"


# -- XP arithmetic enforcement ---------------------------------------------


async def test_duration_question_without_calculate_xp_is_handed_back():
    """mistral-small3.2:24b answered "200,000 XP per hour ... approximately 100
    hours" for 45->99 Mining. Those cannot both be true: the gap is 12,972,919,
    so 200k/hr is 64.9h and the guide's real 126k/hr is 103h."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "mining"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("About 100 hours at 200,000 xp/hr."),
        # after the nudge
        reply(None, [call("calculate_xp",
                          '{"from_level": 45, "to_level": 99, "xp_per_hour": 126000}',
                          "c3")]),
        reply("45 to 99 is 12,972,919 XP, about 103.0 hours at 126,000 xp/hr."),
    ])
    async with chat:
        answer = await agent.ask("how long from 45 to 99 mining?")

    assert answer.xp_calculations == ["45->99"]
    assert "103.0 hours" in answer.text


async def test_an_answer_that_used_the_tool_is_left_alone():
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "mining"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply(None, [call("calculate_xp", '{"from_level": 45, "to_level": 99}', "c3")]),
        # Only the figure the tool actually returned. "103 hours" would be
        # ungrounded here -- calculate_xp was called without a rate, so it never
        # produced an hours figure, and the grounding check is right to say so.
        reply("That is 12,972,919 XP."),
    ])
    async with chat:
        answer = await agent.ask("how long from 45 to 99 mining?")

    assert answer.text == "That is 12,972,919 XP."


async def test_a_quoted_xp_rate_is_not_mistaken_for_arithmetic():
    """Reading "126,000 xp/hr" off a guide is a quoted fact, not a derived
    total. Nudging on it would burn a round trip on every rates question."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "mining"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("Granite gives about 126,000 xp/hr with tick manipulation."),
    ], bodies={"Vorkath": "Granite gives 126,000 xp/hr with tick manipulation."})
    async with chat:
        answer = await agent.ask("what xp rate does granite give?")

    assert answer.text == "Granite gives about 126,000 xp/hr with tick manipulation."
    assert answer.xp_calculations == []


async def test_non_training_questions_are_untouched():
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("Vorkath is on Ungael."),
    ])
    async with chat:
        answer = await agent.ask("where is vorkath")

    assert answer.text == "Vorkath is on Ungael."


# -- forced read when the nudge is ignored ---------------------------------


async def test_ignoring_the_read_nudge_gets_the_page_fetched_for_you():
    """Asking twice is still asking. On "fastest way to train mining from 45",
    two runs in three answered the nudge with prose and no tool call -- one
    opening "I apologize ... there was an issue with accessing the specific
    page" about a call it never made -- then answered from memory."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply("Vorkath lives somewhere, I think."),        # no read
        reply("Sorry, I could not access the page."),      # ignores the nudge
        reply("Vorkath is on Ungael."),                    # answers off the injection
    ])
    async with chat:
        answer = await agent.ask("where is vorkath")

    assert answer.pages_read == ["Vorkath"], "the page should have been fetched for it"
    assert answer.citations == ["https://oldschool.runescape.wiki/w/Vorkath"]
    assert answer.text == "Vorkath is on Ungael."


async def test_the_injected_page_text_is_actually_handed_over():
    """Recording the citation without supplying the text would be a lie in the
    Sources footer -- the answer would still be ungrounded."""
    seen: list[str] = []

    def handler(request):
        import json as _json
        seen.extend(m.get("content") or "" for m in _json.loads(request.content)["messages"])
        return next(it)

    it = iter([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply("no read"),
        reply("still no read"),
        reply("Vorkath is on Ungael."),
    ])
    chat = ChatClient("http://stub/v1", "m", transport=httpx.MockTransport(handler))
    retriever = HybridRetriever(
        FakeWiki(["Vorkath"], bodies={"Vorkath": "UNIQUE-BODY-TEXT"}),
        FakeIndex(["Vorkath"]),
    )
    async with chat:
        await WikiAgent(retriever, chat).ask("where is vorkath")

    assert any("UNIQUE-BODY-TEXT" in m for m in seen), "page text was never supplied"


async def test_no_forced_read_when_the_nudge_worked():
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply("answering without reading"),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("Vorkath is on Ungael."),
    ])
    async with chat:
        answer = await agent.ask("where is vorkath")

    # Read once, by the model. Not fetched again on top.
    assert answer.pages_read == ["Vorkath"]


async def test_a_hiscores_answer_is_not_force_read():
    """Grounding is grounding. Fetching a wiki page for a stats question would
    cite a source the answer did not come from."""
    agent, chat = build_agent([
        reply(None, [call("get_player_stats", '{"username": "Lynx Titan"}')]),
        reply("Their Attack is 99."),
    ])
    async with chat:
        answer = await agent.ask("what is Lynx Titan's attack level")

    assert answer.pages_read == []


# -- forced XP arithmetic when the model refuses ---------------------------


async def test_refusing_to_calculate_gets_the_arithmetic_done_for_you():
    """Asked for hours 45->99 Mining, the model answered the nudge with "I
    apologize, but I currently don't have the tools needed to perform the
    calculations you're asking for" -- while holding calculate_xp."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "mining"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("Roughly 100 hours at 200,000 xp/hr."),      # guessed, unseen
        reply("I don't have the tools to calculate that."),  # refuses the nudge
        reply("12,972,919 XP, which is 103.0 hours at 126,000 xp/hr."),
    ])
    async with chat:
        answer = await agent.ask("how long from 45 to 99 mining at 126,000 xp/hr?")

    assert answer.xp_calculations == ["45->99"]
    assert "103.0 hours" in answer.text


async def test_the_computed_figures_are_actually_supplied():
    seen: list[str] = []

    def handler(request):
        import json as _json
        seen.extend(m.get("content") or "" for m in _json.loads(request.content)["messages"])
        return next(it)

    it = iter([
        reply(None, [call("search_wiki", '{"query": "mining"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("about 100 hours"),
        reply("cannot do it"),
        reply("done"),
    ])
    chat = ChatClient("http://stub/v1", "m", transport=httpx.MockTransport(handler))
    retriever = HybridRetriever(
        FakeWiki(["Vorkath"], bodies={"Vorkath": "x"}), FakeIndex(["Vorkath"])
    )
    async with chat:
        await WikiAgent(retriever, chat).ask("how long from 45 to 99 mining?")

    # The exact gap, computed by skills.xp_between, must reach the model.
    assert any("12,972,919" in m for m in seen), "the XP figure was never supplied"


async def test_no_level_range_means_nothing_to_compute():
    """"how long does mining take" has no levels; inventing a range would be
    worse than declining to help."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "mining"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("It takes many hours."),
        reply("Still many hours."),
    ])
    async with chat:
        answer = await agent.ask("how long does mining take?")

    assert answer.xp_calculations == []


async def test_implausible_level_ranges_are_ignored():
    """Guards the regex: a year, a price or a drop rate is not a level range."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "x"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("That took 3 hours."),
        reply("That took 3 hours."),
    ])
    async with chat:
        answer = await agent.ask("how long from 2007 to 2024 did that take?")

    assert answer.xp_calculations == []


# -- dead ends and empty retries -------------------------------------------


async def test_a_dead_end_page_falls_through_to_the_next_hit():
    """The model read 'Rooftop Agility Courses' and said the Seers course "is
    not mentioned on this page" -- correctly -- then stopped. The read
    enforcement cannot catch that: its test is that something was read, not
    that the something helped."""
    it = iter([
        reply(None, [call("search_wiki", '{"query": "seers agility"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Rooftop"}', "c2")]),
        reply("That is not mentioned on this page."),
        reply("You need 60 Agility."),
    ])
    chat = ChatClient(
        "http://stub/v1", "m",
        transport=httpx.MockTransport(lambda r: next(it, reply("(exhausted)"))),
    )
    retriever = HybridRetriever(
        FakeWiki(["Rooftop", "Seers"],
                 bodies={"Rooftop": "an overview with no requirement",
                         "Seers": "requires an Agility level of 60"}),
        FakeIndex(["Rooftop", "Seers"]),
    )
    async with chat:
        answer = await WikiAgent(retriever, chat).ask("what agility level for seers")

    assert answer.pages_read == ["Rooftop", "Seers"], "should fall through to the next hit"
    assert answer.text == "You need 60 Agility."


async def test_a_dead_end_with_nothing_left_to_read_is_left_alone():
    it = iter([
        reply(None, [call("search_wiki", '{"query": "seers"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Only"}', "c2")]),
        reply("That is not mentioned on this page."),
    ])
    chat = ChatClient(
        "http://stub/v1", "m",
        transport=httpx.MockTransport(lambda r: next(it, reply("(exhausted)"))),
    )
    retriever = HybridRetriever(
        FakeWiki(["Only"], bodies={"Only": "nothing"}), FakeIndex(["Only"])
    )
    async with chat:
        answer = await WikiAgent(retriever, chat).ask("what agility level for seers")

    assert answer.pages_read == ["Only"]
    assert answer.text == "That is not mentioned on this page."


def test_an_empty_retry_never_replaces_a_real_answer():
    """Each enforcement pass re-reads the final assistant turn, and a pass that
    ends on a tool call or exhausts its iterations leaves nothing to read. One
    live run of the mining question returned the empty string that way --
    strictly worse than the guess it replaced, and it reaches the user as a
    blank Discord embed."""
    from reldo.agent import _keep_best

    assert _keep_best("About 100 hours.", "") == "About 100 hours."
    assert _keep_best("About 100 hours.", "   \n ") == "About 100 hours."
    assert _keep_best("About 100 hours.", "103.0 hours.") == "103.0 hours."
    assert _keep_best("", "103.0 hours.") == "103.0 hours."


def test_best_title_picks_relevance_over_shortlist_order():
    """Taking the next hit blindly sent the Seers question to 'Ardougne Rooftop
    Course' -- another rooftop page that also does not mention Seers."""
    from reldo.agent import _best_title

    question = "what agility level do I need for the Seers' Village rooftop course"
    titles = ["Ardougne Rooftop Course", "Seers' Village Rooftop Course", "Falador Rooftop Course"]
    assert _best_title(question, titles) == "Seers' Village Rooftop Course"


def test_best_title_folds_plurals():
    from reldo.agent import _best_title

    assert _best_title("magic logs", ["Yew log", "Magic log"]) == "Magic log"


def test_best_title_keeps_order_on_a_tie():
    """Ties keep shortlist order, so this can only improve on taking the first."""
    from reldo.agent import _best_title

    assert _best_title("something unrelated", ["First", "Second"]) == "First"


# -- numeric grounding -----------------------------------------------------


def test_ungrounded_numbers_are_the_ones_never_shown():
    from reldo.agent import _ungrounded_numbers

    seen = ["Steel cannonball | 35 | 30 | 286"]
    assert _ungrounded_numbers("You need 35 Smithing.", "q", seen) == []
    assert _ungrounded_numbers("You need 42 Smithing.", "q", seen) == ["42"]


def test_numbers_from_the_question_count_as_grounded():
    from reldo.agent import _ungrounded_numbers

    assert _ungrounded_numbers("At level 20 you can build.", "what at level 20?", []) == []


def test_single_digits_are_ignored_as_noise():
    """A bare "3" appears in almost any text by chance; checking it would flag
    every answer and the warning would stop meaning anything."""
    from reldo.agent import _ungrounded_numbers

    assert _ungrounded_numbers("It takes 3 steps.", "q", ["nothing"]) == []


def test_comma_formatting_does_not_hide_a_match():
    from reldo.agent import _ungrounded_numbers

    assert _ungrounded_numbers("12,972,919 XP", "q", ["12972919 xp needed"]) == []


async def test_an_invented_number_is_handed_back_once():
    """"at least 30 Smithing" for cannonballs -- fluent, cited, and invented."""
    agent, chat = build_agent([
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}')]),
        reply("You need 30 Smithing."),
        reply(None, [call("read_wiki_table", '{"title": "Vorkath"}', "c2")]),
        reply("You need 35 Smithing."),
    ], bodies={"Vorkath": "Steel cannonball 35"})
    async with chat:
        answer = await agent.ask("what level for cannonballs")

    assert "35" in answer.text


async def test_a_fully_grounded_answer_is_not_handed_back():
    calls = []

    def handler(request):
        calls.append(1)
        return [
            reply(None, [call("read_wiki_page", '{"title": "Vorkath"}')]),
            reply("Vorkath needs 75 Slayer."),
        ][min(len(calls) - 1, 1)]

    chat = ChatClient("http://stub/v1", "m", transport=httpx.MockTransport(handler))
    retriever = HybridRetriever(
        FakeWiki(["Vorkath"], bodies={"Vorkath": "requires 75 Slayer"}), FakeIndex(["Vorkath"])
    )
    async with chat:
        answer = await WikiAgent(retriever, chat).ask("what slayer level for vorkath")

    assert answer.text == "Vorkath needs 75 Slayer."
    assert len(calls) == 2, "no extra round trip on a grounded answer"


# -- the clients the agent owns --------------------------------------------
# Both of these were per-tool-call, which threw away ge.py's caches (the
# /mapping payload is every tradeable item in the game) and identified every
# deployment as this repo rather than as whoever was running it.


def test_the_ge_client_is_built_once_and_reused():
    agent = WikiAgent(retriever=None, client=None)
    assert agent._ge_client() is agent._ge_client()


def test_the_hiscores_client_is_built_once_and_reused():
    agent = WikiAgent(retriever=None, client=None)
    assert agent._hiscores_client() is agent._hiscores_client()


def test_the_configured_user_agent_reaches_both_clients():
    agent = WikiAgent(retriever=None, client=None, user_agent="reldo/0.1 (you.example)")
    for client in (agent._ge_client(), agent._hiscores_client()):
        assert client._http.headers["User-Agent"] == "reldo/0.1 (you.example)"


def test_no_configured_agent_leaves_the_client_default_alone():
    """Passing an empty string through would be worse than the default it
    replaced: unidentified rather than misidentified."""
    agent = WikiAgent(retriever=None, client=None, user_agent="   ")
    assert "reldo" in agent._ge_client()._http.headers["User-Agent"]


async def test_closing_the_agent_closes_what_it_opened():
    agent = WikiAgent(retriever=None, client=None)
    ge, hiscores = agent._ge_client(), agent._hiscores_client()
    await agent.aclose()
    assert ge._http.is_closed and hiscores._http.is_closed


async def test_closing_twice_is_harmless():
    agent = WikiAgent(retriever=None, client=None)
    agent._ge_client()
    await agent.aclose()
    await agent.aclose()


# -- the asker's own stats -------------------------------------------------


class _Hiscores:
    def __init__(self, summary="TimmyZero -- total level 674, Mining 54", levels=None):
        self._summary = summary
        # A real Player answers level() for every skill, and the preamble reads
        # them so a requirement check can be arithmetic rather than a judgement.
        self._levels = {"Mining": 54, **(levels or {})}
        self.asked: list[str] = []

    async def lookup(self, username):
        self.asked.append(username)
        return SimpleNamespace(
            name="TimmyZero",
            summary=lambda: self._summary,
            level=lambda skill: self._levels.get(skill, 1),
            xp=lambda skill: self._levels.get(skill, 0) * 1000,
        )


async def test_the_askers_stats_go_in_front_of_the_question():
    """'How do I train mining' is useless generically to somebody already at 54,
    and the fix cannot depend on a tool call the model skips when confident."""
    agent, chat = build_agent([reply("Read the guide.")])
    agent._hiscores = _Hiscores()
    async with chat:
        answer = await agent.ask("how do I train mining", player="TimmyZero")

    messages = chat.sent[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]  # never two user turns
    opening = messages[1]["content"]
    assert "Mining 54" in opening
    assert "start from the level I actually have" in opening
    assert opening.endswith("how do I train mining")
    assert answer.player_context == "TimmyZero"


async def test_those_stats_count_as_shown_for_the_grounding_check():
    """Quoting the asker's own Mining level back would otherwise read as an
    invented number and trigger a handback."""
    agent, chat = build_agent([reply("You are 54 Mining.")])
    agent._hiscores = _Hiscores()
    async with chat:
        answer = await agent.ask("what is my mining level", player="TimmyZero")
    assert any("Mining 54" in s for s in answer.seen)


async def test_prefetched_stats_do_not_switch_off_the_forced_read():
    """The regression this exists for. players_checked means the model went and
    looked somebody up, which grounds a stats answer. Ambient context about the
    asker grounds nothing about Vorkath -- recording it there would have quietly
    disabled the forced read for every linked user."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply("Vorkath is a dragon."),          # answered without reading
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("Vorkath is on Ungael."),
    ])
    agent._hiscores = _Hiscores()
    async with chat:
        answer = await agent.ask("how do I kill vorkath", player="TimmyZero")

    assert answer.players_checked == []        # the prefetch is not a tool call
    assert answer.pages_read == ["Vorkath"]    # so the forced read still fired


async def test_a_failed_lookup_costs_the_context_not_the_answer():
    class Broken:
        async def lookup(self, username):
            raise HiscoresError("no such player")

    agent, chat = build_agent([reply("Here is the guide.")])
    agent._hiscores = Broken()
    async with chat:
        answer = await agent.ask("how do I train mining", player="ghost")
    assert answer.text == "Here is the guide."
    assert answer.player_context == ""


async def test_no_player_means_no_lookup_at_all():
    agent, chat = build_agent([reply("An answer.")])
    agent._hiscores = _Hiscores()
    async with chat:
        await agent.ask("what is a whip")
    assert agent._hiscores.asked == []


async def test_a_persona_rides_on_the_system_prompt_not_a_rewrite():
    """Appended, so every enforcement pass still runs on what the model produced.
    A post-hoc restyling would put the voice outside the grounding perimeter."""
    agent, chat = build_agent([reply("An answer.")])
    async with chat:
        await agent.ask("what is a whip", persona="\n\nVOICE. Be warm.")
    system = chat.sent[0]["messages"][0]
    assert system["role"] == "system"
    assert system["content"].endswith("VOICE. Be warm.")
    assert "using the OSRS Wiki" in system["content"]


# -- what happens to an invented number the model will not retract ----------
# The warning alone used to be the whole enforcement: it logged, handed back
# once, and kept whatever came out so long as it was non-empty. A retry that
# repeated its figures verbatim passed that test and reached the user.


def test_drop_ungrounded_claims_cuts_the_sentence_not_the_number():
    """Deleting just the figure leaves a sentence that still asserts something
    and no longer says what."""
    text = "Mine granite at the quarry. You can smash 15 rocks per inventory."
    kept = _drop_ungrounded_claims(text, "how do I mine", ["granite quarry"])
    assert kept == "Mine granite at the quarry."


def test_a_grounded_number_survives():
    kept = _drop_ungrounded_claims(
        "Granite needs 45 Mining.", "q", ["Granite requires 45 Mining"]
    )
    assert kept == "Granite needs 45 Mining."


def test_numbers_from_the_question_are_grounded():
    kept = _drop_ungrounded_claims("You need 70 attack.", "is it good at 70 attack", [])
    assert kept == "You need 70 attack."


def test_comma_formatting_does_not_cause_a_false_excision():
    kept = _drop_ungrounded_claims("It is 13,034,431 XP.", "q", ["13034431 xp to 99"])
    assert kept == "It is 13,034,431 XP."


def test_a_decimal_is_not_split_down_the_middle():
    """The sentence splitter requires whitespace after the terminator; without
    that, '3.5' becomes two sentences and the number vanishes."""
    kept = _drop_ungrounded_claims("It takes 3.5 hours.", "q", ["3.5 hours"])
    assert kept == "It takes 3.5 hours."


def test_a_fully_invented_line_is_dropped_whole():
    text = "Mine granite.\nIronwood mast at level 20.\nThat is the plan."
    kept = _drop_ungrounded_claims(text, "q", ["granite", "plan"])
    assert kept == "Mine granite.\nThat is the plan."


def test_everything_invented_prunes_to_nothing():
    """Signals the caller to keep the flagged answer rather than ship a blank."""
    assert _drop_ungrounded_claims("It is 15 rocks.", "q", ["nothing"]) == ""


def test_fewer_invented_prefers_the_cleaner_draft():
    seen = ["granite 45"]
    assert _fewer_invented("15 rocks and 75 xp.", "45 mining.", "q", seen) == "45 mining."


def test_fewer_invented_keeps_the_original_when_the_retry_is_worse():
    seen = ["granite 45"]
    assert _fewer_invented("45 mining.", "15 rocks and 75 xp.", "q", seen) == "45 mining."


def test_fewer_invented_never_takes_an_empty_retry():
    assert _fewer_invented("45 mining.", "   ", "q", ["45"]) == "45 mining."


async def test_a_repeated_invention_is_excised_rather_than_shipped():
    """The live failure: 'you can smash 15 rocks per inventory, granting 75 XP'
    survived the handback because the retry repeated it verbatim."""
    invented = "Mine granite at the quarry. You can smash 15 rocks per inventory."
    agent, chat = build_agent(
        [
            reply(None, [call("read_wiki_page", '{"title": "Vorkath"}')]),
            reply(invented),
            reply(invented),  # handed back, and says exactly the same thing
        ],
        bodies={"Vorkath": "Granite is mined at the quarry."},
    )
    async with chat:
        answer = await agent.ask("how do I mine granite")

    assert "15" not in answer.text
    assert "Mine granite at the quarry." in answer.text
    assert "did not actually give" in answer.text


async def test_an_answer_the_retry_fixes_keeps_no_excision_note():
    agent, chat = build_agent(
        [
            reply(None, [call("read_wiki_page", '{"title": "Vorkath"}')]),
            reply("You can smash 15 rocks per inventory."),
            reply("Granite is mined at the quarry."),
        ],
        bodies={"Vorkath": "Granite is mined at the quarry."},
    )
    async with chat:
        answer = await agent.ask("how do I mine granite")

    assert answer.text == "Granite is mined at the quarry."
    assert "did not actually give" not in answer.text


async def test_a_wholly_invented_answer_is_kept_rather_than_emptied():
    """A blank Discord embed is worse than a flagged answer; the log says why."""
    agent, chat = build_agent(
        [
            reply(None, [call("read_wiki_page", '{"title": "Vorkath"}')]),
            reply("It is 15 rocks and 75 xp."),
            reply("It is 15 rocks and 75 xp."),
        ],
        bodies={"Vorkath": "Nothing numeric here."},
    )
    async with chat:
        answer = await agent.ask("how do I mine granite")
    assert answer.text.strip()


async def test_a_grounded_answer_is_left_completely_alone():
    agent, chat = build_agent(
        [
            reply(None, [call("read_wiki_page", '{"title": "Vorkath"}')]),
            reply("Granite needs 45 Mining."),
        ],
        bodies={"Vorkath": "Granite requires 45 Mining."},
    )
    async with chat:
        answer = await agent.ask("what level for granite")
    assert answer.text == "Granite needs 45 Mining."


@pytest.mark.parametrize(
    "refusal",
    [
        "I am unable to retrieve the specific section.",
        "I am unable to access that page.",
        "I could not find it on the page.",
        "I do not have the exact XP rates.",
        "That page does not mention it.",
    ],
)
def test_every_refusal_wording_counts_as_a_dead_end(refusal):
    """'unable to find' was the only phrasing matched, and the model has more
    than one. The measured miss was 'unable to retrieve the specific section',
    which left a vague answer with no numbers in it and no fallthrough."""
    assert _DEAD_END.search(refusal), refusal


def test_an_ordinary_answer_is_not_a_dead_end():
    assert not _DEAD_END.search("Granite is mined at the quarry south of Kourend.")


# -- check_quest_requirements ----------------------------------------------
# The comparison happens in code. Requirements are a marked-up list and the
# asker's levels are already in hand, so asking a 24B model to total them up is
# the same mistake ge.py and skills.py exist to avoid.


class _Bucket:
    def __init__(self, result):
        self._result = result
        self.asked: list[str] = []

    async def quest_requirements(self, quest):
        self.asked.append(quest)
        return self._result


def _quest_tool(agent, answer):
    tools = {t.name: t for t in agent._build_tools(answer)}
    return tools["check_quest_requirements"]


async def test_a_quest_check_says_what_the_asker_is_short_of():
    agent, _ = build_agent([])
    agent._bucket = _Bucket(("Dragon Slayer II", {"Magic": 75, "Mining": 68}))
    answer = Answer(text="", player_levels={"Magic": 80, "Mining": 54})

    out = await _quest_tool(agent, answer).handler(quest="Dragon Slayer II")

    assert "75 Magic -- you have 80" in out
    assert "68 Mining -- you have 54, short by 14" in out
    assert "Short of: Mining 54/68" in out


async def test_a_quest_check_says_so_when_everything_is_met():
    agent, _ = build_agent([])
    agent._bucket = _Bucket(("Cook's Assistant", {"Cooking": 10}))
    answer = Answer(text="", player_levels={"Cooking": 50})

    assert "Everything is met." in await _quest_tool(agent, answer).handler(
        quest="Cook's Assistant"
    )


async def test_quest_points_are_listed_without_a_comparison():
    """Not a skill, so there is no level to compare it against -- and inventing
    one would report every account as short of it."""
    agent, _ = build_agent([])
    agent._bucket = _Bucket(("Dragon Slayer II", {"Quest points": 200}))
    answer = Answer(text="", player_levels={"Magic": 80})

    out = await _quest_tool(agent, answer).handler(quest="Dragon Slayer II")
    assert "200 Quest points" in out
    assert "you have" not in out


async def test_an_unlinked_asker_still_gets_the_requirements():
    """No levels known means no comparison, not no answer."""
    agent, _ = build_agent([])
    agent._bucket = _Bucket(("Dragon Slayer II", {"Magic": 75}))

    out = await _quest_tool(agent, Answer(text="")).handler(quest="Dragon Slayer II")
    assert "75 Magic" in out
    assert "Short of" not in out and "you have" not in out


async def test_an_unknown_quest_says_how_to_find_the_right_name():
    agent, _ = build_agent([])
    agent._bucket = _Bucket(None)
    out = await _quest_tool(agent, Answer(text="")).handler(quest="Dragon Slayer 2")
    assert "No quest called" in out and "search_wiki" in out


async def test_a_bucket_failure_is_reported_rather_than_raised():
    """A tool that raises wedges the turn; the model can read an error and
    recover from it."""
    agent, _ = build_agent([])

    class Broken:
        async def quest_requirements(self, quest):
            raise BucketError("Bucket quest does not exist.")

    agent._bucket = Broken()
    out = await _quest_tool(agent, Answer(text="")).handler(quest="Dragon Slayer II")
    assert "Could not read quest requirements" in out


async def test_the_askers_levels_are_captured_for_the_comparison():
    """player_levels is what makes the check arithmetic rather than a judgement,
    and it is filled from the lookup the preamble already does."""
    agent, chat = build_agent([reply("Read the guide.")])
    agent._hiscores = _Hiscores(levels={"Mining": 54, "Magic": 26})
    async with chat:
        answer = await agent.ask("how do I train mining", player="TimmyZero")

    assert answer.player_levels["Mining"] == 54
    assert answer.player_levels["Magic"] == 26


# -- check_quest / check_inventory ------------------------------------------
# Both read what only the player's own client can know. Jagex publishes quest
# completion nowhere, so "not reported" has to be distinguishable from "no".


def _tool(agent, answer, name):
    return {t.name: t for t in agent._build_tools(answer)}[name]


async def test_a_finished_quest_is_reported_from_the_players_own_client():
    agent, _ = build_agent([])
    profile = parse_profile(
        {"player": "T", "quests": {"finished": ["Dragon Slayer I"],
                                   "started": ["Dragon Slayer II"]}},
        at=0.0,
    )
    answer = Answer(text="", profile=profile)
    assert "finished" in await _tool(agent, answer, "check_quest").handler(
        quest="Dragon Slayer I"
    )
    assert "started" in await _tool(agent, answer, "check_quest").handler(
        quest="Dragon Slayer II"
    )


async def test_no_plugin_data_is_said_out_loud_rather_than_guessed():
    """"Not reported" and "not done" are different, and collapsing them tells
    somebody they have not done a quest they finished years ago."""
    agent, _ = build_agent([])
    out = await _tool(agent, Answer(text=""), "check_quest").handler(quest="Dragon Slayer I")
    assert "not installed" in out or "not reported" in out
    assert "cannot see" in out


async def test_an_item_is_found_across_worn_gear_and_bank():
    agent, _ = build_agent([])
    profile = parse_profile(
        {"player": "T", "equipment": ["Rune platebody"], "bank": ["Shark x300"]}, at=0.0
    )
    answer = Answer(text="", profile=profile)
    assert "worn" in await _tool(agent, answer, "check_inventory").handler(item="platebody")
    assert "bank" in await _tool(agent, answer, "check_inventory").handler(item="shark")


async def test_an_unseen_bank_is_not_reported_as_not_owning_it():
    """The client cannot read the bank until it is opened, so a miss there is
    not evidence of anything."""
    agent, _ = build_agent([])
    profile = parse_profile({"player": "T", "equipment": []}, at=0.0)
    out = await _tool(agent, Answer(text="", profile=profile), "check_inventory").handler(
        item="shark"
    )
    assert "not been seen" in out and "not evidence" in out


async def test_a_seen_bank_without_the_item_is_a_straight_no():
    agent, _ = build_agent([])
    profile = parse_profile({"player": "T", "equipment": [], "bank": ["Coal x10"]}, at=0.0)
    out = await _tool(agent, Answer(text="", profile=profile), "check_inventory").handler(
        item="shark"
    )
    assert "No 'shark'" in out


# -- forcing the requirements lookup ---------------------------------------
# Sixth enforcement pass, same shape as the other five. Measured on
# mistral-small3.2:24b asked what Dragon Slayer II needs: three searches, a page
# read, no tool call, and "50 Hunter, 50 Slayer, 70 Magic, 70 Hitpoints" -- two
# invented, two wrong, six dropped.


QUESTS = ["Dragon Slayer I", "Dragon Slayer II", "Cook's Assistant",
          "Desert Treasure I", "Desert Treasure II - The Fallen Empire"]


def test_the_longest_quest_name_wins():
    """28 quest names are a prefix of another. Matching the shorter one answers
    confidently about the wrong quest."""
    assert _named_quest("what do I need for Dragon Slayer II", QUESTS) == "Dragon Slayer II"
    assert _named_quest("requirements for Dragon Slayer I", QUESTS) == "Dragon Slayer I"
    assert _named_quest(
        "am I ready for Desert Treasure II - The Fallen Empire", QUESTS
    ) == "Desert Treasure II - The Fallen Empire"


def test_a_quest_named_with_a_digit_is_still_recognised():
    """People type "Dragon Slayer 2"; the page is "Dragon Slayer II"."""
    assert _named_quest("can I start dragon slayer 2", QUESTS) == "Dragon Slayer II"


def test_a_question_naming_no_quest_matches_nothing():
    assert _named_quest("what do I need for 99 fishing", QUESTS) is None


@pytest.mark.parametrize("question", [
    "what do I need to start Dragon Slayer II",
    "requirements for Dragon Slayer II",
    "am I ready for Dragon Slayer II",
    "can I do Dragon Slayer II",
    "how do I start Dragon Slayer II",
])
def test_requirement_shaped_questions_are_recognised(question):
    assert _ASKS_REQUIREMENTS.search(question)


@pytest.mark.parametrize("question", [
    "how do I kill the dragon in Dragon Slayer I",
    "where is Vorkath",
    "is the whip worth it at 80 attack",
])
def test_other_questions_do_not_trigger_the_pass(question):
    assert not _ASKS_REQUIREMENTS.search(question)


class _QuestBucket:
    def __init__(self, requirements=None):
        self._requirements = requirements or {"Magic": 75, "Hitpoints": 50}
        self.name_calls = 0

    async def quest_names(self):
        self.name_calls += 1
        return QUESTS

    async def quest_requirements(self, quest):
        return (quest, self._requirements)


async def test_a_requirements_answer_without_the_tool_is_corrected():
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "dragon slayer 2"}')]),
        reply("You need 50 Hunter and 70 Hitpoints."),   # invented
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("You need 50 Hunter and 70 Hitpoints."),   # still invented
        reply("You need 75 Magic and 50 Hitpoints."),    # after the handback
    ])
    agent._bucket = _QuestBucket()
    async with chat:
        answer = await agent.ask("what do I need to start Dragon Slayer II")

    assert answer.quests_checked == ["Dragon Slayer II"]
    assert "75 Magic" in answer.text
    # The exact figures were put in front of it, so they count as grounded.
    assert any("75 Magic" in seen for seen in answer.seen)


async def test_the_pass_does_not_fire_when_the_tool_was_used():
    """It costs a Bucket query and a round trip; firing on a grounded answer is
    the same waste the read nudge is careful to avoid."""
    agent, chat = build_agent([
        reply(None, [call("check_quest_requirements", '{"quest": "Dragon Slayer II"}')]),
        reply("You need 75 Magic."),
    ])
    bucket = _QuestBucket()
    agent._bucket = bucket
    async with chat:
        answer = await agent.ask("what do I need for Dragon Slayer II")

    assert answer.quests_checked == ["Dragon Slayer II"]
    assert bucket.name_calls == 0  # never went looking


async def test_a_question_about_no_quest_costs_nothing_beyond_the_lookup():
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "fishing"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("Train at barbarian village."),
    ])
    agent._bucket = _QuestBucket()
    async with chat:
        answer = await agent.ask("what do I need for 99 fishing")
    assert answer.quests_checked == []


async def test_a_bucket_failure_leaves_the_answer_as_it_was():
    """A recovery step is never allowed to leave things worse than it found
    them -- the same call _keep_best makes."""
    class Broken:
        async def quest_names(self):
            raise BucketError("Bucket quest does not exist.")

    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "ds2"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("Some answer."),
    ])
    agent._bucket = Broken()
    async with chat:
        answer = await agent.ask("what do I need for Dragon Slayer II")
    assert answer.text == "Some answer."


# -- a tool call typed out as prose ----------------------------------------
# Measured on "how long does it take to get from 45 to 99 mining": the final
# assistant turn's whole content was `read_wiki_page`. Non-empty, so every guard
# waved it through -- _keep_best tests only that a retry is not blank -- and it
# would reach a user as a Discord embed reading `read_wiki_page`.

TOOLS = {"read_wiki_page", "search_wiki", "calculate_xp"}


@pytest.mark.parametrize("text", [
    "read_wiki_page",
    "  read_wiki_page  ",
    "`read_wiki_page`",
    'read_wiki_page{"title": "Vorkath"}',
    "search_wiki('vorkath')",
    "calculate_xp[45, 99]",
])
def test_a_bare_tool_call_is_not_an_answer(text):
    assert _is_tool_leak(text, TOOLS)


@pytest.mark.parametrize("text", [
    "Vorkath is on Ungael.",
    "You need 75 Magic.",
    "read_wiki_page on Vorkath told me it is on Ungael.",
    "I used read_wiki_page to check.",
    "read_wiki_pages are useful",
])
def test_prose_about_a_tool_is_still_an_answer(text):
    """Narrow on purpose. A model explaining its own reasoning must not be
    silenced -- that would trade a visible bug for an invisible one."""
    assert not _is_tool_leak(text, TOOLS)


def test_an_unknown_identifier_is_left_alone():
    """Only the names of tools that actually exist. Otherwise any answer opening
    with a lone word would be discarded."""
    assert not _is_tool_leak("Vorkath", TOOLS)
    assert not _is_tool_leak("granite", TOOLS)


def test_the_last_real_answer_is_recovered_not_merely_dropped():
    """The turn before the leak is a perfectly good answer, and it is what the
    user should get."""
    messages = [
        {"role": "assistant", "content": "It takes about 103 hours at 126,000 XP/hr."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "assistant", "content": "read_wiki_page"},
    ]
    assert _last_assistant_text(messages, TOOLS) == (
        "It takes about 103 hours at 126,000 XP/hr."
    )


def test_nothing_but_leaks_reads_as_no_answer():
    """Empty is what the Discord layer already renders as "I couldn't find
    anything", which beats an embed whose body is a function name."""
    messages = [{"role": "assistant", "content": "read_wiki_page"}]
    assert _last_assistant_text(messages, TOOLS) == ""


async def test_a_leaked_call_never_replaces_a_real_answer_through_ask():
    """End to end: the duration pass hands back, the retry leaks a tool name,
    and the good answer survives."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "mining"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply(None, [call("calculate_xp", '{"from_level": 45, "to_level": 99}', "c3")]),
        reply("It takes about 103 hours at 126,000 XP/hr."),
        reply("read_wiki_page"),
    ])
    async with chat:
        answer = await agent.ask("how long from 45 to 99 mining")
    assert answer.text == "It takes about 103 hours at 126,000 XP/hr."


@pytest.mark.parametrize("text", [
    "The granite rocks page does not give a concrete XP rate for mining granite.",
    "The page doesn't give the requirement.",
])
def test_a_refusal_worded_with_give_is_a_dead_end(text):
    """Third wording added here after the fact, after "unable to find" and
    "unable to retrieve". Measured on "how long from 45 to 99 mining at granite
    rates": this sailed past, so the fall-through to the next page never fired
    and the answer went out with no duration in it at all."""
    assert _DEAD_END.search(text)


@pytest.mark.parametrize("text", [
    "Vorkath is on Ungael and gives good loot.",
    "Mining granite gives 60 XP per rock.",
    "The guide gives 126,000 XP per hour for 3-tick granite.",
])
def test_an_answer_that_gives_a_figure_is_not_a_refusal(text):
    """The word appears in ordinary answers far more often than in refusals.
    Widening this too far would hand back good answers to be redone."""
    assert not _DEAD_END.search(text)


# -- which page the forced read actually fetches ---------------------------


def test_the_forced_read_picks_by_relevance_not_by_rank():
    """Measured on "how much prayer experience does a dragon bone give when
    buried": the shortlist leads with "Prayer", the general skill page, and
    "Dragon bones" is second. Fetching the top hit blindly answered about burnt
    bones at 4.5 XP; the case scored 0/3."""
    answer = Answer(text="", top_hit="Prayer",
                    shortlist=["Prayer", "Dragon bones", "Bonemeal"])
    assert _target_page(
        "how much prayer experience does a dragon bone give when buried", answer
    ) == "Dragon bones"


def test_the_top_hit_is_used_when_there_is_no_shortlist():
    """A search that returned one title, or none at all, still has to name
    something."""
    answer = Answer(text="", top_hit="Vorkath", shortlist=[])
    assert _target_page("where is vorkath", answer) == "Vorkath"


async def test_the_forced_read_fetches_the_page_the_question_is_about():
    """End to end: search puts the overview first, the model answers without
    reading, and the page fetched for it is the specific one."""
    bodies = {"Prayer": "Prayer is a skill.", "Dragon bones": "Dragon bones give 72."}
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "dragon bones prayer"}')]),
        reply("Some ungrounded answer."),
        reply("Some ungrounded answer."),
        reply("Dragon bones give 72 Prayer experience."),
    ], bodies=bodies)
    agent._retriever = HybridRetriever(
        FakeWiki(["Prayer", "Dragon bones"], bodies=bodies),
        FakeIndex(["Prayer", "Dragon bones"]),
    )
    async with chat:
        answer = await agent.ask("how much prayer experience does a dragon bone give")

    assert "Dragon bones" in answer.pages_read


# -- forcing the recipe lookup ---------------------------------------------
# check_recipe was added and then measured: over three questions whose answers
# are in it, the model called search_wiki, read_wiki_page and read_wiki_table
# and never once called it. A tool a 24B model does not select is not a feature.


_DEFAULT = object()


class _RecipeBucket:
    def __init__(self, recipe=_DEFAULT):
        # A sentinel, not None: None is a meaningful value here -- "this is not
        # made from anything" -- and defaulting on it makes that case untestable.
        self._recipe = recipe if recipe is not _DEFAULT else {
            "page_name": "Steel cannonball",
            "skills": [{"name": "Smithing", "level": "35", "experience": "25.6"}],
        }
        self.asked = 0
        self.skill_asked_for = None
        # What the *material* route would find, once nothing shortlisted turned
        # out to be the product. None means it finds nothing either.
        self.from_material = None
        self.materials_tried = []

    async def first_recipe(self, titles, *, limit=6, skill=None):
        self.asked += 1
        self.skill_asked_for = skill
        return self._recipe

    async def recipe_from_material(self, material, skill):
        self.materials_tried.append((material, skill))
        return self.from_material

    async def recipe(self, item):
        return self._recipe


@pytest.mark.parametrize("question", [
    "what level smithing do I need to make a rune platebody",
    "how do I make cannonballs",
    "what cooking level do I need to cook a shark",
])
def test_make_shaped_questions_are_recognised(question):
    assert _ASKS_HOW_TO_MAKE.search(question)


@pytest.mark.parametrize("question", [
    "where is vorkath",
    "how much is an abyssal whip worth",
    "what agility level do I need for the Seers' Village rooftop course",
])
def test_other_questions_do_not_trigger_the_recipe_pass(question):
    assert not _ASKS_HOW_TO_MAKE.search(question)


async def test_a_make_question_without_the_tool_gets_the_recipe_injected():
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "cannonball"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("You need 16 Smithing."),          # read off the wrong row
        reply("You need 35 Smithing."),          # after the handback
    ])
    agent._bucket = _RecipeBucket()
    async with chat:
        answer = await agent.ask("how do I make cannonballs")

    assert answer.recipes_checked == ["Steel cannonball"]
    assert "35" in answer.text
    assert any("Smithing 35" in seen for seen in answer.seen)


async def test_the_recipe_pass_does_not_fire_when_the_tool_was_used():
    agent, chat = build_agent([
        reply(None, [call("check_recipe", '{"item": "Steel cannonball"}')]),
        reply("You need 35 Smithing."),
    ])
    bucket = _RecipeBucket()
    agent._bucket = bucket
    async with chat:
        await agent.ask("how do I make cannonballs")
    assert bucket.asked == 0  # never walked the shortlist


async def test_no_recipe_leaves_the_answer_alone():
    """Most things are not made. A pass that cannot improve an answer must not
    replace it."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "dragon pickaxe"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("It is a drop from the Kalphite Queen."),
    ])
    agent._bucket = _RecipeBucket(recipe=None)
    async with chat:
        answer = await agent.ask("how do I make a dragon pickaxe")
    assert answer.text == "It is a drop from the Kalphite Queen."


def test_a_rendered_recipe_names_the_tools_it_needs():
    """The handback tells the model to use exactly these facts, so anything left
    out of the block gets left out of the answer. Dropping the ammo mould took
    the cannonball case from 1/3 to 0/3 on "missing 'mould'"."""
    rendered = _render_recipe({
        "page_name": "Steel cannonball",
        "skills": [{"name": "Smithing", "level": "35"}],
        "materials": [{"quantity": "1", "name": "Steel bar"}],
        "tools": "Ammo mould",
        "facilities": "Furnace",
    })
    assert "Smithing 35" in rendered
    assert "Steel bar" in rendered
    assert "Ammo mould" in rendered
    assert "Furnace" in rendered


# -- counting the bars ------------------------------------------------------
# Asked "how much gold do i need to smelt to go from 48 to 50 smithing", the
# model searched, read Smithing, Iron ore and Smithing/Experience table, and
# answered "the wiki does not give the XP needed to go from 48 to 50 smithing in
# a form I can read". It never will: the XP table is a formula, not a page.


_GOLD_BAR = {
    "page_name": "Gold bar",
    "skills": [{"name": "Smithing", "level": "40", "experience": "22.5"}],
    "materials": [{"quantity": "1", "name": "Gold ore"}],
    "facilities": "Furnace",
}


@pytest.mark.parametrize("question,expected", [
    ("how much gold do i need to smelt to go from 48 to 50 smithing",
     ("Smithing", 48, 50)),
    ("how many yew logs from 60 to 99 fletching", ("Fletching", 60, 99)),
    ("how many sharks to get from level 80 to 90", ("", 80, 90)),
    # Word boundaries, not substrings: "crafting" is inside "runecrafting", and
    # answering a Runecraft question with Crafting's numbers is invisible.
    ("how many runes from 1 to 50 runecrafting", ("Runecraft", 1, 50)),
])
def test_quantity_questions_are_recognised(question, expected):
    assert _quantity_request(question) == expected


@pytest.mark.parametrize("question", [
    "how long from 45 to 99 mining",              # a duration; the other pass
    "how much is an abyssal whip worth",          # no level range
    "how much damage does it do from 20 to 30",   # a range, but not of levels
    "how many gold bars can i hold",              # no range at all
])
def test_other_questions_do_not_trigger_the_count(question):
    assert _quantity_request(question) is None


@pytest.mark.parametrize("question,expected", [
    ("what can i build at sailing level 20", ("Sailing", 20)),
    ("everything i can make at level 30 crafting", ("Crafting", 30)),
    # The one that was wrong. A substring test picks Crafting out of
    # "runecrafting" because it comes first in SKILLS, and _force_unlocks then
    # reads Crafting's requirement tables and hands them over with "answer using
    # exactly these entries" -- the invented list this pass exists to prevent,
    # arriving through the pass itself.
    ("what can i make with runecrafting at level 20", ("Runecraft", 20)),
    ("what can i craft at runecraft level 20", ("Runecraft", 20)),
])
def test_list_questions_name_the_skill_they_actually_asked_about(question, expected):
    assert _list_request(question) == expected


@pytest.mark.parametrize("question", [
    "what can i build at level 20",        # no skill named
    "what can i build with sailing",       # no level cap
    "how do i train sailing to level 20",  # not a list question
])
def test_other_questions_do_not_trigger_the_list(question):
    assert _list_request(question) is None


def test_a_rendered_count_bills_every_material_per_action():
    """A steel bar takes two coal, not one of everything. Multiplying the
    recipe's own quantities is the difference between 1,047 coal and 2,094."""
    rendered = _render_training_cost({
        "page_name": "Steel bar",
        "skills": [{"name": "Smithing", "experience": "17.5"}],
        "materials": [{"quantity": "1", "name": "Iron ore"},
                      {"quantity": "2", "name": "Coal"}],
    }, "Smithing", 48, 50)
    assert "18,319 XP" in rendered
    assert "1,047 of them" in rendered
    assert "1,047 x Iron ore" in rendered
    assert "2,094 x Coal" in rendered


def test_a_recipe_with_no_xp_figure_yields_no_count():
    """Better a bare XP gap than a division by a number that is not there."""
    assert _render_training_cost(
        {"page_name": "X", "skills": [{"name": "Smithing", "level": "40"}]},
        "Smithing", 48, 50,
    ) is None


def test_a_non_numeric_quantity_is_not_multiplied():
    """Bucket quantities are strings and not all of them are numbers. Guessing 1
    for "1-3" multiplies a wrong number by a thousand and prints it with a
    comma in it."""
    rendered = _render_training_cost({
        "page_name": "Potion",
        "skills": [{"name": "Herblore", "experience": "100"}],
        "materials": [{"quantity": "1-3", "name": "Grimy herb"}],
    }, "Herblore", 48, 50)
    assert "Grimy herb (quantity varies)" in rendered


async def test_the_wiki_does_not_give_the_xp_gets_the_count_done_for_you():
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "gold smithing"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("I apologize, but the wiki does not give the XP needed to go from "
              "48 to 50 smithing in a form I can read."),
        reply("815 gold ore: 48 to 50 is 18,319 XP and a gold bar gives 22.5."),
    ])
    agent._bucket = _RecipeBucket(recipe=_GOLD_BAR)
    async with chat:
        answer = await agent.ask(
            "how much gold do i need to smelt to go from 48 to 50 smithing"
        )

    assert answer.xp_calculations == ["48->50"]
    assert "815" in answer.text
    assert any("815 x Gold ore" in seen for seen in answer.seen)


async def test_the_count_asks_for_the_skill_the_question_named():
    """The shortlist for "gold ... smithing" puts jewellery above the bar, and a
    gold necklace is a real recipe for the wrong skill."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "gold"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("The wiki does not say."),
        reply("815 gold ore."),
    ])
    bucket = _RecipeBucket(recipe=_GOLD_BAR)
    agent._bucket = bucket
    async with chat:
        await agent.ask("how much gold do i need to smelt from 48 to 50 smithing")
    assert bucket.skill_asked_for == "Smithing"


async def test_the_computed_count_survives_the_grounding_check():
    """815 is in no page the model read -- it cannot be, it is arithmetic. The
    excision pass would have cut the answer's only number without this."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "gold"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("The wiki does not give it."),
        reply("You need 815 gold ore."),
    ])
    agent._bucket = _RecipeBucket(recipe=_GOLD_BAR)
    async with chat:
        answer = await agent.ask(
            "how much gold do i need to smelt to go from 48 to 50 smithing"
        )
    assert answer.text == "You need 815 gold ore."


async def test_the_product_is_found_through_the_material_when_search_missed_it():
    """The shortlist for "how much gold to smelt" is Smithing, Furnace, Gold ore
    and Blast Furnace: four pages about smelting gold and not one of them "Gold
    bar", because the bar is not what the question says. Asking what Smithing
    makes out of each is one indexed query and returns it."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "gold smelting"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("The wiki does not give the XP needed."),
        reply("815 gold ore."),
    ])
    # Nothing shortlisted is a product; Gold ore's own recipe is Mining, which
    # is a real recipe for the wrong skill and must not be counted.
    bucket = _RecipeBucket(recipe={
        "page_name": "Gold ore",
        "skills": [{"name": "Mining", "level": "40", "experience": "65"}],
    })
    bucket.from_material = _GOLD_BAR
    agent._bucket = bucket
    async with chat:
        answer = await agent.ask(
            "how much gold do i need to smelt to go from 48 to 50 smithing"
        )

    assert bucket.materials_tried[0] == ("Vorkath", "Smithing")
    assert answer.recipes_checked == ["Gold bar"]
    assert any("815 x Gold ore" in seen for seen in answer.seen)
    assert "65" not in answer.text, "Mining's XP rate is for another question"


async def test_nothing_makeable_still_gets_the_exact_gap():
    """Neither route found a recipe. The gap is still exact, and it is still the
    part the model was denying it had."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "yew"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("Yew logs give 175 xp each, but I cannot say how many you need."),
        reply("12,760,689 XP, so 72,919 yews."),
    ])
    agent._bucket = _RecipeBucket(recipe=None)
    async with chat:
        answer = await agent.ask("how many yew logs from 60 to 99 woodcutting")

    assert answer.xp_calculations == ["60->99"]
    # The rate came out of the model's own draft, and the division out of code.
    assert any("72,919 actions" in seen for seen in answer.seen)
    assert answer.text == "12,760,689 XP, so 72,919 yews."


async def test_the_count_pass_does_not_fire_when_the_tool_was_used():
    agent, chat = build_agent([
        reply(None, [call("training_cost",
                          '{"item": "Gold bar", "from_level": 48, "to_level": 50}')]),
        reply("815 gold ore."),
    ])
    bucket = _RecipeBucket(recipe=_GOLD_BAR)
    agent._bucket = bucket
    async with chat:
        answer = await agent.ask(
            "how much gold do i need to smelt to go from 48 to 50 smithing"
        )
    assert bucket.asked == 0, "the shortlist should never have been walked"
    assert answer.recipes_checked == ["Gold bar"]
    assert answer.text == "815 gold ore."


async def test_the_tool_says_so_when_a_thing_is_not_made():
    agent, chat = build_agent([
        reply(None, [call("training_cost",
                          '{"item": "Yew logs", "from_level": 60, "to_level": 99}')]),
        reply("Yew logs are cut, not made."),
    ])
    agent._bucket = _RecipeBucket(recipe=None)
    async with chat:
        answer = await agent.ask("how many yew logs from 60 to 99 woodcutting")
    assert any("no XP-per-action to divide by" in seen for seen in answer.seen)


# -- denying GE data it was handed ------------------------------------------
# Asked which jewellery made from gold bars sells best, the model searched, read
# Gold necklace, called compare_ge_prices on the necklace, the amulet and the
# bracelet -- and answered "the wiki does not give the daily volume of gold
# necklaces on the Grand Exchange", having been handed 873,817 traded/24h. Two
# things wrong: it denied data it had, and the three items were its own guess at
# a set the wiki lists forty members of.


class _FakeGE:
    """Enough of GEClient to rank. Prices are per item name, in gp/day order."""

    def __init__(self, prices: dict[str, int]):
        self._prices = prices
        self.looked_up: list[str] = []

    async def lookup(self, name):
        self.looked_up.append(name)
        each = self._prices.get(name)
        if each is None:
            return []
        return [Price(
            item=Item(id=abs(hash(name)) % 100_000, name=name, limit=100,
                      high_alch=0, members=False),
            instant_sell=each, instant_buy=each, avg_sell=each, avg_buy=each,
            volume=1_000_000,
        )]

    async def aclose(self):
        pass


class _ProductBucket:
    """The recipe bucket's uses_material index, for one material."""

    def __init__(self, products, material="Gold bar"):
        self._products = products
        self._material = material
        self.asked_for: list[str] = []

    async def products_of(self, material, *, limit=60):
        self.asked_for.append(material)
        return list(self._products) if material == self._material else []


_JEWELLERY = {"Gold necklace": 133, "Gold amulet": 132, "Gold bracelet": 247,
              "Ruby necklace": 2_500, "Diamond necklace": 1_969}


@pytest.mark.parametrize("question,expected", [
    ("What jewelry made from gold bars sells best on the Grand Exchange?",
     ["Gold bars", "Gold bar", "Gold"]),
    # "glass" is not a plural, but trying the trimmed form anyway costs one
    # query that returns nothing and saves needing to know which words are.
    ("which item made from molten glass is worth the most",
     ["Molten glass", "Molten glas", "Molten"]),
])
def test_the_material_is_read_out_of_the_question(question, expected):
    """The regex over-reads because it cannot know where the name stops, so the
    stop-word cut and the singular both matter: "made from gold bars sells best
    on the" is "gold bars", and the page is "Gold bar"."""
    assert _material_candidates(question) == expected


def test_a_comparison_with_no_material_behind_it_is_left_alone():
    """"Is a whip or a tentacle worth more" is a comparison whose set *is* the
    question. There is nothing for the bucket to widen it to."""
    assert _material_candidates("is an abyssal whip or a tentacle worth more") == []


async def test_denying_the_volume_gets_the_ranking_handed_back():
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "gold jewellery"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply(None, [call("compare_ge_prices",
                          '{"items": ["Gold necklace", "Gold amulet"]}', "c3")]),
        reply("The wiki does not give the daily volume of gold necklaces."),
        reply("Ruby necklace, by a distance."),
    ])
    agent._ge = _FakeGE(_JEWELLERY)
    agent._bucket = _ProductBucket(["Ruby necklace", "Diamond necklace",
                                    "Gold necklace", "Gold amulet"])
    async with chat:
        answer = await agent.ask(
            "What jewelry made from gold bars sells best on the Grand Exchange?"
        )

    assert "Ruby necklace" in answer.text
    assert any("ANSWER: Ruby necklace" in seen for seen in answer.seen)


async def test_the_set_comes_from_the_wiki_not_from_the_model():
    """The model named three items and the wiki lists forty; ranking the three
    it thought of answers a question nobody asked. Its own list is not even
    consulted when the material resolves."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "gold jewellery"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply(None, [call("compare_ge_prices",
                          '{"items": ["Gold necklace", "Gold amulet"]}', "c3")]),
        reply("Gold necklace sells best."),      # true of its three, wrong
        reply("Ruby necklace, at 1.1bn gp/day."),
    ])
    ge = _FakeGE(_JEWELLERY)
    agent._ge = ge
    bucket = _ProductBucket(["Ruby necklace", "Diamond necklace", "Gold necklace"])
    agent._bucket = bucket
    async with chat:
        answer = await agent.ask(
            "What jewelry made from gold bars sells best on the Grand Exchange?"
        )

    # Longest candidate first, and the plural is tried before the singular.
    assert bucket.asked_for[:2] == ["Gold bars", "Gold bar"]
    assert "Ruby necklace" in ge.looked_up, "the wiki's set was never priced"
    assert "Ruby necklace" in answer.text
    assert answer.text != "Gold necklace sells best."


async def test_a_price_question_with_no_material_still_re_ranks_its_own_set():
    """The denial half of the pass, on a comparison the bucket cannot widen."""
    agent, chat = build_agent([
        reply(None, [call("compare_ge_prices",
                          '{"items": ["Gold necklace", "Ruby necklace"]}')]),
        reply("I cannot find the volume for these."),   # a dead end it caused
        reply("Ruby necklace."),
    ])
    agent._ge = _FakeGE(_JEWELLERY)
    agent._bucket = _ProductBucket([], material="nothing")
    async with chat:
        answer = await agent.ask("is a gold necklace or a ruby necklace worth more")

    assert any("ANSWER: Ruby necklace" in seen for seen in answer.seen)
    assert answer.text == "Ruby necklace."


async def test_a_good_price_answer_is_not_re_ranked():
    """The pass costs a round trip and a table. It must not fire on an answer
    that already reported the verdict."""
    agent, chat = build_agent([
        reply(None, [call("compare_ge_prices",
                          '{"items": ["Gold necklace", "Ruby necklace"]}')]),
        reply("The ruby necklace, by a distance."),
    ])
    ge = _FakeGE(_JEWELLERY)
    agent._ge = ge
    async with chat:
        answer = await agent.ask("is a gold necklace or a ruby necklace worth more")

    assert answer.text == "The ruby necklace, by a distance."
    assert ge.looked_up == ["Gold necklace", "Ruby necklace"], "re-ranked anyway"


async def test_made_from_lists_the_whole_set():
    agent, chat = build_agent([
        reply(None, [call("made_from", '{"material": "Gold bar"}')]),
        reply("Forty things, and the ruby necklace is the valuable one."),
    ])
    agent._bucket = _ProductBucket(["Gold necklace", "Ruby necklace"])
    async with chat:
        answer = await agent.ask("what can you make with gold bars")

    assert any("Made from Gold bar (2): Gold necklace, Ruby necklace" in seen
               for seen in answer.seen)


# -- what counts as having been shown a number ------------------------------


async def test_a_search_teaser_is_not_a_source():
    """Summaries are capped at 120 characters precisely so they are too thin to
    answer from, and the prompt says every number must come from a page actually
    read. Counting a figure glimpsed in a teaser as shown let an answer cite a
    page that does not contain the number: on the Seers question "60" is in a
    teaser, the model read the overview whose only "60" is inside "260 marks of
    grace", and the grounding check passed it."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply("done"),
    ])
    answer = Answer(text="")
    tools = {t.name: t for t in agent._build_tools(answer)}
    async with chat:
        await tools["search_wiki"].handler(query="vorkath")
    assert answer.searches == ["vorkath"]
    assert answer.seen == []


async def test_a_page_read_is_a_source():
    agent, chat = build_agent([reply("done")])
    answer = Answer(text="")
    tools = {t.name: t for t in agent._build_tools(answer)}
    async with chat:
        await tools["read_wiki_page"].handler(title="Vorkath")
    assert answer.seen and "Ungael" in answer.seen[0]
# -- coin goals and the skill a question actually asked about -----------------


def test_a_coin_goal_does_not_route_to_the_xp_handback():
    """The routing bug behind three bad answers in a row.

    "how many sharks do i need to get 5 mill and how long will that take farming
    minnows" matches every duration pattern and contains no levels at all. The XP
    handback fired anyway, demanded a from_level and a to_level, and
    _force_xp_calc found no level range and bailed -- so the model was asked
    twice for a calculation it could not make. It answered "I'm sorry, I made a
    mistake" and then returned a map of the Fishing Guild.
    """
    question = "how many sharks do i need to get 5 mill and how long farming minnows?"
    assert parse_goal(question) == 5_000_000
    assert _ASKS_DURATION.search(question)  # still looks like a duration question
    assert not _LEVEL_RANGE.search(question)  # and has nothing calculate_xp can use


def test_a_training_question_is_still_the_xp_path():
    """The fix must not swing the other way. No coin goal here, so the XP
    enforcement keeps the question."""
    assert parse_goal("how long from 45 to 99 mining") is None
    assert parse_goal("How many planks from level 37 to 70 Construction?") is None


def test_quantity_questions_reach_the_arithmetic_at_all():
    """"how many planks" is the same division as "how many hours" and went
    unenforced, because the duration pattern only ever matched time units."""
    question = "How many planks are needed to go from level 37 to 70 Construction?"
    assert _ASKS_QUANTITY.search(question)
    assert _LEVEL_RANGE.search(question)
    assert not _ASKS_DURATION.search(question)  # which is why it was missed


def test_hours_questions_are_not_treated_as_quantity_questions():
    assert not _ASKS_QUANTITY.search("how many hours from 45 to 99 mining")


def test_answering_with_another_skills_level_is_caught():
    """Asked for Sailing, answered 91 -- the Fishing level. Every existing guard
    passed it: a page was read, a page was cited, and 91 genuinely is on that
    page, so the number-grounding check had nothing to say. Provenance was never
    the problem."""
    requirements = {"Sailing": 78, "Fishing": 91, "Construction": 72}
    question = "what sailing level do i need to catch marlin"
    assert _skill_mismatch(question, "You need level 91 Fishing.", requirements) == "Sailing"
    assert _skill_mismatch(question, "Sailing 78, and Fishing 91.", requirements) is None


def test_no_mismatch_when_the_question_names_no_single_skill():
    """Two skills named is a comparison, not a misfire, and zero named means the
    check has nothing to check. Firing on either would hand back good answers."""
    requirements = {"Sailing": 78, "Fishing": 91}
    assert _skill_mismatch("sailing vs fishing for marlin", "either", requirements) is None
    assert _skill_mismatch("what do i need for marlin", "Sailing 78", requirements) is None


def test_no_mismatch_when_the_page_states_no_such_requirement():
    """A Sailing question about a page with no Sailing requirement is answered
    by saying so, not by being handed back forever."""
    assert (
        _skill_mismatch("what sailing level for cannonballs", "35 Smithing", {"Smithing": 35})
        is None
    )


# -- the budget -------------------------------------------------------------
#
# Twelve enforcement passes now run under `ask`, each able to spend two or three
# round trips and several injecting a whole page as a new turn. Nothing bounded
# it, and the overflow is the failure shape this project cares about most: a
# context that quietly loses its head answers fluently with no error anywhere.


def _clock(times):
    """A monotonic clock that returns each value in turn, then holds the last."""
    it = iter(times)
    last = [times[0]]

    def now():
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]

    return now


async def test_the_default_budget_never_binds_on_an_ordinary_question():
    """The one that matters. This is a backstop, not a latency target: every
    pass exists because the model got something wrong without it, so a ceiling
    that fires on a question the eval set covers is a regression wearing a
    limit."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("Vorkath is on Ungael."),
    ])
    async with chat:
        answer = await agent.ask("where is vorkath")

    assert answer.text == "Vorkath is on Ungael."
    assert answer.budget_exhausted == ""


async def test_an_expired_deadline_skips_the_forced_read():
    """A blown deadline stops the cascade rather than the answer: the draft the
    model already produced stands, which is what _keep_best guarantees."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply("Vorkath is in the Fremennik Province."),   # ungrounded
        reply("Vorkath is on Ungael."),                   # would come next
    ])
    # Reading one starts the clock, two is the initial round trip (still inside
    # the budget, so the model gets its first go), three is past it.
    budget = Budget(clock=_clock([0.0, 0.0, 9_999.0]))
    async with chat:
        answer = await agent.ask("where is vorkath", budget=budget)

    assert answer.pages_read == [], "the forced read ran despite the deadline"
    assert answer.text == "Vorkath is in the Fremennik Province."
    assert "300s budget" in answer.budget_exhausted


async def test_an_exhausted_context_skips_the_forced_read():
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply("Vorkath is in the Fremennik Province."),
        reply("Vorkath is on Ungael."),
    ])
    async with chat:
        answer = await agent.ask("where is vorkath", budget=Budget(characters=1))

    assert answer.pages_read == []
    assert "characters of context" in answer.budget_exhausted


async def test_an_injected_page_is_trimmed_to_what_is_left():
    """And `seen` gets the trimmed text, not the whole page.

    `seen` means "what the model was actually shown" and the grounding check
    reads it as exactly that. Recording a full page while injecting part of one
    would bless every figure in the half that never arrived -- turning a context
    limit into a hole in the invented-number check.
    """
    body = "Vorkath is on Ungael. " + "padding. " * 4_000
    agent, chat = build_agent(
        [
            reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
            reply("Vorkath is in the Fremennik Province."),
            reply("Vorkath is on Ungael."),
        ],
        bodies={"Vorkath": body},
    )
    # The system prompt alone is ~5.5k, and _fit_injection reserves 2k for the
    # instruction and the reply -- so this leaves room for a slice of the page
    # and nowhere near all 36k of it.
    async with chat:
        answer = await agent.ask("where is vorkath", budget=Budget(characters=20_000))

    assert answer.pages_read == ["Vorkath"], "the page was not injected at all"
    (shown,) = [s for s in answer.seen if "padding" in s]
    assert len(shown) < len(body), "the whole page went in despite the ceiling"
    injected = chat.sent[-1]["messages"][-1]["content"]
    assert shown in injected, "seen does not match what was actually shown"


async def test_a_budget_of_zero_is_no_budget_at_all():
    """0 disables a limit rather than making it impossible to spend anything,
    matching progress_poll_seconds and every other knob here."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply("Vorkath is in the Fremennik Province."),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("Vorkath is on Ungael."),
    ])
    async with chat:
        answer = await agent.ask(
            "where is vorkath", budget=Budget(seconds=0, characters=0)
        )

    assert answer.pages_read == ["Vorkath"]
    assert answer.budget_exhausted == ""


# -- money-making questions -------------------------------------------------


@pytest.mark.parametrize("question", [
    "what skill is best to get highest for making money based on ge prices",
    "which skilling method makes the most money",
    "what is the best money maker in osrs",
    "most profitable skill",
    "fastest way to earn gp",
])
def test_money_questions_are_recognised(question):
    assert _ASKS_BEST_MONEY.search(question)


@pytest.mark.parametrize("question", [
    "how do I make cannonballs",
    "what level smithing do I need to make a rune platebody",
    "how much is an abyssal whip worth",
])
def test_other_questions_do_not_trigger_the_money_ranking(question):
    assert not _ASKS_BEST_MONEY.search(question)


def test_making_money_is_not_a_crafting_question():
    """The recipe pass fired on it and went looking for a production template
    for the word 'money'. Coins are not an item you smith."""
    assert not _ASKS_HOW_TO_MAKE.search("what skill is best for making money")
    assert not _ASKS_HOW_TO_MAKE.search("fastest way to make gp")


@pytest.mark.parametrize("question", [
    "how do I make cannonballs",
    "how much gold do i need to smelt to go from 48 to 50 smithing",
    "what level to make a rune platebody",
])
def test_real_crafting_questions_still_reach_the_recipe(question):
    """Gold is deliberately not excluded -- 'make gold bars' is a real Smithing
    question and the commonest one in the eval set."""
    assert _ASKS_HOW_TO_MAKE.search(question)


# -- the grounding check ----------------------------------------------


def test_an_invented_quantity_is_still_caught():
    """'you can smash 15 rocks per inventory' -- no 15 anywhere in what was
    read, and nothing near one. The live failure this whole check exists for."""
    seen = ["Granite is mined at the quarry."]
    assert _ungrounded_numbers(
        "You can smash 15 rocks per inventory.", "how do I mine granite", seen
    ) == ["15"]


def test_a_wrong_level_is_caught():
    """A level is exact or it is invented -- that is the whole failure."""
    seen = ["Cannonball: Steel cannonball | 35 | 30 Smithing XP"]
    assert "30" not in _ungrounded_numbers("You need 35 Smithing.", "what level", seen)
    assert _ungrounded_numbers("You need at least 32 Smithing.", "what level", seen) == [
        "32"
    ]


def test_a_level_close_to_a_shown_one_is_still_checked():
    """36 is within 5% of 35, and would sail through if requirements got the
    same tolerance derived figures do."""
    seen = ["Rune platebody requires 99 Smithing"]
    assert _ungrounded_numbers("You need level 96 Smithing.", "what level", seen) == [
        "96"
    ]


def test_the_passes_that_fired_are_recorded():
    """Thirteen passes fire conditionally and several can undo each other. From
    the answer text alone that reads as flakiness."""
    answer = Answer(text="")
    assert answer.passes_fired == []
    assert answer.excised == []


async def test_a_pass_that_fires_is_recorded():
    """The forced read fires here, so it must show up by name. Recorded in
    _out_of_budget -- the one line every recording pass already runs before it
    does anything -- so a pass added later is instrumented by default."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply("Vorkath is in the Fremennik Province."),   # ungrounded, no read
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("Vorkath is on Ungael."),
    ])
    async with chat:
        answer = await agent.ask("where is vorkath")

    assert "the read nudge" in answer.passes_fired


async def test_a_clean_answer_records_no_passes():
    """An answer that needed no enforcement should say so, or the trace is
    noise on every case that already worked."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "vorkath"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Vorkath"}', "c2")]),
        reply("Vorkath is on Ungael."),
    ])
    async with chat:
        answer = await agent.ask("where is vorkath")

    assert answer.passes_fired == []


@pytest.mark.parametrize("question,expected", [
    ("what sailing level do i need to catch marlin", True),
    ("what agility level do I need for the Seers' Village course", True),
    ("what level smithing do I need to make a rune platebody", True),
    # Two skills is a comparison; neither is the one thing asked for.
    ("is sailing or fishing better for marlin", False),
    # No skill named, so there is no requirement to look up.
    ("what level do I need", False),
    ("how much is an abyssal whip worth", False),
])
def test_skill_level_questions_are_recognised(question, expected):
    assert _skill_level_question(question) is expected


# -- arithmetic the question asked for is not an invention -------------------


def test_a_product_of_the_asked_quantity_is_grounded():
    """"How much will 795 sharks bring" times the price it was handed is a
    number in neither the question nor anything it read -- it is what the
    question asked it to work out. Flagging it fired the grounding nudge, which
    tells the model to report an unsourceable figure as one the wiki does not
    give, and the nudge is the last pass, so nothing caught the refusal it
    caused: "The wiki does not give the price of 795 sharks", after a
    successful GE lookup."""
    seen = ["Shark (id 385): ~999 gp each\n  you receive ~979 after 20 GE tax"]
    q = "how much money will 795 sharks bring on ge"
    assert _ungrounded_numbers("795 sharks bring 778,305 gp", q, seen) == []


def test_a_rounded_product_is_grounded_too():
    seen = ["you receive ~979 after tax"]
    q = "how much will 795 sharks bring"
    assert _ungrounded_numbers("about 778,000 gp", q, seen) == []


def test_a_figure_that_is_no_product_is_still_caught():
    seen = ["you receive ~979 after tax"]
    q = "how much will 795 sharks bring"
    assert _ungrounded_numbers("795 sharks bring 5,000,000 gp", q, seen) == ["5,000,000"]


def test_an_invented_level_is_still_caught():
    """The quantity rule must not reach requirements. 30 is not 795 times
    anything the model was shown."""
    assert _ungrounded_numbers(
        "you need 30 Smithing", "what level for cannonballs", ["Steel cannonball | 35"]
    ) == ["30"]


def test_no_quantity_in_the_question_means_no_products():
    """With nothing to multiply by, the rule contributes nothing and the check
    behaves exactly as it did."""
    assert _ungrounded_numbers(
        "it takes 999 hours", "how long does it take", ["the guide says 12 hours"]
    ) == ["999"]


def test_our_own_handback_is_not_an_answer():
    """One run in three of the 795-sharks question returned the grounding nudge
    verbatim -- addressed to the model, printed to the asker. Every emptiness
    and length check waves that through, exactly as they did a bare tool name
    before _is_tool_leak."""
    nudge = _grounding_nudge(["778,305"])
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "how much will 795 sharks bring"},
        {"role": "assistant", "content": "795 sharks bring 778,305 gp."},
        {"role": "user", "content": nudge},
        {"role": "assistant", "content": nudge[:200]},
    ]
    # The echo is skipped and the real answer beneath it stands.
    assert _last_assistant_text(messages) == "795 sharks bring 778,305 gp."


def test_a_real_answer_that_quotes_a_few_words_survives():
    """Only a long verbatim run counts. A model legitimately using the same
    words as an instruction must not be silenced."""
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "what level"},
        {"role": "user", "content": _grounding_nudge(["30"])},
        {"role": "assistant", "content": "The wiki does not give it."},
    ]
    assert _last_assistant_text(messages) == "The wiki does not give it."


async def test_requirements_stay_quiet_when_the_page_lacks_the_skill():
    """Rune platebody's markup carries Construction 28, Smithing 99 and
    Smithing 25 and no Defence at all. Asked for the Defence level to wear one,
    this pass handed all three over under "answer from exactly these" -- an
    invitation to be confidently wrong about a requirement nobody asked for. It
    survived only because the model ignored the injection."""
    agent, chat = build_agent([
        reply(None, [call("search_wiki", '{"query": "rune platebody"}')]),
        reply(None, [call("read_wiki_page", '{"title": "Rune platebody"}', "c2")]),
        reply("You need 40 Defence to wear a rune platebody."),
    ], bodies={"Rune platebody": "Requires 40 Defence to wear."})

    async def requirements(title):
        return [("Construction", 28), ("Smithing", 99), ("Smithing", 25)]

    agent.wiki.requirements = requirements
    async with chat:
        answer = await agent.ask("what defence level do you need to wear a rune platebody")

    # Nothing about Construction or Smithing was injected as the answer.
    assert "Construction 28" not in " ".join(answer.seen)
    assert answer.text == "You need 40 Defence to wear a rune platebody."


def test_the_page_about_the_thing_beats_a_page_about_a_variant():
    """Every Vorkath title shares exactly one word with the question, so the
    winner was whichever came first in the shortlist -- and eight failures in
    ten answered about the Vorkath Veteran achievement while the page that
    states Dragon Slayer II sat in the same list."""
    titles = ["Vorkath/Strategies", "Vorkath Master", "Vorkath Veteran",
              "Vorkath", "Vorkath Speed-Runner"]
    q = "what quest do you need to complete to fight Vorkath"
    assert _best_title(q, titles) == "Vorkath"


def test_more_shared_words_still_wins_over_fewer_surplus():
    """The tie-break is only a tie-break. A title that shares more of the
    question must not lose to a shorter one that shares less."""
    q = "what agility level do I need for the Seers' Village rooftop course"
    assert _best_title(q, ["Agility", "Seers' Village Rooftop Course"]) == (
        "Seers' Village Rooftop Course"
    )


def test_read_nudge_tells_the_model_it_is_not_dialogue():
    """It arrives as a user turn, so the model answers it unless told otherwise.

    Observed live, in persona: "Oh, snap! I see what you did there. You wanted me
    to read the page before answering, huh?" -- the grounding machinery narrated
    aloud to the player it exists to protect.
    """
    from reldo.agent import _read_nudge

    nudge = _read_nudge("Fishing")
    assert "internal instruction" in nudge
    assert "not something the player said" in nudge
    # And it still has to say what to do.
    assert "read_wiki_page on 'Fishing'" in nudge


def test_every_nudge_says_it_is_not_dialogue():
    """Four correctives arrive as user turns; fixing one left three leaking.

    Observed live, after the read nudge alone was fixed: a coaching remark that
    opened "I apologise for the confusion earlier, love" and then answered. The
    apology came from a different nudge three hundred lines away, and _gp_nudge's
    own docstring had already recorded the same failure ("I'm sorry, I made a
    mistake" followed by a map of the Fishing Guild) without it being fixed.
    """
    from reldo.agent import _gp_nudge, _grounding_nudge, _read_nudge, _xp_nudge

    for nudge in (
        _read_nudge("Fishing"),
        _grounding_nudge(["37,502"]),
        _gp_nudge(5_000_000),
        _xp_nudge(),
    ):
        assert "internal instruction" in nudge
        assert "apologise for anything" in nudge


def test_strips_the_apology_the_nudge_provokes():
    """Every one of these was said out loud, in a persona, to the player.

    NOT_DIALOGUE asks the model not to. Across a session it did not always
    listen, so this removes rather than asks -- the same call the ungrounded
    number excision makes.
    """
    from reldo.agent import _strip_meta

    for said, expected_start in (
        ("Oh, my apologies, love. With Thieving 19, you're far off.", "With Thieving 19"),
        (
            "Oh, bless you, love. You're right, let's get that sorted. Attack needs 20.",
            "Attack needs 20",
        ),
        ("I apologize for the confusion earlier, love. Prayer is next.", "Prayer is next"),
    ):
        out, removed = _strip_meta(said)
        assert removed, said
        assert out.startswith(expected_start)


def test_keeps_an_apology_that_is_about_the_game():
    """"Sorry, that method needs 70 Slayer" is the answer, not the machinery."""
    from reldo.agent import _strip_meta

    said = "Sorry, that method needs 70 Slayer before you can touch it."
    assert _strip_meta(said) == (said, [])


def test_never_strips_the_whole_answer():
    """A blank reply is worse than an apologetic one."""
    from reldo.agent import _strip_meta

    assert _strip_meta("I apologise for the confusion.")[1] == []
