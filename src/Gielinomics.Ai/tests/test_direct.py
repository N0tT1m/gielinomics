"""Answering exactly, or declining so the model can try.

The contract under test is the three exits. Two of them return None, and the
second of those is the one worth guarding: a handler that runs and finds
nothing must fall through, not report an absence. A misrouted question that
answers "no data for that" has turned a failure to understand somebody into a
statement about Old School RuneScape -- which is what _force_requirements did
when it answered a Defence question off a page stating only Construction.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from reldo.direct import DirectAnswerer
from reldo.intents import Match, Router


class FixedRouter(Router):
    """Routes to whatever the test says, so these exercise handlers not phrasing."""

    def __init__(self, match: Match | None):
        super().__init__(embed=lambda texts: np.zeros((len(texts), 4), dtype=np.float32))
        self._match = match

    def classify(self, question: str) -> Match | None:
        return self._match


def wiki_stating(only_on: str | None = None, **levels):
    """A wiki that states these requirements, optionally on one page only.

    Page-sensitive where it matters, because "marlin" is a question and "Raw
    marlin" is the page -- a stub answering for any name would pass whether or
    not the resolution step ran at all.
    """
    async def requirements(page):
        if only_on is not None and page.lower() != only_on.lower():
            return []
        return list(levels.items())
    return SimpleNamespace(requirements=requirements)


def retriever_returning(*titles):
    async def shortlist(query, *, k=8, pool=20):
        return [SimpleNamespace(title=t, summary="", score=1.0, found_by=()) for t in titles]
    return SimpleNamespace(shortlist=shortlist)


# -- exit 1: nothing recognised ----------------------------------------------


async def test_an_unrouted_question_returns_none():
    assert await DirectAnswerer(FixedRouter(None)).answer("who wrote the music") is None


# -- exit 2: answered exactly -------------------------------------------------


async def test_a_skill_requirement_is_answered_from_the_marked_up_field():
    """Marlin states Fishing 91 and Sailing 78 as identical-looking numbers
    beside identical-looking icons. The attribute says which is which."""
    d = DirectAnswerer(
        FixedRouter(Match("skill_requirement", {"skill": "Sailing", "thing": "marlin"}, 1.0)),
        wiki=wiki_stating("Raw marlin", Fishing=91, Sailing=78),
        retriever=retriever_returning("Raw marlin"),
    )
    found = await d.answer("what sailing level for marlin")
    assert found is not None
    assert found.text == "Raw marlin requires Sailing 78."
    # Recorded, not just rendered: answer_eval's cite and tool-use checks read
    # these, and a fast path that left them empty would make those checks pass
    # vacuously the moment a question routed here.
    assert found.pages_read == ["Raw marlin"]
    assert found.skill_requirements["Sailing"] == 78


async def test_xp_between_needs_no_client_at_all():
    """The XP table is a formula, not a page. It cannot fail and cannot be
    stale, which is why the agent's version of this exists at all."""
    d = DirectAnswerer(
        FixedRouter(Match("xp_between", {"skill": "Slayer", "from_level": 92,
                                         "to_level": 99}, 1.0))
    )
    found = await d.answer("how much xp 92 to 99 slayer")
    assert found.text == "Slayer 92 to 99 is 6,517,178 XP."
    assert found.xp_calculations == ["92->99"]


# -- exit 3: ran, found nothing, fell through --------------------------------


async def test_a_page_that_lacks_the_skill_falls_through():
    """Rune platebody's markup carries Construction and Smithing and no Defence.
    Answering "no Defence requirement" would be a claim about the game; falling
    through is a claim about this lookup, which is the true one."""
    d = DirectAnswerer(
        FixedRouter(Match("skill_requirement",
                          {"skill": "Defence", "thing": "rune platebody"}, 1.0)),
        wiki=wiki_stating(None, Construction=28, Smithing=99),
        retriever=retriever_returning("Rune platebody"),
    )
    assert await d.answer("what defence level for a rune platebody") is None


async def test_a_missing_client_falls_through_rather_than_failing():
    """There is no configuration in which this answers worse than the agent,
    only ones in which it answers less often."""
    d = DirectAnswerer(
        FixedRouter(Match("price", {"item": "abyssal whip"}, 1.0))  # no ge client
    )
    assert await d.answer("how much is a whip worth") is None


async def test_a_source_that_errors_falls_through():
    """A wiki that will not answer is not a fact about the game."""
    from reldo.ge import GEError

    async def boom(name, **kw):
        raise GEError("prices API is down")

    d = DirectAnswerer(
        FixedRouter(Match("price", {"item": "abyssal whip"}, 1.0)),
        ge=SimpleNamespace(lookup=boom),
    )
    assert await d.answer("how much is a whip worth") is None


async def test_an_intent_with_no_handler_falls_through():
    """Adding an intent without its handler must degrade to the model rather
    than raise -- the two are separate edits and one will land first."""
    d = DirectAnswerer(FixedRouter(Match("not_a_real_intent", {}, 1.0)))
    assert await d.answer("anything") is None


# -- answering in the material that was asked about --------------------------


