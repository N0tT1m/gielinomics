"""The answering agent: a local model, given tools that reach into the wiki.

Retrieval is a *tool*, not a preprocessing step. A fixed retrieve-then-answer
pipeline gets one shot at guessing which pages matter; letting the model search,
read, notice what's missing, and search again handles the questions users actually
ask ("is X better than Y for Z") where the answer lives across several pages and
you can't know which ones until you've read the first.

Everything here runs against your own hardware -- see :mod:`reldo.llm`.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .bucket import BucketClient, BucketError
from .earnings import (
    GUIDE_PAGE,
    SKILLING_GUIDE_PAGE,
    best_per_skill,
    read_guide,
)
from .earnings import rank as rank_methods
from .earnings import render as render_methods
from .ge import GEClient, GEError, compare
from .hiscores import HiscoresClient, HiscoresError
from .llm import ChatClient, Tool
from .money import parse_goal
from .money import plan as gp_plan
from .progress import snapshot_of, summarise
from .retrieval import HybridRetriever, page_url
from .skills import MAX_LEVEL, SKILLS, actions_needed, plan, xp_between, xp_for_level
from .unlocks import count_level_tables, dedupe, render, scan_page
from .wiki import tables_as_text

log = logging.getLogger(__name__)

# Each page costs a sections call plus one call per section, so this is the
# main cost knob on a list question.
PAGES_PER_UNLOCK_SCAN = 3
# Candidates to triage with a single request each before scanning any deeply.
UNLOCK_CANDIDATES = 8
# Shortlisted pages to try as the *material* of a recipe, once none of them
# turned out to be the product. One Bucket query each, and it only pays off on
# the entries that are actually items.
MATERIAL_CANDIDATES = 4
# Pages tried when hunting a rate in a table. Each is one request, and the
# first that carries an XP figure wins -- so this is a ceiling on a miss, not a
# cost paid every time.
TABLE_RATE_CANDIDATES = 3

# Most items a forced comparison will rank. Forty things are made from a gold
# bar and all forty fit; the cap is there so a material feeding hundreds cannot
# turn one question into a wall of table, and the truncation is logged rather
# than silent.
COMPARISON_CEILING = 40

# A 32B model at Q4 is not a frontier model, and the prompt has to do more work
# than it would for one. Three things it needs told explicitly that a bigger model
# infers: search before answering, read pages rather than trusting snippets, and
# stop when the wiki doesn't settle the question instead of filling the gap.
SYSTEM_PROMPT = """\
You answer questions about Old School RuneScape using the OSRS Wiki.

Always call search_wiki before answering, even when you think you know. Your \
training data has a cutoff and the game does not: items get rebalanced, bosses \
get released, prices move. Answering from memory is the worst thing you can do \
here, because you will sound confident and be a year out of date.

Then read the page. Search summaries are not enough to answer from -- they tell \
you which page to read, not what it says.

Read the page about the specific thing asked about, not the general skill page. \
For "what Smithing level is a rune platebody", read "Rune platebody", not \
"Smithing" -- the skill overview has tables covering every item and you will pick \
the wrong row.

Player names are not wiki pages. If the question mentions a specific account, \
username or player -- "am I ready for", "what should I train next", "the player \
Zezima", "my stats" -- call get_player_stats. Never search_wiki for a player name: \
the wiki has articles about NPCs and items with similar names, and you will answer \
confidently about the wrong thing. Ask for their username if they have not given \
one. Then compare their levels against requirements you read from the wiki.

Never quote a price from memory. Any question about value, profit, or what \
something sells for needs get_ge_price or compare_ge_prices -- the economy moves \
weekly and your training data does not. The wiki page for an item does NOT have a \
current price either; only the GE tools do.

Two different numbers, and which one answers depends on what was asked. "What is \
a shark worth" wants the market price. "How much will 795 sharks bring", "what \
will I make", "how much do I actually get" want the AFTER-TAX figure the tool \
labels "you receive" -- the Grand Exchange takes 2% on the way out, and the gap \
between the two is the whole of what somebody is asking about when they ask what \
they will get. Say which one you gave: "about 775,000 after tax" rather than a \
bare total that could be either.

When the question compares things to sell -- "X or Y", "what is best to sell", \
"what should I mine" -- call compare_ge_prices ONCE with all of them, not \
get_ge_price several times. It ranks them for you and gives you a gp/day column; \
doing it yourself across separate lookups is where you get it backwards.

Rank by gp/day, never by how many units trade. 127 sandstone at 2,387gp each is \
303,000 gp/day; 354 granite at 240gp each is 85,000 gp/day. Granite trades in \
larger numbers and is still the worse item to sell. Comparing unit counts across \
items with different prices is the single most common way to be wrong here.

If the tool warns that a market is dead or erratic, say so in your answer. That \
warning is usually the most useful thing you can tell someone asking what to \
sell -- an item nobody buys has no real price, and passing its number along \
without the caveat is worse than saying nothing.

When the question is "what made from X sells best", the *set* is half the \
answer and you will get it wrong from memory. Call made_from to get every item, \
then compare_ge_prices on all of them. Forty things are made from a gold bar, \
and the three anyone thinks of are worth a fourteenth of the best one.

The wiki carries no prices and no trade volumes -- only the GE tools do, and \
once they have answered you have the figure. Never report a price or a volume \
as unavailable after calling them, and never say the wiki does not give it: \
that is true, irrelevant, and reads as though you found nothing.

If a page seems to be missing a number -- a level requirement, a stat, a drop \
rate -- it is probably in a table, and read_wiki_page cannot see tables at all. \
Call read_wiki_table on the same page rather than concluding the wiki does not \
say, and never fill the gap from memory.

When the question asks for a LIST -- "what can I build at Sailing 20", \
"everything up to level N", "all the X I can make" -- call list_unlocks. Those \
requirements live in wiki tables, and read_wiki_page cannot see tables at all: \
it returns the page with every table silently removed, so assembling the list \
yourself means inventing it. list_unlocks reads the table and does the filtering \
in code, so it cannot drop a row or include one over the level.

Never do XP arithmetic yourself. Read the XP/hr or XP-per-action figure from the \
guide, then call calculate_xp. Seven-digit subtraction and division is exactly \
where you will be confidently wrong.

The XP a level costs is not on the wiki -- it is a formula, and calculate_xp and \
training_cost are the only things here that know it. Never search for it, never \
read a page hoping to find it, and never tell anyone the wiki does not give it. \
For "how many/how much X to get from level A to B", call training_cost with what \
they would be making: it does the gap, the count and the materials in one.

For skilling, questing and min-maxing questions the answer usually lives in a long \
guide organised by level bracket or quest step. On those, call list_page_sections \
first and then read_wiki_section for the part that matters -- "Levels 45-99: \
Granite" rather than the whole 27,000-character mining guide. Use read_wiki_page \
for short pages like items and monsters.

Give the concrete numbers the guide gives: XP rates per hour, level brackets, \
required items and quantities, quest prerequisites. Naming a method without its \
numbers is only half an answer. Every number you give must come from a page you \
read in this conversation -- never from this prompt, and never from memory.

If the first search misses, search again with different wording. Players describe \
things loosely ("the poison boss", "that dragon in the cave") and the wiki indexes \
them under proper names.

When you have read enough, answer in plain prose. Keep it short: lead with the \
direct answer in the first sentence, then only detail that changes what the reader \
would do. No preamble, no restating the question, no headers. A one-line question \
gets a one-line answer.

Brevity does not apply to a figure a tool worked out for you. When a tool hands \
you a line starting ANSWER:, report every number on that line, including the \
ones the question did not literally ask for. Those are the figures nobody can \
check by eye and the whole reason the tool ran: asked how much gold to smelt, \
"815 gold bars" is half of it and "815 gold bars, which is 18,319 Smithing XP" \
is the answer. This is the one place where saying more is right.

