"""What a question is asking for, decided before the model sees it.

The agent answers everything by handing a 24B model some tools and then running
fourteen enforcement passes to catch what it got wrong. Those passes share one
shape -- *recognise the question, compute the answer in code, hand it over and
say do not change it* -- and the recognising half is already written:
``_ASKS_REQUIREMENTS``, ``_quantity_request``, ``_list_request``,
``_ASKS_BEST_MONEY``, ``parse_goal`` and the rest are an intent classifier
wearing a different hat. They run *after* the model, as corrections, rather than
*before* it, as routing.

The observation that started this module: nineteen of the twenty-eight
answer-eval questions of the time already matched one of those rules, and most
of the remaining nine were a price lookup or a hiscores lookup. So for the
overwhelming majority of what anybody asks about Old School RuneScape there is
an exact answer available in code, and the model's only real contribution is
deciding -- badly, slowly, and with a safety net -- which of them to fetch.

Measured since, over the thirty-four cases there are now: twenty-six are
answered here and eight go to the model. The gap between nineteen and twenty-six
is not the router getting greedier. Four of those eight are questions it used to
take and now refuses, and the cases it gained are ones it learned to stop
answering wrongly -- which is the whole content of this module's history.

**Why this is tractable here and not in general.** The domain is bounded and the
wiki is unusually well structured: 48 Bucket tables publish items, monsters,
quests, recipes, drops, shops, locations and money-making methods as *rows*.
Most OSRS questions are lookups against data somebody has already curated. That
is not true of open-domain question answering and it is why this is worth doing.

**Two ways to recognise a question, and both are needed.**

* A **regex** is exact and brittle. "how many yew logs from 60 to 99 fletching"
  carries its slots in its punctuation, and a pattern reads them with no
  ambiguity and no model. It also fails completely on a rewording.
* An **embedding** is robust to phrasing and vague about slots. It knows "what
  do I need to catch marlin" and "marlin fishing level requirement?" are the
  same question, and it cannot tell you which skill was named.

So: the regex decides when it fires, the embedding decides when it does not, and
the slots always come from the regex. An intent with no readable slots is not a
match, however close the phrasing -- there is nothing to look up.

**An unrecognised question returns None**, and the caller falls through to the
model. That is the whole safety property. A router that forces every question
into its nearest intent answers confidently about something nobody asked, which
is precisely the failure this project keeps finding.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .money import parse_goal
from .skills import MAX_LEVEL, SKILLS
from .training import singular

log = logging.getLogger(__name__)

# Cosine similarity a question must reach before the nearest intent counts as a
# match. Measured rather than guessed, and the first guess was wrong: real
# questions score 0.58-0.75 against their own intent, not the 0.68+ I assumed,
# so "what slayer level do I need to kill abyssal demons" -- a textbook skill
# requirement, ranked first, slots read cleanly -- was being handed to the model
# at 0.60.
#
# The floor is deliberately low because it is not what does the discriminating.
# The intent must also be able to READ ITS ARGUMENTS out of the question, and
# that is a far sharper test: a price question with no item in it and a level
# question with no skill in it both fail it outright, whatever they score.
MATCH_THRESHOLD = 0.55

_SKILL_NAMES = "|".join(re.escape(s.lower()) for s in SKILLS)

# "45 to 99", "from 60-99", "92 until 99".
_LEVEL_RANGE = re.compile(
    r"(?:from\s+)?\b(\d{1,3})\b\s*(?:to|-|–|until)\s*\b(\d{1,3})\b", re.I
)
_MENTIONS_LEVEL = re.compile(r"\blevels?\b|\blvls?\b", re.I)


def named_skill(text: str) -> str:
    """The one skill a question names, or "" for none or several.

    Word boundaries with an optional -ing, never a substring: "crafting" is
    inside "runecrafting", and answering a Runecraft question with Crafting's
    numbers is invisible to every check downstream. Several skills is a
    comparison, where no single one is the subject.
    """
    low = text.lower()
    found = [
        skill
        for skill in SKILLS
        if re.search(rf"\b{re.escape(skill.lower())}(?:ing|s)?\b", low)
    ]
    return found[0] if len(found) == 1 else ""


def level_range(text: str) -> tuple[int, int] | None:
    """A plausible from/to level pair, or None.

    Bounded by MAX_LEVEL and required to ascend, which rejects the false
    positives a bare number range produces -- a year, a price, a drop rate.
    """
    found = _LEVEL_RANGE.search(text)
    if not found:
        return None
    low, high = int(found.group(1)), int(found.group(2))
    return (low, high) if 1 <= low < high <= MAX_LEVEL else None


# Any number that could be a level, with what follows it, so a quantity can be
# told from a level. "5 mill" and "500 gp" are not levels; a bare 40 next to a
# skill name is.
_LOOSE_LEVEL = re.compile(r"\b(\d{1,3})\b\s*(k\b|m\b|gp\b|xp\b|hours?\b|%)?", re.I)


def level_span(text: str) -> tuple[int, int] | None:
    """The lowest and highest levels a question mentions, however far apart.

    level_range() wants the pair adjacent -- "from 45 to 99" -- and a plan
    question does not oblige: "how many oak planks from 40 then the planks for
    the other levels to get to 70" puts eleven words between its two levels, so
    the adjacent form finds nothing and the question falls to the model.

    Anything carrying a unit is skipped, which is what stops "5 mill" and "500
    gp" being read as levels. Two distinct values are required, ascending: one
    number is a target and not a span.
    """
    seen: list[int] = []
    for value, unit in _LOOSE_LEVEL.findall(text):
        if unit:
            continue
        number = int(value)
        if 1 <= number <= MAX_LEVEL:
            seen.append(number)
    if len(set(seen)) < 2:
        return None
    low, high = min(seen), max(seen)
    return (low, high) if low < high else None


# The thing a question is about, once the question words are stripped off. Kept
# deliberately dumb: it feeds a wiki lookup that will fail cleanly on a bad
# name, and guessing harder here buys less than letting the lookup say no.
_SUBJECT_NOISE = re.compile(
    r"^(?:what|which|how much|how many|how long|how do i|do i|can i|is|are|the|a|an|"
    r"level|lvl|do|does|need|needed|require[sd]?|to|for|make|making|craft|get|"
    r"you|i|my|osrs|in|on|at|with)\b",
    re.I,
)
_TRAILING_NOISE = re.compile(
    r"\b(?:in|on|for|at|to|from)?\s*(?:osrs|old school runescape|oldschool|rs)\s*\??$",
    re.I,
)


def subject(text: str, *, drop: tuple[str, ...] = ()) -> str:
    """What the question is about, as a wiki page name might spell it.

    Strips the interrogative scaffolding off the front and the "in osrs" off the
    back, then removes any words the intent knows are its own -- a skill name in
    a level question is part of the question, not part of the subject.
    """
    cleaned = " ".join(text.strip().rstrip("?").split())
    cleaned = _TRAILING_NOISE.sub("", cleaned).strip()
    for word in drop:
        cleaned = re.sub(rf"\b{re.escape(word)}\b", " ", cleaned, flags=re.I)
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = _SUBJECT_NOISE.sub("", cleaned).strip()
    return " ".join(cleaned.split())


# The head nouns a training material actually has, as a closed list, because
# "how many mahogany planks", "how many hours" and "how many levels" are the
# same sentence and only one of them names a thing you buy.
#
# Closed rather than clever: an unlisted material reads as no material, which
# leaves the answer exactly as wide as it was before this existed. A *wrong*
# material would narrow it to the wrong brackets, which is the failure being
# fixed, so the list only grows on evidence.
_MATERIAL_NOUN = (
    "plank|log|bar|ore|essence|leather|hide|seed|sapling|bone|fibre|fiber|bark|"
    "rune|gem|glass|sand|herb|potion|feather|arrow|shaft|dart tip|cannonball|"
    "nail|thread|karambwan|fish|coal|clay|wire|orb|net"
)
_NAMED_MATERIAL = re.compile(
    rf"\b((?:[a-z']+\s+){{0,2}}?(?:{_MATERIAL_NOUN})s?)\b", re.I
)

# Words that sit where a material's adjective sits without being one. "how many
# mahogany planks" carries its count word right up against the material.
_NOT_A_MODIFIER = frozenset({
    "how", "many", "much", "more", "the", "a", "an", "some", "my", "your", "of",
    "and", "or", "for", "to", "do", "i", "need", "needed", "get", "buy", "use",
    "using", "with", "in", "at", "am", "is", "are", "it", "from", "about",
    "sell", "sells", "selling", "buying", "cover", "pay", "fund",
})


# "how many sharks will I need to sell", "selling sharks to pay for it". The
# item somebody proposes to *fund* a grind with, which is a different role from
# the material they propose to spend it on -- one question can name both, and
# the mahogany question that started all this named both in one sentence.
_FUNDED_BY = re.compile(
    r"\bhow many\s+([a-z'][a-z' ]{1,30}?)\s+"
    r"(?:i\s+|you\s+)?(?:will\s+|would\s+|do\s+i\s+|should\s+i\s+)?"
    r"(?:need\s+to\s+|have\s+to\s+|must\s+)?sell\b"
    r"|\bsell(?:ing)?\s+([a-z'][a-z' ]{1,30}?)\s+(?:to|for)\s+(?:pay|fund|afford|cover|buy)",
    re.I,
)


def named_funding(text: str) -> str:
    """The item a question proposes to sell to pay for the thing it asks about.

    "how many sharks I will need to sell on the grand exchange" -> "shark".
    Empty when the question names none, which is most of them.
    """
    found = _FUNDED_BY.search(text)
    if not found:
        return ""
    raw = next((g for g in found.groups() if g), "")
    words = [w for w in raw.lower().split() if w not in _NOT_A_MODIFIER]
    if not words:
        return ""
    words[-1] = singular(words[-1])
    return " ".join(words)


# "at granite rates", "at the Motherlode Mine", "doing gem rocks". The method a
# duration question names, which is the only thing that makes the question
# answerable: the Mining guide states twelve rates for 45-99 and picking one
# without being told which is the same act as inventing it.
_NAMED_METHOD = re.compile(
    r"\bat\s+(?:the\s+)?([a-z][a-z' ]{2,28}?)\s+rates?\b"
    r"|\b(?:doing|training (?:at|with|on)|at)\s+(?:the\s+)?([a-z][a-z' ]{2,28}?)\s*\??$",
    re.I,
)


def named_method(text: str) -> str:
    """The training method a question names, or "" if it names none."""
    found = _NAMED_METHOD.search(text.strip())
    if not found:
        return ""
    raw = next((g for g in found.groups() if g), "")
    words = [w for w in raw.lower().split() if w not in _NOT_A_MODIFIER]
    return " ".join(words)


def named_material(text: str) -> str:
    """The material a question names, singular-ish and stripped of scaffolding.

    "how many mahogany planks do i need" -> "mahogany planks"; "how many planks
    from 37 to 70" -> "planks", which is a real answer and a wider one. Returns
    "" when the question names none, and "" means every method, unchanged.
    """
    found = _NAMED_MATERIAL.search(text)
    if not found:
        return ""
    words = [w for w in found.group(1).lower().split() if w not in _NOT_A_MODIFIER]
    if not words:
        return ""
    # Singular, because this is quoted back in prose the asker reads -- "2
    # mahogany plank methods" rather than "2 mahogany planks methods". The
    # matching itself does not care either way.
    words[-1] = singular(words[-1])
    return " ".join(words)


# -- slot readers ------------------------------------------------------------
# Each returns the arguments its answerer needs, or None when the question turns
# out not to be this after all. Returning None is a real answer: it sends the
# question to the next intent, or to the model.


# What the level is *for*, which is whatever follows the verb the question ends
# its frame with. "...do I need to catch marlin" -> marlin; "...for an abyssal
# whip" -> abyssal whip. Reading it here rather than with subject() because the
# scaffolding sits in the middle of these questions, not only at the front.
_FOR_THE_THING = re.compile(
    r"\b(?:to|for)\s+"
    r"(?:wear|wield|use|equip|kill|catch|cut|mine|fish|cast|enter|make|cook|"
    r"craft|smith|fletch|brew|smelt|do|access|fight)?\s*"
    r"(?:\b(?:a|an|the)\s+)?(.+?)\s*$",
    re.I,
)


# "what is the fastest way to train mining from level 45" names a skill, says
# "level", and is not a requirement question at all -- it is asking what to do,
# which is prose in a guide and the model's job. Read as a requirement it
# resolved to the guide page itself and answered "Pay-to-play mining training
# requires Mining 72", which is true of that page and no answer to the question.
_ASKS_HOW_TO_TRAIN = re.compile(
    r"\b(?:fastest|quickest|best|good|efficient)\s+way\b|\btrain(?:ing)?\b|"
    r"\bhow (?:do i|should i|to)\s+(?:get|train|level)\b",
    re.I,
)


def _slots_skill_requirement(q: str) -> dict | None:
    """"what sailing level do i need to catch marlin" -> skill + thing."""
    skill = named_skill(q)
    if not skill or not _MENTIONS_LEVEL.search(q):
        return None
    if _ASKS_HOW_TO_TRAIN.search(q):
        return None
    found = _FOR_THE_THING.search(q.rstrip(" ?"))
    thing = " ".join(found.group(1).split()) if found else ""
    if not thing:
        thing = subject(q, drop=(skill, "wear", "wield", "use"))
    return {"skill": skill, "thing": thing} if thing else None


def _slots_training_count(q: str) -> dict | None:
    """"how many yew logs from 60 to 99 fletching" -> item + levels."""
    if not re.search(r"how (?:many|much)\b", q, re.I):
        return None
    levels = level_range(q)
    if not levels:
        return None
    skill = named_skill(q)
    if not skill and not _MENTIONS_LEVEL.search(q):
        return None
    # What is being made, which is what the count is a count *of*. Without it
    # the handler has a gap and no per-action rate, so it answers the XP and
    # not the question: "how much gold do i need" came back "18,319 XP".
    found = re.search(
        r"how (?:many|much)\s+(.+?)\s+(?:do|does|will|to|from|are|is|needed|need)\b",
        q, re.I,
    )
    item = " ".join(found.group(1).split()) if found else ""
    return {
        "skill": skill, "item": item, "material": named_material(q),
        "funding": named_funding(q),
        "from_level": levels[0], "to_level": levels[1],
    }


# A plan asks across brackets rather than for one number: "then", "other
# levels", "after that", "best way", "plan". A single-item count -- "how many
# yew logs from 60 to 99" -- is training_count and answers in one line.
_WANTS_A_PLAN = re.compile(
    r"\bthen\b|\bafter (?:that|which)\b|\bother levels?\b|\bplan\b|"
    r"\bbest way\b|\bwhat should i\b|\broute\b|\ball the way\b",
    re.I,
)


def _slots_training_plan(q: str) -> dict | None:
    """A skill and a level range, for a plan that spans several brackets.

    "how many oak planks from 40 then the planks for the other levels to get to
    70" is two questions with a switch point in the middle, and no single
    recipe answers it -- oak larders stop being the method at 52. The guides
    are written in exactly these brackets, so the answer is to read them.
    """
    skill = named_skill(q)
    if not skill or not _WANTS_A_PLAN.search(q):
        return None
    # The span, not the adjacent pair: a plan question scatters its levels.
    levels = level_range(q) or level_span(q)
    if not levels:
        return None
    return {
        "skill": skill, "material": named_material(q),
        "funding": named_funding(q),
        "from_level": levels[0], "to_level": levels[1],
    }


# "at 40k xp per hour", "if I get 126,000 experience an hour", "at 200k xp/hr".
# The k suffix is not optional decoration: everybody writes rates that way, and
# reading "40k" as forty turns a 307-hour answer into 307,000 of them.
_STATED_RATE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(k|m)?\s*(?:xp|exp|experience)\s*"
    r"(?:per|/|an|a)\s*(?:hour|hr)\b",
    re.I,
)
_RATE_MULTIPLIER = {"k": 1_000, "m": 1_000_000}

# "how long does it take", "how many hours". A different question from "how much
# XP", and the difference is not cosmetic: the gap is arithmetic and the
# duration needs a rate, which only the wiki or the asker can supply.
_ASKS_DURATION = re.compile(r"\bhow long\b|\bhours?\b|\bhow much time\b", re.I)


def stated_rate(text: str) -> float | None:
    """The XP-per-hour rate a question supplies, or None if it supplies none."""
    found = _STATED_RATE.search(text)
    if not found:
        return None
    value = float(found.group(1).replace(",", ""))
    if found.group(2):
        value *= _RATE_MULTIPLIER[found.group(2).lower()]
    return value or None


def _slots_xp_between(q: str) -> dict | None:
    """"how much xp from 92 to 99 slayer", "how long from 45 to 99 mining".

    Carries whether a duration was asked for and what rate, if any, the question
    named. Both belong here rather than in the handler: this is where the
    question's own words are read, and "if I get 40k xp per hour" was sitting in
    them unread while the answer came back as the XP gap.
    """
    levels = level_range(q)
    if not levels:
        return None
    return {
        "skill": named_skill(q),
        "from_level": levels[0],
        "to_level": levels[1],
        "wants_hours": bool(_ASKS_DURATION.search(q)),
        "rate": stated_rate(q),
        "method": named_method(q),
    }


# "how much is X worth", "what does X sell for", "if I sell a X how much do I
# get". The item sits between the frame's two halves, so it is read out rather
# than left behind by removing everything else.
_PRICED = re.compile(
    r"how much (?:is|are|does|do)\s+(?:\b(?:a|an|the)\s+)?(.+?)\s*(?:worth|cost|sell|go for)"
    r"|what (?:is|are|does)\s+(?:\b(?:a|an|the)\s+)?(.+?)\s*(?:worth|cost|sell|go for)"
    r"|(?:if i )?sell(?:ing)?\s+(?:\b(?:a|an|the)\s+)?(.+?)\s*(?:,|\bhow\b|\bon\b|$)"
    r"|(?:price|value) of\s+(?:\b(?:a|an|the)\s+)?(.+?)\s*$",
    re.I,
)


# A pronoun is not an item, and treating one as an item is not a near miss. "or
# sell it" gave the price handler "it", the catalogue matched every item with
# those two letters inside it -- kiteshield, adamantite -- and "should i keep my
# facility bottle or sell it" came back "Adamantite ore is worth about 555 gp".
# The thing being asked about is right there in the question; the frame just
# grabbed the wrong half of it.
# The abbreviations go in for the same reason: "to sell on the GE" leaves "GE"
# behind, which is a place to sell things and not a thing.
_PRONOUN = frozenset({
    "it", "them", "they", "these", "those", "this", "that", "one", "ones",
    "mine", "some", "any", "stuff", "thing", "things",
    "ge", "grand exchange", "exchange", "market",
})


# What the frame leaves stuck to the front of the item. "sell my abyssal whip"
# and "sell on the GE" both come out of the pattern above with the item's name
# preceded by a word that is not part of it.
_NOT_THE_ITEM = re.compile(
    r"^(?:my|your|his|her|their|our|a|an|the|on|in|at|for|to|of|some)\b\s*", re.I
)


def _clean_item(thing: str) -> str:
    """An item name with the frame's leftovers stripped off the front."""
    cleaned = " ".join(thing.split())
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = _NOT_THE_ITEM.sub("", cleaned).strip()
    return "" if cleaned.lower() in _PRONOUN else cleaned