class FakeGuide:
    """A training guide as (heading, text) pairs, for the two calls made of it."""

    def __init__(self, title, sections):
        self._title = title
        self._sections = sections

    async def sections(self, title):
        if title != self._title:
            raise LookupError(title)
        return [
            SimpleNamespace(line=heading, index=str(i))
            for i, (heading, _) in enumerate(self._sections)
        ]

    async def section_text(self, title, index):
        return self._sections[int(index)][1]


def construction_guide():
    """Four real brackets from Construction training, two of them in teak."""
    return FakeGuide(
        "Construction training",
        [
            ("Levels 33–52/74: Oak larders",
             "Oak larders require 8 oak planks to build, and they grant 480 "
             "experience each."),
            ("Levels 52–99: Mahogany furniture",
             "Each mahogany table requires 6 mahogany planks and gives 840 "
             "experience."),
            ("Levels 50–99: Mounted mythical capes",
             "They require 3 teak planks each along with a mythical cape. Each "
             "mounted mythical cape gives 370 experience."),
            ("Levels 66–74/99: Teak garden benches",
             "They require 6 teak planks to build and give 540 experience each."),
        ],
    )


async def test_a_plan_answers_in_the_material_that_was_asked_for():
    """Asked for mahogany planks 52 to 84, the answer came back in teak: 22,929
    teak planks and 7,643 mythical capes, off the bracket with the best XP rate
    rather than the one about the wood in the question."""
    d = DirectAnswerer(
        FixedRouter(Match("training_plan",
                          {"skill": "Construction", "material": "mahogany plank",
                           "from_level": 52, "to_level": 84}, 1.0)),
        wiki=construction_guide(),
    )
    out = (await d.answer("how many mahogany planks for construction 52 to 84")).text
    assert "20,202 mahogany planks" in out  # 3,367 tables x 6
    assert "teak" not in out
    # ...and says it narrowed, so the cheaper teak route being absent is visible
    # rather than silent.
    assert "1 mahogany plank method" in out


async def test_a_plan_naming_no_material_still_lists_every_method():
    d = DirectAnswerer(
        FixedRouter(Match("training_plan",
                          {"skill": "Construction", "from_level": 52,
                           "to_level": 84}, 1.0)),
        wiki=construction_guide(),
    )
    out = (await d.answer("best way to train construction 52 to 84")).text
    assert "4 methods" in out
    assert "teak" in out and "mahogany" in out


async def test_a_material_the_guide_has_no_bracket_for_falls_through():
    """Exit 3. The guide has brackets and none of them are about yew logs, so
    this is the wrong handler rather than an absence of methods."""
    d = DirectAnswerer(
        FixedRouter(Match("training_plan",
                          {"skill": "Construction", "material": "yew log",
                           "from_level": 52, "to_level": 84}, 1.0)),
        wiki=construction_guide(),
    )
    assert await d.answer("how many yew logs for construction 52 to 84") is None


def bucket_returning(recipe):
    async def first_recipe(pages, *, skill=None):
        return None

    async def recipe_from_material(page, skill):
        return recipe

    return SimpleNamespace(
        first_recipe=first_recipe, recipe_from_material=recipe_from_material
    )


async def test_a_count_of_a_material_is_not_answered_with_any_recipe_that_uses_it():
    """"how many planks 37 to 70" came back "8,163 x Rocking chair": the index
    picked one of the hundred things planks build and reported it with a
    straight face. The guide names the object -- tables, because they are the
    fastest -- so a question naming a material asks the guide."""
    d = DirectAnswerer(
        FixedRouter(Match("training_count",
                          {"skill": "Construction", "item": "mahogany planks",
                           "material": "mahogany plank", "from_level": 52,
                           "to_level": 84}, 1.0)),
        wiki=construction_guide(),
        bucket=bucket_returning({
            "page_name": "Rocking chair",
            "skills": [{"name": "Construction", "experience": "350"}],
        }),
        retriever=retriever_returning("Mahogany plank"),
    )
    out = (await d.answer("how many mahogany planks from 52 to 84 construction")).text
    assert "Rocking chair" not in out
    assert "20,202 mahogany planks" in out


# -- and what it costs -------------------------------------------------------


def ge_pricing(**by_name):
    """A GE client that knows the buy price of the given materials."""
    from reldo.ge import Item, Price

    async def exactly(name):
        found = by_name.get(name.replace(" ", "_"))
        if found is None:
            return None
        gp, volume, limit = found
        return Price(
            item=Item(id=1, name=name, limit=limit, high_alch=0, members=True),
            instant_sell=gp, instant_buy=gp, avg_sell=gp, avg_buy=gp, volume=volume,
        )

    return SimpleNamespace(exactly=exactly)


