"""Wiki client tests. No network: every response is mocked.

The suite must not depend on the wiki being up or on any given page saying any
given thing -- both change without notice.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from reldo.wiki import (
    API_URL,
    MAX_TITLES_PER_CONTENT_CALL,
    WikiClient,
    WikiError,
    _chunked,
    _html_to_text,
    _strip_html,
    parse_skill_requirements,
    parse_tables,
)


def test_constructor_rejects_missing_user_agent():
    with pytest.raises(ValueError, match="User-Agent"):
        WikiClient("")
    with pytest.raises(ValueError, match="User-Agent"):
        WikiClient("   ")


def test_constructor_accepts_descriptive_user_agent():
    client = WikiClient("reldo/0.1 (github.com/x/reldo)")
    assert client is not None


@respx.mock
async def test_search_strips_snippet_markup():
    respx.get(API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "query": {
                    "search": [
                        {
                            "title": "Abyssal whip",
                            "snippet": (
                                'The <span class="searchmatch">whip</span>'
                                " is &quot;good&quot;"
                            ),
                            "wordcount": 1200,
                        }
                    ]
                }
            },
        )
    )
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        hits = await client.search("whip")
    assert hits[0].title == "Abyssal whip"
    assert hits[0].snippet == 'The whip is "good"'
    assert hits[0].word_count == 1200


@respx.mock
async def test_maxlag_error_body_is_retried_not_raised():
    """maxlag arrives as HTTP 200 with an error body -- easy to mistake for success."""
    route = respx.get(API_URL)
    route.side_effect = [
        httpx.Response(200, json={"error": {"code": "maxlag", "info": "Waiting for db"}}),
        httpx.Response(200, json={"query": {"search": []}}),
    ]
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        assert await client.search("anything") == []
    assert route.call_count == 2


@respx.mock
async def test_other_api_errors_raise():
    respx.get(API_URL).mock(
        return_value=httpx.Response(
            200, json={"error": {"code": "badvalue", "info": "Unrecognised value"}}
        )
    )
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        with pytest.raises(WikiError, match="badvalue"):
            await client.search("x")


@respx.mock
async def test_summaries_drops_missing_pages_rather_than_raising():
    respx.get(API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {"title": "Real page", "extract": "  Body text.  "},
                        {"title": "Deleted page", "missing": True},
                        {"title": "Empty page", "extract": ""},
                    ]
                }
            },
        )
    )
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        summaries = await client.summaries(["Real page", "Deleted page", "Empty page"])
    assert [s.title for s in summaries] == ["Real page"]
    assert summaries[0].summary == "Body text."


async def test_summaries_rejects_oversized_batch():
    """MediaWiki silently clamps past 20 titles; we'd rather fail loudly."""
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        with pytest.raises(ValueError, match="20 titles"):
            await client.summaries([f"Page {i}" for i in range(21)])


@respx.mock
async def test_page_text_raises_on_missing_page():
    respx.get(API_URL).mock(
        return_value=httpx.Response(200, json={"query": {"pages": [{"missing": True}]}})
    )
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        with pytest.raises(WikiError, match="No such page"):
            await client.page_text("Nonexistent")


@respx.mock
async def test_article_enumeration_follows_continuation():
    route = respx.get(API_URL)
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "query": {"allpages": [{"title": "A"}, {"title": "B"}]},
                "continue": {"apcontinue": "C", "continue": "-||"},
            },
        ),
        httpx.Response(200, json={"query": {"allpages": [{"title": "C"}]}}),
    ]
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        titles = [t async for t in client.iter_article_titles()]
    assert titles == ["A", "B", "C"]


@respx.mock
async def test_429_is_retried_honouring_retry_after():
    route = respx.get(API_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"query": {"search": []}}),
    ]
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        assert await client.search("x") == []
    assert route.call_count == 2


@respx.mock
async def test_a_server_error_is_retried_like_a_503():
    """502 and 504 are the same transient failure as 503 wearing a CDN's number.

    Treating them as fatal threw away a whole index build over one blip.
    """
    route = respx.get(API_URL)
    route.side_effect = [
        httpx.Response(502),
        httpx.Response(504),
        httpx.Response(200, json={"query": {"search": []}}),
    ]
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        assert await client.search("x") == []
    assert route.call_count == 3


@respx.mock
async def test_a_persistent_server_error_raises_wiki_error_naming_it():
    """Not httpx.HTTPStatusError. Every caller here catches WikiError and only
    WikiError, so the wrong type does not degrade -- it escapes."""
    respx.get(API_URL).mock(return_value=httpx.Response(500))
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        with pytest.raises(WikiError, match="HTTP 500"):
            await client.search("x")


@respx.mock
async def test_a_client_error_raises_wiki_error_without_retrying():
    """A 400 is our malformed query and will not improve on a retry."""
    route = respx.get(API_URL)
    route.mock(return_value=httpx.Response(400, text="bad request"))
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        with pytest.raises(WikiError, match="HTTP 400"):
            await client.search("x")
    assert route.call_count == 1


