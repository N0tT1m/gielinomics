"""Bucket queries, without network.

The query parameter is Lua *source*, so the escaping here is not ergonomics, it
is the whole security boundary: quest names arrive from the model, and an
unescaped apostrophe in "Cook's Assistant" does not break the query, it ends the
string literal and the rest of the value is code.

The wire shape is pinned too, because two things about it are surprising and
both were learned by probing rather than from documentation: the extension
answers errors as a bare *string* where the rest of the MediaWiki API uses an
object, and a builder without `.run()` comes back as the builder rather than as
rows.
"""

from __future__ import annotations

import json

import httpx
import pytest

from reldo.bucket import (
    BucketClient,
    BucketError,
    build_query,
    lua_string,
    name_variants,
)
from reldo.wiki import WikiClient


def client_over(handler) -> BucketClient:
    return BucketClient(
        WikiClient("reldo/test (tests)", transport=httpx.MockTransport(handler))
    )


def responds(payload):
    return lambda request: httpx.Response(200, json=payload)


def rows(*records):
    return responds({"bucketQuery": "...", "bucket": list(records)})


# -- escaping ---------------------------------------------------------------


def test_an_apostrophe_is_escaped_not_passed_through():
    """Cook's Assistant is a real quest and the single most likely thing to be
    looked up with an apostrophe in it."""
    assert lua_string("Cook's Assistant") == r"'Cook\'s Assistant'"


def test_a_backslash_is_escaped_before_the_quote_is():
    r"""Order matters. Escaping the quote first would give \' its own backslash
    on the second pass, closing the literal after all."""
    assert lua_string(r"a\'b") == r"'a\\\'b'"


def test_a_lua_break_out_attempt_stays_inside_the_string():
    hostile = "x') os.exit() --"
    escaped = lua_string(hostile)
    # The only unescaped quotes are the ones this function added.
    assert escaped.startswith("'") and escaped.endswith("'")
    assert "\\'" in escaped
    assert escaped.count("'") - escaped.count("\\'") == 2


@pytest.mark.parametrize("bad", ["line\nbreak", "tab\there", "nul\x00"])
def test_control_characters_are_refused_rather_than_escaped(bad):
    """Nothing legitimate contains one, and a raw newline inside a Lua literal
    is a syntax error anyway -- so this is a bug being reported, not a value
    being cleaned."""
    with pytest.raises(BucketError, match="Control character"):
        lua_string(bad)


# -- query building ---------------------------------------------------------


def test_a_plain_select_reads_as_the_lua_it_is():
    assert build_query("quest", ["page_name", "json"], limit=1) == (
        "bucket('quest').select('page_name', 'json').limit(1).run()"
    )


def test_a_where_clause_escapes_its_value():
    assert "'Cook\\'s Assistant'" in build_query(
        "quest", ["page_name"], where={"page_name": "Cook's Assistant"}
    )


@pytest.mark.parametrize("name", ["drop table", "quest;--", "", "1quest", "a'b"])
def test_an_identifier_that_would_need_escaping_is_a_bug_not_a_value(name):
    with pytest.raises(BucketError, match="Not a valid"):
        build_query(name, ["page_name"])


def test_field_names_are_validated_too():
    with pytest.raises(BucketError, match="Not a valid field name"):
        build_query("quest", ["page_name; os.exit()"])


def test_selecting_nothing_is_refused_before_a_request_is_made():
    """The extension's own message for this is "You must select at least one
    field", which costs a round trip to be told what we already know."""
    with pytest.raises(BucketError, match="at least one field"):
        build_query("quest", [])


# -- the wire ---------------------------------------------------------------


async def test_rows_come_back_as_dicts():
    client = client_over(rows({"page_name": "Cook's Assistant"}))
    assert await client.select("quest", ["page_name"]) == [
        {"page_name": "Cook's Assistant"}
    ]


async def test_a_string_error_is_reported_not_crashed_on():
    """Every other MediaWiki endpoint answers with an object. Assuming that
    shape turned the extension's own good message into an AttributeError."""
    client = client_over(responds({"bucketQuery": "...", "error": "Bucket x does not exist."}))
    with pytest.raises(BucketError, match="does not exist"):
        await client.select("quest", ["page_name"])