async def test_a_plan_prices_its_own_shopping_list():
    """The half of the question that was answered by inventing a number: "how
    much money will it take" came back as 1,311 sharks against a plank bill it
    never computed. 20,202 planks at 1,876 is a multiplication, so it is done
    in code and not by a 24B model."""
    d = DirectAnswerer(
        FixedRouter(Match("training_plan",
                          {"skill": "Construction", "material": "mahogany plank",
                           "from_level": 52, "to_level": 84}, 1.0)),
        wiki=construction_guide(),
        ge=ge_pricing(mahogany_plank=(1_876, 5_155_802, 13_000)),
    )
    found = await d.answer("how many mahogany planks for construction 52 to 84")
    assert "20,202 mahogany planks" in found.text
    assert "at 1,876 gp each: 37,898,952 gp for the mahogany planks" in found.text
    assert "2 windows to buy that many" in found.text
    # The guide and the price are both on the record, so answer_eval's cite and
    # must_price checks mean the same thing on this path as on the model's.
    assert found.pages_read == ["Construction training"]
    assert found.prices_checked == ["mahogany plank"]
    assert found.xp_calculations == ["52->84"]


async def test_a_plan_without_prices_is_still_the_plan():
    """The materials are the answer and the price is a courtesy. No client, no
    line, same plan."""
    d = DirectAnswerer(
        FixedRouter(Match("training_plan",
                          {"skill": "Construction", "material": "mahogany plank",
                           "from_level": 52, "to_level": 84}, 1.0)),
        wiki=construction_guide(),
    )
    out = (await d.answer("how many mahogany planks for construction 52 to 84")).text
    assert "20,202 mahogany planks" in out
    assert "gp each" not in out


async def test_a_prices_api_that_is_down_does_not_take_the_plan_with_it():
    from reldo.ge import GEError

    async def boom(name):
        raise GEError("prices API is down")

    d = DirectAnswerer(
        FixedRouter(Match("training_plan",
                          {"skill": "Construction", "material": "mahogany plank",
                           "from_level": 52, "to_level": 84}, 1.0)),
        wiki=construction_guide(),
        ge=SimpleNamespace(exactly=boom),
    )
    out = (await d.answer("how many mahogany planks for construction 52 to 84")).text
    assert "20,202 mahogany planks" in out
    assert "gp each" not in out


async def test_each_bracket_is_priced_in_its_own_material():
    """The teak brackets are teak's price and the oak bracket is oak's. One
    price applied to every leg is the same failure as one material."""
    d = DirectAnswerer(
        FixedRouter(Match("training_plan",
                          {"skill": "Construction", "from_level": 52,
                           "to_level": 84}, 1.0)),
        wiki=construction_guide(),
        ge=ge_pricing(
            mahogany_plank=(1_876, 5_155_802, 13_000),
            teak_plank=(749, 1_976_506, 13_000),
            oak_plank=(300, 2_000_000, 13_000),
        ),
    )
    out = (await d.answer("best way to train construction 52 to 84")).text
    assert "at 1,876 gp each" in out   # 3,367 mahogany tables
    assert "at 749 gp each" in out     # 7,643 mounted capes
    assert "at 300 gp each" in out     # oak larders


# -- the question asked, not the question answerable --------------------------


async def test_a_duration_question_with_no_rate_falls_through():
    """"how long does it take to get from 45 to 99 mining at granite rates"
    came back with the XP gap: a true sentence, and not the answer. The rate is
    on a wiki page, which is the model's job."""
    d = DirectAnswerer(
        FixedRouter(Match("xp_between",
                          {"skill": "Mining", "from_level": 45, "to_level": 99,
                           "wants_hours": True, "rate": None}, 1.0))
    )
    assert await d.answer("how long from 45 to 99 mining at granite rates") is None


async def test_a_duration_question_that_states_its_rate_is_answered_in_hours():
    """The rate is in the question, so this is pure arithmetic -- exactly the
    kind the model gets wrong by an order of magnitude."""
    d = DirectAnswerer(
        FixedRouter(Match("xp_between",
                          {"skill": "Fishing", "from_level": 70, "to_level": 99,
                           "wants_hours": True, "rate": 40_000.0}, 1.0))
    )
    found = await d.answer("how many hours from 70 to 99 fishing at 40k xp per hour")
    assert found.text == (
        "Fishing 70 to 99 is 12,296,804 XP, which at 40,000 XP/hour is 307.4 hours."
    )
    assert found.xp_calculations == ["70->99"]


async def test_a_page_that_states_the_skill_but_is_not_about_the_thing_is_skipped():
    """Retrieval put "Money making guide/combat" first for "piety" and that page
    states Prayer 74, so the lookup succeeded and the answer was about something
    else entirely. Stating the skill is not enough to be the answer."""
    d = DirectAnswerer(
        FixedRouter(Match("skill_requirement",
                          {"skill": "Prayer", "thing": "piety"}, 1.0)),
        # The requirement is stated on the money-making guide and nowhere else,
        # which is exactly the situation: no page about piety says Prayer 74.
        wiki=wiki_stating("Money making guide/combat", Prayer=74),
        retriever=retriever_returning("Money making guide/combat"),
    )
    assert await d.answer("what prayer level do I need for piety") is None


async def test_the_right_page_further_down_the_shortlist_still_wins():
    """Skipping the irrelevant hit is only useful if the search continues."""
    d = DirectAnswerer(
        FixedRouter(Match("skill_requirement",
                          {"skill": "Prayer", "thing": "piety"}, 1.0)),
        wiki=wiki_stating("Piety", Prayer=70, Defence=70),
        retriever=retriever_returning("Money making guide/combat", "Piety"),
    )
    found = await d.answer("what prayer level do I need for piety")
    assert found.text == "Piety requires Prayer 70."
    # Spelling is the retriever's or the question's, whichever answered; what
    # matters is that the page on the record is the one the level came off.
    assert [p.lower() for p in found.pages_read] == ["piety"]