@respx.mock
async def test_a_non_json_body_is_a_wiki_error():
    """An interstitial from whatever sits in front of the wiki, not from the API."""
    respx.get(API_URL).mock(return_value=httpx.Response(200, text="<html>nope</html>"))
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        with pytest.raises(WikiError, match="non-JSON"):
            await client.search("x")


@respx.mock
async def test_one_failed_batch_does_not_abort_a_wikitext_sweep():
    """The whole point of the WikiError contract, stated as behaviour.

    ``iter_wikitext`` catches WikiError per batch so a 700-call index build
    survives one bad response. With `raise_for_status` here that promise held
    for a 503 and broke for a 500 -- the sweep died mid-build.
    """
    # One title past the 50-per-call cap, so this is genuinely two batches: the
    # first exhausts its five attempts on 500s, the second must still be served.
    titles = [f"p{n}" for n in range(MAX_TITLES_PER_CONTENT_CALL + 1)]
    survivor = titles[-1]
    page = {
        "query": {
            "pages": [
                {
                    "title": survivor,
                    "revisions": [{"slots": {"main": {"content": "text"}}}],
                }
            ]
        }
    }
    route = respx.get(API_URL)
    route.side_effect = [httpx.Response(500)] * 5 + [httpx.Response(200, json=page)]
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        found = {t: c async for t, c in client.iter_wikitext(titles, concurrency=1)}
    assert found == {survivor: "text"}


def test_chunked_splits_evenly_and_keeps_remainder():
    assert list(_chunked(list("abcde"), 2)) == [["a", "b"], ["c", "d"], ["e"]]
    assert list(_chunked([], 3)) == []


def test_strip_html_handles_entities():
    assert _strip_html("<b>x</b> &amp; <i>y</i>") == "x & y"


@respx.mock
async def test_headings_parsed_from_wikitext_with_markup_stripped():
    wikitext = (
        "Intro prose.\n"
        "== Combat stats ==\n"
        "stuff\n"
        "=== [[Special attack|Special]] ===\n"
        "more\n"
        "== '''Bold''' heading ==\n"
        "==== {{Template}}Deep ====\n"
        "not a heading = still not\n"
    )
    respx.get(API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {
                            "title": "Some page",
                            "revisions": [{"slots": {"main": {"content": wikitext}}}],
                        }
                    ]
                }
            },
        )
    )
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        found = await client.headings(["Some page"])
    assert found["Some page"] == ["Combat stats", "Special", "Bold heading", "Deep"]


@respx.mock
async def test_headings_omits_pages_without_revisions():
    respx.get(API_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "query": {
                    "pages": [
                        {"title": "Gone", "missing": True},
                        {
                            "title": "Here",
                            "revisions": [{"slots": {"main": {"content": "== A ==\n"}}}],
                        },
                    ]
                }
            },
        )
    )
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        found = await client.headings(["Gone", "Here"])
    assert found == {"Here": ["A"]}


async def test_headings_rejects_oversized_batch():
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        with pytest.raises(ValueError, match="50 titles"):
            await client.headings([f"P{i}" for i in range(51)])


@respx.mock
async def test_transport_errors_are_retried():
    """A blip mid-build must not throw away a 20-minute run."""
    route = respx.get(API_URL)
    route.side_effect = [
        httpx.ConnectError("connection reset"),
        httpx.ReadTimeout("slow"),
        httpx.Response(200, json={"query": {"search": []}}),
    ]
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        assert await client.search("x") == []
    assert route.call_count == 3


@respx.mock
async def test_transport_errors_eventually_give_up():
    respx.get(API_URL).mock(side_effect=httpx.ConnectError("down"))
    async with WikiClient("test/1.0 (x)", requests_per_second=0, backoff_base=0) as client:
        with pytest.raises(WikiError, match="Transport failure"):
            await client.search("x")


# -- skill requirements -------------------------------------------------------
#
# The markup below is copied verbatim from the rendered "Raw marlin" page. It is
# what "what Sailing level do I need to catch marlin" was answered from, and the
# answer was 91 -- the Fishing level. Both requirements are in this cell, and
# before the parser knew about `data-skill` the cell read "78 91": two bare
# numbers next to two indistinguishable icons, with nothing saying which was
# which. The prose spelled out the Fishing one, so the model took it.

MARLIN_SKILLS_CELL = (
    '<table><tr><th>Method</th><th>Skills</th></tr>'
    '<tr><td>Deep sea trawling for marlin</td><td class="plainlist"><ul>'
    '<li><span class="scp" data-skill="Sailing" data-level="78">'
    '<a href="/w/Sailing" title="Sailing"><img alt="Sailing" src="/images/x.png" /></a>'
    " 78 </span></li>"
    '<li><span class="scp" data-skill="Fishing" data-level="91">'
    '<a href="/w/Fishing" title="Fishing"><img alt="Fishing" src="/images/y.png" /></a>'
    " 91 </span><sup>[boostable]</sup></li>"
    '<li><span class="scp" data-skill="Construction" data-level="72">'
    '<img alt="Construction" src="/images/z.png" /> 72 </span></li>'
    "</ul></td></tr></table>"
)


