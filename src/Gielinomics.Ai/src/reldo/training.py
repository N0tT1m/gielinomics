"""Training plans: the XP gap in code, the materials from the wiki's own tables.

"I am at 27,473 Construction XP and I want 70 -- what do I need?" is two
questions wearing one coat. The first half is arithmetic and belongs in
:mod:`reldo.skills`, exact and free. The second half is a fact about the game
that changes with every update, lives in a table, and must come from the wiki.

Asked the second half unaided, the agent answered "the wiki does not give the
XP per Oak plank". The Oak plank page gives it in a table -- ``Armour stand |
Construction 55 | 500 xp | 8 x Oak plank`` -- and ``read_wiki_page`` cannot see
tables, so the honest-sounding refusal was a fact about the extractor rather
than about the wiki. The training guides are better still: their bracket tables
carry ``Levels | XP needed | Object | # for goal | Planks required`` outright.

So this module finds the brackets that overlap the range asked for and hands
back the guide's own rows. It computes the XP gap and it computes material
counts when the guide states an XP-per-object figure; it never invents a rate.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from .skills import (
    MAX_LEVEL,
    SKILLS,
    hours_needed,
    level_at_xp,
    xp_between,
    xp_for_level,
)

log = logging.getLogger(__name__)

_SKILL_WORD = "|".join(re.escape(skill) for skill in SKILLS)

# "Levels 33-52/74: Oak larders", "Levels 1-99: Mahogany Homes", "Levels 30-99
# Fishing crane repair". Hyphen, en dash and em dash all appear; the "/74" form
# means the bracket runs to 52 or, if you keep going, to 74.
_BRACKET = re.compile(
    r"levels?\s*(\d{1,3})\s*[-‐-―−]\s*(\d{1,3})(?:\s*/\s*(\d{1,3}))?",
    re.I,
)

# A rate stated two ways, because the guides state it two ways: led by a verb --
# "grant 480 experience each", "granting 265.5 experience", "gives 90 xp",
# "grants 600 Construction experience" -- or by what one of them is for, as in
# "55.5 experience per cast" and "1 experience per headless arrow fletched".
#
# The optional word before "experience" is a skill name and only a skill name.
# Anything looser reads "gives 5 million experience" as five, and the guides name
# the skill often enough that leaving it out costs whole brackets: oak doors
# state their rate exactly once, as "grants 600 Construction experience".
#
# Neither form may end in a unit of time. "gives up to 144,600 experience per
# hour" is the same sentence as "gives 840 experience" up to the last two words,
# and read as a per-action figure it turns a 2.8M XP plan into twenty actions.
_NOT_A_RATE = r"(?!\s*(?:per|an|each)\s+(?:hour|hr\b|day|week|minute))"
_XP_EACH = re.compile(
    r"(?:"
    r"(?:grants?|granting|gives?|awards?|yields?|worth)\s+(?:up to\s+)?"
    rf"([\d,]+(?:\.\d+)?)\s*(?:(?:{_SKILL_WORD})\s+)?(?:experience|xp)\b{_NOT_A_RATE}"
    r"|"
    rf"([\d,]+(?:\.\d+)?)\s*(?:(?:{_SKILL_WORD})\s+)?(?:experience|xp)\s+per\s+"
    r"(?!hour|hr\b|day|week|minute)"
    r")",
    re.I,
)

# Above this, a stated figure is a quest reward or a whole-bracket total rather
# than one action's worth. "The Knight's Sword grants 12,725 experience" sits in
# the Smithing guide's 1-39 bracket, and dividing a level range by it gives an
# action count of one.
MAX_XP_PER_ACTION = 10_000

# Small counts are spelled out about as often as they are written in digits --
# "requires two redwood logs", "requires one piece of Celastrus bark" -- and a
# pattern that reads only digits drops those sections without saying so.
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# No single build, cast or craft consumes more than this many of one material.
# The cap is what tells a per-action cost from a whole-range total wearing the
# same grammar: the Runecraft guide's "requires 792,400 pure essence and yields
# 1,584,800 nature runes" is the figure for levels 91-99, and multiplying it by
# the action count is how a plan comes to ask somebody for a billion essence.
MAX_PER_ACTION = 100

# "require 8 oak planks to build", "requires 6 mahogany planks and gives 840
# experience", "crafted from 2 jute fibre each". The count and the material
# both, because "8 planks" and "8 oak planks" are different shopping lists.
#
# The trailing clause is the load-bearing half and it is a filter, not
# decoration. A guide states counts of three different kinds in one voice --
# per action, per hour, and per level bracket -- so the pattern only accepts a
# number that is followed by something making it per-action: what it builds,
# "each", "per <a thing that is not a unit of time>", or the XP that one of them
# gives. Without that last form the Construction guide's own "Each mahogany
# table requires 6 mahogany planks and gives 840 experience" reads as no
# material at all, which is how a mahogany plan came back with no planks in it.
_MATERIAL_EACH = re.compile(
    r"(?:requires?|uses?|needs?|takes?|consumes?|"
    r"(?:made|crafted|created|built|fletched|smithed)\s+(?:from|with|out of))\s+"
    rf"(\d[\d,]*|{'|'.join(_WORD_NUMBERS)})\s+"
    # "one piece of Celastrus bark" -- the unit is not the material.
    r"(?:(?:pieces?|sets?|units?|portions?)\s+of\s+)?"
    # A skill name here means a level requirement, not a shopping list: "requires
    # 70 Construction to build" fits this grammar exactly and is not 70 of
    # anything.
    rf"(?!(?:{_SKILL_WORD})\b)([a-z][a-z' ]{{2,30}}?)s?"
    r"(?:"
    r"\s+to\s+(?:build|make|construct|craft|create|fletch|smith|cook|brew|string)"
    r"|\s+each\b"
    r"|\s+per\s+(?!hour|hr\b|day|week|minute|second|tick|inventory|trip|round|run\b|game|load)"
    r"|,?\s+(?:and\s+)?(?:it\s+|they\s+)?(?:gives?|grants?|awards?|yields?)\s+"
    rf"[\d,.]+\s+(?:(?:{_SKILL_WORD})\s+)?(?:experience|xp)\b"
    r")",
    re.I,
)

# Words that carry no identity, so that a question about "mahogany planks" and a
# section about "6 mahogany planks" are compared on the two words that matter.
_FILLER = frozenset({"a", "an", "the", "of", "and", "or", "some", "x"})


def guide_titles(skill: str) -> list[str]:
    """Candidate training-guide page titles for a skill, best first.

    The wiki is inconsistent here and both forms are live: Construction's guide
    is at "Construction training", Mining's at "Pay-to-play Mining training".
    Trying the plain form first and falling through costs one lookup and stops a
    missing page from reading as a missing guide.
    """
    name = skill.strip().capitalize()
    return [
        f"{name} training",
        f"Pay-to-play {name} training",
        f"Free-to-play {name} training",
    ]


def parse_bracket(heading: str) -> tuple[int, int] | None:
    """The level range a guide section covers, or None if it names none.

    Takes the *widest* endpoint from the "52/74" form. The narrower one is a
    switch point to a different method, not the end of this one, so a plan
    filtered on the narrow reading silently drops the section that covers 60.
    """
    match = _BRACKET.search(heading)
    if not match:
        return None
    low = int(match.group(1))
    high = max(int(match.group(2)), int(match.group(3) or 0))
    if not 1 <= low <= MAX_LEVEL or not low <= high <= MAX_LEVEL:
        return None
    return low, high


def overlaps(bracket: tuple[int, int], from_level: int, to_level: int) -> bool:
    """Does a guide bracket cover any part of the range being trained?

    Inclusive at both ends: a bracket starting exactly at the target level is
    still worth showing, because the target is where the next decision happens.
    """
    low, high = bracket
    return low <= to_level and high >= from_level


def xp_per_action(text: str) -> float | None:
    """The XP-per-action figure a guide section states, if it states one."""
    match = _XP_EACH.search(text)
    if not match:
        return None
    value = float((match.group(1) or match.group(2)).replace(",", ""))
    return value if 0 < value <= MAX_XP_PER_ACTION else None


def materials_per_action(text: str) -> tuple[int, str] | None:
    """``(count, material)`` a section says each action consumes.

    ``None`` covers both "the guide does not say" and "the guide says something
    this cannot be sure is per-action", and the two are deliberately not
    distinguished: a count that is really a total for the whole bracket is worse
    than no count, because it is multiplied by the action count downstream.
    """
    match = _MATERIAL_EACH.search(text)
    if not match:
        return None
    raw = match.group(1).lower()
    count = _WORD_NUMBERS.get(raw) or int(raw.replace(",", ""))
    if not 0 < count <= MAX_PER_ACTION:
        return None
    return count, match.group(2).strip().lower()


def singular(word: str) -> str:
    """"planks" -> "plank", and "glass" -> "glass".

    Crude on purpose: the only thing depending on it is whether two spellings of
    the same material compare equal, and the -ss guard is the only exception the
    wiki's material names actually contain.
    """
    lowered = word.lower()
    if len(lowered) > 3 and lowered.endswith("s") and not lowered.endswith("ss"):
        return lowered[:-1]
    return lowered


# "126,000 experience per hour", "around 63,000 xp/hr", "up to 480,000
# experience an hour". The mirror of _XP_EACH, which rejects exactly these.
_XP_HOUR = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(?:[-–—]\s*([\d,]+(?:\.\d+)?)\s*)?"
    rf"(?:(?:{_SKILL_WORD})\s+)?(?:experience|xp)\s*(?:per|an|/)\s*(?:hour|hr)\b",
    re.I,
)

# Which stated rate to plan with, when a section states several. The Mining
# guide's granite bracket states four -- 134,000 tick-perfect, a 126,000
# long-term benchmark, 120,000-125,000 for players making occasional mistakes,
# and 63,000 without tick manipulation -- and they are not four opinions about
# one number. Picking the largest promises a theoretical maximum as a plan;
# picking the first takes whichever the section happened to open with.
_BENCHMARK = re.compile(
    r"\b(?:benchmark|expect\w*|average|realistic|typical|long[- ]term|generally)\b",
    re.I,
)

_SENTENCE = re.compile(r"[^.!?]+[.!?]*")


def xp_per_hour(text: str) -> tuple[float, str] | None:
    """``(rate, the sentence stating it)``, or None if the guide states none.

    The sentence comes back with the number because the number alone is not
    honest here. A rate is a claim about equipment, attention and tick
    manipulation, and the guides say so in the same breath -- "the theoretical
    tick-perfect maximum", "without tick manipulation" -- so an answer quoting
    134,000 and an answer quoting 63,000 differ by a factor of two and both are
    the wiki's. Handing the wording along lets the caller say which one it used.

    A benchmark framing wins where one exists, and a stated range reads as its
    lower bound: both choices err towards the duration being longer, which is
    the side to be wrong on when somebody is deciding whether to start.
    """
    best: tuple[float, str] | None = None
    for sentence in _SENTENCE.findall(text):
        found = _XP_HOUR.search(sentence)
        if not found:
            continue
        rate = float(found.group(1).replace(",", ""))
        if not rate:
            continue
        note = " ".join(sentence.split())
        if _BENCHMARK.search(sentence):
            return rate, note
        if best is None:
            best = rate, note
    return best


def about_method(heading: str, method: str) -> bool:
    """Is this bracket the method the question named?

    The heading only, unlike :func:`about_material`, and the difference is
    measured: four brackets of the Mining guide mention granite in their prose
    -- iron ore says to switch to it at 45, crashed stars compares against it --
    and exactly one is *about* it. Guide headings name the method, so that is
    where the question's word has to land.
    """
    wanted = terms(method)
    return not wanted or wanted <= terms(heading)


def terms(text: str) -> set[str]:
    """Lowercase, roughly singular, meaningful words, for loose name matching.

    Public because :mod:`reldo.direct` compares a question's wording against a
    page name the same way, and two spellings of "roughly the same word" is one
    more than this is worth.
    """
    words = (singular(word) for word in re.findall(r"[a-z']+", text.lower()))
    return {word for word in words if word and word not in _FILLER}


def about_material(heading: str, section_text: str, material: str) -> bool:
    """Is this bracket about the material the question named?

    Asked for mahogany planks, a bracket answering in teak has answered a
    different question -- and the Construction guide offers both across
    overlapping brackets, so *which method* is the asker's choice and *which
    material* is not.

    The section's stated material decides it whenever there is one, because that
    is a fact rather than an association: mounted mythical capes are a teak
    method under a heading naming neither teak nor planks. Only a section
    stating no material at all is matched on its heading and prose, and a
    question naming no material matches everything, as before.
    """
    wanted = terms(material)
    if not wanted:
        return True
    stated = materials_per_action(section_text)
    if stated:
        return wanted <= terms(stated[1])
    return wanted <= terms(f"{heading} {section_text}")


def actions_for(total_xp: int, per_action: float) -> int:
    """Actions to cover an XP gap, rounded up."""
    if per_action <= 0:
        raise ValueError("per_action must be positive")
    return math.ceil(total_xp / per_action)


def header(skill: str, current_xp: int, to_level: int) -> str:
    """The exact arithmetic, before any wiki content.

    Deliberately first and deliberately separate. This part cannot be wrong and
    does not depend on the wiki being reachable; everything after it is quoted
    from a page and carries the page's name.
    """
    from_level = level_at_xp(current_xp)
    if to_level <= from_level:
        return (
            f"{current_xp:,} {skill} XP is level {from_level}, which is already "
            f"at or past {to_level}."
        )
    target_xp = xp_for_level(to_level)
    lines = [
        f"{skill}: level {from_level} ({current_xp:,} XP) -> level {to_level} "
        f"({target_xp:,} XP)",
        f"  {target_xp - current_xp:,} XP to go",
    ]
    # Only worth saying when the two differ. Somebody sitting exactly on a level
    # threshold would otherwise get the same number twice in one line, which
    # reads as a bug in the arithmetic this whole module exists to be trusted on.
    banked = current_xp - xp_for_level(from_level)
    if banked:
        lines.append(
            f"  ({xp_between(from_level, to_level):,} from the start of level "
            f"{from_level}; you have {banked:,} banked)"
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class Leg:
    """One guide bracket, clipped to the part of it you actually need.

    Data rather than a paragraph, because the shopping list has one more reader
    than the person: whoever prices it. A caller holding live GE prices can
    multiply :attr:`materials` by one of them, and could not have done that
    against the sentence this used to return -- not without parsing back out the
    number it had just formatted in.
    """

    heading: str
    start: int
    end: int
    xp: int
    xp_each: float | None = None
    material: str = ""
    per_action: int = 0
    # The other rate, and the sentence it was stated in. Carried rather than
    # rendered: a plan is a shopping list and does not want a duration in it,
    # but "how long does this take" is answered from exactly the same bracket
    # and has nowhere else to read it from.
    xp_hour: float = 0.0
    rate_note: str = ""

    def hours(self) -> float | None:
        """How long this stretch takes at the rate the guide states, if it does."""
        if not self.xp_hour:
            return None
        return hours_needed(self.xp, self.xp_hour)

    @property
    def actions(self) -> int | None:
        """Actions to cover this stretch, or None if the guide states no rate."""
        if self.xp_each is None:
            return None
        return actions_for(self.xp, self.xp_each)

    @property
    def materials(self) -> int | None:
        """Total of :attr:`material` this stretch needs, when both are known."""
        actions = self.actions
        if actions is None or not self.per_action:
            return None
        return actions * self.per_action

    def render(self, *, extra: Sequence[str] = ()) -> str:
        """The bracket as a person reads it, plus any lines the caller adds.

        ``extra`` is indented to match rather than taken verbatim, so that a
        cost line computed somewhere that knows about money lines up under the
        count it prices without that module knowing this one's layout.
        """
        lines = [f"{self.heading.strip()}  [levels {self.start}-{self.end} of this]"]
        lines.append(f"  {self.xp:,} XP over that stretch")
        if self.xp_each is None:
            lines.append("  (the guide does not state XP per action for this method)")
        else:
            lines.append(f"  at {self.xp_each:,g} XP each: {self.actions:,} actions")
            if self.material and self.per_action:
                plural = self.material if self.per_action == 1 else f"{self.material}s"
                lines.append(
                    f"  at {self.per_action} {plural} each: "
                    f"{self.materials:,} {self.material}s"
                )
        lines.extend(f"  {line}" for line in extra)
        return "\n".join(lines)


def read_bracket(
    heading: str,
    section_text: str,
    from_level: int,
    to_level: int,
) -> Leg | None:
    """What one guide bracket costs over the part of it you actually need.

    Clipped to the overlap rather than reported whole: a plan from 37 to 70 that
    quotes the 33-52 bracket's own "1,760 planks" figure is answering a question
    nobody asked, and the number is wrong for the range by the four levels at
    the bottom that are already trained.
    """
    bracket = parse_bracket(heading)
    if not bracket or not overlaps(bracket, from_level, to_level):
        return None
    low, high = bracket
    start, end = max(low, from_level), min(high, to_level)
    if end <= start:
        return None

    rate = xp_per_action(section_text)
    count, material = materials_per_action(section_text) or (0, "")
    hourly, note = xp_per_hour(section_text) or (0.0, "")
    return Leg(
        heading=heading,
        start=start,
        end=end,
        xp=xp_between(start, end),
        xp_each=rate,
        material=material,
        per_action=count,
        xp_hour=hourly,
        rate_note=note,
    )


async def brackets_for(
    wiki, skill: str, from_level: int, to_level: int, *, material: str = "",
    method: str = "",
) -> tuple[list[Leg], str]:
    """Every guide bracket overlapping a level range, and the page they came from.

    ``method`` narrows them to the bracket *named* by the question -- "at
    granite rates" -- and is matched on the heading alone, where ``material`` is
    matched on what the section says it consumes. Both empty means every
    bracket, as before.

    ``material`` narrows the brackets to the ones about it. Empty means every
    bracket, which is what a question naming no material asks for. A named
    material matching nothing returns nothing rather than falling back to the
    full list: an answer about teak to a question about mahogany is the failure
    this argument exists to stop, and an empty return sends the question on to
    somebody who might do better with it.

    Public and living here rather than in commands.py, where it started: it is
    about reading guides and has nothing to do with Discord, and the direct
    answerer needs it too. Importing it from the command module would have
    dragged the gateway in behind it.

    Sequential rather than gathered, and that is the cheap call: only the
    sections that overlap are fetched, which on a Construction plan from 37 to
    70 is six of twenty-six. Fetching all of them to filter afterwards would
    triple the wiki traffic for the same answer.
    """
    for title in guide_titles(skill):
        try:
            sections = await wiki.sections(title)
        except Exception:
            continue
        wanted = [
            s for s in sections
            if (b := parse_bracket(s.line)) and overlaps(b, from_level, to_level)
        ]
        if not wanted:
            continue
        out: list[Leg] = []
        for section in wanted:
            try:
                text = await wiki.section_text(title, section.index)
            except Exception:
                log.warning("Could not read section %s of %r", section.index, title)
                continue
            if not about_material(section.line, text, material):
                log.debug("Bracket %r is not about %r", section.line, material)
                continue
            if not about_method(section.line, method):
                log.debug("Bracket %r is not the %r method", section.line, method)
                continue
            leg = read_bracket(section.line, text, from_level, to_level)
            if leg:
                out.append(leg)
        if out:
            return out, title
    return [], ""