If the wiki does not settle the question, say so plainly rather than guessing.\
"""

# Every corrective below is appended as a *user* turn, which is the only way to
# get the model to act on it -- and means the model reads it as something the
# player said and answers it. That is how "Oh, snap! I see what you did there.
# You wanted me to read the page before answering, huh?" reached a player's
# speakers, and how a Prayer answer opened "I apologise for the confusion
# earlier, love". A persona makes it worse, because a character told to be
# reactive reacts.
#
# One clause, shared, rather than four copies drifting apart: the first version
# of this fixed the read nudge only, and the next leak came from a different
# nudge three hundred lines away.
NOT_DIALOGUE = (
    "\n\nThis is an internal instruction, not something the player said. Do not "
    "acknowledge it, apologise for anything, or mention reading, pages, sources, "
    "checking or correcting yourself. Reply only with the answer to their "
    "original question, as if you had got it right the first time."
)

# Sentences that are the machinery talking, not the answer. Every corrective in
# this module arrives as a *user* turn -- the only way to make the model act on
# one -- so the model reads it as something the player said and replies to it.
# NOT_DIALOGUE asks it not to. Measured across a session of live coaching, it
# does not always listen: "Oh, my apologies, love", "Oh, bless you, love. You're
# right, let's get that sorted", "I apologise for the confusion earlier".
#
# So this is the same call as the ungrounded-number excision. Asking is a
# prompt; removing is a guarantee.
_META_OPENER = re.compile(
    r"""^\s*(?:oh[,!\s]+)?          # "Oh, "
        (?:my\s+)?                  # "my apologies"
        (?:i\s+(?:do\s+)?)?         # "I apologise"
        (?:apolog(?:ise|ize|ies)|sorry|bless\s+you|you(?:'re|\s+are)\s+right
          |let\s+me\s+correct|let['’]?s\s+get\s+that\s+sorted
          |my\s+mistake|i\s+made\s+a\s+mistake|thanks\s+for\s+(?:the\s+)?
           (?:correction|clarification))
        [^.!?]*[.!?]+\s*""",
    re.I | re.X,
)


def _strip_meta(text: str) -> tuple[str, list[str]]:
    """Drop leading sentences that apologise to the player or narrate a retry.

    Leading only, and never all of it. The apology is always an opener -- the
    model is answering the nudge before answering the question -- and a rule that
    cut anywhere would eat "sorry, that method needs 70 Slayer", which is about
    the game and belongs in the answer.
    """
    removed: list[str] = []
    out = text
    # Twice at most: "Oh, my apologies, love. You're right, let's get that
    # sorted." is two sentences of it, and three would be reaching.
    for _ in range(2):
        match = _META_OPENER.match(out)
        if not match:
            break
        candidate = out[match.end():]
        if not candidate.strip():
            break  # It was the whole answer; a blank reply is worse.
        removed.append(match.group(0).strip())
        out = candidate
    return (out.lstrip() if removed else text), removed


def _read_nudge(top_hit: str) -> str:
    """Tell the model exactly which page to read.

    An earlier version said "the most relevant search result" and left the choice
    open. Asked for the Smithing level of a rune platebody, the model had
    "Rune platebody" ranked first by both rankers and went and read the general
    "Smithing" page instead, then answered from a table it misread. Naming the
    page removes the choice.
    """
    target = f"read_wiki_page on {top_hit!r}" if top_hit else "read_wiki_page"
    return (
        "You answered without reading any page, so that answer is not grounded and "
        f"may be wrong. Ground it now: call {target} -- the specific page about the "
        "thing asked about, not a general skill or overview page -- or "
        "get_player_stats if the question is about a player's account. Then answer "
        "again using only what that source actually says.\n\n"
        # This arrives as a user turn, so the model treats it as something said
        # to it and answers it. In a persona that is ruinous -- the first time a
        # character hit this she opened with "Oh, snap! I see what you did there.
        # You wanted me to read the page before answering, huh?", narrating the
        # grounding machinery to the person it exists to protect. The milder
        # version was already known: an apology "for the confusion" about a call
        # that was never made. Say plainly that this is not part of the
        # conversation.
    ) + NOT_DIALOGUE

# A question that asks how long something takes, or an answer that states a
# duration, both mean arithmetic happened. If calculate_xp was never called then
# it happened in the model's head, which is the one place it is reliably wrong.
_ASKS_DURATION = re.compile(
    r"how long|how many (?:hours|days|weeks)|how much time|"
    r"time to (?:get|reach|go)|hours? (?:to|from)\b|how fast",
    re.I,
)
# Deliberately only totals, not rates. "126,000 xp/hr" read off a guide is a
# quoted fact and fine; "approximately 100 hours" is a derived number.
_CLAIMS_DURATION = re.compile(r"\b\d[\d,.]*\s*(?:hours?|hrs?|days?|weeks?)\b", re.I)

# The model announcing that the page it read was the wrong one. It is right that
# this is a dead end and wrong that the turn should end there.
_DEAD_END = re.compile(
    # "give" is the third wording to be added here after the fact. Measured on
    # "how long from 45 to 99 mining at granite rates": "The granite rocks page
    # does not give a concrete XP rate for mining granite" -- a refusal by any
    # reading, which sailed past this, so the fall-through to the next candidate
    # never fired and the answer went out with no duration in it at all.
    # "include" is the fourth wording added here after the fact, and it arrived
    # the same way as the other three -- from a real answer that sailed past.
    # Asked what 795 sharks fetch, the model called the GE tool, was handed the
    # price, and replied "the information provided does not include the current
    # Grand Exchange price for sharks". A refusal by any reading, about data it
    # was holding, and the pass built for exactly that could not see it.
    r"not mentioned|does not (?:list|mention|state|contain|say|provide|specify|give|include)|"
    r"doesn't (?:list|mention|state|contain|say|provide|specify|give|include)|"
    r"no information|not (?:listed|stated|"
    # "cannot find" was missing while "couldn't find" was here, which is the
    # same refusal in the register the model actually writes in: "I cannot find
    # the volume for these" sailed past and the fall-through never fired.
    r"specified)|(?:could|can) ?n[o']?t find|can[']?t find|i will search again|"
    # "unable to find" was the only refusal wording here, and the model has more
    # than one. Measured on "how do I level mining": "I apologize, but I am
    # unable to retrieve the specific section" -- a refusal by any reading, which
    # sailed past this and left a vague answer with no numbers in it at all.
    r"unable to (?:find|retrieve|access|provide|locate)|"
    r"(?:do|does) not have the (?:exact|specific)|"
    r"unable to (?:complete|perform)",
    re.I,
)

# For doing the arithmetic ourselves when the model refuses to. "from 45 to 99",
# "92 to 99 slayer", "70-99". The level bounds at the call site reject the false
# positives -- a year, a price, a drop rate.
_LEVEL_RANGE = re.compile(r"(?:from\s+)?\b(\d{1,3})\b\s*(?:to|-|–|until)\s*\b(\d{1,3})\b", re.I)
_XP_RATE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(k\b)?\s*(?:xp|experience)\s*(?:per|/|an|a)\s*(?:hour|hr)\b",
    re.I,
)
# The other rate: what one bar, log or fish is worth. Only explicit per-action
# wordings, because the alternative -- taking any number near the word
# "experience" -- is how a page's total XP or its level requirement ends up
# being divided into an XP gap and presented as a count of ores.
_XP_PER_ACTION = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(?:xp|experience)\s+(?:each\b|a\s?piece\b|"
    r"per\s+(?!hour\b|hr\b|h\b)[a-z]+)",
    re.I,
)


# "268,875 an hour", "1.4m gp/hr", "212,706 per hour". Money-making guides say
# it both with and without the "gp", so the unit cannot be required -- the
# minnow guide's own sentence is "players can expect to earn between 268,875
# and 448,125 an hour", and demanding "gp" there finds nothing.
_GP_RATE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(k|m\b)?\s*(?:gp|gold|coins)?\s*(?:per|/|an|a)\s*(?:hour|hr)\b",
    re.I,
)
# "raw sharks worth 717", "sells for 1,200 gp", "worth 717 each".
_GP_EACH = re.compile(
    r"(?:worth|sells? for|price of|each at)\s*([\d,]+(?:\.\d+)?)\s*(k|m\b)?", re.I
)


# "how many gold bars", "how much coal do I need". A quantity of a *thing*, which
# is the XP gap divided by what one of them gives -- two exact numbers the model
# reliably gets wrong by hand. Duration words are excluded because
# _ASKS_DURATION already owns them and the log lines should say which fired.
_ASKS_QUANTITY = re.compile(
    r"how (?:many|much)\b(?!\s+(?:hours?|hrs?|days?|weeks?|time|longer)\b)", re.I
)
_MENTIONS_LEVEL = re.compile(r"\blevels?\b|\blvls?\b", re.I)


def _named_skill(question: str) -> str:
    """The skill a question names, if it names one.

    Word boundaries with an optional -ing, rather than a substring test:
    "crafting" is inside "runecrafting", and a substring match answers a
    Runecraft question with Crafting's numbers and nothing to make the swap
    visible.
    """
    for skill in SKILLS:
        if re.search(rf"\b{re.escape(skill.lower())}(?:ing|s)?\b", question.lower()):
            return skill
    return ""


def _quantity_request(question: str) -> tuple[str, int, int] | None:
    """Skill and level range for a "how many X from A to B" question.

    Requires a skill name or the word "level" on top of the range. "How much
    damage does it do from 20 to 30" is a level range by the regex and is not a
    training question, and computing an XP gap for it would be confident noise.
    """
    if not _ASKS_QUANTITY.search(question):
        return None
    match = _LEVEL_RANGE.search(question)
    if not match:
        return None
    low, high = int(match.group(1)), int(match.group(2))
    if not 1 <= low < high <= MAX_LEVEL:
        return None
    skill = _named_skill(question)
    if not skill and not _MENTIONS_LEVEL.search(question):
        return None
    return skill, low, high


# "which jewellery made from gold bars sells best". Two halves: a comparison,
# and a set defined by what it is made of. compare() has always done the first
# half in code; the second was left to the model and it invented it.
_ASKS_BEST_SELLER = re.compile(
    r"sells? (?:the )?best|best (?:to sell|seller)|most (?:profitable|valuable)|"
    r"worth (?:the )?most|most (?:money|gp)|highest (?:value|price)|"
    r"makes? the most",
    re.I,
)
_MADE_FROM = re.compile(
    r"\bmade (?:from|with|out of|using)\s+([a-z][a-z' ]{2,30})", re.I
)
# Where the material name stops. The regex above cannot know, so it over-reads
# and this cuts back: "made from gold bars sells best on the" is "gold bars".
_NOT_A_MATERIAL = frozenset({
    "a", "an", "and", "are", "at", "best", "by", "for", "in", "is", "make",
    "makes", "most", "of", "on", "or", "profitable", "sell", "sells", "that",
    "the", "to", "valuable", "which", "with", "worth",
})


def _material_candidates(question: str) -> list[str]:
    """Page titles the phrase after "made from" might name, longest first.

    Longest first because "Gold bar" is a page and "Gold" is not the same
    thing, and the singular alongside each because people type "gold bars" and
    the wiki titles the page for one of them. Two or three cheap Bucket queries
    settle which, and getting it wrong costs a lookup that returns nothing.
    """
    match = _MADE_FROM.search(question)
    if not match:
        return []
    words: list[str] = []
    for word in match.group(1).lower().split():
        if word in _NOT_A_MATERIAL:
            break
        words.append(word)

    titles: list[str] = []
    for count in range(len(words), 0, -1):
        phrase = " ".join(words[:count])
        singular = phrase[:-1] if phrase.endswith("s") else ""
        for candidate in (phrase, singular):
            title = candidate[:1].upper() + candidate[1:]
            if title and title not in titles:
                titles.append(title)
    return titles


# "what skill is best for making money", "which skilling method makes the most
# gp", "best money maker". The profit figures are in a table on the money-making
# guide, so read_wiki_page returns that page with every number stripped out --
# and the model then either says the wiki does not say, which is what it did, or
# names a method it remembers. Both are measured; see reldo.earnings.
_ASKS_BEST_MONEY = re.compile(
    r"(?:best|most|highest|top|fastest)\b[^?]{0,40}?"
    r"\b(?:money|gp\b|profit|income|earn|gold per)|"
    r"\b(?:money|gp|profit)[\s-]*(?:mak\w+|earn\w+)\b|"
    r"\bmake\s+(?:the\s+)?most\b",
    re.I,
)
# Whether they asked about *skills* or about methods in general. The two want
# different pages and different groupings: one best method per skill, or a flat
# ranking of everything including combat.
_ASKS_WHICH_SKILL = re.compile(r"\bskill(?:s|ing)?\b", re.I)


# The activity a duration question names: "how long will that take farming
# minnows" -> "minnows". Needed because the guide to read is the guide for what
# they said they would be *doing*, and the question is usually full of words
# about something else -- this one names sharks four times and minnows once, and
# the sharks are the product, not the activity.
_ACTIVITY = re.compile(
    r"\b(?:farming|catching|doing|killing|mining|fishing|cutting|hunting|"
    r"picking|pickpocketing|thieving|crafting|smithing|cooking|smelting|"
    r"fletching|running|by)\s+([a-z][a-z' ]{2,28})",
    re.I,
)


# "what can I build at Sailing 20", "everything I can make up to level 30".
_ASKS_FOR_LIST = re.compile(
    r"\b(everything|all|what|which)\b[^?]{0,60}?"
    r"\b(build|buildable|make|craft|create|wear|equip|unlock)\w*\b",
    re.I,
)
_LIST_LEVEL = re.compile(
    r"level\s*(\d{1,3})|(\d{1,3})\s*(?:and|or)\s*(?:below|under|lower)", re.I
)


async def gather_unlocks(retriever, skill: str, level: int, page: str | None = None):
    """Rows for a "<skill> level <= N" question, from the best pages available.

    Two queries, because neither alone works. "Sailing level requirements"
    returns the alpha and beta dev-blog pages and misses Shipbuilding entirely;
    the bare skill name finds it at rank four. The union costs a few extra
    pages and is the difference between listing the four component categories
    and listing one mast.
    """
    if page:
        titles = [page]
    else:
        ranked = [
            [h.title for h in await retriever.shortlist(query, k=4)]
            for query in (f"{skill} level requirements", skill)
        ]
        # Interleaved, not concatenated. Concatenating let the first query's
        # four results fill the budget on their own, so Shipbuilding -- rank
        # four on the *second* query and the only page that actually answers
        # this -- was cut off before it was ever fetched.
        titles = []
        for rank in range(max(len(r) for r in ranked)):
            for results in ranked:
                if rank < len(results) and results[rank] not in titles:
                    titles.append(results[rank])
        # Triage before scanning. One request per candidate rules out the
        # soundtrack and dev-blog pages, so the section-by-section scan -- which
        # costs a request per section -- only runs where it will find something.
        scored = []
        for title in titles[:UNLOCK_CANDIDATES]:
            count = await count_level_tables(retriever.client, title, skill)
            if count:
                scored.append((count, title))
        # Most qualifying tables first, not best search rank. Search put the
        # single-item mast page above Shipbuilding, and scanning in that order
        # spent the whole budget before reaching the page with nine of them.
        scored.sort(reverse=True)
        titles = [t for _, t in scored[:PAGES_PER_UNLOCK_SCAN]]

    by_page: dict[str, list] = {}
    for title in titles:
        try:
            got = await scan_page(retriever.client, title, skill, level)
        except Exception as exc:
            log.warning("Unlock scan of %r failed: %s", title, exc)
            continue
        if got:
            by_page[title] = got

    # Richest page last: dedupe keeps the last of identical rows, and the mast
    # table appears both on "Shipbuilding" and on the single-item page for one
    # mast. Attributing it to the hub is what makes the listing read as
    # categories rather than a pile of unrelated items.
    return [
        u
        for _, rows in sorted(by_page.items(), key=lambda kv: len(kv[1]))
        for u in rows
    ], list(by_page)


def _list_request(question: str) -> tuple[str, int] | None:
    """Skill and cap for a "what can I build at N" question, if it is one.

    Through :func:`_named_skill` rather than a substring test, which is what
    this did and which named the wrong skill on the one word where it matters:
    "crafting" is inside "runecrafting", and Crafting comes first in
    :data:`SKILLS`, so "what can I make with runecrafting at level 20" resolved
    to Crafting. That is not a near miss -- _force_unlocks then reads Crafting's
    requirement tables and hands them over with "answer using exactly these
    entries", which is the invented-list failure this whole pass exists to stop,
    arriving through the pass itself.
    """
    if not _ASKS_FOR_LIST.search(question):
        return None
    match = _LIST_LEVEL.search(question)
    if not match:
        return None
    level = int(match.group(1) or match.group(2))
    if not 1 <= level <= MAX_LEVEL:
        return None
    skill = _named_skill(question)
    return (skill, level) if skill else None


# Two digits or more. Single digits appear in almost any text by chance, so
# checking them produces noise rather than signal.
_NUMBER = re.compile(r"\b\d[\d,]{1,}\b")


# Sentence boundary: terminator, then whitespace. Requiring the whitespace is
# what keeps "3.5" and "1.2m" from splitting down the middle.
_SENTENCE = re.compile(r"(?<=[.!?])\s+")

_EXCISION_NOTE = (
    "\n\n(I left out a figure or two the pages I read did not actually give.)"
)


def _known_numbers(question: str, seen: list[str]) -> set[str]:
    """Every number the model was shown, comma formatting normalised away."""
    haystack = " ".join(seen) + " " + question
    return {m.replace(",", "") for m in _NUMBER.findall(haystack)}


# How near a figure must be to a product of what the model was shown to read as
# that product rather than as an invention. Loose enough for the rounding anyone
# does when reporting a total -- 773,535 written as "774,000" is 0.06% out --
# and far tighter than the gap between any two plausible prices.
_ARITHMETIC_TOLERANCE = 0.01


def _derivable(question: str, seen: list[str]) -> list[float]:
    """Products of a quantity in the question and a figure the model was shown.

    "How much will 795 sharks bring on the GE" times the 973gp it was handed is
    773,535 -- a number that is, by construction, in neither the question nor
    anything it read. It is the thing the question asked it to work out.

    Counting that as invented does not merely mislabel it. The grounding nudge
    tells the model that a figure it cannot source should be reported as one the
    wiki does not give, and the nudge is the last pass, so nothing runs after it
    to notice that a correct answer has just been argued into a refusal.
    Measured, twice in two runs: "The wiki does not give the price of 795
    sharks", and "I apologize, but the search results did not provide the
    specific information" -- both after a successful GE lookup.

    Deliberately only question x shown, not every pair of shown numbers. A page
    carries hundreds of figures and their products cover the number line densely
    enough that anything would look derivable; a quantity the asker actually
    typed is a much smaller and much better motivated set.
    """
    asked = [
        float(m.replace(",", ""))
        for m in _NUMBER.findall(question)
    ]
    if not asked:
        return []
    shown = []
    for m in _NUMBER.findall(" ".join(seen)):
        try:
            shown.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return [a * b for a in asked for b in shown if a and b]


def _ungrounded_numbers(text: str, question: str, seen: list[str]) -> list[str]:
    """Numbers asserted in the answer that the model was never shown.

    The strongest grounding check available here, and it needs no knowledge of
    what the right answer is: the model saw the question and the tool outputs
    and nothing else, so a figure in neither was recalled from training. That is
    how "at least 30 Smithing" and "Ironwood mast at level 20" get through -- both
    fluent, both cited, both invented.
    """
    grounded = _grounded_test(question, seen)
    return [
        m
        for m in dict.fromkeys(_NUMBER.findall(text))
        if not grounded.shown(m.replace(",", ""))
    ]


def _fewer_invented(previous: str, retried: str, question: str, seen: list[str]) -> str:
    """Of two drafts, the one asserting fewer numbers it was never shown.

    :func:`_keep_best` only guarantees the retry is non-empty, which was enough
    for every other pass here and is not enough for this one: a handback that
    repeats its invented figures verbatim passed that test and shipped. Ties go
    to the retry, which may have gone and read the table -- in which case those
    numbers are in ``seen`` by now and count as grounded on this recount.
    """
    if not retried.strip():
        return previous
    if len(_ungrounded_numbers(retried, question, seen)) <= len(
        _ungrounded_numbers(previous, question, seen)
    ):
        return retried
    return previous


def _grounded_test(question: str, seen: list[str]):
    """Whether a piece of text asserts no number it was not shown.

    Exact. A rounding tolerance was tried here and reverted: it was built on
    the theory that the excision below was destroying answers by cutting
    sentences whose figures had merely been rounded, and the measurement said
    otherwise -- across two full eval runs the excision fired zero times, so
    that was never the mechanism. What the tolerance did do was suppress the
    grounding *nudge*, which is a corrective step, and two cases that had been
    passing every run slipped to two in three. Neutral at best, and loosening
    the one check that catches invented figures needs better than neutral.
    """
    known = _known_numbers(question, seen)
    products = _derivable(question, seen)

    def shown(normalised: str) -> bool:
        if normalised in known:
            return True
        try:
            value = float(normalised)
        except ValueError:
            return False
        return any(
            abs(value - p) <= _ARITHMETIC_TOLERANCE * max(abs(p), 1.0) for p in products
        )

    def grounded(piece: str) -> bool:
        return all(shown(n.replace(",", "")) for n in _NUMBER.findall(piece))

    grounded.shown = shown
    return grounded


def _ungrounded_pieces(text: str, question: str, seen: list[str]) -> list[str]:
    """The sentences :func:`_drop_ungrounded_claims` would remove."""
    grounded = _grounded_test(question, seen)
    return [
        piece
        for line in text.splitlines()
        if line.strip()
        for piece in _SENTENCE.split(line)
        if not grounded(piece)
    ]


def _drop_ungrounded_claims(text: str, question: str, seen: list[str]) -> str:
    """Cut the sentences carrying numbers the model was never shown.

    Sentences rather than the numbers themselves, because deleting the figure
    out of "you can smash 15 rocks per inventory" leaves a sentence that still
    asserts something and no longer says what -- worse than either keeping or
    dropping it. Whole lines go when every sentence on them fails, so a bullet
    list loses the invented row rather than becoming a stub.

    This is the Cannonball conclusion applied at the sentence level: the eval
    case that asserted a number the page did not contain was rewarding
    hallucination and punishing honesty, and the resolution there was the same
    as here -- say less, rather than say something invented.
    """
    grounded = _grounded_test(question, seen)

    kept: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            kept.append(line)
            continue
        survivors = [p for p in _SENTENCE.split(line) if grounded(p)]
        if survivors:
            kept.append(" ".join(survivors))
    return "\n".join(kept).strip()


def _grounding_nudge(numbers: list[str]) -> str:
    return (
        f"These numbers are not in anything you were shown: {', '.join(numbers)}. "
        "You recalled them, which means they are probably out of date or simply "
        "wrong. Level requirements and stats usually live in a table, and "
        "read_wiki_page cannot see tables -- call read_wiki_table on the relevant "
        "page and answer again from what it returns. If you cannot find the "
        "figure, say you could not find it rather than repeating the number.\n\n"
        "Two things this does NOT mean. Arithmetic you were asked to do is not "
        "an invented figure -- a total, a count or a duration worked out from "
        "numbers you were given is the answer, not a fabrication, so keep it. "
        "And a price or volume that came from the Grand Exchange tools is not "
        "something 'the wiki does not give': the wiki carries no prices at all "
        "and the GE feed already answered you. Do not turn a good answer into a "
        "refusal on the strength of this message."
    ) + NOT_DIALOGUE


def _gp_nudge(goal: int) -> str:
    """Hand back a coin-goal answer whose arithmetic happened in the model's head.

    The XP nudge's twin, and it exists because the XP nudge was firing on these
    questions and asking for something impossible. "How many sharks do I need to
    get 5 mill and how long will that take farming minnows" matches every
    duration pattern here and contains no levels at all, so the handback demanded
    a from_level and a to_level that do not exist, _force_xp_calc found no level
    range and bailed, and the model -- asked twice for a calculation it could not
    make -- answered "I'm sorry, I made a mistake" and then returned a map of the
    Fishing Guild. Two of the three failures were the enforcement's own doing.
    """
    return (
        "You gave a coin figure without calling calculate_gp, so those numbers "
        "came out of your head. Do it properly: take the gp/hr and the price per "
        "item from the page you read -- do not invent either -- and call "
        f"calculate_gp with goal_gp={goal} and those figures. If the method "
        "produces something exchanged at a fixed rate (minnows for sharks, for "
        "instance), pass that rate as inputs_per_item. Then answer using its "
        "output verbatim."
    ) + NOT_DIALOGUE


# "what sailing level do i need to catch marlin", "what agility level for the
# rooftop course". A question naming exactly one skill and asking for a level of
# it -- which is the shape get_requirements exists to answer exactly, and the
# shape where a bare number read off the prose is most likely another skill's.
_ASKS_SKILL_LEVEL = re.compile(
    r"\b(?:what|which|how much|how high)\b[^?]{0,40}?\blevel\b|"
    r"\blevel\b[^?]{0,20}?\b(?:do|does|to)\b[^?]{0,20}?\b(?:need|require)",
    re.I,
)


def _skill_level_question(question: str) -> bool:
    """Whether this asks for one named skill's level requirement.

    Exactly one skill, on _named_skill's word-boundary rule. Two is a
    comparison, where neither is the thing asked for -- the same call
    :func:`_skill_mismatch` makes, and for the same reason.
    """
    if not _ASKS_SKILL_LEVEL.search(question):
        return False
    low = question.lower()
    named = [
        skill
        for skill in SKILLS
        if re.search(rf"\b{re.escape(skill.lower())}(?:ing|s)?\b", low)
    ]
    return len(named) == 1


def _skill_mismatch(
    question: str, answer_text: str, requirements: dict[str, int]
) -> str | None:
    """The skill the question named, when the answer reports a different one's level.

    Asked what Sailing level marlin needs, the agent answered 91 -- the Fishing
    level, which the prose spells out and the Sailing requirement does not.
    Every other guard passed it: a page was read, a page was cited, and 91 is
    genuinely on that page, so the number-grounding check had nothing to say.
    Provenance was never the problem. Relevance was.
    """
    # Exactly one, using _named_skill's word-boundary rule rather than a
    # substring test -- "crafting" is inside "runecrafting". Two skills named is
    # a comparison ("sailing or fishing for marlin"), where neither is the one
    # thing asked for, and handing those back would break good answers.
    low = question.lower()
    asked = [
        skill
        for skill in SKILLS
        if re.search(rf"\b{re.escape(skill.lower())}(?:ing|s)?\b", low)
    ]
    if len(asked) != 1:
        return None
    required = requirements.get(asked[0])
    if required is None:
        return None
    return None if str(required) in answer_text else asked[0]


def _xp_nudge() -> str:
    """Hand back an answer whose duration was guessed rather than computed.

    Measured on mistral-small3.2:24b, asked for hours from 45 to 99 Mining: it
    reported "200,000 XP per hour" and "approximately 100 hours" in one
    sentence. Those cannot both be true -- 100h at 200k/hr is 20M XP and the gap
    is 12,972,919 -- and the guide's own figure is 126,000/hr, which is 103
    hours. Two fabricated numbers that happen to look plausible together.
    """
    return (
        "You gave a duration without calling calculate_xp, so that number came "
        "out of your head and is very likely wrong. Do it properly: take the "
        "XP/hr figure from the page you read -- do not invent one -- and call "
        "calculate_xp with the from_level, to_level and that xp_per_hour. Then "
        "answer again using its output verbatim. If the question was not about "
        "training time after all, simply give your previous answer again."
    ) + NOT_DIALOGUE


# Quests get named with a digit where the wiki uses a numeral: people type
# "Dragon Slayer 2" and the page is "Dragon Slayer II".
_ROMAN_TAIL = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}


def _quest_aliases(name: str) -> set[str]:
    low = " ".join(name.lower().split())
    head, _, tail = low.rpartition(" ")
    if head and tail in _ROMAN_TAIL:
        return {low, f"{head} {_ROMAN_TAIL[tail]}"}
    return {low}


def _named_quest(question: str, names) -> str | None:
    """The quest a question is about, matching the longest name that fits.

    Longest, because 28 quest names are a prefix of another: "Dragon Slayer I"
    of "Dragon Slayer II", "Desert Treasure I" of "Desert Treasure II - The
    Fallen Empire", "Mage Arena I" of "Mage Arena II". Taking the first match
    answers confidently about the wrong quest, which is the failure this whole
    pass exists to stop.
    """
    asked = " ".join(question.lower().split())
    best: tuple[str, str] | None = None
    for name in names:
        for alias in _quest_aliases(name):
            if alias in asked and (best is None or len(alias) > len(best[1])):
                best = (name, alias)
    return best[0] if best else None


def _render_recipe(recipe: dict) -> str:
    """A recipe as the few facts an answer needs, not the whole payload."""
    name = recipe.get("page_name", "It")
    lines = []
    skills = recipe.get("skills") or []
    for skill in skills:
        level, named = skill.get("level"), skill.get("name")
        if level and named:
            boost = " (boostable)" if str(skill.get("boostable", "")).lower() == "yes" else ""
            experience = f", {skill['experience']} XP" if skill.get("experience") else ""
            lines.append(f"  {named} {level}{boost}{experience}")
    head = f"{name} requires:" if lines else f"{name} needs no skill level to make."

    materials = [
        f"{m.get('quantity', '')} x {m.get('name')}".strip(" x")
        for m in (recipe.get("materials") or [])
        if m.get("name")
    ]
    if materials:
        lines.append("  from: " + ", ".join(materials[:8]))
    tools = recipe.get("tools")
    if tools:
        # Omitting these actively made an answer worse. The handback says to use
        # exactly what it is given, so leaving the ammo mould out of a cannonball
        # recipe took it out of an answer that had mentioned it -- the eval case
        # went from 1/3 to 0/3 on "missing 'mould'". A block that tells the model
        # to use only these facts has to carry all of them.
        lines.append(
            "  with: " + (", ".join(map(str, tools)) if isinstance(tools, list) else str(tools))
        )
    if recipe.get("facilities"):
        lines.append(f"  at: {recipe['facilities']}")
    return "\n".join([head, *lines])


def _as_number(value: object) -> float | None:
    """A Bucket quantity as a number, or None if it is not one.

    Quantities come back as strings and are not all numeric: a recipe can say
    "1-3" or "2 (noted)". Guessing 1 for those would multiply a wrong number by
    a thousand and print it with a comma in it.
    """
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _recipe_xp(recipe: dict, skill: str = "") -> tuple[str, float] | None:
    """The skill a recipe trains and the XP one of it gives.

    A named skill is a requirement, not a preference. The shortlist for "how
    much gold to smelt for Smithing" contains Gold ore, whose recipe is real and
    trains Mining at 65 XP -- reading it anyway produces a confident count of
    ores for the wrong skill, which is worse than declining. With no skill
    named, the first entry carrying an XP figure wins, since a recipe listing
    exactly one skill is the common case.
    """
    for entry in recipe.get("skills") or []:
        name, experience = entry.get("name"), _as_number(entry.get("experience"))
        if not name or not experience:
            continue
        if skill and str(name).lower() != skill.lower():
            continue
        return str(name), experience
    return None


def _render_training_cost(recipe: dict, skill: str, low: int, high: int) -> str | None:
    """How many of a thing a level range takes, and what they cost to make.

    Both halves are exact and neither is prose. The XP gap is Jagex's published
    formula (:mod:`reldo.skills`), the XP per action is the recipe's own
    ``experience`` field, and the materials are its ``materials`` -- so a steel
    bar correctly bills two coal per action rather than one of everything.

    None when the recipe carries no XP figure, which leaves the caller to fall
    back to the bare gap rather than print a division it cannot do.
    """
    found = _recipe_xp(recipe, skill)
    if found is None:
        return None
    trained, per_action = found
    total = xp_between(low, high)
    actions = actions_needed(total, per_action)
    name = recipe.get("page_name", "each one")
    lines = [
        # Led with, and spelled as a finished sentence, for the reason
        # ge._verdict is: the model reliably copies a stated conclusion and
        # unreliably derives one. Told to report both figures it reported the
        # count and dropped the XP -- which is the number this whole pass exists
        # to produce, since the failure it replaced was claiming the wiki does
        # not give it. A conclusion it can copy is what gets both into the answer.
        f"ANSWER: {actions:,} x {name}, which is {total:,} {trained} XP "
        f"from {low} to {high}.",
        "",
        f"{trained} {low} -> {high}: {total:,} XP "
        f"({xp_for_level(low):,} -> {xp_for_level(high):,})",
        f"  {name} gives {per_action:,g} {trained} XP each, so that is "
        f"{actions:,} of them",
    ]
    materials = []
    for material in recipe.get("materials") or []:
        item = material.get("name")
        if not item:
            continue
        per = _as_number(material.get("quantity"))
        materials.append(
            f"{round(per * actions):,} x {item}" if per is not None
            else f"{item} (quantity varies)"
        )
    if materials:
        lines.append("  which costs: " + ", ".join(materials[:8]))
    if recipe.get("facilities"):
        lines.append(f"  at: {recipe['facilities']}")
    # In the rendered block, not only in the handback that sometimes wraps it.
    # There are two routes to these figures -- the training_cost tool, which the
    # model calls itself, and _force_training_cost, which injects them when it
    # will not -- and only the second said what to do with them. Measured on
    # "how much gold do i need to smelt from 48 to 50": three runs in three
    # fired no enforcement at all, because the model used the tool correctly, so
    # the guard skipped the pass and the instruction went with it.
    #
    # **This did not fix that case.** The line is here because closing the gap
    # between the two routes is right regardless, and it is a line of prose in a
    # block the model already reads. But it still answers "815 gold bars" and
    # drops the 18,319, which says the instruction is not what is missing:
    # SYSTEM_PROMPT tells it a one-line question gets a one-line answer, and
    # "how much gold do I need" is one. That is a prompt-level tension and
    # resolving it means changing a global rule for one case, which wants
    # measuring across all 28 first.
    lines.append(
        "  (report both: the count and the XP gap it covers, even if only the "
        "count was asked for)"
    )
    return "\n".join(lines)


def _render_requirements(title: str, requirements: dict[str, int], levels: dict[str, int]) -> str:
    """Requirements, compared against the asker where their levels are known.

    Shared by the tool and by the enforcement pass below so the model sees the
    identical text either way -- a handback that reads differently from the tool
    output invites it to treat one of them as the more authoritative.
    """
    if not requirements:
        return f"{title} has no skill or quest-point requirements."
    lines = [f"{title} requires:"]
    missing = []
    for skill, needed in sorted(requirements.items(), key=lambda kv: -kv[1]):
        have = levels.get(skill)
        if have is None:  # 'Quest points', or a skill we have no level for
            lines.append(f"  {needed} {skill}")
            continue
        ok = have >= needed
        lines.append(
            f"  {needed} {skill} -- you have {have}"
            + ("" if ok else f", short by {needed - have}")
        )
        if not ok:
            missing.append(f"{skill} {have}/{needed}")
    if levels:
        lines.append("Everything is met." if not missing else "Short of: " + ", ".join(missing))
    return "\n".join(lines)


# "what level to make X", "how do I make X". Narrow: the question has to be
# about *making* something, since that is the only thing a recipe answers.
_ASKS_HOW_TO_MAKE = re.compile(
    r"\b(?:make|makes|making|craft|crafting|cook|cooking|smith|smithing|"
    r"fletch|fletching|brew|brewing|smelt|smelting|create)\b"
    # "making money" is not a crafting question, and this fired on one: the
    # recipe pass went looking for a production template for the word "money"
    # and spent a Bucket query proving there isn't one. Coins are not an item
    # you smith. Gold is deliberately not excluded -- "make gold bars" is a
    # real Smithing question and the commonest one in the eval set.
    r"(?!\s+(?:money|gp|cash|coins)\b)",
    re.I,
)


# A question about what a quest needs, or whether the asker can start it. Narrow
# on purpose: merely mentioning a quest is not a requirements question, and
# firing on every mention would spend a Bucket query on "how do I kill the
# dragon in Dragon Slayer I".
_ASKS_REQUIREMENTS = re.compile(
    r"what (?:do|will) i need|what(?:'s| is) needed|requirement|require[sd]?\b|"
    r"prerequisite|prereq|am i ready|can i (?:do|start|begin|attempt)|"
    r"how do i (?:start|begin)|ready (?:for|to)|eligible|qualify",
    re.I,
)


# Tools whose output the model may read but may not treat as a source.
#
# search_wiki caps each summary at 120 characters precisely so it is too thin to
# answer from, and SYSTEM_PROMPT says every number must come from a page
# actually read -- yet the grounding check was counting a figure glimpsed in a
# teaser as shown. Measured on "what agility level do I need for the Seers'
# Village rooftop course": "60" appears in a search teaser, the model then read
# the *overview* page, whose only "60" is inside "260 marks of grace", answered
# 60, and the check passed it. The answer was right and its citation pointed at
# a page not containing the number, which is the ungrounded answer this whole
# apparatus exists to prevent -- it just happened to be ungrounded and correct.
NOT_GROUNDING = frozenset({"search_wiki"})


SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "What to look for. Works with exact in-game names "
                "('abyssal whip') and vague descriptions ('boss that heals itself')."
            ),
        }
    },
    "required": ["query"],
}

SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Exact page title."},
        "section": {
            "type": "string",
            "description": "Section index from list_page_sections, e.g. '11'.",
        },
    },
    "required": ["title", "section"],
}

PLAYER_SCHEMA = {
    "type": "object",
    "properties": {
        "username": {"type": "string", "description": "OSRS account name."}
    },
    "required": ["username"],
}

XP_SCHEMA = {
    "type": "object",
    "properties": {
        "from_level": {"type": "integer", "description": "Current level, 1-126."},
        "to_level": {"type": "integer", "description": "Target level, 1-126."},
        "xp_per_hour": {
            "type": "number",
            "description": "Optional XP/hr from a training guide, to get hours.",
        },
        "xp_per_action": {
            "type": "number",
            "description": "Optional XP per action, to get how many actions.",
        },
    },
    "required": ["from_level", "to_level"],
}

TRAINING_SCHEMA = {
    "type": "object",
    "properties": {
        "item": {
            "type": "string",
            "description": (
                "What they would be making, exactly as the wiki names it: "
                "'Gold bar', 'Yew longbow', 'Shark'. The product, not the raw "
                "material -- smelting gold ore makes a 'Gold bar'."
            ),
        },
        "from_level": {"type": "integer", "description": "Current level, 1-126."},
        "to_level": {"type": "integer", "description": "Target level, 1-126."},
    },
    "required": ["item", "from_level", "to_level"],
}

MATERIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "material": {
            "type": "string",
            "description": (
                "The material's exact page name, singular: 'Gold bar', "
                "'Yew logs', 'Molten glass'. Not the product made from it."
            ),
        }
    },
    "required": ["material"],
}

GP_SCHEMA = {
    "type": "object",
    "properties": {
        "goal_gp": {
            "type": "integer",
            "description": "The coin target, in gp. '5 mill' is 5000000.",
        },
        "gp_each": {
            "type": "number",
            "description": (
                "Optional price per item, to get how many you need. Use the live "
                "GE price from get_ge_price when the item is tradeable."
            ),
        },
        "gp_per_hour": {
            "type": "number",
            "description": "Optional gp/hr from a money-making guide, to get hours.",
        },
        "item_name": {
            "type": "string",
            "description": "What is being sold, e.g. 'sharks'.",
        },
        "inputs_per_item": {
            "type": "number",
            "description": (
                "Optional fixed exchange rate, when the thing you gather is not "
                "the thing you sell -- 40 minnows per shark is 40."
            ),
        },
        "input_name": {
            "type": "string",
            "description": "What is gathered, e.g. 'minnows'.",
        },
    },
    "required": ["goal_gp"],
}

REQUIREMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Exact page title."},
        "skill": {
            "type": "string",
            "description": "Optional skill to filter to, e.g. 'Sailing'.",
        },
    },
    "required": ["title"],
}

MONEY_SCHEMA = {
    "type": "object",
    "properties": {
        "skill": {
            "type": "string",
            "description": (
                "Optional skill to narrow to, e.g. 'Thieving'. Omit to get the "
                "best-paying method for every skill, which is what a 'which "
                "skill makes the most money' question wants."
            ),
        }
    },
}

UNLOCKS_SCHEMA = {
    "type": "object",
    "properties": {
        "skill": {"type": "string", "description": "Skill name, e.g. 'Sailing'."},
        "level": {"type": "integer", "description": "Maximum level, inclusive."},
        "page": {
            "type": "string",
            "description": (
                "Optional exact page holding the requirements table, e.g. "
                "'Shipbuilding'. Omit to search for it."
            ),
        },
    },
    "required": ["skill", "level"],
}

GE_SCHEMA = {
    "type": "object",
    "properties": {
        "item": {
            "type": "string",
            "description": (
                "Item name, exact or partial. A partial name returns every "
                "variant, which is usually what you want: 'granite' gets all "
                "three rock sizes plus the granite gear."
            ),
        }
    },
    "required": ["item"],
}

GE_COMPARE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Two or more item names to rank, e.g. ['sandstone', 'granite']. "
                "Partial names expand to all their variants."
            ),
        }
    },
    "required": ["items"],
}

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "item": {
            "type": "string",
            "description": "Item name or part of one, e.g. 'rune platebody'.",
        }
    },
    "required": ["item"],
}

QUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "quest": {
            "type": "string",
            "description": "Exact quest name, e.g. 'Dragon Slayer II'.",
        }
    },
    "required": ["quest"],
}

TITLE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Exact page title as returned by search_wiki, e.g. 'Abyssal whip'.",
        }
    },
    "required": ["title"],
}


def _tokens(text: str) -> set[str]:
    """Lowercased words, plurals folded, so "courses" matches "course"."""
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def _best_title(question: str, titles: list[str]) -> str:
    """The candidate title sharing the most words with the question.

    Crude on purpose -- it is picking between a handful of titles the retriever
    already shortlisted, not doing retrieval. For "what agility level do I need
    for the Seers' Village rooftop course" it scores "Seers' Village Rooftop
    Course" at 4 against "Ardougne Rooftop Course" at 2, which is the whole job.
    Ties keep shortlist order, so this can only improve on taking the first.

    Ties are the problem, though, and they are not rare. Asked "what quest do
    you need to complete to fight Vorkath", the shortlist offers Vorkath,
    Vorkath Master, Vorkath Veteran, Vorkath Speed-Runner and Vorkath/Strategies
    -- every one of them sharing exactly one word, so the winner was whichever
    came first. Measured at ten repeats: two passes in ten, and eight identical
    failures answering "the Vorkath Veteran achievement requires you to kill
    Vorkath 50 times". The page that answers, Vorkath, was in the list the whole
    time and states Dragon Slayer II outright.

    So the tie-break is surplus: among titles sharing the same number of the
    question's words, prefer the one carrying fewest words it did not ask about.
    "Vorkath" is the page about Vorkath; "Vorkath Veteran" is a page about an
    achievement that mentions it. One extra word is one step further from the
    subject, and that ordering is exactly what was missing.
    """
    wanted = _tokens(question)

    def score(title: str) -> tuple[int, int]:
        found = _tokens(title)
        return len(wanted & found), -len(found - wanted)

    return max(titles, key=score)


def _target_page(question: str, answer: Answer) -> str:
    """The page to fetch, or to name in the nudge.

    By title relevance, not shortlist rank. :meth:`_try_next_candidate` already
    makes this call and documents why; it had simply never been applied to the
    first read. Measured on "how much prayer experience does a dragon bone give
    when buried": the shortlist leads with "Prayer" -- the general skill page --
    and "Dragon bones" is second. The forced read fetched "Prayer" and the
    answer came back about burnt bones at 4.5 XP.

    SYSTEM_PROMPT tells the model in as many words to read the specific page
    rather than the skill overview. The pass that exists because the model will
    not obey that was disobeying it too.
    """
    return _best_title(question, answer.shortlist) if answer.shortlist else answer.top_hit


def _keep_best(previous: str, retried: str) -> str:
    """Never let a retry make the answer worse by making it empty.

    Each enforcement pass re-reads the final assistant turn, and a pass that
    ends on a tool call or exhausts its iterations leaves nothing to read. One
    run of "how long from 45 to 99 mining" returned the empty string that way --
    strictly worse than the guessed answer it replaced, and it reaches the user
    as a blank Discord embed.
    """
    return retried.strip() or previous


def _names(tools) -> set[str]:
    return {t.name for t in tools}


def _is_tool_leak(text: str, tool_names: Iterable[str]) -> bool:
    """Whether an "answer" is really a tool call the model typed out as prose.

    Measured on "how long does it take to get from 45 to 99 mining": the final
    assistant turn's entire content was ``read_wiki_page``. It is non-empty, so
    every guard here waved it through -- :func:`_keep_best` tests only that a
    retry is not blank -- and it reached the user as a Discord embed whose whole
    body read ``read_wiki_page``.

    :mod:`reldo.llm` documents the florid version of this, where
    ``qwen3-coder:30b`` writes a whole ``<tools>{...}`` blob into content. The
    mild version is worse, because a bare identifier looks like an answer to
    every length and emptiness check in the pipeline.

    Deliberately narrow. The text must *start* with the name of a tool that
    actually exists, followed by nothing or by the opening of an argument list.
    "read_wiki_page on Vorkath told me..." is prose about a tool and is left
    alone; a model explaining its own reasoning must not be silenced.
    """
    stripped = text.strip().strip("`").strip()
    head = re.match(r"[A-Za-z_][A-Za-z0-9_]*", stripped)
    if head is None or head.group(0) not in set(tool_names):
        return False
    rest = stripped[head.end() :].lstrip()
    return not rest or rest[0] in "({[\"'"


def _is_echo(text: str, injected: list[str]) -> bool:
    """Whether an "answer" is one of our own handbacks quoted back at us.

    The passes below hand the model instructions as user turns, and a model that
    has run out of ideas sometimes replies with the instruction. Measured on
    "how much money will 795 sharks bring on ge", one run in three: the whole
    answer was the grounding nudge verbatim, opening "You recalled them, which
    means they are probably out of date or simply wrong" -- addressed to the
    model, printed to the asker.

    Every emptiness and length check waves that through, exactly as they waved
    through a bare tool name before :func:`_is_tool_leak`. The difference here
    is that we know precisely what we said, so this needs no heuristic about
    what an answer looks like: if the reply is inside something we injected, it
    is not a reply.

    A prefix rather than the whole text, since the model usually quotes part.
    Forty characters is long enough that no genuine sentence collides with a
    handback by chance.
    """
    probe = " ".join(text.split())[:80]
    if len(probe) < 40:
        return False
    return any(probe in " ".join(one.split()) for one in injected)


def _last_assistant_text(messages: list[dict], tool_names: Iterable[str] = ()) -> str:
    """Text of the final assistant turn that is actually an answer.

    Skips pure tool-call turns, and turns whose content is a tool call the model
    wrote out instead of emitting -- see :func:`_is_tool_leak`. Filtered here
    rather than at the six call sites, for the same reason the tool recorder
    wraps centrally: a pass added later is covered by default.
    """
    # Everything we injected. The first user turn is the question; the rest are
    # handbacks this module wrote, and an answer that is one of them is not an
    # answer. See _is_echo.
    injected = [
        str(m.get("content") or "")
        for m in messages[2:]
        if m.get("role") == "user"
    ]
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        text = str(message.get("content") or "").strip()
        if not text or _is_tool_leak(text, tool_names):
            continue
        if _is_echo(text, injected):
            log.warning("Discarded an answer that was our own handback: %r", text[:60])
            continue
        return text
    return ""


# A backstop, not a target. Twelve enforcement passes now run under `ask`, each
# able to spend two or three model round trips, and several inject a whole wiki
# page as a fresh user turn -- so the worst case is ~40 calls and a message list
# well past what a 24B model's window holds. Nothing bounded either, and the
# overflow failure is the shape this project cares about most: a context that
# quietly loses its head produces a fluent answer with no error anywhere.
#
# Both numbers are deliberately far above anything measured. A normal question
# settles in 8 calls and ~25k characters; the heaviest cascade seen in the eval
# set is around 18 and ~90k. These stop the pathological case and are not meant
# to bind on a question that is merely hard -- tighten them with a measured
# `evals/answer_eval.py` run rather than by eye, since every pass they cut is a
# pass that was added because the model got something wrong without it.
MAX_ANSWER_SECONDS = 300.0
MAX_CONTEXT_CHARS = 180_000


def _fit_injection(
    text: str, budget: Budget | None, messages: list[dict], *, reserve: int = 2_000
) -> str:
    """As much of a page as the context ceiling still has room for.

    The two passes that hand the model a whole page inject up to 30k characters
    each, on top of a message list already carrying every tool result. Trimming
    here is what stops one of them being the thing that crosses the line.

    The reserve leaves space for the instruction wrapped around the page and for
    the model's own reply, neither of which is worth losing to fit more of an
    article that is already being cut.
    """
    if budget is None:
        return text
    room = budget.room(messages) - reserve
    if room >= len(text):
        return text
    if room <= 0:
        return ""
    log.warning("Trimmed an injected page from %d to %d characters", len(text), room)
    return text[:room]


def _context_chars(messages: list[dict]) -> int:
    """Roughly what the conversation costs, counted where it actually grows.

    Message content only. Tool-call payloads are names and short argument
    blobs; the size here is page bodies, which arrive as tool results and as
    the injected turns the passes below write.
    """
    return sum(len(str(m.get("content") or "")) for m in messages)


class Budget:
    """What one question may spend, across every pass in :meth:`WikiAgent.ask`.

    Per question rather than per agent: the Discord bot answers concurrently,
    and a shared budget would have one user's hard question starve another's.
    It rides on :class:`Answer` for the same reason everything else per-question
    does -- every pass already receives one, so a pass added later is inside the
    budget without its author having to remember, which is the lesson
    :func:`reldo.llm.client_for` and the tool recorder both exist to encode.

    Args:
        clock: Injectable so a test can exhaust a deadline without waiting.
    """

    def __init__(
        self,
        *,
        seconds: float = MAX_ANSWER_SECONDS,
        characters: int = MAX_CONTEXT_CHARS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.seconds = seconds
        self.characters = characters
        self._clock = clock
        self._started = clock()

    def elapsed(self) -> float:
        return self._clock() - self._started

    def spent(self, messages: list[dict]) -> str:
        """Why there is nothing left, or ``""`` while there still is.

        A sentence rather than a bool because it goes straight into the log
        line, and "which of the two ran out" is the only thing worth knowing
        when an answer comes back short of its usual enforcement.
        """
        if self.seconds and self.elapsed() >= self.seconds:
            return f"{self.elapsed():.0f}s of a {self.seconds:.0f}s budget"
        used = _context_chars(messages)
        if self.characters and used >= self.characters:
            return f"{used:,} of {self.characters:,} characters of context"
        return ""

    def room(self, messages: list[dict]) -> int:
        """Characters left before the ceiling. Used to trim what gets injected,
        rather than let one 30k page be the thing that crosses it.

        A ceiling of 0 disables the check, so the room is whatever the caller
        wanted; callers pass it to a slice, and slicing past the end is fine.
        """
        if not self.characters:
            return MAX_CONTEXT_CHARS
        return max(0, self.characters - _context_chars(messages))


@dataclass
class Answer:
    """A finished answer plus the pages that produced it."""

    text: str
    pages_read: list[str] = field(default_factory=list)
    searches: list[str] = field(default_factory=list)
    players_checked: list[str] = field(default_factory=list)
    prices_checked: list[str] = field(default_factory=list)
    xp_calculations: list[str] = field(default_factory=list)
    gp_calculations: list[str] = field(default_factory=list)
    # Skill -> level, from get_requirements. Kept as data rather than prose
    # so the answer can be checked against it: a Sailing question answered
    # with a Fishing level is caught here and nowhere else.
    skill_requirements: dict[str, int] = field(default_factory=dict)
    # Every title the last search offered, so a dead end can fall through to the
    # next candidate instead of giving up on the first page that disappoints.
    shortlist: list[str] = field(default_factory=list)
    unlocks_listed: list[str] = field(default_factory=list)
    # Every byte of tool output the model was shown. A number in the answer that
    # is not in here and not in the question did not come from the wiki.
    seen: list[str] = field(default_factory=list)
    # Top-ranked title from the most recent search, so the enforcement below can
    # name a page instead of saying "the most relevant result" and hoping.
    top_hit: str = ""
    # Quests whose requirements were actually looked up, so the enforcement
    # pass below can tell 'the model used the tool' from 'it did not'.
    quests_checked: list[str] = field(default_factory=list)
    # Items whose recipe was actually looked up, for the same reason.
    recipes_checked: list[str] = field(default_factory=list)
    # Set once the money-making guide has actually been ranked, so the pass
    # below can tell 'the model used the tool' from 'it answered from memory'.
    money_ranked: list[str] = field(default_factory=list)
    # What the asker's own client has reported: quests done, diaries, gear,
    # bank. None when there is no plugin or no linked account.
    profile: object = None
    # The asker's levels, when they are known, so a requirement check can be
    # done in code rather than by asking the model to compare two lists of
    # numbers. Per-Answer for the same reason as everything else here.
    player_levels: dict[str, int] = field(default_factory=dict)
    # Whose stats were put in front of the question. Deliberately NOT
    # players_checked: that field means "the model went and looked somebody up",
    # which is what makes a stats answer grounded. Ambient context about the
    # asker grounds nothing about Vorkath, and counting it as a tool call would
    # silently switch off the forced read for every linked user.
    player_context: str = ""
    # What this question is still allowed to spend. None means unbounded, which
    # is what a directly-constructed Answer gets: only `ask` sets one, and the
    # tests that build an Answer by hand should not have to know about it.
    budget: Budget | None = None
    # Set when a pass was skipped for want of budget, so a short answer can be
    # told from a complete one after the fact. The log line says which ran out.
    budget_exhausted: str = ""
    # Which enforcement passes actually ran, in order. Thirteen of them fire
    # conditionally and several can undo each other -- the shark question runs
    # the forced read, then the coin calculation, and then the excision throws
    # away what the first two built. From the answer text alone that reads as
    # flakiness; named, it reads as a pass eating another pass's work.
    passes_fired: list[str] = field(default_factory=list)
    # Sentences the grounding excision removed, so what it took can be read
    # back rather than inferred from a list of numbers.
    excised: list[str] = field(default_factory=list)

    @property
    def citations(self) -> list[str]:
        return [page_url(t) for t in self.pages_read]


class WikiAgent:
    """Wraps a local-model tool loop over a :class:`HybridRetriever`."""

    def __init__(
        self,
        retriever: HybridRetriever,
        client: ChatClient,
        *,
        max_tokens: int = 2048,
        user_agent: str = "",
        progress=None,
        direct=None,
        ge: GEClient | None = None,
        hiscores: HiscoresClient | None = None,
    ) -> None:
        self._retriever = retriever
        self._client = client
        self._max_tokens = max_tokens
        # Passed to the hiscores and GE clients so they identify as whoever is
        # running this, not as this repo. wiki.py refuses to start without one;
        # these two have a default, which is how every deployment ended up
        # announcing itself to Jagex under the author's name.
        self._user_agent = user_agent
        # Optional: XP snapshots, so the coaching can be about what the asker
        # actually did rather than only what they currently have.
        self._progress = progress
        # Tried before the model. Optional so an agent built without one behaves
        # exactly as it always has -- this is a fast path, never a requirement.
        self._direct = direct
        # Built once and reused. These were constructed per tool call, which
        # threw away the caches ge.py goes to the trouble of keeping -- the
        # /mapping payload is every tradeable item in the game and was being
        # refetched for each price lookup, three times over on a comparison.
        # Injected rather than built when the caller has already decided where
        # the data comes from. reldo.clients makes that decision once from the
        # settings; the agent stays neutral about it, which is the same reason
        # the persona lives at the front end and not in here.
        self._ge: GEClient | None = ge
        self._hiscores: HiscoresClient | None = hiscores
        self._bucket: BucketClient | None = None

    def use_direct(self, answerer) -> None:
        """Try this before the model on every question.

        A method rather than a constructor argument because the answerer shares
        the clients this agent owns, so it cannot be built until the agent
        exists. Public because three callers need it and reaching into
        ``_direct`` from outside is how a private stops meaning anything.
        """
        self._direct = answerer

    @property
    def bucket(self) -> BucketClient:
        """Structured wiki data. Shares the retriever's WikiClient, so Bucket
        queries are rate-limited alongside every other call to the wiki."""
        if self._bucket is None:
            self._bucket = BucketClient(self._retriever.client)
        return self._bucket

    @property
    def retriever(self) -> HybridRetriever:
        return self._retriever

    @property
    def wiki(self):
        """The underlying WikiClient, for callers that want the raw API."""
        return self._retriever.client

    @property
    def ge(self) -> GEClient:
        """Shared GE client. Going through here rather than building a second
        one is what lets the Discord ``/ge`` command hit an already-warm
        /mapping cache instead of refetching every tradeable item in the game."""
        return self._ge_client()

    @property
    def hiscores(self) -> HiscoresClient:
        return self._hiscores_client()

    def _ge_client(self) -> GEClient:
        if self._ge is None:
            self._ge = GEClient(**self._identity())
        return self._ge

    def _hiscores_client(self) -> HiscoresClient:
        if self._hiscores is None:
            self._hiscores = HiscoresClient(**self._identity())
        return self._hiscores

    def _identity(self) -> dict[str, str]:
        """Only override the client's own default when we were given something.

        Passing an empty string through would be worse than the default it
        replaced -- an unidentified agent rather than a misidentified one.
        """
        return {"user_agent": self._user_agent} if self._user_agent.strip() else {}

    async def aclose(self) -> None:
        """Close the clients this agent owns. Idempotent."""
        for client in (self._ge, self._hiscores):
            if client is not None:
                await client.aclose()
        self._ge = self._hiscores = None

    async def __aenter__(self) -> WikiAgent:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _build_tools(self, answer: Answer) -> list[Tool]:
        """Tools close over `answer` so we can report what was actually consulted.

        Rebuilt per question: the Discord bot serves concurrent users, and sharing
        these would cross-contaminate citations between them.
        """
        retriever = self._retriever
        # Retrieval is deterministic, so re-running a query cannot return
        # anything new -- but the model does not know that and will happily
        # spend the whole iteration budget rediscovering it. Measured: "how many
        # hours from 45 to 99 mining" issued the identical search five times,
        # hit max_iterations, and answered from the wrong page.
        searched: dict[str, str] = {}

        async def search_wiki(query: str) -> str:
            key = " ".join(query.lower().split())
            if key in searched:
                return (
                    f"You already searched for {query!r} this turn. Retrieval is "
                    "deterministic, so running it again returns exactly this and "
                    "wastes a step. Either read one of these pages with "
                    "read_wiki_page, or search genuinely different wording.\n\n"
                    + searched[key]
                )

            answer.searches.append(query)
            hits = await retriever.shortlist(query, k=8)
            if not hits:
                result = f"No results for {query!r}. Try different wording."
                searched[key] = result
                return result
            answer.top_hit = hits[0].title
            answer.shortlist = [h.title for h in hits]
            # Deliberately short. A 400-char summary is often *just* enough for a
            # small model to answer from, which is how you get an uncited answer
            # that is confidently wrong. This is enough to choose a page, not
            # enough to substitute for reading one.
            result = "\n".join(f"- {h.title}: {h.summary[:120]}" for h in hits)
            searched[key] = result
            return result

        async def read_wiki_page(title: str) -> str:
            pages = await retriever.fetch([title])
            if not pages:
                return f"No page titled {title!r}. Use an exact title from search_wiki."
            answer.pages_read.append(pages[0].title)
            return pages[0].text

        async def list_page_sections(title: str) -> str:
            sections = await retriever.sections(title)
            if not sections:
                return f"No sections found for {title!r}."
            return "\n".join(sections)

        async def get_player_stats(username: str) -> str:
            try:
                player = await self._hiscores_client().lookup(username)
            except HiscoresError as exc:
                return str(exc)
            answer.players_checked.append(player.name)
            return player.summary()

        async def get_ge_price(item: str) -> str:
            try:
                prices = await self._ge_client().lookup(item)
            except GEError as exc:
                return str(exc)
            if not prices:
                # Phrased as a lookup miss, not as a fact about the item. This
                # used to lead with untradeability, and the model repeated it as
                # one: "Sharks are untradeable and have no GE price", about an
                # item that trades in the hundreds of thousands daily. A failed
                # match is evidence about the name, and only then about the item.
                return (
                    f"Nothing in the Grand Exchange catalogue is named {item!r}. "
                    "That is usually the name rather than the item -- try the "
                    "exact singular the wiki uses. If it really is not listed, "
                    "it is untradeable and has no GE price at all; check the "
                    "wiki page for a shop price instead. Do not say an item is "
                    "untradeable on the strength of one failed lookup."
                )
            answer.prices_checked.append(item)
            return "\n".join(p.summary() for p in prices)

        async def compare_ge_prices(items: list[str]) -> str:
            if isinstance(items, str):  # some models send a bare string
                items = [items]
            found = []
            ge = self._ge_client()
            try:
                for name in items:
                    found.extend(await ge.lookup(name))
            except GEError as exc:
                return str(exc)
            if not found:
                return f"No tradeable items matched any of: {', '.join(items)}."
            answer.prices_checked.extend(items)
            # De-duplicate: overlapping queries ("granite", "granite maul") would
            # otherwise list the same row twice and skew the ranking's look.
            unique = list({p.item.id: p for p in found}.values())
            return compare(unique)

        async def read_wiki_table(title: str) -> str:
            try:
                tables = await retriever.client.tables(title)
            except Exception as exc:
                return f"Could not read tables on {title!r}: {exc}"
            answer.pages_read.append(title)
            return tables_as_text(tables)

        async def list_unlocks(skill: str, level: int, page: str | None = None) -> str:
            found, _ = await gather_unlocks(retriever, skill, int(level), page)
            if not found:
                return (
                    f"No table with a '{skill} level' column found. Name the page "
                    "that holds the requirements table if you know it."
                )
            answer.pages_read.extend(dict.fromkeys(u.source.split(" - ")[0] for u in found))
            answer.unlocks_listed.append(f"{skill} {level}")
            return render(dedupe(found), skill, int(level))

        async def calculate_xp(
            from_level: int,
            to_level: int,
            xp_per_hour: float | None = None,
            xp_per_action: float | None = None,
        ) -> str:
            try:
                result = plan(
                    int(from_level),
                    int(to_level),
                    xp_per_hour=xp_per_hour,
                    xp_per_action=xp_per_action,
                )
            except ValueError as exc:
                return f"Cannot compute that: {exc}"
            answer.xp_calculations.append(f"{from_level}->{to_level}")
            return result


        async def training_cost(item: str, from_level: int, to_level: int) -> str:
            """How many of something a level range takes, end to end.

            calculate_xp already had the arithmetic and check_recipe already had
            the XP per action, and the model would not compose them: asked how
            much gold to smelt from 48 to 50 Smithing it read three pages and
            answered that the wiki does not give the XP in a form it can read.
            The XP table is not on the wiki at all -- it is a formula, in
            skills.py -- so no amount of reading was ever going to produce it.
            """
            try:
                found = await self.bucket.recipe(item)
            except BucketError as exc:
                return f"Could not read the recipe: {exc}"
            if found is None:
                return (
                    f"No recipe for {item!r}, so there is no XP-per-action to "
                    "divide by. If it is gathered rather than made -- ore, logs, "
                    "fish -- read the XP per action off the skill's guide and "
                    "pass it to calculate_xp as xp_per_action."
                )
            try:
                rendered = _render_training_cost(found, "", int(from_level), int(to_level))
            except ValueError as exc:
                return f"Cannot compute that: {exc}"
            if rendered is None:
                return (
                    f"The recipe for {item!r} gives no XP figure, so the count "
                    "cannot be computed. " + _render_recipe(found)
                )
            title = found.get("page_name", item)
            answer.recipes_checked.append(title)
            answer.pages_read.append(title)
            answer.xp_calculations.append(f"{from_level}->{to_level}")
            return rendered

        async def made_from(material: str) -> str:
            """Everything the wiki says is made from something.

            The set half of "what made from gold bars sells best". Naming the
            members yourself is the list_unlocks mistake in a different skin:
            the model named the gold necklace, amulet and bracelet, and the
            wiki lists forty, because every gem ring is a gold bar plus a gem.
            """
            try:
                products = await self.bucket.products_of(material)
            except BucketError as exc:
                return f"Could not list what is made from that: {exc}"
            if not products:
                return (
                    f"Nothing on the wiki is made from {material!r}. Check the "
                    "exact page name -- it is the singular ('Gold bar', not "
                    "'gold bars') and it is the material, not the product."
                )
            answer.pages_read.append(material)
            return f"Made from {material} ({len(products)}): " + ", ".join(products)

        async def check_quest(quest: str) -> str:
            """Whether the asker has actually done a quest.

            Requirements come from the wiki; this is the other half, and no
            public API has it -- Jagex publishes quest completion nowhere, so
            the only source is the player's own client. Without it "am I ready
            for X" can compare levels and has to guess at every quest gate.
            """
            profile = answer.profile
            if profile is None:
                return (
                    "No quest data for this player. It comes from the RuneLite "
                    "plugin, which is either not installed or has not reported "
                    "yet. Say that you cannot see their quest log rather than "
                    "assuming either way."
                )
            state = profile.quest_state(quest)
            done = len(profile.quests_finished)
            return (
                f"{quest}: {state} (from their own client; they have {done} "
                "quests finished)"
            )

        async def check_inventory(item: str) -> str:
            """Whether the asker owns something, across worn gear and bank."""
            profile = answer.profile
            if profile is None:
                return (
                    "No equipment or bank data for this player -- the RuneLite "
                    "plugin is not reporting. Do not assume they have or lack it."
                )
            found = profile.holding(item)
            if found:
                return f"{item}: " + ", ".join(found[:12])
            if profile.bank is None:
                return (
                    f"No {item!r} worn. Their bank has not been seen -- the "
                    "client only reports it once they open it -- so this is not "
                    "evidence they do not own one."
                )
            return f"No {item!r} in their worn gear or their bank."

        async def check_recipe(item: str) -> str:
            """What making something actually requires, from structured data.

            The level lives in the page's production template, which
            read_wiki_page strips. Left to read the prose around it the model
            picks whichever nearby number looks like a requirement -- the
            burn-free Cooking level for a shark, the wield level beside the
            mining level for a pickaxe.
            """
            try:
                found = await self.bucket.recipe(item)
            except BucketError as exc:
                return f"Could not read the recipe: {exc}"
            if found is None:
                return (
                    f"No recipe for {item!r} -- it is not something you make. It "
                    "may be a drop, a shop item or a reward, so look for a "
                    "requirement to *use* it rather than to create it."
                )
            answer.recipes_checked.append(found.get("page_name", item))
            answer.pages_read.append(found.get("page_name", item))
            return _render_recipe(found)

        async def check_quest_requirements(quest: str) -> str:
            """Requirements as numbers, compared in code against the asker.

            The same call skills.py and ge.py make. The wiki marks each
            requirement up with the skill and the level as attributes, so this
            reads a machine-readable field rather than a sentence -- and the
            comparison against the asker's levels is arithmetic, which is where
            a 24B model drops a row and sounds complete.
            """
            try:
                found = await self.bucket.quest_requirements(quest)
            except BucketError as exc:
                return f"Could not read quest requirements: {exc}"
            if found is None:
                return (
                    f"No quest called {quest!r} on the wiki. Check the exact name "
                    "with search_wiki -- sequels are numbered ('Dragon Slayer II')."
                )
            title, requirements = found
            answer.quests_checked.append(title)
            # The requirements are that page's own structured data, so citing it
            # points a reader at the source they can check -- the same call
            # _force_read makes about a page it fetched directly. It also stops
            # the read enforcement below treating a Bucket-grounded answer as
            # ungrounded and forcing a page read it does not need.
            answer.pages_read.append(title)
            return _render_requirements(title, requirements, answer.player_levels)

        async def calculate_gp(
            goal_gp: int,
            gp_each: float | None = None,
            gp_per_hour: float | None = None,
            item_name: str = "item",
            inputs_per_item: float | None = None,
            input_name: str = "input",
        ) -> str:
            try:
                result = gp_plan(
                    int(goal_gp),
                    gp_each=gp_each,
                    gp_per_hour=gp_per_hour,
                    item_name=item_name or "item",
                    inputs_per_item=inputs_per_item,
                    input_name=input_name or "input",
                )
            except ValueError as exc:
                return f"Cannot compute that: {exc}"
            answer.gp_calculations.append(f"{goal_gp}")
            answer.seen.append(result)
            return result

        async def get_requirements(title: str, skill: str | None = None) -> str:
            try:
                found = await retriever.client.requirements(title)
            except Exception as exc:
                return f"Could not read requirements on {title!r}: {exc}"
            answer.pages_read.append(title)
            # Highest wins on a repeat: a page states the same skill at several
            # levels (the item's own requirement, then a money-making method
            # needing more of it), and the binding one is the larger.
            merged: dict[str, int] = {}
            for name, level in found:
                merged[name] = max(merged.get(name, 0), level)
            answer.skill_requirements.update(merged)
            listing = ", ".join(f"{k} {v}" for k, v in merged.items())
            if skill:
                wanted = skill.strip().capitalize()
                level = merged.get(wanted)
                if level is None:
                    return (
                        f"{title} states no {wanted} requirement. It requires: "
                        + (listing or "nothing")
                    )
                return f"{title} requires {wanted} {level}."
            if not merged:
                return f"{title} states no skill requirements."
            return f"{title} requires: {listing}"

        async def best_money_methods(skill: str | None = None) -> str:
            """Rank the money-making guide by its own gp/hour column."""
            try:
                methods = await read_guide(retriever.client, SKILLING_GUIDE_PAGE)
            except Exception as exc:
                return f"Could not read the money-making guide: {exc}"
            if not methods:
                return "The money-making guide's tables could not be read."
            if skill:
                wanted = skill.strip().lower()
                found = [m for m in methods if m.skill.lower() == wanted]
                if not found:
                    return (
                        f"No {skill} methods with an hourly profit are listed. Call "
                        "this without a skill to see which skills pay best."
                    )
                answer.money_ranked.append(skill)
                answer.pages_read.append(SKILLING_GUIDE_PAGE)
                return render_methods(rank_methods(found), by_skill=False)
            answer.money_ranked.append("all")
            answer.pages_read.append(SKILLING_GUIDE_PAGE)
            return render_methods(best_per_skill(methods))

        async def read_wiki_section(title: str, section: str) -> str:
            try:
                text = await retriever.section_text(title, section)
            except Exception as exc:
                return f"Could not read section {section!r} of {title!r}: {exc}"
            answer.pages_read.append(title)
            return text

        tools = [
            Tool(
                name="search_wiki",
                description=(
                    "Search the OSRS Wiki and get back matching page titles with "
                    "summaries. Combines keyword and semantic search, so it handles "
                    "both exact in-game names and vague descriptions. Call this first, "
                    "and again with different wording if the results look wrong."
                ),
                parameters=SEARCH_SCHEMA,
                handler=search_wiki,
            ),
            Tool(
                name="read_wiki_page",
                description=(
                    "Read the full text of one wiki page. Use this on the most "
                    "relevant search result before answering -- summaries alone are "
                    "not enough to answer from."
                ),
                parameters=TITLE_SCHEMA,
                handler=read_wiki_page,
            ),
            Tool(
                name="list_page_sections",
                description=(
                    "List a page's section headings with their indices. Use this "
                    "FIRST on long guides -- skill training guides, quest "
                    "walkthroughs, money-making guides -- then read only the "
                    "section you need with read_wiki_section. A training guide is "
                    "organised by level bracket ('Levels 45-99: Granite'), so the "
                    "section list tells you exactly where the answer is."
                ),
                parameters=TITLE_SCHEMA,
                handler=list_page_sections,
            ),
            Tool(
                name="get_player_stats",
                description=(
                    "Look up a player's levels on the official OSRS hiscores. Call "
                    "this whenever the question is about *them* -- 'am I ready for', "
                    "'what should I train', 'can I do X yet' -- and whenever they "
                    "give you their username. Compare the returned levels against "
                    "requirements you read from the wiki."
                ),
                parameters=PLAYER_SCHEMA,
                handler=get_player_stats,
            ),
            Tool(
                name="get_ge_price",
                description=(
                    "Live Grand Exchange price for one item, with how many traded "
                    "in the last 24 hours. ALWAYS use this for any question about "
                    "price, value, profit or what something sells for -- prices "
                    "move constantly and anything you remember is out of date. "
                    "Report the volume alongside the price: a high price on an "
                    "item nobody trades is not money you can actually make. For "
                    "comparing two or more items use compare_ge_prices instead -- "
                    "it ranks them correctly, which separate lookups do not."
                ),
                parameters=GE_SCHEMA,
                handler=get_ge_price,
            ),
            Tool(
                name="compare_ge_prices",
                description=(
                    "Rank several items by what they sell for, with volume and "
                    "liquidity for each. Use this for 'what is best to sell', "
                    "'is X or Y worth more', and any question comparing what to "
                    "gather, mine or flip. Pass partial names to cover every "
                    "variant at once -- ['sandstone', 'granite'] compares all the "
                    "rock sizes together."
                ),
                parameters=GE_COMPARE_SCHEMA,
                handler=compare_ge_prices,
            ),
            Tool(
                name="read_wiki_table",
                description=(
                    "Read a page's TABLES, which read_wiki_page cannot see at "
                    "all -- it silently drops every table. Level requirements, "
                    "item stats, drop rates and comparison charts live in "
                    "tables, so if read_wiki_page seemed to be missing the "
                    "number you wanted, it was: call this instead. 'Cannonball' "
                    "has no Smithing level in its text and 'Steel cannonball | "
                    "35' in its table."
                ),
                parameters=TITLE_SCHEMA,
                handler=read_wiki_table,
            ),
            Tool(
                name="calculate_gp",
                description=(
                    "Work out how many of an item you must sell to reach a coin "
                    "goal, and how long that takes at a stated gp/hr. ALWAYS use "
                    "this instead of doing the division yourself for any question "
                    "with a coin target -- 'how do I make 5m', 'how many sharks "
                    "for 10 mill', 'how long to afford a whip'. Take the price "
                    "from get_ge_price and the gp/hr from the money-making guide."
                ),
                parameters=GP_SCHEMA,
                handler=calculate_gp,
            ),
            Tool(
                name="get_requirements",
                description=(
                    "Every skill requirement a page states, as exact skill/level "
                    "pairs. Use this for ANY 'what level do I need' question, "
                    "especially when more than one skill is involved -- deep sea "
                    "trawling needs both Sailing and Fishing, and the page's prose "
                    "usually names only one of them. Pass skill to filter."
                ),
                parameters=REQUIREMENTS_SCHEMA,
                handler=get_requirements,
            ),
            Tool(
                name="list_unlocks",
                description=(
                    "Everything a skill unlocks at or below a level, read out of "
                    "the wiki's requirement tables and filtered in code. Use this "
                    "for 'what can I build/make/wear at X level', 'everything up "
                    "to level N', and any request for a complete list. Do NOT try "
                    "to assemble such a list yourself from a page: the numbers are "
                    "in tables that read_wiki_page cannot see at all, and filtering "
                    "thirty rows by hand is where you drop one and sound complete."
                ),
                parameters=UNLOCKS_SCHEMA,
                handler=list_unlocks,
            ),
            Tool(
                name="calculate_xp",
                description=(
                    "Exact XP between two levels, and optionally how many actions "
                    "or hours that takes. ALWAYS use this instead of doing the "
                    "arithmetic yourself -- get the XP/hr or XP/action figure from "
                    "a wiki guide, then pass it here. Your mental arithmetic on "
                    "seven-digit numbers is not reliable."
                ),
                parameters=XP_SCHEMA,
                handler=calculate_xp,
            ),
            Tool(
                name="training_cost",
                description=(
                    "How MANY of something it takes to train a skill between two "
                    "levels, with the materials that many costs. Use this for "
                    "'how much gold do I need to smelt from 48 to 50 Smithing', "
                    "'how many yew logs from 60 to 99 Fletching' -- any 'how "
                    "many/how much X to get from A to B'. It does the whole "
                    "thing: the XP gap comes from the level formula, the XP per "
                    "action from the item's own recipe. Do not try to assemble "
                    "it from check_recipe and mental arithmetic, and never say "
                    "the wiki does not give the XP for a level range -- the XP "
                    "table is a formula, not a page."
                ),
                parameters=TRAINING_SCHEMA,
                handler=training_cost,
            ),
            Tool(
                name="made_from",
                description=(
                    "Every item the wiki says is made from a material, read out "
                    "of its recipe data. Use this BEFORE compare_ge_prices on "
                    "any 'what made from X is worth most / sells best' question "
                    "-- naming the members yourself is inventing the set, and "
                    "the set is the answer. Forty things are made from a gold "
                    "bar, and the three anyone thinks of are not the valuable "
                    "ones. Pass the material's exact singular page name."
                ),
                parameters=MATERIAL_SCHEMA,
                handler=made_from,
            ),
            Tool(
                name="check_quest",
                description=(
                    "Whether this player has actually completed a quest, from "
                    "their own game client. The hiscores do not carry quest "
                    "completion and neither does any third party, so this is "
                    "the only way to know. Use it alongside "
                    "check_quest_requirements: that gives the gate, this gives "
                    "whether they are through it."
                ),
                parameters=QUEST_SCHEMA,
                handler=check_quest,
            ),
            Tool(
                name="check_inventory",
                description=(
                    "Whether the player owns an item, across worn equipment and "
                    "bank, from their own client. Use for 'do I have', 'am I "
                    "geared for'. A bank that has not been opened this session "
                    "is not visible, and the tool says so rather than reporting "
                    "an empty bank."
                ),
                parameters=ITEM_SCHEMA,
                handler=check_inventory,
            ),
            Tool(
                name="check_recipe",
                description=(
                    "Exact skill level to MAKE something -- cook, smith, craft, "
                    "fletch, brew -- with the materials and XP, read from the "
                    "wiki's structured data. Use this for 'what level do I need "
                    "to make/cook/smith X'. The level is in a template that "
                    "read_wiki_page strips, so reading the page instead means "
                    "picking whichever nearby number looks right: the level you "
                    "stop burning a fish is not the level you can cook it."
                ),
                parameters=ITEM_SCHEMA,
                handler=check_recipe,
            ),
            Tool(
                name="check_quest_requirements",
                description=(
                    "Exact skill and quest-point requirements for a quest, read "
                    "from the wiki's structured data and compared against the "
                    "asker's own levels in code. ALWAYS use this for 'can I do "
                    "X', 'am I ready for X', 'what do I need for X' rather than "
                    "reading the quest page: the requirements are a marked-up "
                    "list, and totalling one up by eye is where you drop a row "
                    "and sound complete. It tells you what they are short of."
                ),
                parameters=QUEST_SCHEMA,
                handler=check_quest_requirements,
            ),
            Tool(
                name="best_money_methods",
                description=(
                    "Which skills and methods actually make the most money, "
                    "ranked from the wiki's money-making guide by its own "
                    "gp/hour column. USE THIS for 'what skill is best for "
                    "making money', 'best money maker', 'most profitable "
                    "skill'. Those figures are in a table, and read_wiki_page "
                    "cannot see tables at all -- it returns the guide with "
                    "every number stripped out, which is why the page looks "
                    "as though it does not say. The wiki computes that column "
                    "from live Grand Exchange prices."
                ),
                parameters=MONEY_SCHEMA,
                handler=best_money_methods,
            ),
            Tool(
                name="read_wiki_section",
                description=(
                    "Read one section of a page, by the index from "
                    "list_page_sections. Prefer this over read_wiki_page on long "
                    "guides: it returns the part that answers the question instead "
                    "of the first chunk of a 45,000-character walkthrough."
                ),
                parameters=SECTION_SCHEMA,
                handler=read_wiki_section,
            ),
        ]

        # Every tool result the model sees, recorded for the grounding check.
        # Wrapping here rather than inside each handler means a tool added later
        # is covered by default -- the opposite of the client_for lesson, where
        # three call sites each had to remember.
        for tool in tools:
            handler = tool.handler
            grounds = tool.name not in NOT_GROUNDING

            async def recorded(_handler=handler, _grounds=grounds, **kwargs):
                result = await _handler(**kwargs)
                if _grounds:
                    answer.seen.append(str(result))
                return result

            tool.handler = recorded
        return tools

    def _out_of_budget(self, answer: Answer, messages: list[dict], what: str) -> bool:
        """Whether a pass should not start. Checked *before* it does any work.

        :meth:`_run_tools` alone is not enough for the passes below, and the
        difference is not cosmetic. Each of them records what it found -- into
        ``pages_read``, and into ``seen`` -- and only then re-asks the model. Let
        one run to the round trip and get refused there and it has claimed a
        citation for a page nobody read, and grown ``seen`` with text the model
        was never shown. ``seen`` is what :func:`_ungrounded_numbers` trusts to
        mean "shown", so inflating it does not merely mislabel a source, it
        punches a hole in the invented-number check: a figure the model recalled
        would match the injection it never received and survive the excision.

        So: ask first, work second. The round-trip guard stays underneath as a
        backstop for the nudges in `ask`, which append no findings and are safe
        to abandon at the call.
        """
        budget = answer.budget
        if budget is None:
            answer.passes_fired.append(what)
            return False
        spent = budget.spent(messages)
        if not spent:
            # Noted here rather than at thirteen call sites, for the reason the
            # tool recorder gives: a pass added later is instrumented by
            # default. This is already the one line every recording pass runs
            # before it does anything.
            answer.passes_fired.append(what)
            return False
        if not answer.budget_exhausted:
            log.warning("Budget exhausted (%s); skipping %s", spent, what)
        answer.budget_exhausted = spent
        return True

    async def _run_tools(
        self, messages: list[dict], tools: list[Tool], answer: Answer, *, max_iterations: int
    ) -> list[dict]:
        """Every model round trip under :meth:`ask` goes through here.

        One choke point rather than a check in each of the twelve passes, and
        for the reason the tool recorder above gives: a pass added later is
        inside the budget by default, where twelve call sites would each be a
        chance to forget -- and a forgotten one does not fail, it silently
        restores the unbounded cascade this exists to stop.

        An exhausted budget returns the conversation untouched, which every
        caller already handles: a pass whose ``run_tools`` changes nothing
        leaves the previous draft in place, and :func:`_keep_best` keeps it.
        The passes are ordered by how much they matter, so what gets dropped
        when the budget runs out is the tail rather than an arbitrary slice.
        """
        budget = answer.budget
        if budget is not None:
            spent = budget.spent(messages)
            if spent:
                if not answer.budget_exhausted:
                    log.warning(
                        "Budget exhausted (%s); skipping the remaining enforcement",
                        spent,
                    )
                answer.budget_exhausted = spent
                return messages
        return await self._client.run_tools(
            messages, tools, max_iterations=max_iterations, max_tokens=self._max_tokens
        )

    async def _force_read(
        self, messages: list[dict], tools: list[Tool], answer: Answer, question: str
    ) -> list[dict]:
        """Fetch the top hit and hand the model its text, having asked twice.

        The same principle as :mod:`reldo.skills` and the GE verdict: what the
        model does unreliably and we can do exactly, we do exactly. Recording
        the page in ``pages_read`` is honest -- it really was fetched and really
        is what the answer is now derived from, so the citation points at the
        source a reader can check.
        """
        if self._out_of_budget(answer, messages, "the forced read"):
            return messages

        target = _target_page(question, answer)
        pages = await self._retriever.fetch([target])
        if not pages:
            log.warning("Forced read of %r found no page", target)
            return messages

        page = pages[0]
        # Trimmed before it is recorded, not after. `seen` means "what the model
        # was actually shown" and the grounding check reads it as exactly that,
        # so recording the whole page while injecting part of one would bless
        # every figure in the half that never arrived.
        body = _fit_injection(page.text, answer.budget, messages)
        if not body:
            log.warning("No room left to inject %r; skipping the forced read", page.title)
            return messages

        answer.pages_read.append(page.title)
        answer.seen.append(body)
        log.warning("Fetched %r directly and injected it", page.title)
        messages.append(
            {
                "role": "user",
                "content": (
                    f"You did not read it, so here is {page.title!r} in full. "
                    "Answer the original question using only what appears below, "
                    "with the concrete numbers it gives. Do not say you were "
                    "unable to access it -- it is right here.\n\n" + body
                ),
            }
        )
        return await self._run_tools(
            messages, tools, answer, max_iterations=2
        )

    async def _try_next_candidate(
        self, messages: list[dict], tools: list[Tool], answer: Answer, question: str
    ) -> list[dict]:
        """Fall through to the next search hit when the page read was a dud.

        Asked for the Seers' Village Agility requirement, the model read
        'Rooftop Agility Courses' and reported "The Seers' Village Rooftop
        Course is not mentioned on this page." It was right -- that overview
        genuinely does not state it -- and then stopped, having read *a* page
        without answering. The read enforcement above cannot catch this: its
        test is that something was read, not that the something helped.

        The shortlist already holds the other candidates, and for this question
        every remaining 'Seers' page does state 60.
        """
        if self._out_of_budget(answer, messages, "the dead-end fall-through"):
            return messages

        unread = [t for t in answer.shortlist if t not in answer.pages_read]
        if not unread:
            return messages

        # By title relevance, not shortlist order. Taking the next hit blindly
        # sent the Seers question to 'Ardougne Rooftop Course' -- a second
        # rooftop-course page that also does not mention Seers. The ranking that
        # produced the dud first is not the one to trust for the retry.
        pages = await self._retriever.fetch([_best_title(question, unread)])
        if not pages:
            return messages

        page = pages[0]
        # Trimmed before it is recorded; see _force_read.
        body = _fit_injection(page.text, answer.budget, messages)
        if not body:
            log.warning("No room left to inject %r; skipping the fall-through", page.title)
            return messages

        answer.pages_read.append(page.title)
        answer.seen.append(body)
        log.warning("Dead end; falling through to %r", page.title)
        messages.append(
            {
                "role": "user",
                "content": (
                    f"That page did not answer it, so here is {page.title!r} "
                    "instead. Answer the original question from this text. If it "
                    "genuinely is not here either, say so plainly rather than "
                    "offering to search again -- this is your last "
                    f"source.\n\n{body}"
                ),
            }
        )
        return await self._run_tools(
            messages, tools, answer, max_iterations=2
        )

    async def _force_unlocks(
        self, messages: list[dict], tools: list[Tool], answer: Answer, skill: str, level: int
    ) -> list[dict]:
        """Build the list ourselves. Asked "what can I build at Sailing 20", the
        model skipped list_unlocks entirely, went straight to a page it
        remembered, and answered "an ironwood mast" -- which is level 81. The
        requirement tables are invisible to read_wiki_page, so an unaided answer
        here is not a misread, it is invention."""
        if self._out_of_budget(answer, messages, "the unlock listing"):
            return messages

        found, _ = await gather_unlocks(self._retriever, skill, level)
        if not found:
            return messages

        listing = render(dedupe(found), skill, level)
        answer.pages_read.extend(dict.fromkeys(u.source.split(" - ")[0] for u in found))
        answer.seen.append(listing)
        log.warning("Built the %s %d list directly and injected it", skill, level)
        messages.append(
            {
                "role": "user",
                "content": (
                    "Here is the actual list, read from the wiki's requirement "
                    f"tables and filtered in code:\n\n{listing}\n\nAnswer using "
                    "exactly these entries. Do not add anything from memory and do "
                    "not drop any."
                ),
            }
        )
        return await self._run_tools(
            messages, tools, answer, max_iterations=2
        )

    async def _force_xp_calc(
        self, messages: list[dict], tools: list[Tool], answer: Answer, question: str
    ) -> list[dict]:
        """Do the arithmetic ourselves when the model will not.

        Asked for hours from 45 to 99 Mining, mistral-small3.2:24b answered the
        nudge with "I apologize, but I currently don't have the tools needed to
        perform the calculations you're asking for" -- while holding
        calculate_xp. Third time this shape has appeared, after the read nudge
        and the GE ranking, and the answer is the same each time: stop asking a
        24B model to do what a division does exactly.

        The levels come from the question and the rate from whichever of the
        question or the draft answer states one; with no rate we still hand back
        the exact XP gap, which is the part it gets wrong anyway.
        """
        if self._out_of_budget(answer, messages, "the XP calculation"):
            return messages

        match = _LEVEL_RANGE.search(question)
        if not match:
            return messages
        low, high = int(match.group(1)), int(match.group(2))
        if not 1 <= low < high <= MAX_LEVEL:
            return messages

        rate = per_action = None
        for source in (question, answer.text):
            found = _XP_RATE.search(source)
            if found:
                rate = float(found.group(1).replace(",", "")) * (1000 if found.group(2) else 1)
                break
        # And the per-action rate, for the gathering skills that have no recipe
        # to read one out of: "yew logs give 175 xp each" is a quoted fact, and
        # dividing it into the gap is the half the model gets wrong.
        for source in (question, answer.text):
            found = _XP_PER_ACTION.search(source)
            if found:
                per_action = float(found.group(1).replace(",", ""))
                break

        # Neither rate anywhere, which is the state in which this pass hands
        # back a bare XP gap and the model says the wiki does not give the rate.
        # Measured on "how long from 45 to 99 mining at granite": the read nudge
        # fires, nothing finds a rate, and the answer is "The wiki does not give
        # the XP rate for 3-tick mining granite" -- true of the prose and false
        # of the page. Same shape as the planks case, so the same remedy: the
        # rates are in a table, and _force_training_cost already goes and looks.
        # This is the duration half of the question doing the same.
        if rate is None and per_action is None:
            skill = _named_skill(question)
            if skill and await self._read_tables_for_a_rate(
                messages, answer, question, skill
            ):
                return await self._run_tools(messages, tools, answer, max_iterations=2)

        try:
            computed = plan(low, high, xp_per_hour=rate, xp_per_action=per_action)
        except ValueError:
            return messages

        answer.xp_calculations.append(f"{low}->{high}")
        answer.seen.append(computed)
        log.warning("Computed %d->%d directly and injected it", low, high)
        messages.append(
            {
                "role": "user",
                "content": (
                    "You do have that tool, but never mind -- the calculation is "
                    f"done for you:\n\n{computed}\n\nAnswer the original question "
                    "using these figures exactly as written. Do not recompute "
                    "them and do not say you are unable to."
                ),
            }
        )
        return await self._run_tools(
            messages, tools, answer, max_iterations=2
        )


    async def _comparison_set(
        self, question: str, answer: Answer
    ) -> tuple[str, list[str]]:
        """What should have been compared, and the material that defines it.

        The wiki's list beats the model's every time it has one. Asked which
        jewellery made from gold bars sells best, the model compared the gold
        necklace, amulet and bracelet -- three items it thought of -- and the
        recipe bucket lists forty, because every sapphire, ruby and zenyte ring
        is a gold bar plus a gem. The winner it named does 114M gp/day and the
        real one, a ruby necklace, does 1.6 billion.

        Falls back to whatever the model did look up, which is the right answer
        for "is a whip or a tentacle worth more" -- a comparison with no
        material behind it, where the model's set is the question.
        """
        for material in _material_candidates(question):
            try:
                products = await self.bucket.products_of(material)
            except BucketError as exc:
                log.warning("Could not list products of %r: %s", material, exc)
                break
            if products:
                if len(products) > COMPARISON_CEILING:
                    log.warning(
                        "%d products of %r, ranking the first %d",
                        len(products), material, COMPARISON_CEILING,
                    )
                return material, products[:COMPARISON_CEILING]
        return "", list(dict.fromkeys(answer.prices_checked))

    async def _force_ge_verdict(
        self, messages: list[dict], tools: list[Tool], answer: Answer, question: str
    ) -> list[dict]:
        """Hand back the ranking when the answer denies having been given it.

        Ninth pass of this shape and the first where the tool was actually
        called. Asked which jewellery made from gold bars sells best, the model
        searched, read Gold necklace, called compare_ge_prices on the necklace,
        the amulet and the bracelet -- and answered "the wiki does not give the
        daily volume of gold necklaces on the Grand Exchange". It had been
        handed 873,817 traded/24h and a verdict line naming the necklace
        outright. The wiki indeed does not carry volumes; the GE feed does, and
        it had already arrived.

        So this is not a retrieval failure and no amount of searching fixes it.
        The ranking is recomputed from the same names the model passed and given
        back as a finding. The lookups are served from
        :class:`~reldo.ge.GEClient`'s own caches, so the pass costs a round trip
        with the model and usually no HTTP at all.
        """
        if self._out_of_budget(answer, messages, "the GE re-rank"):
            return messages

        material, names = await self._comparison_set(question, answer)
        if not names:
            return messages

        ge = self._ge_client()
        found = []
        try:
            for name in names:
                found.extend(await ge.lookup(name))
        except GEError as exc:
            log.warning("Could not re-rank %s: %s", names[:4], exc)
            return messages
        if not found:
            return messages

        unique = list({p.item.id: p for p in found}.values())
        ranked = compare(unique)
        answer.seen.append(ranked)
        answer.prices_checked.extend(names)
        log.warning("Re-ranked %d item(s) and handed the verdict back", len(unique))
        scope = (
            f"everything the wiki lists as made from {material}"
            if material else "the items you looked up"
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    "You already have that data -- it is not on the wiki and it "
                    "does not need to be, because the live Grand Exchange feed "
                    f"carries it. Here is {scope}, ranked by what a seller "
                    f"actually realises:\n\n{ranked}\n\nAnswer the original "
                    "question from this. The ANSWER line is the conclusion; "
                    "report it and the figures behind it, and do not say the "
                    "volume or the price is unavailable. This is the whole set, "
                    "so do not narrow it back down to the items you named "
                    "earlier -- but do say if the winner is not the kind of "
                    "thing that was asked about."
                ),
            }
        )
        return await self._run_tools(
            messages, tools, answer, max_iterations=2
        )

    async def _costed_recipe(
        self, shortlist: list[str], skill: str, question: str
    ) -> dict | None:
        """The recipe a count should divide by, tried two ways round.

        The shortlist holds the pages the question is *about*, which is not
        always the page the recipe is on. "How much gold do I need to smelt"
        shortlists Smithing, Furnace, Gold ore and Blast Furnace: four pages
        about smelting gold, none of them "Gold bar". So when no shortlisted
        page is the product, each is tried as the *material* instead -- one
        indexed Bucket query that asks what the named skill makes out of it.

        Fewer titles on the second pass than the first. It only pays off on the
        one or two entries that are actually items, and every title costs a
        request either way.
        """
        try:
            found = await self.bucket.first_recipe(shortlist, skill=skill or None)
            if found and _recipe_xp(found, skill):
                return found
            if not skill:
                return None
            for title in shortlist[:MATERIAL_CANDIDATES]:
                found = await self.bucket.recipe_from_material(title, skill)
                if found and _recipe_xp(found, skill):
                    return found
        except BucketError as exc:
            log.warning("Could not read a recipe for %r: %s", question[:50], exc)
        return None

    async def _force_training_cost(
        self,
        messages: list[dict],
        tools: list[Tool],
        answer: Answer,
        question: str,
        request: tuple[str, int, int],
    ) -> list[dict]:
        """Count the bars ourselves: the gap, the rate, and the materials.

        Eighth pass of this shape, and the one that motivated the tool it backs.
        Asked how much gold to smelt from 48 to 50 Smithing, the model searched,
        read Smithing, Iron ore and Smithing/Experience table, and answered "the
        wiki does not give the XP needed to go from 48 to 50 smithing in a form
        I can read" -- cited, courteous, and describing a number that is not on
        the wiki in any form because it is a formula.

        Nothing here is skill-specific. The gap is :func:`skills.xp_between`,
        which is the same formula for all twenty-four; the XP per action is the
        recipe's own field, which every production skill fills in. A skill with
        no recipe -- ore, logs, fish -- falls through to the bare gap, plus
        whatever rate the model actually read.
        """
        if self._out_of_budget(answer, messages, "the training count"):
            return messages

        skill, low, high = request
        recipe = await self._costed_recipe(answer.shortlist, skill, question)
        computed = _render_training_cost(recipe, skill, low, high) if recipe else None
        if computed is None:
            # No XP on the recipe. Before falling back to the bare gap, look in
            # the tables -- which is where the rate is for a whole class of
            # skill, and which page_text cannot see at all.
            #
            # Measured on "how many planks from 37 to 70 Construction": the
            # count pass and the XP pass both fired, three runs in three, and
            # the answer was still "The wiki does not give the XP per Oak
            # plank". True of the prose extract and false of the page, whose
            # tables carry Levels | XP needed | Object | Planks required
            # outright. Construction XP is per object built rather than per
            # plank made, so no recipe will ever carry it and no amount of
            # falling through to the gap will produce it.
            read = await self._read_tables_for_a_rate(messages, answer, question, skill)
            if read:
                return await self._run_tools(messages, tools, answer, max_iterations=2)
            # Gathered, or a recipe with no XP on it. The gap alone is still
            # exact, and it is the part the answer above claimed not to have.
            return await self._force_xp_calc(messages, tools, answer, question)

        title = recipe.get("page_name", "")
        answer.recipes_checked.append(title)
        answer.pages_read.append(title)
        answer.xp_calculations.append(f"{low}->{high}")
        answer.seen.append(computed)
        log.warning("Counted %d->%d off %r's recipe and injected it", low, high, title)
        messages.append(
            {
                "role": "user",
                "content": (
                    "The wiki does not carry that XP gap because it is a formula, "
                    "not a page -- so here it is, worked out, with the wiki's own "
                    f"recipe for the rate:\n\n{computed}\n\nAnswer the original "
                    "question using these figures exactly as written. Do not "
                    "recompute them, do not round them, and do not say the wiki "
                    "does not give them. The ANSWER line is the conclusion: "
                    "report it whole, both numbers, even though they only asked "
                    "for the count. Brevity does not apply to the XP figure -- "
                    "this handback exists because it was once reported as "
                    "something the wiki does not give, and stating it is the "
                    "only thing that shows it is known."
                ),
            }
        )
        return await self._run_tools(
            messages, tools, answer, max_iterations=2
        )

    async def _force_money_methods(
        self, messages: list[dict], tools: list[Tool], answer: Answer, question: str
    ) -> list[dict]:
        """Rank the guide ourselves when the model will not.

        Thirteenth pass of this shape, and the same story as the twelve before
        it. Asked which skill is best for making money based on GE prices,
        mistral-small3.2:24b read the money-making guide and answered "The wiki
        does not say which skilling method makes the most money", citing the two
        pages that do; a second run named High Level Alchemy, which is not in
        the top thirty. Neither is a retrieval failure -- it reached the right
        page both times. The profit column is a table, and page_text strips
        tables, so the page genuinely arrives with every figure removed.

        Reading it and sorting it are both exact, so both happen in code.
        """
        if self._out_of_budget(answer, messages, "the money-making ranking"):
            return messages

        # "which skill" wants one method per skill off the skilling subpage;
        # "what makes the most money" wants everything, combat included.
        by_skill = bool(_ASKS_WHICH_SKILL.search(question))
        page = SKILLING_GUIDE_PAGE if by_skill else GUIDE_PAGE
        try:
            methods = await read_guide(self.wiki, page)
        except Exception as exc:
            log.warning("Could not read %r: %s", page, exc)
            return messages
        if not methods:
            # Loudly, because the way this fails is by looking like nothing
            # happened. The page fetched fine and had no rankable rows, which
            # means its columns are not the ones earnings.py knows -- and the
            # visible result is the model going back to "the wiki does not say
            # which skilling method makes the most money", which is the bug this
            # pass was written to fix, with nothing tying it to a wiki edit.
            log.warning(
                "No rankable rows on %r -- its table shape has changed, so this "
                "pass is now doing nothing. See reldo.earnings._is_hourly_profit.",
                page,
            )
            return messages

        listing = render_methods(
            best_per_skill(methods) if by_skill else rank_methods(methods),
            by_skill=by_skill,
        )
        answer.money_ranked.append(page)
        answer.pages_read.append(page)
        answer.seen.append(listing)
        log.warning("Ranked %d methods off %r and injected it", len(methods), page)
        messages.append(
            {
                "role": "user",
                "content": (
                    "Those figures are in a table, which is why the page looked "
                    f"as though it had none -- here is {page!r} read and sorted "
                    f"in code:\n\n{listing}\n\nAnswer the original question from "
                    "this. The ANSWER line is the conclusion; report it and the "
                    "figures behind it, and do not say the wiki does not give "
                    "them. They are the wiki's own, from live GE prices."
                ),
            }
        )
        return await self._run_tools(messages, tools, answer, max_iterations=2)

    async def _force_requirements(
        self, messages: list[dict], tools: list[Tool], answer: Answer, question: str
    ) -> list[dict]:
        """Read the marked-up requirement when the model will not.

        Fourteenth pass of this shape and the plainest of them: the tool was
        added, the mismatch check that consumes it was added, and nothing was
        ever added to make the tool *run*. _skill_mismatch only fires once
        skill_requirements is populated, which only happens once
        get_requirements has been called -- so a model that never calls it
        disables its own safety net, silently, and the case reads as a plain
        wrong answer.

        Measured on "what sailing level do i need to catch marlin", three runs
        in three: the read nudge fires, nothing else does, and the answer is
        whichever bare number the prose spelled out. Both requirements render
        as numbers beside indistinguishable icons, so a page read is not enough
        -- data-skill is the only thing on that page saying which level belongs
        to which skill, and only this tool reads it.
        """
        if self._out_of_budget(answer, messages, "the requirements lookup"):
            return messages
        if not answer.shortlist and not answer.pages_read:
            return messages

        # The last page read. Measured, not reasoned -- two other targetings
        # were tried here and both are worse:
        #
        #     pages_read[-1]   marlin 3/3   Seers 0/3   24/28   74/84
        #     _best_title      marlin 1/3   Seers 1/3   21/28   73/84
        #
        # _best_title is the one that sounds right. The dead-end fall-through
        # runs immediately before this and exists to read another page *because*
        # the last one disappointed, so "most recent read" is sometimes the page
        # that just failed -- which is exactly what cites the Seers answer to
        # 'Draynor Village Rooftop Course'. It repairs that one run in three and
        # costs marlin two, because title overlap picks the page sharing words
        # with the question over the page carrying the answer.
        #
        # A third option -- walk the candidates and take the first that states
        # the skill actually asked about -- is better reasoned than either and
        # is not here, because on single runs it left marlin uncited and Seers
        # refusing outright, and it was never measured. Seers stays a known miss
        # with a known cause; going back to the best measured configuration
        # beats shipping a third heuristic on a hunch at the end of a long day.
        # Four targetings have been tried here and this one measured best:
        #
        #     pages_read[-1]          marlin 3/3  Seers 0/3   24/28  74/84
        #     _best_title             marlin 1/3  Seers 1/3   21/28  73/84
        #     first page with the
        #       named skill           unmeasured; picked 'Sailing/Experience
        #                             table', which states Sailing 95 and is
        #                             about nothing anybody asked
        #
        # The third is the instructive one. "States the skill asked about"
        # sounds like the checkable test that title overlap is not, and it
        # is -- it just admits every page that *mentions* the skill, including
        # the skill's own XP table. Marlin needs a page that states Sailing and
        # is about marlin, and neither half alone gets there.
        #
        # So: the measured default. Seers and marlin both stay partial, with
        # their causes recorded rather than papered over.
        target = answer.pages_read[-1] if answer.pages_read else _best_title(
            question, answer.shortlist
        )
        try:
            found = await self.wiki.requirements(target)
        except Exception as exc:
            log.warning("Could not read requirements on %r: %s", target, exc)
            return messages
        if not found:
            return messages

        merged: dict[str, int] = {}
        for name, level in found:
            merged[name] = max(merged.get(name, 0), level)
        answer.skill_requirements.update(merged)
        answer.pages_read.append(target)
        listing = ", ".join(f"{k} {v}" for k, v in merged.items())
        # Lead with the skill they asked about. Handing over all six and saying
        # "answer from exactly these" is not enough: measured on marlin, the
        # page states Fishing 91, Sailing 78, Cooking 99, Construction 72,
        # Crafting 62 and Woodcutting 66, and the answer came back "you need a
        # Cooking level of 91 to cook marlin" -- grounded in the data, cited to
        # the right page, and about a skill nobody mentioned. The lookup worked
        # and the picking is what failed, so the picking moves into code. Same
        # call ge._verdict makes about a ranked table.
        wanted = _named_skill(question)
        if wanted and wanted not in merged:
            # Say nothing rather than something adjacent. The handback below
            # tells the model to answer from exactly these, and a page that does
            # not state the skill asked about cannot answer it -- so injecting
            # anyway is inviting a confident answer about the wrong requirement.
            #
            # Measured on "what defence level do you need to wear a rune
            # platebody". The markup on that page carries Construction 28,
            # Smithing 99 and Smithing 25 and no Defence at all, and this pass
            # handed all three over under an instruction to use exactly them.
            # It survived only because the model ignored the injection; the
            # requirement it wanted was on the page as prose all along.
            #
            # The read enforcement above has already put the page in front of
            # it, so leaving this alone costs nothing and removes a way to be
            # confidently wrong.
            log.warning(
                "%r states no %s requirement (only %s); leaving the answer alone",
                target,
                wanted,
                ", ".join(f"{k} {v}" for k, v in merged.items()) or "nothing",
            )
            return messages

        if wanted:
            listing = (
                f"ANSWER: {target} requires {wanted} {merged[wanted]}.\n"
                f"  (it also states {listing}, none of which was asked about)"
            )
        answer.seen.append(listing)
        log.warning("Read %r's requirements directly: %s", target, listing)
        messages.append(
            {
                "role": "user",
                "content": (
                    f"{target} states these requirements, read from the skill "
                    f"attributes the wiki marks each one up with:\n\n{listing}"
                    "\n\nAnswer the original question from exactly these. The "
                    "page shows several levels as bare numbers beside similar "
                    "icons, so a number read off the prose is very likely the "
                    "wrong skill's -- these are the ones that say which skill "
                    "they belong to."
                ),
            }
        )
        return await self._run_tools(messages, tools, answer, max_iterations=2)

    async def _read_tables_for_a_rate(
        self, messages: list[dict], answer: Answer, question: str, skill: str
    ) -> bool:
        """Hand over the page's tables when the recipe carries no XP.

        Whether anything was injected. The tables are the only place a good many
        rates exist: Construction XP is per object built rather than per plank
        made, Firemaking per log burnt, Prayer per bone -- none of them a
        production recipe, all of them a table that page_text strips out
        wholesale, which is why the page reads as though it says nothing.

        Not parsed into a number here, deliberately. Which column is the rate
        depends on the skill and the page, and guessing wrong yields a
        confident count computed from the wrong figure -- the shipbuilding
        lesson in :mod:`reldo.unlocks`, where a Construction column sat beside
        a Sailing one. Handing over the table lets the model read the row while
        the arithmetic stays in calculate_xp, where it is exact.
        """
        # The skill's own page first, then whatever was already in play. Picking
        # by title relevance alone sent this to "Plank" -- which has eighteen
        # tables, none of them an XP rate, because a plank is the material and
        # the XP is paid for the object you build out of it. The page that
        # carries a skill's rates is reliably the skill's page, and the model's
        # shortlist is whatever it last happened to search for.
        candidates = [skill] + [t for t in (answer.pages_read + answer.shortlist) if t]

        target, rendered = "", ""
        for candidate in list(dict.fromkeys(candidates))[:TABLE_RATE_CANDIDATES]:
            try:
                tables = await self.wiki.tables(candidate)
            except Exception as exc:
                log.warning("Could not read tables on %r: %s", candidate, exc)
                continue
            text = tables_as_text(tables)
            if not tables or text.startswith("No tables"):
                continue
            # A table is only worth handing over if it plausibly holds the rate.
            # Every page has tables; "Released | Released | Released" is not an
            # XP figure, and injecting it costs the context and answers nothing.
            low = text.lower()
            if "xp" not in low and "experience" not in low:
                continue
            target, rendered = candidate, text
            break

        if not target:
            return False

        body = _fit_injection(rendered, answer.budget, messages)
        if not body:
            return False
        answer.pages_read.append(target)
        answer.seen.append(body)
        log.warning("Read %r's tables for a rate and injected them", target)
        messages.append(
            {
                "role": "user",
                "content": (
                    f"The XP is not in that page's prose and it is not on a "
                    f"recipe -- it is in a table, which read_wiki_page strips "
                    f"out entirely. Here are {target!r}'s tables:\n\n{body}\n\n"
                    "Find the row for what they asked about, take the XP from "
                    "it, and call calculate_xp with that figure and the two "
                    "levels. Do not do the division yourself, and do not say "
                    "the wiki does not give the XP -- it is above."
                ),
            }
        )
        return True

    async def _force_recipe(
        self, messages: list[dict], tools: list[Tool], answer: Answer, question: str
    ) -> list[dict]:
        """Read the make-level out of structured data when the model will not.

        Seventh pass of this shape, and it exists because check_recipe was added
        and then measured: over three questions whose answers are in that tool,
        the model called search_wiki, read_wiki_page and read_wiki_table and
        never once called it. A tool a 24B model does not select is not a
        feature, which is the same thing check_quest_requirements needed a pass
        for.

        The level lives in a production template that read_wiki_page strips, so
        an unaided answer reads the prose around it and takes whichever nearby
        number looks like a requirement -- the level you stop burning a fish
        rather than the level you can cook it.
        """
        if self._out_of_budget(answer, messages, "the recipe lookup"):
            return messages

        if not answer.shortlist:
            return messages
        try:
            found = await self.bucket.first_recipe(answer.shortlist)
        except BucketError as exc:
            log.warning("Could not read a recipe for %r: %s", question[:50], exc)
            return messages
        if found is None:
            return messages

        listing = _render_recipe(found)
        title = found.get("page_name", "")
        answer.recipes_checked.append(title)
        answer.pages_read.append(title)
        answer.seen.append(listing)
        log.warning("Read %r's recipe directly and injected it", title)
        messages.append(
            {
                "role": "user",
                "content": (
                    "Here is what that actually takes, from the wiki's own "
                    f"structured data:\n\n{listing}\n\nAnswer the original "
                    "question using exactly these figures. The level to make "
                    "something is not the level at which you stop failing or "
                    "burning it, and it is not the level to wield it."
                ),
            }
        )
        return await self._run_tools(
            messages, tools, answer, max_iterations=2
        )

    async def _force_quest_requirements(
        self, messages: list[dict], tools: list[Tool], answer: Answer, question: str
    ) -> list[dict]:
        """Look the requirements up ourselves when the model would not.

        Sixth time this shape has appeared, after the read, the list, the dead
        end, the arithmetic and the grounding check, and the answer is the same
        every time: what the model does unreliably and we can do exactly, we do
        exactly.

        Measured on mistral-small3.2:24b, asked what is needed to start Dragon
        Slayer II. It searched three times, read the page, never touched
        check_quest_requirements, and answered "50 Hunter, 50 Slayer, 70 Magic,
        70 Hitpoints" -- Magic is 75, Hitpoints is 50, Hunter and Slayer are not
        requirements at all, and it dropped six that are. The grounding check
        cannot catch it either: the page it read is full of numbers, so 50 and
        70 both count as seen.
        """
        if self._out_of_budget(answer, messages, "the quest requirements"):
            return messages

        try:
            names = await self.bucket.quest_names()
        except BucketError as exc:
            log.warning("Could not list quests to check the question: %s", exc)
            return messages

        quest = _named_quest(question, names)
        if quest is None:
            return messages
        try:
            found = await self.bucket.quest_requirements(quest)
        except BucketError as exc:
            log.warning("Could not read requirements for %r: %s", quest, exc)
            return messages
        if found is None:
            return messages

        title, requirements = found
        if not requirements:
            return messages

        listing = _render_requirements(title, requirements, answer.player_levels)
        answer.quests_checked.append(title)
        answer.pages_read.append(title)  # cite where the numbers came from
        answer.seen.append(listing)
        log.warning("Read %s's requirements directly and injected them", title)
        messages.append(
            {
                "role": "user",
                "content": (
                    "Those requirements are not right. Here they are from the "
                    f"wiki's own structured data:\n\n{listing}\n\nAnswer the "
                    "original question using exactly these. Do not add a "
                    "requirement that is not listed and do not drop one that is."
                ),
            }
        )
        return await self._run_tools(
            messages, tools, answer, max_iterations=2
        )

    async def _read_money_guide(
        self, messages: list[dict], answer: Answer, question: str
    ) -> str:
        """Fetch a money-making guide for the activity and return its text.

        The gp/hour for an *activity* is on none of the sources the model
        reaches for first. The GE feed prices items and knows nothing about how
        long anything takes; the item's own page carries no rate either. It is
        on the money-making guide, in prose, and if nothing goes and reads one
        then the "how long" half of the question is unanswerable no matter how
        the arithmetic is handed back.
        """
        # Search for the activity, not for the question. "how many sharks for 5m
        # and how long farming minnows" names sharks four times and minnows
        # once, so the question as a whole retrieves the guide to *cooking
        # sharks* -- which has a real gp/hour on it and answers how long the
        # cooking takes, not how long the farming they asked about takes.
        named = _ACTIVITY.findall(question)
        activity = named[-1].strip() if named else ""
        try:
            hits = await self._retriever.shortlist(
                f"{activity or question} money making guide", k=6
            )
        except Exception as exc:
            log.warning("Could not look for a money-making guide: %s", exc)
            return ""
        # Only an actual guide. _best_title over the whole shortlist picks the
        # item page for a question full of item words, and the item page is the
        # one place this rate is guaranteed not to be.
        guides = [h.title for h in hits if "money making guide" in h.title.lower()]
        if not guides:
            return ""
        # Among guides, the one whose title matches the activity. Both
        # "Catching minnows" and "Cooking raw sharks" are plausible guides for
        # this question and only one of them is the thing they said they'd do.
        target = _best_title(activity or question, guides)
        try:
            pages = await self._retriever.fetch([target])
        except Exception as exc:
            log.warning("Could not read %r: %s", target, exc)
            return ""
        if not pages:
            return ""
        page = pages[0]
        body = _fit_injection(page.text, answer.budget, messages)
        if not body:
            return ""
        answer.pages_read.append(page.title)
        answer.seen.append(body)
        log.warning("Read %r for a gp/hour figure", page.title)
        return body

    async def _force_gp_calc(
        self, messages: list[dict], tools: list[Tool], answer: Answer, goal: int,
        question: str = "",
    ) -> list[dict]:
        """Do the coin arithmetic ourselves when the model will not.

        The rates come from what the model was actually shown, never from here.
        The minnow guide states "raw sharks worth 717", "40 minnows for 1 shark"
        and "between 268,875 and 448,125 an hour" in plain prose on the page the
        agent had already read -- so the correct answer was fully determined by
        the text in front of it, and the only thing missing was the division.
        """
        if self._out_of_budget(answer, messages, "the coin calculation"):
            return messages

        seen = " ".join(answer.seen)
        rate = _GP_RATE.search(seen) or _GP_RATE.search(answer.text)
        each = _GP_EACH.search(seen)

        # No gp/hour anywhere, which is what happens when the model priced the
        # item off the GE and never opened a guide -- and pricing the item is
        # exactly what it does, because that is the half of the question it can
        # see how to answer. Without a rate this pass returns a count and no
        # duration, which is the answer that failed here: half the question,
        # and no page cited for any of it.
        if not rate and question:
            body = await self._read_money_guide(messages, answer, question)
            if body:
                rate = _GP_RATE.search(body)
                each = each or _GP_EACH.search(body)

        if not rate and not each:
            return messages

        def value(match) -> float | None:
            if not match:
                return None
            suffix = (match.group(2) or "").lower()
            scale = 1_000_000 if suffix.startswith("m") else 1_000 if suffix else 1
            return float(match.group(1).replace(",", "")) * scale

        try:
            computed = gp_plan(
                goal,
                gp_each=value(each),
                gp_per_hour=value(rate),
                item_name="items",
            )
        except ValueError:
            return messages

        answer.gp_calculations.append(str(goal))
        answer.seen.append(computed)
        log.warning("Computed the %d gp goal directly and injected it", goal)
        messages.append(
            {
                "role": "user",
                "content": (
                    "Never mind -- the calculation is done for you, from the "
                    f"figures on the page you read:\n\n{computed}\n\nAnswer the "
                    "original question using these numbers exactly as written. Do "
                    "not recompute them. Name the item and say which page the "
                    "rate came from."
                ),
            }
        )
        return await self._run_tools(
            messages, tools, answer, max_iterations=2
        )

    async def _player_preamble(self, player: str, answer: Answer) -> str:
        """The asker's stats, to be folded into their question.

        Returns a prefix rather than a message, and that is not cosmetic.
        Mistral's chat template expects user and assistant turns to alternate;
        sending the stats as their own user turn puts two user messages back to
        back and the template degrades badly. Measured on
        mistral-small3.2:24b, three runs in three: no tool call at all, one of
        them emitting `read_wiki_page{"page": ...}` into the message content as
        text. It reads as the model losing tool calling, and it is really the
        conversation being malformed before it ever gets there.

        The stats go in ``seen`` either way, so the grounding check treats the
        asker's own levels as shown -- otherwise quoting their Mining level back
        at them would read as an invented number.
        """
        try:
            found = await self._hiscores_client().lookup(player)
        except HiscoresError as exc:
            log.warning("Hiscores lookup for %r failed: %s", player, exc)
            return ""

        stats = found.summary()
        answer.player_context = found.name
        answer.player_levels = {skill: found.level(skill) for skill in SKILLS}
        answer.seen.append(stats)

        # The lookup already happened, so the snapshot is free. Recording it here
        # rather than in the Discord layer means the CLI builds history too.
        recent = ""
        if self._progress is not None:
            try:
                # snapshot_of, not a dict comprehension spelled out here:
                # the scheduled sampler stores the same shape, and record()
                # dedupes on exact equality.
                self._progress.record(found.name, snapshot_of(found))
                recent = summarise(self._progress.gains(found.name))
            except Exception as exc:  # history is a nicety; never fail a question
                log.warning("Progress tracking for %r failed: %s", found.name, exc)
        if recent:
            answer.seen.append(recent)
            recent = f"{recent}\n\n"

        return (
            f"I am {found.name}. My current stats, live from the hiscores:\n\n"
            f"{stats}\n\n{recent}Use them: if I ask how to train a skill, start "
            "from the level I actually have and say what it is rather than giving "
            "me the level-1 answer; if I ask whether I can do something, compare "
            "against these levels and tell me what I am short of.\n\n"
        )

    async def ask(
        self,
        question: str,
        *,
        max_iterations: int = 8,
        player: str | None = None,
        persona: str = "",
        live: str = "",
        profile=None,
        budget: Budget | None = None,
    ) -> Answer:
        """Answer one question, searching the wiki as needed.

        Args:
            player: RuneScape name of whoever is asking, when it is known. Their
                stats are fetched and put in front of the question, so training
                and readiness answers start from where they actually are.
            persona: Optional voice, appended to the system prompt by the Discord
                layer. Appended rather than applied afterwards on purpose: every
                enforcement pass below runs on the text the model produces, so a
                persona cannot add a number or drop a citation after the checks.
            budget: Ceiling on what this one question may spend in wall clock
                and in context. Defaults to :class:`Budget`'s own, which are set
                far above anything measured -- this is a backstop against the
                cascade below running away, not a target to answer within.
        """
        # The fast path first. Most OSRS questions have an exact answer in code
        # -- a marked-up requirement, a live price, a formula -- and reaching it
        # by persuading a 24B model to select the right tool is the slow and
        # unreliable way round. A second against twenty to forty, and it returns
        # a fully populated Answer, so citations and the eval's tool-use checks
        # mean the same thing either way.
        #
        # Skipped when a persona is set: those answers are templates, and a
        # template cannot be in character. Somebody who chose a voice asked for
        # the model.
        if self._direct is not None and not persona:
            try:
                straight = await self._direct.answer(question)
            except Exception as exc:  # a fast path must never cost the answer
                log.warning("Direct answer failed for %r: %s", question[:50], exc)
                straight = None
            if straight is not None:
                if player:
                    # The asker's own levels still belong on the record even
                    # when the answer did not need them.
                    await self._player_preamble(player, straight)
                straight.profile = profile
                return straight

        answer = Answer(text="", profile=profile, budget=budget or Budget())
        preamble = await self._player_preamble(player, answer) if player else ""
        if live:
            # What they are doing right now, ahead of what they have. Recorded in
            # `seen` so a session XP rate quoted back reads as grounded rather
            # than invented.
            answer.seen.append(live)
            preamble += f"{live}\n\n"
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT + persona},
            # One user turn, not two. See _player_preamble.
            {"role": "user", "content": preamble + question},
        ]

        tools = self._build_tools(answer)
        messages = await self._run_tools(
            messages, tools, answer, max_iterations=max_iterations
        )

        # A 32B model does not reliably obey "read before you answer". Measured on
        # qwen3:32b: "how do I kill Vorkath" searched, skipped the read, then
        # answered from memory -- naming the wrong quest, the wrong island and the
        # wrong attack type, with no citation to make the gap visible. Stronger
        # prompting did not fix it, so the invariant is enforced here instead:
        # an ungrounded answer gets handed back once, with instructions.
        # A price grounds "what is a whip worth" and grounds nothing about how
        # long an activity takes: no GE feed carries a gp/hour, that lives on
        # the money-making guide. Counting the price lookup as sufficient is why
        # "how many sharks for 5m and how long farming minnows" came back having
        # answered half the question, citing no page at all -- it priced a
        # shark, skipped the read on the strength of that, and then the coin
        # pass had no rate to divide with because nothing had been read.
        grounded = (
            answer.pages_read
            or answer.players_checked
            or (answer.prices_checked and not _ASKS_DURATION.search(question))
        )
        if answer.searches and not grounded:
            log.warning("No page read for %r -- forcing a read", question[:60])
            answer.passes_fired.append("the read nudge")
            messages.append(
                {"role": "user", "content": _read_nudge(_target_page(question, answer))}
            )
            messages = await self._run_tools(
                messages, tools, answer, max_iterations=3
            )
            # Asking twice is still only asking. Measured on "fastest way to
            # train mining from 45", two runs in three answered the nudge with
            # prose and no tool call at all -- one of them opening "I apologize
            # for the confusion, it seems there was an issue with accessing the
            # specific page" about a call it never made -- then answered from
            # memory. So stop asking. We know the title and we have the
            # retriever; fetching it ourselves cannot be declined.
            if not answer.pages_read and answer.top_hit:
                messages = await self._force_read(messages, tools, answer, question)

        answer.text = _last_assistant_text(messages, _names(tools))

        # A list question the model answered from memory. The tables it would
        # have needed are invisible to read_wiki_page, so this is not a misread.
        listing = _list_request(question)
        if listing and not answer.unlocks_listed:
            log.warning("List question answered without list_unlocks: %r", question[:60])
            messages = await self._force_unlocks(messages, tools, answer, *listing)
            answer.text = _keep_best(answer.text, _last_assistant_text(messages, _names(tools)))

        # Two failures, one remedy. Either the answer denies having GE data it
        # was handed, or it is a "what made from X sells best" question whose
        # comparison set the model assembled from memory -- and a set of three
        # where the wiki lists forty is wrong before the ranking even starts.
        #
        # Runs ahead of the next-candidate pass below because reading another
        # wiki page is not merely useless here, it is the wrong shape of answer:
        # no page has a live price, and the fall-through would spend a request
        # confirming that.
        ranked_set = _ASKS_BEST_SELLER.search(question) and _material_candidates(question)
        if ranked_set or (answer.prices_checked and _DEAD_END.search(answer.text)):
            log.warning("Denied GE data it was shown for %r", question[:60])
            messages = await self._force_ge_verdict(messages, tools, answer, question)
            answer.text = _keep_best(answer.text, _last_assistant_text(messages, _names(tools)))

        # "Which skill makes the most money" -- a ranking over a table the
        # model cannot see. Ahead of the dead-end fall-through for the same
        # reason the GE verdict is: no page states this in prose, so reading
        # another one spends a request confirming that.
        if _ASKS_BEST_MONEY.search(question) and not answer.money_ranked:
            log.warning("Money question answered without the guide: %r", question[:60])
            messages = await self._force_money_methods(messages, tools, answer, question)
            answer.text = _keep_best(answer.text, _last_assistant_text(messages, _names(tools)))

        # Read a page, and it turned out to be the wrong page. Try the next one.
        if _DEAD_END.search(answer.text) and answer.shortlist:
            log.warning("Dead-end answer for %r -- trying the next hit", question[:60])
            messages = await self._try_next_candidate(messages, tools, answer, question)
            answer.text = _keep_best(answer.text, _last_assistant_text(messages, _names(tools)))

        # "How much gold do I need to smelt from 48 to 50" -- a count, which is
        # the XP gap over the XP one action gives. Runs ahead of the recipe and
        # duration passes because it subsumes both: it injects the recipe *and*
        # the arithmetic, and records each, so neither fires again underneath it.
        quantity = _quantity_request(question)
        if quantity and not answer.xp_calculations:
            log.warning("Quantity question answered without the count: %r", question[:60])
            messages = await self._force_training_cost(
                messages, tools, answer, question, quantity
            )
            answer.text = _keep_best(answer.text, _last_assistant_text(messages, _names(tools)))

        # A "what level to make X" question answered without the tool that reads
        # the production template. The level is not in the prose.
        if _ASKS_HOW_TO_MAKE.search(question) and not answer.recipes_checked:
            log.warning("Make question answered without the recipe: %r", question[:60])
            messages = await self._force_recipe(messages, tools, answer, question)
            answer.text = _keep_best(answer.text, _last_assistant_text(messages, _names(tools)))

        # A requirements question answered without the tool that returns them
        # exactly. Reading them off the page is where six get dropped and two
        # get invented.
        if _ASKS_REQUIREMENTS.search(question) and not answer.quests_checked:
            log.warning("Requirements question answered without the tool: %r", question[:60])
            messages = await self._force_quest_requirements(
                messages, tools, answer, question
            )
            answer.text = _keep_best(answer.text, _last_assistant_text(messages, _names(tools)))

        # A coin goal is its own arithmetic and its own tool. Checked before the
        # XP branch and allowed to claim the question, because "how long will
        # that take farming minnows" matches every duration pattern below while
        # containing no levels for calculate_xp to work from -- and the XP
        # handback fired on it, demanded a from_level and a to_level, and talked
        # a working answer into three broken ones.
        goal = parse_goal(question)
        if goal and not answer.gp_calculations:
            log.warning(
                "Coin goal %d claimed without calculate_gp for %r", goal, question[:60]
            )
            answer.passes_fired.append("the coin nudge")
            messages.append({"role": "user", "content": _gp_nudge(goal)})
            messages = await self._run_tools(
                messages, tools, answer, max_iterations=3
            )
            if not answer.gp_calculations:
                messages = await self._force_gp_calc(
                    messages, tools, answer, goal, question
                )
            answer.text = _keep_best(answer.text, _last_assistant_text(messages, _names(tools)))

        # A skill-level question the model answered without the tool that
        # reads the marked-up requirement. Ahead of the mismatch check below
        # because that check is a consumer of this data: with nothing in
        # skill_requirements it cannot fire at all, which is exactly how the
        # marlin case slipped through with its own guard installed.
        if (
            _skill_level_question(question)
            and not answer.skill_requirements
            and answer.pages_read
        ):
            log.warning("Skill-level question answered without the tool: %r", question[:60])
            messages = await self._force_requirements(messages, tools, answer, question)
            answer.text = _keep_best(answer.text, _last_assistant_text(messages, _names(tools)))

        # A page was read and it turned out to state the level of a different
        # skill than the one asked about. Only reachable once get_requirements
        # has run, which is the point: the pairs are exact, so disagreeing with
        # them is not a judgement call.
        mismatch = _skill_mismatch(question, answer.text, answer.skill_requirements)
        if mismatch:
            log.warning(
                "Answer to %r omits the %s level it asked for", question[:50], mismatch
            )
            answer.passes_fired.append("the skill-mismatch nudge")
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"The question asked about {mismatch}, and your answer does "
                        f"not give the {mismatch} level. The page states "
                        f"{mismatch} {answer.skill_requirements[mismatch]}. Other "
                        "skills' levels are on that page too and are not what was "
                        f"asked for. Answer again, leading with the {mismatch} "
                        "requirement."
                    ),
                }
            )
            messages = await self._run_tools(
                messages, tools, answer, max_iterations=2
            )
            answer.text = _keep_best(answer.text, _last_assistant_text(messages, _names(tools)))

        # Same shape as the read enforcement above and for the same reason: the
        # prompt already says "never do XP arithmetic yourself", and the model
        # does it anyway. One handback, with the tool named.
        if not answer.xp_calculations and not goal and (
            _ASKS_DURATION.search(question) or _CLAIMS_DURATION.search(answer.text)
        ):
            log.warning("Duration claimed without calculate_xp for %r", question[:60])
            answer.passes_fired.append("the XP nudge")
            messages.append({"role": "user", "content": _xp_nudge()})
            messages = await self._run_tools(
                messages, tools, answer, max_iterations=3
            )
            if not answer.xp_calculations:
                messages = await self._force_xp_calc(messages, tools, answer, question)
            answer.text = _keep_best(answer.text, _last_assistant_text(messages, _names(tools)))

        # Last line of defence, after every other pass has had its chance. A
        # figure the model was never shown came out of its weights: "at least 30
        # Smithing" for cannonballs, "Ironwood mast" at Sailing 20. Both fluent,
        # both cited, both invented.
        invented = _ungrounded_numbers(answer.text, question, answer.seen)
        if invented and answer.seen:
            log.warning("Ungrounded numbers %s in answer to %r", invented, question[:50])
            answer.passes_fired.append("the grounding nudge")
            messages.append({"role": "user", "content": _grounding_nudge(invented)})
            messages = await self._run_tools(
                messages, tools, answer, max_iterations=3
            )
            # Whichever draft invents less, not merely whichever is non-empty.
            # The retry may have gone and read the table, in which case its
            # figures are in `seen` now and this recount clears them.
            answer.text = _fewer_invented(
                answer.text, _last_assistant_text(messages, _names(tools)), question, answer.seen
            )

            # The model has now been asked twice. What it will not remove, we
            # remove -- the same call as every other enforcement here, and the
            # one the warning alone was never making: a figure out of the
            # weights reached the user with nothing but a log line against it.
            # No narrowing here any more, and deliberately: the rounding
            # tolerance in _grounded_test does that job now and does it better.
            # Narrowing the *cut* to requirement-shaped numbers spared "you can
            # smash 15 rocks per inventory" -- an invented quantity with no 15
            # and nothing near one anywhere in what was read, and the live
            # failure this excision was written for. What needed to stop being
            # cut was a rounding, not everything that is not a level.
            remaining = _ungrounded_numbers(answer.text, question, answer.seen)
            if remaining:
                pruned = _drop_ungrounded_claims(answer.text, question, answer.seen)
                if pruned:
                    # Recorded, not just counted. A list of numbers does not say
                    # which sentence went, and that is the thing you need when an
                    # answer comes back shorter than it should be.
                    answer.excised = _ungrounded_pieces(
                        answer.text, question, answer.seen
                    )
                    log.warning(
                        "Excised ungrounded %s: %s",
                        remaining,
                        " | ".join(p.strip() for p in answer.excised)[:300],
                    )
                    answer.text = pruned + _EXCISION_NOTE
                else:
                    # Every sentence failed. Cutting them all leaves a blank
                    # Discord embed, which is worse than a flagged answer -- so
                    # keep it, and make the log say why it got through.
                    log.warning(
                        "Every claim in the answer to %r was ungrounded (%s); kept "
                        "rather than emptied",
                        question[:50],
                        remaining,
                    )

        if not (
            answer.searches
            or answer.pages_read
            or answer.players_checked
            or answer.prices_checked
            or answer.player_context
        ):
            # Only a genuinely ungrounded answer is worth warning about. A stats
            # question answered from the hiscores used a tool and is grounded --
            # warning on it teaches you to ignore the warning that matters. Same
            # for a price question answered from the live GE feed.
            log.warning("Model answered %r from memory -- no tool was used", question[:60])

        # Last, after every pass that might have provoked one: an apology is a
        # reply to a corrective, so it can only exist once the correctives have
        # run.
        answer.text, apologised = _strip_meta(answer.text)
        if apologised:
            answer.passes_fired.append("the apology strip")
            log.warning("Removed meta opener(s): %s", " | ".join(apologised)[:200])

        # De-duplicate while preserving order. Searches too: a model that tried
        # three wordings before finding the page should report three, not the
        # same one five times.
        answer.pages_read = list(dict.fromkeys(answer.pages_read))
        answer.searches = list(dict.fromkeys(answer.searches))
        return answer
