"""Routing an OSRS question to the code that answers it exactly.

The property that matters most is the one about *declining*. A router that
forces every question into its nearest intent answers confidently about
something nobody asked, which is the failure this whole project keeps finding --
so an unrecognised question must return None and fall through to the model.

Embeddings are stubbed here. The real embedder needs a GPU and these tests are
about the routing rules, not about nomic-embed-text.
"""

from __future__ import annotations

import zlib

import numpy as np
import pytest

from reldo.intents import INTENTS, Router, level_range, named_skill, subject

DIMS = 128


def _fake_embed():
    """A hashed bag-of-words embedder: cosine similarity that means something.

    Hashed into a fixed width rather than a per-call vocabulary, because the
    phrasings and the question are embedded in separate calls and a vocabulary
    built per call gives them different dimensionalities.

    crc32 rather than hash(): Python randomises str hashing per process unless
    PYTHONHASHSEED is set, so hash() would make these tests pass or fail
    depending on the run. A flaky test about a flaky router is not a test.

    Real vectors would make these a measurement of nomic-embed-text rather than
    of the routing rules, and would need the GPU box that fell over twice while
    this was being written.
    """
    def embed(texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), DIMS), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in text.lower().split():
                out[row, zlib.crc32(word.encode()) % DIMS] += 1.0
        return out
    return embed


def router() -> Router:
    return Router(embed=_fake_embed())


# -- the slot readers, which are what actually discriminate ------------------


def test_one_named_skill_is_read_and_two_are_not():
    """Two skills is a comparison, where neither is the subject."""
    assert named_skill("what sailing level for marlin") == "Sailing"
    assert named_skill("is sailing or fishing better for marlin") == ""
    assert named_skill("how much is a whip worth") == ""


def test_runecrafting_is_not_crafting():
    """Word boundaries, never substrings -- 'crafting' is inside 'runecrafting'
    and answering a Runecraft question with Crafting's numbers is invisible."""
    assert named_skill("how many runes from 1 to 50 runecrafting") == "Runecraft"


def test_a_level_range_must_ascend_and_be_plausible():
    assert level_range("from 45 to 99 mining") == (45, 99)
    assert level_range("how much damage from 20 to 30") == (20, 30)
    assert level_range("worth 2000 to 3000 gp") is None   # past MAX_LEVEL
    assert level_range("from 99 to 45") is None            # descends


def test_subject_strips_the_question_frame():
    assert subject("what is the abyssal whip in osrs") == "abyssal whip"
    assert subject("how do i make cannonballs") == "cannonballs"


# -- routing -----------------------------------------------------------------


def test_a_skill_level_question_routes_with_its_slots():
    match = router().classify("what sailing level do i need to catch marlin")
    assert match is not None
    assert match.intent == "skill_requirement"
    assert match.slots == {"skill": "Sailing", "thing": "marlin"}


def test_an_article_is_not_eaten_off_the_noun():
    """(?:a|an|the)? without word boundaries matched the 'a' of 'abyssal', and
    the lookup went out for 'byssal demons'."""
    match = router().classify("what slayer level do I need to kill abyssal demons")
    assert match is not None and match.slots["thing"] == "abyssal demons"

    match = router().classify("what attack level do I need for an abyssal whip")
    assert match is not None and match.slots["thing"] == "abyssal whip"


def test_a_gate_beats_a_topic_match():
    """The embedding matches on topic, not question shape: with 'abyssal whip'
    in a price phrasing, every abyssal-whip question drifted to price. The gate
    settles the shapes that are unambiguous in words."""
    match = router().classify("what attack level do I need for an abyssal whip")
    assert match is not None
    assert match.intent == "skill_requirement"
    assert match.certain


def test_a_count_question_is_not_an_xp_question():
    """'how much xp from 92 to 99' is the gap; 'how much gold to smelt from 48
    to 50' is the gap divided by what one bar gives. Different answers."""
    r = router()
    counted = r.classify("how much gold do i need to smelt to go from 48 to 50 smithing")
    assert counted is not None and counted.intent == "training_count"