async def test_a_shared_word_is_not_enough_to_answer_in_objects():
    """The first guard let "8,080 x Mahogany hull parts" through to a question
    about mahogany planks: it passes a shared-word test on "mahogany" while
    being a different object at 350 XP instead of 840. A material's name is two
    words doing different jobs, and the page has to match both."""
    d = DirectAnswerer(
        FixedRouter(Match("training_count",
                          {"skill": "Construction", "item": "mahogany planks",
                           "material": "mahogany plank", "from_level": 52,
                           "to_level": 84}, 1.0)),
        wiki=construction_guide(),
        bucket=bucket_returning({
            "page_name": "Mahogany hull parts",
            "skills": [{"name": "Construction", "experience": "350"}],
        }),
        retriever=retriever_returning("Mahogany hull parts"),
    )
    out = (await d.answer("how many mahogany planks from 52 to 84 construction")).text
    assert "hull" not in out
    assert "20,202 mahogany planks" in out


async def test_the_material_itself_is_still_answered_as_a_recipe():
    """The strict test must not reject the case it exists to allow: asked for
    gold bars, "Gold bar" is the thing asked for and the count is the answer."""
    d = DirectAnswerer(
        FixedRouter(Match("training_count",
                          {"skill": "Smithing", "item": "gold bars",
                           "material": "gold bar", "from_level": 48,
                           "to_level": 50}, 1.0)),
        bucket=bucket_returning({
            "page_name": "Gold bar",
            "skills": [{"name": "Smithing", "experience": "22.5"}],
        }),
        retriever=retriever_returning("Gold bar"),
    )
    out = (await d.answer("how many gold bars from 48 to 50 smithing")).text
    assert out == "815 x Gold bar, which is 18,319 Smithing XP from 48 to 50 at 22.5 each."


async def test_a_plan_says_what_it_costs_and_what_pays_for_it():
    """The whole question, in one answer: "how many mahogany planks do i need
    ... including how much money it will take and how many sharks I will need to
    sell". It came back as 22,929 teak planks, 7,643 mythical capes and 1,311
    sharks -- the wrong material, an item that is not consumed, and a coin
    figure with no arithmetic connecting it to a bill it never computed."""
    d = DirectAnswerer(
        FixedRouter(Match("training_plan",
                          {"skill": "Construction", "material": "mahogany plank",
                           "funding": "shark", "from_level": 52,
                           "to_level": 84}, 1.0)),
        wiki=construction_guide(),
        ge=ge_pricing(
            mahogany_plank=(1_876, 5_155_802, 13_000),
            shark=(1_000, 3_089_395, 10_000),
        ),
    )
    found = await d.answer(
        "how many mahogany planks 52 to 84 and how many sharks do i sell for it"
    )
    assert "20,202 mahogany planks" in found.text
    assert "at 1,876 gp each: 37,898,952 gp" in found.text
    # ceil(37,898,952 / 980), the net price: selling pays the tax that buying
    # does not, and counting at the gross 1,000 gives 37,899 -- 774 sharks short
    # of the bill.
    assert "38,673 sharks sold at 980 gp each after tax pays for that" in found.text
    assert found.prices_checked == ["mahogany plank", "shark"]


async def test_naming_nothing_to_sell_leaves_the_plan_as_it_was():
    d = DirectAnswerer(
        FixedRouter(Match("training_plan",
                          {"skill": "Construction", "material": "mahogany plank",
                           "from_level": 52, "to_level": 84}, 1.0)),
        wiki=construction_guide(),
        ge=ge_pricing(mahogany_plank=(1_876, 5_155_802, 13_000)),
    )
    out = (await d.answer("how many mahogany planks 52 to 84")).text
    assert "37,898,952 gp" in out
    assert "sold at" not in out


async def test_an_unpriceable_thing_to_sell_costs_only_its_own_line():
    """The plan and the bill are still the answer if the catalogue has never
    heard of what somebody proposes to sell."""
    d = DirectAnswerer(
        FixedRouter(Match("training_plan",
                          {"skill": "Construction", "material": "mahogany plank",
                           "funding": "gnome child", "from_level": 52,
                           "to_level": 84}, 1.0)),
        wiki=construction_guide(),
        ge=ge_pricing(mahogany_plank=(1_876, 5_155_802, 13_000)),
    )
    out = (await d.answer("how many mahogany planks and gnome children to sell")).text
    assert "37,898,952 gp" in out
    assert "sold at" not in out


# -- which quest gates a boss ------------------------------------------------


def wiki_with_lead(**leads):
    """A wiki whose pages have these lead paragraphs, and no others."""
    async def summaries(titles):
        return [
            SimpleNamespace(title=t, summary=leads[t.replace(" ", "_")])
            for t in titles
            if t.replace(" ", "_") in leads
        ]
    return SimpleNamespace(summaries=summaries)


def bucket_with_quests(*names):
    async def quest_names():
        return list(names)
    return SimpleNamespace(quest_names=quest_names)