# "is sandstone or granite better to sell on the GE" is a comparison, and this
# intent prices one thing. Tested against the question rather than against the
# extracted name, because the extraction drops "or" as frame wording -- "keep my
# bottle or sell it" is not a comparison and needs that word gone.
_COMPARES_TWO = re.compile(
    r"\bor\b[^?]{0,40}\b(?:better|best|more|higher)\b"
    r"|\b(?:better|best)\b[^?]{0,40}\bor\b",
    re.I,
)


def _slots_price(q: str) -> dict | None:
    if _COMPARES_TWO.search(q):
        return None
    found = _PRICED.search(q.rstrip(" ?"))
    thing = _clean_item(next((g for g in found.groups() if g), "")) if found else ""
    if not thing:
        # Second chance rather than a decline: "should i keep my facility bottle
        # or sell it" names the bottle plainly, and the frame reading "sell it"
        # is a fact about the frame.
        thing = _clean_item(subject(q, drop=(
            "keep", "keeping", "kept", "sell", "selling", "sold", "should",
            "worth", "it", "still", "just", "now", "or", "instead",
        )))
    # "is sandstone or granite better to sell" is a comparison and this intent
    # prices one thing. Answering it with either half is answering half a
    # question, and the agent has a comparison path that does it properly.
    if not thing or re.search(r"\bor\b", thing, re.I):
        return None
    return {"item": thing}