def test_an_unrecognised_question_declines():
    """The safety property. None means the model gets it, which is right --
    forcing it into the nearest intent is how you answer the wrong question."""
    r = router()
    for question in (
        "who composed the old school runescape soundtrack",
        "what is the lore behind the elven civil war",
        "should I play an ironman or a main",
        "hello",
        "",
    ):
        assert r.classify(question) is None, question


def test_an_intent_that_cannot_read_its_slots_is_not_a_match():
    """Close on phrasing and missing its arguments is not a match: there is
    nothing to look up. It falls to the next intent, or to the model."""
    # Names no skill, so skill_requirement cannot fire however it scores.
    assert router().classify("what level do I need") is None


def test_every_intent_has_phrasings_and_a_slot_reader():
    for intent in INTENTS:
        assert intent.phrasings, intent.name
        assert callable(intent.slots), intent.name


def test_a_plan_question_reads_levels_that_are_far_apart():
    """level_range() wants them adjacent and a plan question does not oblige:
    "from 40 then the planks for the other levels to get to 70" puts eleven
    words between its two levels.

    Against the slot reader rather than through classify(), because this intent
    has no gate and the stubbed embedder cannot score phrasing -- asserting it
    end to end would be a test of nomic-embed-text wearing a rules test's name.
    """
    from reldo.intents import _slots_training_plan

    assert _slots_training_plan(
        "how many oak planks from 40 then the planks for the other levels "
        "to get to 70 construction"
    ) == {
        "skill": "Construction", "material": "oak plank", "funding": "",
        "from_level": 40, "to_level": 70,
    }


def test_a_plan_needs_more_than_a_skill_and_a_range():
    """Without a plan word it is a count, and training_count answers it in one
    line rather than listing every bracket the guide has."""
    from reldo.intents import _slots_training_plan

    assert _slots_training_plan("how many yew logs from 60 to 99 fletching") is None


def test_a_single_item_count_is_not_a_plan():
    """"how many yew logs from 60 to 99 fletching" answers in one line."""
    m = router().classify("how many yew logs from 60 to 99 fletching")
    assert m is not None and m.intent == "training_count"


def test_a_quantity_with_units_is_not_a_level_span():
    """"5 mill" and "500 gp" are not levels, and reading them as one would
    plan a Fishing route from level 5."""
    from reldo.intents import level_span
    assert level_span("how many sharks do i need to get 5 mill and how long") is None
    assert level_span("how much is 500 gp worth") is None


# -- the material a question names -------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        # The question this came from. Naming the wood is the whole point of
        # naming it, and it was being read as decoration.
        ("how many mahogany planks do i need to get my construction from 52 to 84",
         "mahogany plank"),
        # A kind rather than a wood, which is a wider question and a real one.
        ("how many planks from 37 to 70 construction", "plank"),
        ("how many oak planks from 40 then the planks for the other levels to "
         "get to 70", "oak plank"),
        ("how many yew logs from 60 to 99 fletching", "yew log"),
        ("how many law runes to get from 45 to 55 magic", "law rune"),
    ],
)
def test_reads_the_material_a_question_names(question, expected):
    from reldo.intents import named_material

    assert named_material(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        # "how many X" where X is not a thing you buy. These are the sentences a
        # material reader has to leave alone, because a wrong material narrows
        # the answer to the wrong brackets -- worse than not narrowing at all.
        "how many hours from 45 to 99 mining",
        "how much xp from 92 to 99 slayer",
        "how much gold do i need to smelt from 48 to 50",
        "whats the best way to train construction from 52 to 84",
    ],
)
def test_names_no_material_when_the_question_names_none(question):
    from reldo.intents import named_material

    assert named_material(question) == ""


def test_a_count_question_carries_its_material_too():
    """training_count answers in one line and falls back to the guide when no
    single recipe covers the range -- and the fallback needs to know the
    question said mahogany."""
    from reldo.intents import _slots_training_count

    slots = _slots_training_count(
        "how many mahogany planks do i need to get my construction from 52 to 84"
    )
    assert slots["material"] == "mahogany plank"
    assert (slots["from_level"], slots["to_level"]) == (52, 84)
    assert slots["skill"] == "Construction"