async def test_the_quest_that_gates_a_boss_is_read_off_the_boss_page():
    """The model answered this by reading a Combat Achievement page: "the
    Vorkath Master achievement requires killing Vorkath 100 times, but it does
    not mention any quest requirements" -- a page that mentions Vorkath
    standing in for the page about him."""
    d = DirectAnswerer(
        FixedRouter(Match("which_quest", {"thing": "Vorkath"}, 1.0)),
        wiki=wiki_with_lead(
            Vorkath="Vorkath is a draconic boss-monster first encountered "
                    "during the Dragon Slayer II quest as the penultimate boss."
        ),
        bucket=bucket_with_quests("Dragon Slayer I", "Dragon Slayer II"),
        retriever=retriever_returning("Vorkath Master", "Vorkath"),
    )
    found = await d.answer("what quest do you need to complete to fight Vorkath")
    assert found.text == "Vorkath requires Dragon Slayer II."
    assert found.pages_read == ["Vorkath"]
    assert found.quests_checked == ["Dragon Slayer II"]


async def test_a_thing_with_no_quest_gate_falls_through():
    """Most bosses are gated by a Slayer level or by nothing at all, and the
    honest answer to "which quest" is not a quest."""
    d = DirectAnswerer(
        FixedRouter(Match("which_quest", {"thing": "Alchemical Hydra"}, 1.0)),
        wiki=wiki_with_lead(
            Alchemical_Hydra="The Alchemical Hydra is a boss version of hydra, "
                             "requiring level 95 Slayer to kill."
        ),
        bucket=bucket_with_quests("Dragon Slayer II", "Bone Voyage"),
        retriever=retriever_returning("Alchemical Hydra"),
    )
    assert await d.answer("what quest do I need for the Alchemical Hydra") is None


# -- a duration for a method the question names ------------------------------


def mining_guide():
    """The two Mining brackets that matter: one names granite, one mentions it."""
    return FakeGuide(
        "Pay-to-play Mining training",
        [
            ("Levels 15–45/70: Iron ore",
             "Players can gain around 45,000–55,000 experience per hour below "
             "level 45. Switch to granite at 45 for faster rates."),
            ("Levels 45–99: Granite",
             "At level 99, the theoretical tick-perfect maximum is approximately "
             "134,000 experience per hour. The table uses an efficient long-term "
             "benchmark of 126,000 experience per hour. Without tick "
             "manipulation, players can gain around 63,000 experience per hour."),
        ],
    )


async def test_a_duration_is_looked_up_for_the_method_the_question_names():
    """"at granite rates" points at a figure on a page rather than supplying
    one. The answer was the XP gap and no hours at all, and before that a
    fabricated 200,000 xp/hr with ~100 hours beside it -- two numbers that
    cannot both be true of a 12,972,919 XP gap."""
    d = DirectAnswerer(
        FixedRouter(Match("xp_between",
                          {"skill": "Mining", "from_level": 45, "to_level": 99,
                           "wants_hours": True, "rate": None,
                           "method": "granite"}, 1.0)),
        wiki=mining_guide(),
    )
    found = await d.answer("how long from 45 to 99 mining at granite rates")
    assert "12,972,919 XP" in found.text
    assert "126,000 XP/hour" in found.text        # the benchmark, not the maximum
    assert "103.0 hours" in found.text            # 12,972,919 / 126,000
    assert "long-term benchmark" in found.text    # which of the four rates it is
    assert found.pages_read == ["Pay-to-play Mining training"]
    assert found.xp_calculations == ["45->99"]


async def test_a_duration_naming_no_method_still_declines():
    """The guide states twelve rates across the brackets overlapping 45 to 99.
    Choosing among them unprompted is the same act as inventing one."""
    d = DirectAnswerer(
        FixedRouter(Match("xp_between",
                          {"skill": "Mining", "from_level": 45, "to_level": 99,
                           "wants_hours": True, "rate": None, "method": ""}, 1.0)),
        wiki=mining_guide(),
    )
    assert await d.answer("how long from 45 to 99 mining") is None


async def test_a_bracket_covering_only_part_of_the_range_is_not_quoted_for_all_of_it():
    """A rate stated for levels 15-70 does not describe 45 to 99, and pairing
    the full gap with it would contradict itself in exactly the way the eval's
    consistency check exists to catch."""
    d = DirectAnswerer(
        FixedRouter(Match("xp_between",
                          {"skill": "Mining", "from_level": 45, "to_level": 99,
                           "wants_hours": True, "rate": None,
                           "method": "iron ore"}, 1.0)),
        wiki=mining_guide(),
    )
    assert await d.answer("how long from 45 to 99 mining at iron ore rates") is None


async def test_a_price_must_be_for_something_the_question_named():
    """find() falls back to substring matching, so a wrong needle reaches items
    with nothing to do with the question -- "it" matches kiteshield and
    adamantite, and the highest-volume of those answered a question about a
    facility bottle."""
    from reldo.ge import Item, Price

    async def lookup(query, **kw):
        return [
            Price(
                item=Item(id=1, name="Adamantite ore", limit=13000, high_alch=0,
                          members=False),
                instant_sell=555, instant_buy=560, avg_sell=555, avg_buy=560,
                volume=3_846_836,
            )
        ]

    d = DirectAnswerer(
        FixedRouter(Match("price", {"item": "facility bottle"}, 1.0)),
        ge=SimpleNamespace(lookup=lookup),
    )
    assert await d.answer("how much is a facility bottle") is None