# "how many sharks do i need to get 5 mill" -- the thing being counted, read
# out of the frame rather than left behind by stripping everything else.
# subject() returned "how sharks do i to 5 mill and how long will that take
# farming minnows" for that question, which is not an item and looks up as
# nothing, so the case fell to the model and the model invented nine sharks.
_COUNTED = re.compile(
    r"how (?:many|much)\s+(.+?)\s+(?:do|does|will|would|can|to|for|are|is|need)\b",
    re.I,
)

# "and how long will that take farming minnows" -- what the asker proposes to
# spend the time on. Scoped to the clause, because the question names two
# fishes: sharks are what is being sold and minnows are what is being caught,
# and matching the method against the whole sentence picks whichever comes
# first.
_TAKES_DOING = re.compile(
    r"how long[^?]*?\b(?:take|takes|taking)\s+(?:you\s+)?(.+?)\s*\??$", re.I
)


def _slots_quantity_for_goal(q: str) -> dict | None:
    goal = parse_goal(q)
    if not goal:
        return None
    found = _COUNTED.search(q)
    thing = " ".join(found.group(1).split()) if found else ""
    if not thing:
        thing = subject(q, drop=("many", "much", "need", "get", "sell", "selling",
                                 "gp", "gold", "coins", "off", "from", "ge"))
    doing = _TAKES_DOING.search(q)
    return {
        "goal": goal,
        "item": thing,
        "doing": " ".join(doing.group(1).split()) if doing else "",
    } if thing else None