# -- what the router must refuse to take -------------------------------------
# Each of these routed somewhere confident and wrong, and a confident wrong
# answer is worse than a slow right one: it returns before any of the agent's
# enforcement passes can see it.


@pytest.mark.parametrize(
    "question",
    [
        "what is the fastest way to train mining from level 45",
        "what is the best way to train construction from level 52",
        "how do i train slayer at level 60",
    ],
)
def test_a_question_about_how_to_train_is_not_a_requirement_question(question):
    """It names a skill, it says "level", and it is asking what to do. Read as
    a requirement it resolved to the guide page and answered "Pay-to-play mining
    training requires Mining 72" -- true of that page, no answer to this."""
    from reldo.intents import _slots_quest_requirements, _slots_skill_requirement

    assert _slots_skill_requirement(question) is None
    # ...and not a quest either. quest_requirements cannot demand the word
    # "quest" -- "what do I need to start Dragon Slayer II" does not say it --
    # so it has to refuse this shape by name.
    assert _slots_quest_requirements(question) is None


def test_a_real_requirement_question_still_reads_its_slots():
    from reldo.intents import _slots_skill_requirement

    assert _slots_skill_requirement("what sailing level do i need to catch marlin") == {
        "skill": "Sailing", "thing": "marlin",
    }


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("how many hours from 70 to 99 fishing if I get 40k xp per hour?", 40_000),
        ("at 126,000 experience an hour how long is 92 to 99", 126_000),
        ("200k xp/hr from 45 to 99", 200_000),
        ("1.5m xp per hour", 1_500_000),
    ],
)
def test_a_rate_the_question_states_is_read_out_of_it(question, expected):
    """"if I get 40k xp per hour" was sitting in the question unread while the
    answer came back as the XP gap. The k is not decoration -- reading it as
    forty turns 307 hours into 307,000 of them."""
    from reldo.intents import stated_rate

    assert stated_rate(question) == expected


def test_no_rate_stated_reads_as_no_rate():
    from reldo.intents import stated_rate

    assert stated_rate("how long does it take to get from 45 to 99 mining") is None
    assert stated_rate("granite is 60,000 gp per hour") is None


def test_a_duration_question_is_marked_as_one():
    """"how much xp" and "how long" are different questions over the same two
    levels, and only one of them can be answered without a rate."""
    from reldo.intents import _slots_xp_between

    duration = _slots_xp_between("how long does it take to get from 45 to 99 mining")
    assert duration["wants_hours"] and duration["rate"] is None

    quantity = _slots_xp_between("how much xp do I need to go from 92 to 99 slayer")
    assert not quantity["wants_hours"]

    supplied = _slots_xp_between("how many hours from 70 to 99 fishing at 40k xp per hour")
    assert supplied["wants_hours"] and supplied["rate"] == 40_000


def test_a_comparison_over_items_is_not_a_money_making_question():
    """"what jewellery made from gold bars sells best" names a material and
    wants items ranked. The guide ranks methods, and answering from it came
    back "Thieving ... Pickpocketing elves"."""
    from reldo.intents import _slots_money_methods

    assert _slots_money_methods(
        "what jewellery made from gold bars sells best on the Grand Exchange"
    ) is None
    assert _slots_money_methods("what skill makes the most money") == {"skill": ""}


# -- a pronoun is not an item -------------------------------------------------


def test_a_pronoun_does_not_become_the_item():
    """"or sell it" handed the price handler "it", the catalogue matched every
    item with those two letters inside it, and "should i keep my facility bottle
    or sell it" came back "Adamantite ore is worth about 555 gp"."""
    from reldo.intents import _slots_price

    assert _slots_price("should i keep my facility bottle or sell it") == {
        "item": "facility bottle"
    }


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("should i sell my abyssal whip", "abyssal whip"),
        ("how much is an abyssal whip worth right now?", "abyssal whip"),
        ("if I sell a lobster on the GE, how much do I actually get?", "lobster"),
        ("what is the price of a dragon pickaxe", "dragon pickaxe"),
    ],
)
def test_the_frame_s_leftovers_are_not_part_of_the_name(question, expected):
    """"sell my abyssal whip" and "sell on the GE" both come out of the pattern
    with a word stuck to the front that is not part of the item."""
    from reldo.intents import _slots_price

    assert _slots_price(question) == {"item": expected}