async def test_a_thing_with_no_quest_says_what_its_page_states_instead():
    """Asked which quest the Alchemical Hydra needs, the model answered "the
    elite Kourend & Kebos Diary" -- not a quest, and not required. Most things
    are gated by a skill or by nothing, and saying so beats falling through."""
    d = DirectAnswerer(
        FixedRouter(Match("which_quest", {"thing": "Alchemical Hydra"}, 1.0)),
        wiki=SimpleNamespace(
            summaries=wiki_with_lead(
                Alchemical_Hydra="The Alchemical Hydra is a boss version of "
                                 "hydra, found in the Karuulm Slayer Dungeon."
            ).summaries,
            requirements=wiki_stating("Alchemical Hydra", Slayer=95).requirements,
        ),
        bucket=bucket_with_quests("Dragon Slayer II", "Bone Voyage"),
        retriever=retriever_returning("Alchemical Hydra"),
    )
    found = await d.answer("what quest is required to fight the Alchemical Hydra")
    # A claim about the page, not about the game: a page stating no quest is
    # weaker evidence than a page stating one, and the wording says so.
    assert found.text == "Alchemical Hydra's page names no quest, only Slayer 95."
    assert found.skill_requirements == {"Slayer": 95}


async def test_a_quest_further_down_the_shortlist_beats_an_earlier_page_without_one():
    """The absence has to be held rather than returned: a candidate naming no
    quest must not outrank a later one that names the real gate."""
    d = DirectAnswerer(
        FixedRouter(Match("which_quest", {"thing": "Vorkath"}, 1.0)),
        wiki=SimpleNamespace(
            summaries=wiki_with_lead(
                Vorkath_Master="The Vorkath Master achievement requires killing "
                               "Vorkath 100 times.",
                Vorkath="Vorkath is first encountered during the Dragon Slayer "
                        "II quest.",
            ).summaries,
            requirements=wiki_stating("Vorkath Master", Slayer=1).requirements,
        ),
        bucket=bucket_with_quests("Dragon Slayer II"),
        retriever=retriever_returning("Vorkath Master", "Vorkath"),
    )
    found = await d.answer("what quest do you need to fight Vorkath")
    assert found.text == "Vorkath requires Dragon Slayer II."


async def test_a_page_stating_neither_a_quest_nor_a_requirement_falls_through():
    """A thin page is not evidence of no gate, and this says nothing rather
    than turning its own silence into an answer."""
    d = DirectAnswerer(
        FixedRouter(Match("which_quest", {"thing": "Kraken"}, 1.0)),
        wiki=SimpleNamespace(
            summaries=wiki_with_lead(Kraken="The Kraken is a boss monster.").summaries,
            requirements=wiki_stating("nothing at all").requirements,
        ),
        bucket=bucket_with_quests("Dragon Slayer II"),
        retriever=retriever_returning("Kraken"),
    )
    assert await d.answer("what quest do I need for the Kraken") is None


# -- the last three cases that leaned on the model ---------------------------


def bucket_with_recipe_on(page, recipe):
    """A bucket whose recipe lives on one page and nowhere else."""
    async def get(item, *, variants=True):
        return recipe if item == page else None
    return SimpleNamespace(recipe=get)


async def test_a_recipe_filed_under_another_name_is_still_found():
    """The bucket has no "Cannonball" row at all -- the recipe is on "Steel
    cannonball" -- and name_variants covers plurals and spacing rather than a
    missing qualifier. Every other handler resolves a loose name through
    retrieval; this was the one that did not."""
    d = DirectAnswerer(
        FixedRouter(Match("recipe", {"item": "cannonballs"}, 1.0)),
        bucket=bucket_with_recipe_on("Steel cannonball", {
            "page_name": "Steel cannonball",
            "skills": [{"name": "Smithing", "level": "35", "experience": "25.6"}],
            "materials": [{"quantity": "1", "name": "Steel bar"}],
            "facilities": "Furnace",
            "tools": "Ammo mould",
            "output": {"quantity": "4"},
        }),
        retriever=retriever_returning("Cannonball", "Steel cannonball"),
    )
    found = await d.answer("how do I make cannonballs")
    # The tool is half the answer and the bucket had it all along: without it
    # this tells somebody to take steel bars to a furnace and stand there.
    assert found.text == (
        "Steel cannonball needs Smithing 35. Made from 1 x Steel bar, at a "
        "Furnace, using an Ammo mould. One of those makes 4."
    )
    assert found.pages_read == ["Steel cannonball"]


async def test_a_recipe_that_resolves_to_something_unrelated_is_not_used():
    d = DirectAnswerer(
        FixedRouter(Match("recipe", {"item": "cannonballs"}, 1.0)),
        bucket=bucket_with_recipe_on("Bronze bar", {"page_name": "Bronze bar"}),
        retriever=retriever_returning("Bronze bar"),
    )
    assert await d.answer("how do I make cannonballs") is None