# "what jewellery made from gold bars sells best on the Grand Exchange", "which
# item made from magic logs is worth most". The material is the only slot: what
# it makes is the bucket's answer, not the asker's, and that is the whole point
# -- the model compared three things it thought of where the recipe bucket lists
# forty, and the best of the forty was not among them.
_MADE_FROM = re.compile(
    r"\bmade (?:from|of|with)\s+(?:a\s+|an\s+|the\s+)?([a-z][a-z' ]{2,28}?)"
    r"\s*(?:sells?|sold|is|are|worth|best|most|on\b|for\b|$)",
    re.I,
)


def _slots_best_seller(q: str) -> dict | None:
    """The material a "what made from X sells best" question names."""
    if not re.search(r"\bsells? (?:best|the most|for the most)\b|\bworth (?:the )?most\b"
                     r"|\bbest to sell\b|\bmost profitable\b", q, re.I):
        return None
    found = _MADE_FROM.search(q)
    if not found:
        return None
    words = [w for w in found.group(1).lower().split() if w not in _NOT_A_MODIFIER]
    if not words:
        return None
    # Singular, because the recipe bucket matches page_name exactly: forty
    # products come back for "gold bar" and none at all for "gold bars".
    words[-1] = singular(words[-1])
    return {"material": " ".join(words)}