async def test_a_builder_that_was_never_run_is_not_mistaken_for_no_rows():
    """Omitting .run() returns the builder object. Read as rows it looks like a
    successful query that matched nothing, which is the wrong thing to believe."""
    client = client_over(responds({"bucketQuery": "...", "bucket": {"bucketName": "quest"}}))
    with pytest.raises(BucketError, match="never|not run|forget"):
        await client.query("bucket('quest')")


# -- quest requirements -----------------------------------------------------


def quest_row(requirements_html: str):
    return rows({"page_name": "Dragon Slayer II",
                 "json": json.dumps({"requirements": requirements_html})})


async def test_skill_requirements_are_read_from_the_markup_not_the_prose():
    """The wiki tags each requirement with the skill and level for its own
    scripts, so this reads a field rather than parsing a sentence."""
    client = client_over(quest_row(
        '<span data-skill="Magic" data-level="75">75 Magic</span>'
        '<span data-skill="Smithing" data-level="70">70 Smithing</span>'
    ))
    title, found = await client.quest_requirements("Dragon Slayer II")
    assert title == "Dragon Slayer II"
    assert found == {"Magic": 75, "Smithing": 70}


async def test_the_higher_of_two_levels_for_one_skill_wins():
    """A quest can list a skill twice -- once to start and once to finish -- and
    the binding requirement is the larger."""
    client = client_over(quest_row(
        '<span data-skill="Magic" data-level="60">to start</span>'
        '<span data-skill="Magic" data-level="75">to finish</span>'
    ))
    _, found = await client.quest_requirements("Dragon Slayer II")
    assert found == {"Magic": 75}


async def test_quest_points_come_through_like_any_other_requirement():
    """Not a skill, but it gates a quest the same way and the wiki marks it up
    the same way."""
    client = client_over(quest_row('<span data-skill="Quest points" data-level="200">x</span>'))
    _, found = await client.quest_requirements("Dragon Slayer II")
    assert found == {"Quest points": 200}


async def test_a_quest_with_no_requirements_is_empty_not_missing():
    """Distinct outcomes: Cook's Assistant needs nothing, and a typo needs
    telling. Collapsing them would report every misspelling as a free quest."""
    client = client_over(rows({"page_name": "Cook's Assistant", "json": "{}"}))
    title, found = await client.quest_requirements("Cook's Assistant")
    assert (title, found) == ("Cook's Assistant", {})


async def test_an_unknown_quest_is_none():
    client = client_over(rows())
    assert await client.quest_requirements("Not A Quest") is None


async def test_unparseable_quest_json_costs_the_requirements_not_the_answer():
    client = client_over(rows({"page_name": "Dragon Slayer II", "json": "{not json"}))
    title, found = await client.quest_requirements("Dragon Slayer II")
    assert (title, found) == ("Dragon Slayer II", {})


# -- schema -----------------------------------------------------------------


async def test_page_name_is_added_to_a_schema_that_never_lists_it():
    """It is queryable on every bucket and appears in no schema page. A caller
    reading the schema to decide what to select would otherwise never learn the
    one field that names the row -- Bucket:Quest defines nine fields and not one
    of them is the quest's name."""
    schema = {"requirements": {"type": "TEXT"}}
    client = client_over(responds({
        "query": {"pages": [{
            "title": "Bucket:Quest",
            "revisions": [{"slots": {"main": {"content": json.dumps(schema)}}}],
        }]}
    }))
    fields = await client.schema("quest")
    assert "page_name" in fields and "requirements" in fields


async def test_a_schema_is_fetched_once_and_remembered():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json={"query": {"pages": [{
            "title": "Bucket:Quest",
            "revisions": [{"slots": {"main": {"content": "{}"}}}],
        }]}})

    client = client_over(handler)
    await client.schema("quest")
    await client.schema("quest")
    assert len(calls) == 1


# -- picking which shortlisted page has the recipe --------------------------


async def test_the_first_page_with_a_recipe_wins_not_the_first_page():
    """Choosing by word overlap picks "Cannonball" over "Steel cannonball",
    which is the page carrying the level. The shortlist is already ordered by
    relevance; the job is to find which entry has the data."""
    def handler(request):
        if "Steel" in str(request.url):
            return httpx.Response(200, json={"bucket": [{
                "page_name": "Steel cannonball",
                "production_json": json.dumps({"skills": [{"name": "Smithing", "level": "35"}]}),
            }]})
        return httpx.Response(200, json={"bucket": []})

    found = await client_over(handler).first_recipe(["Cannonball", "Steel cannonball"])
    assert found["page_name"] == "Steel cannonball"