def ge_ranking(**by_name):
    """A GE client over a fixed catalogue, for ranking without the network."""
    from reldo.ge import Item, Price

    def priced(name, gp, volume):
        return Price(
            item=Item(id=abs(hash(name)) % 9999, name=name, limit=100,
                      high_alch=0, members=True),
            instant_sell=gp, instant_buy=gp, avg_sell=gp, avg_buy=gp,
            volume=volume,
        )

    async def find(query, *, limit=12):
        row = by_name.get(query.replace(" ", "_"))
        return [priced(query, *row).item] if row else []

    async def prices(items):
        return [priced(i.name, *by_name[i.name.replace(" ", "_")]) for i in items]

    return SimpleNamespace(find=find, prices=prices)


async def test_the_best_seller_is_ranked_over_the_whole_product_set():
    """The failure was the set, not the arithmetic. Asked what jewellery made
    from gold bars sells best, the model compared three items it thought of and
    answered Gold necklace -- true of those three, and the bucket lists forty."""
    d = DirectAnswerer(
        FixedRouter(Match("best_seller", {"material": "gold bar"}, 1.0)),
        bucket=SimpleNamespace(products_of=lambda material, **kw: _products()),
        ge=ge_ranking(
            Gold_necklace=(140, 477_272),
            Ruby_necklace=(1_100, 1_461_428),
            Gold_ring=(180, 112_000),
        ),
    )
    found = await d.answer("what jewellery made from gold bars sells best")
    assert "Ruby necklace is the best of these to sell" in found.text
    assert "Gold necklace is the best" not in found.text
    assert found.money_ranked == ["Gold bar"]


async def _products():
    return ["Gold necklace", "Ruby necklace", "Gold ring"]


async def test_one_product_is_not_a_comparison():
    d = DirectAnswerer(
        FixedRouter(Match("best_seller", {"material": "bronze bar"}, 1.0)),
        bucket=SimpleNamespace(products_of=lambda material, **kw: _one()),
        ge=ge_ranking(Bronze_dagger=(20, 5_000)),
    )
    assert await d.answer("what made from bronze bars sells best") is None


async def _one():
    return ["Bronze dagger"]


async def test_a_coin_goal_with_a_method_named_gets_its_hours_too():
    """"9 raw sharks, 29.1 hours" for a 5,000,000 gp goal -- 9 sharks is 6,453
    gp, so the answer contradicted itself by three orders of magnitude. Both
    halves are divisions and neither is the model's to do."""
    from reldo.earnings import Method
    from reldo.ge import Item, Price

    async def lookup(query, **kw):
        return [Price(
            item=Item(id=385, name="Shark", limit=10_000, high_alch=0, members=True),
            instant_sell=1_000, instant_buy=1_000, avg_sell=1_000, avg_buy=1_000,
            volume=3_089_395,
        )]

    async def read_guide(client, title=None):
        return [
            Method("Catching minnows", 259_000, "Fishing", "Skilling (Fishing)"),
            Method("Catching sharks", 110_000, "Fishing", "Skilling (Fishing)"),
            Method("Farming ranarr weed", 300_000, "Farming", "Skilling (Farming)"),
        ]

    import reldo.earnings as earnings
    original, earnings.read_guide = earnings.read_guide, read_guide
    try:
        d = DirectAnswerer(
            FixedRouter(Match("quantity_for_goal",
                              {"goal": 5_000_000, "item": "sharks",
                               "doing": "farming minnows"}, 1.0)),
            ge=SimpleNamespace(lookup=lookup),
            wiki=SimpleNamespace(),
        )
        found = await d.answer("how many sharks for 5 mill farming minnows")
    finally:
        earnings.read_guide = original

    assert "5,103 x Shark" in found.text          # ceil(5,000,000 / 980)
    assert "19.3 hours" in found.text             # 5,000,000 / 259,000
    # Weighted so the rare word decides: "farming" is shared with the ranarr
    # method and "minnow" with one, and an unweighted overlap ties them.
    assert found.pages_read == ["Money making guide/Catching minnows"]


# -- how many of these for one of those --------------------------------------


def wiki_saying(text):
    async def page_text(title):
        return text
    return SimpleNamespace(page_text=page_text)


MINNOW_PAGE = (
    "Minnow can be exchanged for noted raw sharks by trading with Kylie Minnow "
    "at a rate of 40 minnows for one raw shark."
)


async def test_a_swap_between_two_items_is_multiplied_out():
    """The model answered "the wiki does not say how much a shark costs. Ask
    for a price per shark" -- a refusal about a question nobody asked, with the
    rate in the first sentence of the page. A fixed swap is neither a price nor
    a recipe and had no path here at all."""
    d = DirectAnswerer(
        FixedRouter(Match("exchange",
                          {"wanted": "minnows", "per": "sharks",
                           "count": 5_103}, 1.0)),
        wiki=wiki_saying(MINNOW_PAGE),
        retriever=retriever_returning("Minnow"),
    )
    found = await d.answer("how many minnows do i need for 5,103 sharks")
    assert found.text.startswith("204,120 minnows.")   # 5,103 x 40
    assert "Kylie Minnow" in found.text
    assert found.pages_read == ["Minnow"]
    assert found.gp_calculations == ["5103 x 40/1"]