# "how many minnows do I need for 5,103 sharks", "how many minnows per shark".
# A fixed swap between two items, which is neither a price nor a recipe: Kylie
# Minnow does not sell anything, she trades 40 for 1. Asked this, the model
# answered "the wiki does not say how much a shark costs" -- a fact about a
# question nobody had asked.
_PER_OTHER = re.compile(
    r"how many\s+([a-z][a-z' ]{2,24}?)\s+"
    r"(?:do (?:i|you) need\s+|to get\s+|to make\s+)?"
    r"(?:for|per|to)\s+(?:a\s+|an\s+|each\s+)?([\d,]*)\s*([a-z][a-z' ]{2,24}?)\s*\??$",
    re.I,
)


# The same swap asked from the other end: "if i have 200,000 minnows how many
# sharks does that give me". The count belongs to what they hold rather than to
# what they want, which is the only difference and inverts the division.
_HOLDING = re.compile(
    r"(?:if\s+)?(?:i|you)\s+(?:have|had|got|catch|caught)\s+([\d,]+)\s+"
    r"([a-z][a-z' ]{2,24}?)[,.]?\s+how many\s+([a-z][a-z' ]{2,24}?)\b",
    re.I,
)

# "...and how much money on grand exchange is that". A second question stapled
# to the first, and answerable off the same count.
_ALSO_WORTH = re.compile(
    r"\bhow much (?:money|gp|gold|is that worth)\b|\bworth\b|\bgrand exchange\b|\bge\b",
    re.I,
)