@pytest.mark.parametrize(
    "question",
    [
        "is sandstone or granite better to sell on the GE?",
        "how much is sandstone or granite worth",
        "which is better, a whip or a tentacle",
    ],
)
def test_a_comparison_is_not_a_price_lookup(question):
    """This intent prices one thing, and answering a comparison with either half
    answers half a question. The agent has a comparison path that does it
    properly."""
    from reldo.intents import _slots_price

    assert _slots_price(question) is None


# -- the three cases that were still leaning on the model ---------------------


def test_the_thing_being_counted_is_read_out_of_the_frame():
    """subject() returned "how sharks do i to 5 mill and how long will that take
    farming minnows" for this, which is not an item, looks up as nothing, and
    sent the question to the model -- which invented nine sharks for a five
    million gp goal."""
    from reldo.intents import _slots_quantity_for_goal

    slots = _slots_quantity_for_goal(
        "how many sharks do i need to get 5 mill and how long will that take "
        "farming minnows?"
    )
    assert slots["item"] == "sharks"
    assert slots["goal"] == 5_000_000
    # Scoped to its own clause, because the question names two fishes and the
    # other one is what is being sold.
    assert slots["doing"] == "farming minnows"


def test_a_goal_with_no_method_named_carries_no_method():
    from reldo.intents import _slots_quantity_for_goal

    slots = _slots_quantity_for_goal("how many sharks do i need to get 5 mill")
    assert slots["item"] == "sharks" and slots["doing"] == ""


def test_a_best_seller_question_reads_its_material_singular():
    """The recipe bucket matches page_name exactly: forty products come back
    for "gold bar" and none at all for "gold bars"."""
    from reldo.intents import _slots_best_seller

    assert _slots_best_seller(
        "what jewellery made from gold bars sells best on the Grand Exchange"
    ) == {"material": "gold bar"}


@pytest.mark.parametrize(
    "question",
    [
        "what do you make from gold bars",          # not asking what sells best
        "what skill makes the most money",          # no material named
        "how much is a gold bar worth",             # a price, one item
    ],
)
def test_a_question_that_is_not_about_ranking_products_declines(question):
    from reldo.intents import _slots_best_seller

    assert _slots_best_seller(question) is None


# -- a swap between two items ------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("How many minnows do I need for 5,103 sharks?",
         {"wanted": "minnows", "per": "sharks", "count": 5103, "worth": False}),
        ("how many minnows per shark",
         {"wanted": "minnows", "per": "shark", "count": 1, "worth": False}),
        ("how many minnows for a shark",
         {"wanted": "minnows", "per": "shark", "count": 1, "worth": False}),
        # The same swap from the other end, where the count belongs to what
        # they hold rather than to what they want -- which inverts the division.
        ("if i have 200,000 minnows how many sharks does that give me and how "
         "much money on grand exchange is that",
         {"wanted": "sharks", "per": "minnows", "count": 200_000, "worth": True}),
    ],
)
def test_an_exchange_question_reads_both_items_and_the_count(question, expected):
    """No number is "how many per one", which is the rate itself. 5,103 is a
    count of sharks and not a coin goal, which is what parse_goal would have
    made of it."""
    from reldo.intents import _slots_exchange

    assert _slots_exchange(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "how many yew logs from 60 to 99 fletching",   # a training count
        "how many sharks do i need to get 5 mill",     # a coin goal
        "how many minnows for minnows",                # the same thing twice
    ],
)
def test_an_exchange_does_not_take_the_questions_next_door(question):
    from reldo.intents import _slots_exchange

    assert _slots_exchange(question) is None