async def test_no_count_asks_for_the_rate_itself():
    d = DirectAnswerer(
        FixedRouter(Match("exchange",
                          {"wanted": "minnows", "per": "shark", "count": 1}, 1.0)),
        wiki=wiki_saying(MINNOW_PAGE),
        retriever=retriever_returning("Minnow"),
    )
    found = await d.answer("how many minnows per shark")
    assert found.text.startswith("40 minnows per shark.")


async def test_a_page_that_states_no_such_swap_falls_through():
    d = DirectAnswerer(
        FixedRouter(Match("exchange",
                          {"wanted": "minnows", "per": "lobsters",
                           "count": 10}, 1.0)),
        wiki=wiki_saying(MINNOW_PAGE),
        retriever=retriever_returning("Minnow"),
    )
    assert await d.answer("how many minnows for 10 lobsters") is None


async def test_holding_a_pile_of_one_thing_prices_the_other():
    """"if i have 200,000 minnows how many sharks does that give me and how
    much money on grand exchange is that" came back "205 sharks, which is
    about 200,000 gp after tax" -- the asker's own number returned as the
    answer to a different question, because parse_goal read 200,000 minnows as
    a 200,000 gp goal. 50,000 minnows is already over a thousand sharks."""
    from reldo.ge import Item, Price

    async def exactly(name):
        assert name == "raw shark", f"priced {name!r}, not what the page gives"
        return Price(
            item=Item(id=383, name="Raw shark", limit=15_000, high_alch=0,
                      members=True),
            instant_sell=710, instant_buy=710, avg_sell=710, avg_buy=710,
            volume=2_229_732,
        )

    d = DirectAnswerer(
        FixedRouter(Match("exchange",
                          {"wanted": "sharks", "per": "minnows",
                           "count": 200_000, "worth": True}, 1.0)),
        wiki=wiki_saying(MINNOW_PAGE),
        ge=SimpleNamespace(exactly=exactly),
        retriever=retriever_returning("Minnow"),
    )
    found = await d.answer("if i have 200,000 minnows how many sharks and how much gp")
    # The page's word for it, not the asker's: Kylie gives noted *raw* sharks
    # at 696 net, and the cooked fish is 980 -- a 41% overstatement.
    assert found.text.startswith("5,000 raw sharks.")
    assert "696 gp each after tax that is 3,480,000 gp" in found.text
    assert found.prices_checked == ["Raw shark"]


# -- a level stated in a table and nowhere in the markup ---------------------


def wiki_with_tables(**pages):
    """A wiki whose pages carry tables and no marked-up requirements."""
    async def requirements(page):
        return []

    async def tables(title, section=None):
        return pages.get(title.replace(" ", "_").replace("'", ""), [])

    return SimpleNamespace(requirements=requirements, tables=tables)


GARDEN_TABLE = [
    ["Thieving Level", "Season", "# of sq'irks"],
    ["N/A", "Winter", "5"],
    ["25", "Spring", "4"],
    ["45", "Autumn", "3"],
    ["65", "Summer", "2"],
]


async def test_a_level_only_in_a_table_is_still_answered():
    """Sorceress's Garden marks up no requirements at all -- requirements()
    returns [] -- so this declined and the model answered from memory. The four
    Thieving levels are in a table."""
    d = DirectAnswerer(
        FixedRouter(Match("skill_requirement",
                          {"skill": "Thieving", "thing": "sorceress's garden"}, 1.0)),
        wiki=wiki_with_tables(Sorceresss_Garden=[GARDEN_TABLE]),
        retriever=retriever_returning("Sorceress's Garden"),
    )
    found = await d.answer("what thieving level for sorceress's garden")
    # Every level, not the largest. A table of gardens is a table of
    # alternatives, and the markup path's take-the-max rule would answer 65 to
    # somebody who can walk in today.
    assert "Spring 25" in found.text
    assert "Autumn 45" in found.text and "Summer 65" in found.text
    # And the row with no level is the answer that you need none.
    assert "Winter states no level" in found.text
    assert found.pages_read == ["Sorceress's Garden"]


async def test_a_table_about_something_else_is_not_used():
    """The page has to be about the thing, same as the markup path."""
    d = DirectAnswerer(
        FixedRouter(Match("skill_requirement",
                          {"skill": "Thieving", "thing": "blast furnace"}, 1.0)),
        wiki=wiki_with_tables(Sorceresss_Garden=[GARDEN_TABLE]),
        retriever=retriever_returning("Sorceress's Garden"),
    )
    assert await d.answer("what thieving level for the blast furnace") is None


async def test_a_page_with_neither_markup_nor_a_level_table_falls_through():
    d = DirectAnswerer(
        FixedRouter(Match("skill_requirement",
                          {"skill": "Thieving", "thing": "sorceress's garden"}, 1.0)),
        wiki=wiki_with_tables(Sorceresss_Garden=[[["Season"], ["Winter"]]]),
        retriever=retriever_returning("Sorceress's Garden"),
    )
    assert await d.answer("what thieving level for sorceress's garden") is None