def _slots_exchange(q: str) -> dict | None:
    """``wanted`` per ``count`` of ``per``, for a stated item-for-item rate."""
    holding = _HOLDING.search(q.strip())
    if holding:
        count, held, wanted = holding.groups()
        held = " ".join(w for w in held.lower().split() if w not in _NOT_A_MODIFIER)
        wanted = " ".join(w for w in wanted.lower().split() if w not in _NOT_A_MODIFIER)
        if not held or not wanted or held == wanted:
            return None
        return {
            "wanted": wanted,
            "per": held,
            "count": int(count.replace(",", "")),
            "worth": bool(_ALSO_WORTH.search(q)),
        }
    found = _PER_OTHER.search(q.strip())
    if not found:
        return None
    wanted, count, other = found.groups()
    wanted = " ".join(w for w in wanted.lower().split() if w not in _NOT_A_MODIFIER)
    other = " ".join(w for w in other.lower().split() if w not in _NOT_A_MODIFIER)
    if not wanted or not other or wanted == other:
        return None
    return {
        "wanted": wanted,
        "per": other,
        # No number is "how many per one", which is the rate itself.
        "count": int(count.replace(",", "")) if count.strip() else 1,
        "worth": bool(_ALSO_WORTH.search(q)),
    }


def _slots_unlocks(q: str) -> dict | None:
    skill = named_skill(q)
    if not skill:
        return None
    found = re.search(r"level\s*(\d{1,3})|(\d{1,3})\s*(?:and|or)\s*(?:below|under)", q, re.I)
    if not found:
        return None
    level = int(found.group(1) or found.group(2))
    return {"skill": skill, "level": level} if 1 <= level <= MAX_LEVEL else None


def _slots_recipe(q: str) -> dict | None:
    thing = subject(q, drop=("make", "making", "craft", "crafting", "cook",
                             "cooking", "smith", "smithing", "smelt", "smelting",
                             "brew", "brewing", "fletch", "fletching", "create"))
    return {"item": thing} if thing else None


def _slots_quest_requirements(q: str) -> dict | None:
    # Cannot require the word "quest" -- "what do I need to start Dragon Slayer
    # II" does not say it. So decline the shape it otherwise swallows whole: a
    # training question has a subject too, and "fastest way to train mining from
    # level 45" is not the name of a quest.
    if _ASKS_HOW_TO_TRAIN.search(q):
        return None
    thing = subject(q, drop=("start", "begin", "do", "complete", "quest",
                             "requirements", "requirement", "prerequisites",
                             "ready", "eligible"))
    return {"quest": thing} if thing else None


def _slots_which_quest(q: str) -> dict | None:
    """"what quest do you need to complete to fight Vorkath" -> the gated thing.

    The mirror of quest_requirements and easy to confuse with it. That one takes
    a quest and returns its requirements; this takes a *boss or an area* and
    returns the quest gating it. Routed apart because the subject is a different
    kind of thing and the lookup goes the other way.
    """
    if not re.search(r"\bquest\b", q, re.I):
        return None
    # The verb is the asker's, not the subject's: "enter Prifddinas" and "fight
    # Vorkath" are both a place or a boss with a word stuck to the front, and
    # retrieval given the pair answers about the Prifddinas Agility Course.
    thing = subject(q, drop=("quest", "complete", "completed", "unlock", "unlocks",
                             "fight", "access", "before", "required", "which",
                             "enter", "entering", "reach", "visit", "start",
                             "kill", "killing", "into"))
    return {"thing": thing} if thing else None


def _slots_player(q: str) -> dict | None:
    """"what is the total level of the player Lynx Titan" -> a username.

    The wiki has articles about NPCs with player-like names, so a player
    question that guesses wrong answers confidently about a monster. Requires
    the question to say so.
    """
    if not re.search(r"\bplayer\b|\baccount\b|\bhiscores?\b|\bmy stats\b", q, re.I):
        return None
    found = re.search(
        r"(?:player|account|user)\s+([A-Za-z0-9][\w \-]{0,11})", q, re.I
    )
    name = " ".join(found.group(1).split()) if found else ""
    return {"player": name} if name else None


# "what jewellery made from gold bars sells best on the Grand Exchange" is a
# comparison over *items* -- which of these things is worth the most to sell --
# and the money-making guide ranks *methods*. Answering it from the guide came
# back "Thieving ... Pickpocketing elves", which is the best money maker and not
# a piece of jewellery, to somebody who had named the material.
_COMPARES_ITEMS = re.compile(
    r"\bsells? (?:best|the most|for the most)\b|\bmade (?:from|of|with)\b|"
    r"\bwhich .{0,20}\b(?:is|are) worth\b",
    re.I,
)