async def test_a_recipe_with_no_skill_level_is_not_an_answer():
    """Plenty of things are made from something and need no level. Returning one
    would hand the model a block with no requirement in it."""
    client = client_over(responds({"bucket": [{
        "page_name": "Bucket of water",
        "production_json": json.dumps({"materials": [{"name": "Bucket"}]}),
    }]}))
    assert await client.first_recipe(["Bucket of water"]) is None


def _jewellery_and_bar(request):
    """A shortlist where the wrong skill ranks first: "gold ... smithing"
    surfaces the necklace above the bar, and a gold necklace is a real recipe
    for Crafting."""
    skill = "Smithing" if "bar" in str(request.url).lower() else "Crafting"
    return httpx.Response(200, json={"bucket": [{
        "page_name": "Gold bar" if skill == "Smithing" else "Gold necklace",
        "production_json": json.dumps(
            {"skills": [{"name": skill, "level": "5", "experience": "22.5"}]}
        ),
    }]})


async def test_the_named_skill_beats_the_higher_ranked_page():
    found = await client_over(_jewellery_and_bar).first_recipe(
        ["Gold necklace", "Gold bar"], skill="Smithing"
    )
    assert found["page_name"] == "Gold bar"


async def test_the_wrong_skill_is_still_better_than_nothing():
    """No recipe trains the named skill, so the best of the rest is returned
    rather than the pass giving up on a question it can partly answer."""
    found = await client_over(_jewellery_and_bar).first_recipe(
        ["Gold necklace"], skill="Smithing"
    )
    assert found["page_name"] == "Gold necklace"


async def test_what_a_skill_makes_out_of_a_material():
    """The direction search cannot cover: "Gold bar" is not in the shortlist for
    "how much gold do I need to smelt", because it is not what the question
    says. uses_material and uses_skill are indexed, so it is one query."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"bucket": [
            # The same product repeats per facility, and the first row can be
            # empty -- the parse has to walk them rather than take row zero.
            {"page_name": "Gold bar", "production_json": ""},
            {"page_name": "Gold bar", "production_json": json.dumps(
                {"skills": [{"name": "Smithing", "level": "40", "experience": "22.5"}]}
            )},
        ]})

    found = await client_over(handler).recipe_from_material("Gold ore", "Smithing")
    assert found["page_name"] == "Gold bar"
    assert "Gold+ore" in seen[0] or "Gold%20ore" in seen[0]


async def test_a_material_that_makes_nothing_in_that_skill():
    client = client_over(responds({"bucket": []}))
    assert await client.recipe_from_material("Vorkath", "Smithing") is None


async def test_everything_made_from_a_material_is_one_query():
    """A product repeats per facility and per recipe variant -- gold bar comes
    back three times for the three furnaces -- and the caller wants the set."""
    client = client_over(responds({"bucket": [
        {"page_name": "Gold necklace"},
        {"page_name": "Gold necklace"},
        {"page_name": "Ruby necklace"},
        {"page_name": ""},
    ]}))
    assert await client.products_of("Gold bar") == ["Gold necklace", "Ruby necklace"]


async def test_a_material_nothing_is_made_from():
    client = client_over(responds({"bucket": []}))
    assert await client.products_of("Vorkath") == []


async def test_only_the_first_few_titles_are_tried():
    """One Bucket query per title, so the shortlist is walked and not exhausted."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"bucket": []})

    await client_over(handler).first_recipe([f"Page {n}" for n in range(50)], limit=3)
    assert len(calls) == 3


def test_name_variants_cover_the_ways_people_type_an_item():
    """Bucket matches page_name exactly and forgives only case, so a plural or a
    space in a closed-up compound is a total miss -- reported as "no recipe for
    that", which reads as a fact about the item rather than the spelling."""
    assert name_variants("Gold bar")[0] == "Gold bar"
    assert "Gold bar" in name_variants("Gold bars")        # plural
    assert "Swordfish" in name_variants("Sword fish")      # split compound
    assert "swordfish" in name_variants("swordfishes")     # -es plural


def test_two_mistakes_at_once_are_not_covered():
    """"Sword fishes" is a split compound *and* an -es plural, and reaching
    "Swordfish" from it needs six spellings -- every form crossed with every
    spacing. The -s rule cannot be dropped to make room, because "Bones" is
    "Bone" and not "Bon". Six requests on every miss, to serve an input nobody
    has typed, is the wrong trade; one mistake at a time is covered."""
    assert "Swordfish" not in name_variants("Sword fishes")