def test_requirement_cells_keep_the_skill_name():
    (table,) = parse_tables(MARLIN_SKILLS_CELL)
    cell = table[1][1]
    assert "Sailing 78" in cell
    assert "Fishing 91" in cell
    assert "Construction 72" in cell


def test_the_level_is_not_doubled_by_keeping_the_icon():
    """The span carries the level twice -- once as an attribute and once as its
    text. Emitting both turns "Sailing 78" into "Sailing 78 78", which reads as
    two requirements."""
    (table,) = parse_tables(MARLIN_SKILLS_CELL)
    assert table[1][1].count("78") == 1


def test_parses_requirements_as_pairs():
    assert parse_skill_requirements(MARLIN_SKILLS_CELL) == [
        ("Sailing", 78),
        ("Fishing", 91),
        ("Construction", 72),
    ]


def test_xp_rates_in_the_same_markup_are_not_reported_as_levels():
    """Money-making guides render "30,274 Fishing XP per hour" with the same
    template. A 30,274 in a requirements list would read as an impossible level
    and, worse, as a real one somewhere it got truncated."""
    xp_row = (
        '<span class="scp" data-skill="Fishing" data-level="30,274"></span>'
        '<span class="scp" data-skill="Sailing" data-level="28750"></span>'
        '<span class="scp" data-skill="Fishing" data-level="82"></span>'
    )
    assert parse_skill_requirements(xp_row) == [("Fishing", 82)]


def test_section_text_keeps_the_skill_name_too():
    """read_wiki_section goes through a different path than read_wiki_table and
    had the same hole."""
    text = _html_to_text(MARLIN_SKILLS_CELL)
    assert "Sailing 78" in text
    assert "Fishing 91" in text


def test_pages_with_no_requirements_return_nothing_rather_than_guessing():
    assert parse_skill_requirements("<p>Marlin is a type of fish.</p>") == []


# -- a fixed swap between two items ------------------------------------------
# Verbatim from the two pages that state it, because the parsing is entirely
# about how the wiki phrases a rate that is neither a price nor a recipe.

MINNOW = (
    "Minnow can be exchanged for noted raw sharks by trading with Kylie Minnow "
    "at a rate of 40 minnows for one raw shark. Minnow fishing spots move "
    "around in a consistent 1-tile clockwise rotation at a fixed rate of 15 "
    "seconds, or 25 ticks."
)
GUIDE = (
    "Minnows are available to catch on the minnow platform; having no use on "
    "their own, they are exchanged with Kylie Minnow for noted raw sharks, at "
    "a rate of 40 minnows for 1 shark."
)


def test_a_rate_is_read_off_the_page_and_oriented_to_the_question():
    from reldo.wiki import exchange_rate

    assert exchange_rate(MINNOW, "minnows", "sharks")[:2] == (40, 1)
    # The other way round is the same sentence and a different answer, and
    # which number is the numerator is the asker's business rather than the
    # page author's.
    assert exchange_rate(MINNOW, "sharks", "minnows")[:2] == (1, 40)


def test_the_item_name_is_not_cut_short_at_an_adjective():
    """A lazy match ends the second name at "raw", and "raw" is not the item --
    so a question about sharks failed against the page that answers it. Same
    bug as "mahogany planks" reaching "Mahogany hull parts": half a name."""
    from reldo.wiki import exchange_rate

    found = exchange_rate(MINNOW, "minnows", "raw shark")
    assert found is not None and found[:2] == (40, 1)


def test_both_phrasings_of_the_same_rate_agree():
    from reldo.wiki import exchange_rate

    assert exchange_rate(GUIDE, "minnows", "sharks")[:2] == (40, 1)


def test_the_sentence_has_to_be_about_swapping():
    """Two numbers in a sentence are not a rate. The minnow page's next line is
    "spots move ... at a fixed rate of 15 seconds, or 25 ticks", which has the
    grammar and none of the meaning."""
    from reldo.wiki import exchange_rate

    assert exchange_rate(
        "Minnow fishing spots move in a rotation at a fixed rate of 15 seconds, "
        "or 25 ticks.",
        "minnows", "seconds",
    ) is None


def test_an_item_the_page_does_not_trade_reads_as_no_rate():
    from reldo.wiki import exchange_rate

    assert exchange_rate(MINNOW, "minnows", "lobsters") is None
    assert exchange_rate(MINNOW, "", "sharks") is None


def test_the_pages_own_name_for_the_thing_comes_back_too():
    """Kylie Minnow gives *noted raw sharks*, and somebody asking about
    "sharks" means the fish. Priced as the cooked item that is 980 gp each
    against the raw 696, so the answer is out by 40%."""
    from reldo.wiki import exchange_rate

    assert exchange_rate(MINNOW, "sharks", "minnows")[2] == "raw shark"


def test_the_sentence_comes_back_with_the_numbers():
    """The rate is a claim about one NPC in one place, and the answer should be
    able to say which -- Kylie Minnow, not the Grand Exchange."""
    from reldo.wiki import exchange_rate

    assert "Kylie Minnow" in exchange_rate(MINNOW, "minnows", "sharks")[3]