def _slots_money_methods(q: str) -> dict | None:
    if _COMPARES_ITEMS.search(q):
        return None
    return {"skill": named_skill(q)}


def _slots_drops(q: str) -> dict | None:
    thing = subject(q, drop=("drop", "drops", "dropped", "by", "loot"))
    return {"monster": thing} if thing else None


def _slots_location(q: str) -> dict | None:
    thing = subject(q, drop=("where", "located", "location", "find", "found"))
    return {"place": thing} if thing else None


@dataclass(frozen=True, slots=True)
class Intent:
    """One shape of question, and how to read its arguments out."""

    name: str
    # Embedded once and compared against the question. Several phrasings per
    # intent because one sentence is a narrow target -- these are what makes a
    # reworded question route at all.
    phrasings: tuple[str, ...]
    slots: Callable[[str], dict | None]
    # A pattern that, when it matches, decides the intent outright. Precision
    # over the embedding: "how many X from A to B" is unambiguous and should
    # never lose a similarity contest to a neighbouring intent.
    gate: re.Pattern | None = None


INTENTS: tuple[Intent, ...] = (
    Intent(
        name="skill_requirement",
        phrasings=(
            "what level do I need to use this item",
            "what skill level is required to wear this",
            "what sailing level do i need to catch marlin",
            "minimum level requirement for this equipment",
            "what agility level for this course",
        ),
        slots=_slots_skill_requirement,
        # "what <skill> level do I need" is not ambiguous, and leaving it to the
        # embedding let a shared noun decide instead: "what attack level do I
        # need for an abyssal whip" routed to price at 0.75, because a price
        # phrasing also mentioned an abyssal whip. Topic is not intent.
        gate=re.compile(r"\b(?:what|which)\b[^?]{0,30}\blevels?\b[^?]{0,20}"
                        r"\b(?:need|require[sd]?|to)\b", re.I),
    ),
    Intent(
        name="training_count",
        phrasings=(
            "how many of these do I need to make to reach a level",
            "how much material to train from one level to another",
            "how many yew logs from 60 to 99 fletching",
        ),
        slots=_slots_training_count,
        # Not hours, and not experience. "how much xp from 92 to 99" is the
        # gap itself and belongs to xp_between; this intent is a count of
        # *things*, which is the gap divided by what one of them gives.
        gate=re.compile(
            r"how (?:many|much)\b(?![^?]*\b(?:hours?|long|xp|exp|experience)\b)", re.I
        ),
    ),
    Intent(
        name="training_plan",
        phrasings=(
            "what should I train from one level to the next and then after that",
            "how many of each material all the way to the target level",
            "training plan from forty to seventy",
            "what do I make first and what do I switch to",
        ),
        slots=_slots_training_plan,
    ),
    Intent(
        name="xp_between",
        phrasings=(
            "how much experience between these two levels",
            "how long does it take to get from one level to another",
            "how many hours to reach 99 at this rate",
        ),
        slots=_slots_xp_between,
    ),
    Intent(
        name="exchange",
        phrasings=(
            "how many of these do I need for one of those",
            "how many minnows for a shark",
            "what is the exchange rate between these two items",
        ),
        slots=_slots_exchange,
        # Ahead of training_count's gate, which takes "how many X" outright and
        # would read this as a question about levels it does not have.
        gate=re.compile(
            r"how many\s+[a-z' ]+\s+(?:do (?:i|you) need\s+)?(?:for|per)\s+"
            r"[\d, ]*[a-z' ]+\??$"
            r"|(?:if\s+)?(?:i|you)\s+(?:have|had|got|catch|caught)\s+[\d,]+\s+"
            r"[a-z' ]+\s*,?\s*how many\b",
            re.I,
        ),
    ),
    Intent(
        name="best_seller",
        phrasings=(
            "what item made from this material sells best",
            "which of the things made from this is worth the most to sell",
            "what jewellery made from gold bars sells best on the grand exchange",
        ),
        slots=_slots_best_seller,
        # Unambiguous in words, and it has to beat both price and money_methods:
        # it names a material like a price question and asks what earns most
        # like a money question, and it is neither.
        gate=re.compile(
            r"\bmade (?:from|of|with)\b[^?]{0,40}\b(?:sells?|worth|best|most)\b",
            re.I,
        ),
    ),
    Intent(
        name="price",
        phrasings=(
            "how much is this item worth on the grand exchange",
            "what does this sell for right now",
            "what do I actually get if I sell one after tax",
            "is this or that better to sell",
            "how much is an abyssal whip worth right now",
            "what is the current price of this",
            "if I sell one how much do I actually receive",
        ),
        slots=_slots_price,
        gate=re.compile(r"\b(?:worth|sells? for|go(?:es)? for|current price|"
                        r"price of|how much (?:is|are|do i (?:actually )?get))\b", re.I),
    ),
    Intent(
        name="quantity_for_goal",
        phrasings=(
            "how many do I need to sell to make five million gp",
            "how many of these to reach a coin goal",
        ),
        slots=_slots_quantity_for_goal,
    ),
    Intent(
        name="unlocks",
        phrasings=(
            "what can I build at this level",
            "everything I can make up to level thirty",
            "what does this skill unlock at level twenty",
        ),
        slots=_slots_unlocks,
    ),
    Intent(
        name="recipe",
        phrasings=(
            "how do I make this item",
            "what does it take to craft this",
            "what materials does this need",
        ),
        slots=_slots_recipe,
    ),
    Intent(
        name="quest_requirements",
        phrasings=(
            "what do I need to start this quest",
            "what are the requirements for this quest",
            "am I ready to do this quest",
        ),
        slots=_slots_quest_requirements,
    ),
    Intent(
        name="which_quest",
        phrasings=(
            "what quest do you need to complete to fight this boss",
            "which quest unlocks this",
            "what quest is required before I can do this",
        ),
        slots=_slots_which_quest,
    ),
    Intent(
        name="player_stats",
        phrasings=(
            "what is the total level of this player",
            "what are this account's stats on the hiscores",
            "what combat level is this player",
        ),
        slots=_slots_player,
    ),
    Intent(
        name="money_methods",
        phrasings=(
            "what skill makes the most money",
            "best money making method",
            "what is the most profitable thing to do",
        ),
        slots=_slots_money_methods,
    ),
    Intent(
        name="drops",
        phrasings=(
            "what does this monster drop",
            "what drops this item",
            "drop table for this boss",
        ),
        slots=_slots_drops,
    ),
    Intent(
        name="location",
        phrasings=(
            "where is this place",
            "where can I find this monster",
            "how do I get to this location",
        ),
        slots=_slots_location,
    ),
)