def test_name_variants_are_capped_and_deduplicated():
    """Each one is a request."""
    assert len(name_variants("Gold bars")) <= 4
    assert name_variants("Shark") == ["Shark"]


async def test_a_typed_plural_finds_the_recipe():
    seen = []

    def handler(request):
        seen.append(request.url.params.get("query", ""))
        # Only the singular has a row, as the live bucket behaves.
        if "'Gold bar'" in seen[-1]:
            return httpx.Response(200, json={"bucket": [
                {"page_name": "Gold bar", "production_json": '{"skills": []}'}]})
        return httpx.Response(200, json={"bucket": []})

    found = await client_over(handler).recipe("Gold bars")
    assert found and found["page_name"] == "Gold bar"


async def test_a_shortlist_title_is_not_expanded():
    """Titles come out of the wiki's own index and are already exact. Expanding
    them would turn first_recipe's six lookups into twenty-four."""
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"bucket": []})

    await client_over(handler).recipe("Gold bars", variants=False)
    assert len(calls) == 1


# -- which quest gates a thing -----------------------------------------------
# Every fixture below is the live wiki's own lead paragraph, because the whole
# question is which shapes of sentence the wiki actually writes.

QUESTS = [
    "Dragon Slayer I", "Dragon Slayer II", "Bone Voyage", "Regicide",
    "Song of the Elves", "The Frozen Door", "Secrets of the North",
    "Priest in Peril", "Recipe for Disaster",
]

VORKATH = (
    "Vorkath is a draconic boss-monster first encountered during the Dragon "
    "Slayer II quest as the penultimate boss. Created by Zorgoth during the "
    "Fourth Age's Dragonkin Conflicts, the blue dragon is one of his subjects."
)
FOSSIL_ISLAND = (
    "Fossil Island is a members-only area located north-east of Morytania. The "
    "island itself is surrounded by seven other smaller islands, though they "
    "are currently inaccessible save for Lithkren, the island north-west that "
    "players visit during the quest Dragon Slayer II. In order to reach the "
    "island, players must have completed the Bone Voyage quest."
)
PRIFDDINAS = (
    "Prifddinas is the city of the elves and the capital city of Tirannwn. In "
    "order to enter the city, the quest Song of the Elves must be completed."
)
HYDRA = (
    "The Alchemical Hydra is a boss version of hydra, found in the lower level "
    "of the Karuulm Slayer Dungeon in Mount Karuulm, requiring level 95 Slayer "
    "to kill."
)


def test_a_quest_named_as_a_gate_is_read_off_the_lead():
    from reldo.bucket import gating_quest

    assert gating_quest(VORKATH, QUESTS) == "Dragon Slayer II"


def test_the_longer_quest_name_wins_over_its_own_prefix():
    """"Dragon Slayer I" and "Dragon Slayer II" are both quests and one is a
    prefix of the other. This case has already been answered "Dragon Slayer I"
    once, from memory, by the model."""
    from reldo.bucket import gating_quest

    assert gating_quest(VORKATH, QUESTS) != "Dragon Slayer I"


def test_a_stated_gate_beats_a_quest_merely_mentioned_first():
    """Fossil Island names Dragon Slayer II a sentence before it names its own
    gate -- and that mention is about a neighbouring island."""
    from reldo.bucket import gating_quest

    assert gating_quest(FOSSIL_ISLAND, QUESTS) == "Bone Voyage"


def test_the_passive_form_is_a_stated_gate_too():
    """"the quest Song of the Elves must be completed" -- same claim as "must
    have completed", one auxiliary apart."""
    from reldo.bucket import gating_quest

    assert gating_quest(PRIFDDINAS, QUESTS) == "Song of the Elves"


def test_a_thing_with_no_quest_gate_reads_as_none():
    """Most bosses are gated by Slayer level or by nothing. None sends the
    question to the model rather than inventing a quest."""
    from reldo.bucket import gating_quest

    assert gating_quest(HYDRA, QUESTS) is None


def test_a_name_that_is_not_a_quest_cannot_come_out():
    """The list is the wiki's. This reads prose but does not parse names out of
    it, which is what stops "the Vorkath Master achievement" becoming an answer."""
    from reldo.bucket import gating_quest

    text = "Vorkath is fought after completing the Vorkath Master achievement."
    assert gating_quest(text, QUESTS) is None