@dataclass(frozen=True, slots=True)
class Match:
    """A routed question: which intent, its arguments, and how sure."""

    intent: str
    slots: dict
    score: float
    by: str = "embedding"  # or "gate"

    @property
    def certain(self) -> bool:
        return self.by == "gate"


@dataclass
class Router:
    """Classify an OSRS question, or decline to.

    Args:
        embed: takes a list of strings, returns a matrix of vectors. Defaults to
            the project's own embedder, so this adds no dependency and no second
            model -- the same nomic-embed-text that already serves retrieval.
        threshold: minimum cosine similarity for an embedding match.
    """

    embed: Callable[[list[str]], np.ndarray]
    threshold: float = MATCH_THRESHOLD
    intents: tuple[Intent, ...] = INTENTS
    _vectors: np.ndarray | None = field(default=None, init=False, repr=False)
    _owner: list[int] = field(default_factory=list, init=False, repr=False)

    def _prepare(self) -> None:
        """Embed every phrasing once, remembering which intent each belongs to."""
        if self._vectors is not None:
            return
        texts: list[str] = []
        for index, intent in enumerate(self.intents):
            for phrase in intent.phrasings:
                texts.append(phrase)
                self._owner.append(index)
        self._vectors = _normalise(self.embed(texts))

    def classify(self, question: str) -> Match | None:
        """The intent this question is asking for, or None to let the model have it.

        A gate wins outright when it fires and its slots read. Otherwise the
        nearest phrasing decides, provided it clears the threshold *and* the
        intent can actually read its arguments -- a question that looks like a
        price question but names no item has nothing to look up, so it falls
        through rather than being answered about nothing.
        """
        asked = " ".join(question.split())
        if not asked:
            return None

        for intent in self.intents:
            if intent.gate and intent.gate.search(asked):
                slots = intent.slots(asked)
                if slots is not None:
                    return Match(intent.name, slots, 1.0, by="gate")

        self._prepare()
        assert self._vectors is not None
        similarity = self._vectors @ _normalise(self.embed([asked]))[0]

        # Best intent, not best phrasing: several phrasings of one intent should
        # reinforce it rather than compete with each other.
        best: dict[int, float] = {}
        for position, score in enumerate(similarity):
            owner = self._owner[position]
            best[owner] = max(best.get(owner, -1.0), float(score))

        for index, score in sorted(best.items(), key=lambda kv: -kv[1]):
            if score < self.threshold:
                break
            intent = self.intents[index]
            slots = intent.slots(asked)
            if slots is not None:
                return Match(intent.name, slots, score)
            # Close on phrasing and missing its arguments. Try the next intent
            # rather than give up: "how much is a whip worth" is near both the
            # price and the quantity-for-goal phrasings, and only one of them
            # can read what it needs.
        return None


def _normalise(vectors: np.ndarray) -> np.ndarray:
    if vectors.ndim == 1:
        vectors = vectors[None, :]
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


def router_for(settings) -> Router:
    """A Router using whichever embedder the rest of the project is using."""
    from .index import embed_texts

    model = (
        settings.ollama_embed_model
        if settings.embed_backend == "ollama"
        else settings.local_embed_model
    )

    def embed(texts: list[str]) -> np.ndarray:
        return embed_texts(texts, model, settings.embed_backend, settings.ollama_api_url)

    return Router(embed=embed)
