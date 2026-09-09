"""Coin arithmetic, in code rather than in the model.

The twin of :mod:`skills`, and it exists for the same measured reason. Asked
"how many sharks do I need to get 5 mill and how long will that take farming
minnows", the agent read the right page -- the minnow money-making guide, which
states 40 minnows per shark, sharks worth 717, and 268,875-448,125 gp/hr in
plain prose -- and then answered:

    375 minnows, exchanged for 9 raw sharks, approximately 29.1 hours.

Every figure is wrong, and they are not wrong independently. 9 sharks is 6,453
gp, not 5,000,000; 375 minnows is 9 sharks only if the rate is 40:1, which it
is, so the one ratio it got right is the one that makes the rest incoherent.
That is what a division looks like when a 24B model does it in its head: locally
plausible, globally three orders of magnitude out.

The right answer is 6,974 sharks, 278,960 minnows, and 11-19 hours depending on
Fishing level. Nothing here is a judgement call, so nothing here is left to the
model.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

# "5m", "5 mill", "5 million", "500k", "1.5b", "5,000,000", "5000000 gp".
#
# The doubled l in `il{1,2}` is not decoration. "mill" is how people write it
# and `m(?:il(?:lion)?)?` cannot match it: "mil" leaves a trailing l, the
# trailing \b rejects that, the suffix backtracks to empty, and "5 mill" parses
# as a five-coin goal -- the exact off-by-a-million this module exists to stop,
# reintroduced one line below the docstring warning about it.
_GOAL = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(b(?:il{1,2}(?:ion)?)?|m(?:il{1,2}(?:ion)?)?|k|t(?:housand)?)?\b",
    re.I,
)
_MULTIPLIER = {"k": 1_000, "t": 1_000, "m": 1_000_000, "b": 1_000_000_000}

# A *bare* number is only a coin goal when the question is about coins. "how
# many planks from 37 to 70" is not one, and reading 70 as a 70gp target is how
# a Construction question becomes a money-making answer. A number carrying a
# k/m/b suffix needs no such evidence -- "500k" is not a level, a quantity or a
# tick count -- so the suffix is checked separately and this is not asked.
_MONEY_WORD = re.compile(
    r"\b(gp|gold|coins?|cash|money|profit|afford|worth|bank)\b", re.I
)

# The coin word *attached* to the number, which is what tells "5,000,000 gp"
# from "200,000 minnows" in a sentence that mentions money either way.
_MONEY_UNIT = re.compile(r"^\W{0,3}(?:gp|gold|coins?|cash)\b", re.I)

# ...unless the question is counting XP. "how many bones for 5m prayer xp" and
# "500k Slayer xp" are training goals wearing a coin goal's clothes, and routing
# them here costs the asker the entire XP path -- the mirror of the misroute
# that sent "how long farming minnows" to calculate_xp. A question naming both
# ("5m gp for 99 Prayer xp") keeps the coin reading, because the coin word is
# the more deliberate of the two to type.
_XP_UNIT = re.compile(r"\b(?:xp|exp|experience)\b", re.I)


def parse_goal(text: str) -> int | None:
    """The coin figure a question is aiming at, or None if it names none.

    Written for how people actually type it. "5 mill", "5m" and "5,000,000" are
    the same goal, and the bare "5" in "5 mill" is the trap: read without its
    suffix it is a five-coin target, which divides into a rate to give an answer
    that looks like a rounding error rather than the nonsense it is.
    """
    spelled_out = bool(_MONEY_WORD.search(text))
    if _XP_UNIT.search(text) and not spelled_out:
        return None
    best: int | None = None
    for match in _GOAL.finditer(text):
        raw, suffix = match.group(1), match.group(2)
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if suffix:
            value *= _MULTIPLIER[suffix[0].lower()]
        elif value < 1000 or not _MONEY_UNIT.match(text[match.end():]):
            # A bare small number in a money question is a quantity or a level,
            # not a target. Nobody grinds toward 5 coins.
            #
            # And a bare *large* number is only a goal when the coin word is
            # attached to it. A money word loose in the sentence is not enough:
            # "if i have 200,000 minnows how many sharks does that give me and
            # how much money on grand exchange is that" was read as a 200,000 gp
            # target, which came back "205 sharks" -- the right division of the
            # wrong number, for a question whose 200,000 was a count of fish.
            continue
        if best is None or value > best:
            best = int(value)
    return best


def items_for_goal(goal_gp: int, gp_each: float) -> int:
    """How many of a thing you must sell to reach a coin goal, rounded up."""
    if gp_each <= 0:
        raise ValueError("gp_each must be positive")
    if goal_gp <= 0:
        raise ValueError("goal_gp must be positive")
    return math.ceil(goal_gp / gp_each)


def hours_for_goal(goal_gp: int, gp_per_hour: float) -> float:
    """Hours of a method at a stated gp/hr."""
    if gp_per_hour <= 0:
        raise ValueError("gp_per_hour must be positive")
    if goal_gp <= 0:
        raise ValueError("goal_gp must be positive")
    return goal_gp / gp_per_hour


def plan(
    goal_gp: int,
    *,
    gp_each: float | None = None,
    gp_per_hour: float | None = None,
    item_name: str = "item",
    inputs_per_item: float | None = None,
    input_name: str = "input",
) -> str:
    """A worked answer to "how much of X for N gp, and how long".

    Every line is a division the model would otherwise estimate. The shape
    mirrors :func:`skills.plan`, including the reason: handing back a rendered
    result the model can only quote is what stops it recomputing and getting a
    different number in the same sentence.
    """
    if goal_gp <= 0:
        raise ValueError("goal_gp must be positive")
    lines = [f"Goal: {goal_gp:,} gp"]

    count: int | None = None
    if gp_each:
        count = items_for_goal(goal_gp, gp_each)
        lines.append(f"  at {gp_each:,g} gp each: {count:,} {item_name}")
        if inputs_per_item:
            total = math.ceil(count * inputs_per_item)
            lines.append(
                f"  at {inputs_per_item:,g} {input_name} per {item_name}: "
                f"{total:,} {input_name}"
            )
    if gp_per_hour:
        lines.append(
            f"  at {gp_per_hour:,g} gp/hr: {hours_for_goal(goal_gp, gp_per_hour):.1f} hours"
        )
    if count is not None and gp_per_hour:
        # State the cross-check rather than leaving it implied. The failure this
        # module exists for was an answer whose own numbers contradicted each
        # other, and a model that can see the product spelled out has one fewer
        # opportunity to restate it wrongly.
        lines.append(
            f"  check: {count:,} x {gp_each:,g} gp = {math.floor(count * gp_each):,} gp"
        )
    if len(lines) == 1:
        lines.append("  (no rate given -- ask for a gp/hr or a price per item)")
    return "\n".join(lines)


def from_market(
    goal_gp: int,
    *,
    item_name: str,
    net_each: int,
    gross_each: int | None = None,
    volume: int | None = None,
    buy_limit: int | None = None,
    gp_per_hour: float | None = None,
) -> str:
    """How many to sell for a coin goal, priced off the live market.

    Counts against the **net** price, which is the whole reason this is not a
    one-line division the asker could do themselves. Tax is 2% and it applies
    per item, so a goal met by selling many cheap things costs meaningfully more
    units than the gross price suggests -- and the gross price is the one on
    every wiki page and every third-party site.

    Volume and buy limit are here because "how many do I need" has a second
    answer nobody asks for and everybody needs: whether the market will take
    that many. 6,974 sharks against 21,315 traded a day is a third of daily
    volume, which is fine. The same count of a dead item is not sellable at all.
    """
    if net_each <= 0:
        raise ValueError("net_each must be positive")
    count = items_for_goal(goal_gp, net_each)
    lines = [
        f"Goal: {goal_gp:,} gp",
        f"  {count:,} x {item_name} at {net_each:,} gp each after tax",
    ]
    if gross_each and gross_each != net_each:
        lines.append(
            f"  ({gross_each:,} gp each before {(gross_each - net_each) * count:,} gp "
            "of GE tax across the lot)"
        )
    if volume:
        share = count / volume
        # "0%" of a three-million-a-day market reads as a broken number rather
        # than as the good news it is, so anything under a percent says so in
        # words instead.
        shown = "under 1%" if share < 0.01 else f"{share:.0%}"
        lines.append(
            f"  {volume:,} trade per day, so that is {shown} of daily volume"
            + ("" if share <= 0.5 else " -- more than the market will absorb quickly")
        )
    elif volume == 0:
        lines.append("  nothing traded in the last 24h -- this cannot be sold at any price")
    if buy_limit:
        windows = math.ceil(count / buy_limit)
        lines.append(
            f"  buy limit {buy_limit:,} per 4h: {windows:,} window"
            f"{'s' if windows != 1 else ''} to buy this many"
        )
    if gp_per_hour:
        lines.append(
            f"  at {gp_per_hour:,g} gp/hr: {hours_for_goal(goal_gp, gp_per_hour):.1f} hours"
        )
    return "\n".join(lines)


def shopping_list(
    count: int,
    *,
    item_name: str,
    gp_each: int,
    volume: int | None = None,
    buy_limit: int | None = None,
    warnings: Sequence[str] = (),
) -> list[str]:
    """What a training plan's materials cost at a live price, as lines.

    Priced at what a *buyer* pays, which is the other side of the book from
    every other figure in this module. Selling 6,974 sharks realises the low
    price; buying 20,202 mahogany planks costs the high one, and quoting the
    seller's price for a purchase understates a 38M gp grind by the spread.

    No tax line, on purpose: GE tax is charged to the seller. The person buying
    planks pays the price and nothing else, and a "net of tax" figure here would
    be a discount that does not exist.

    Returns lines rather than a paragraph so the caller can indent them into
    whatever it is already printing, and returns the market's own caveats with
    them -- a per-item price is only a total if the market will sell you that
    many. 20,202 planks against 5.2M traded a day is nothing; the same count of
    something thin is a number and not a plan.
    """
    if count <= 0 or gp_each <= 0:
        return []
    lines = [f"at {gp_each:,} gp each: {count * gp_each:,} gp for the {item_name}s"]
    if volume is not None and volume < count:
        # Not a warning about liquidity grade -- this one is arithmetic. Wanting
        # more units than the entire market moves in a day is a fact about the
        # plan regardless of how the volume grades.
        lines.append(
            f"that is more than the {volume:,} traded in the last 24h, so expect "
            "to buy it over several days"
        )
    if buy_limit:
        windows = math.ceil(count / buy_limit)
        if windows > 1:
            lines.append(
                f"GE buy limit {buy_limit:,} per 4h: {windows:,} windows to buy "
                "that many"
            )
    lines.extend(str(warning) for warning in warnings)
    return lines


# How far an answer's own arithmetic may drift before it is called wrong rather
# than rounded. Guides quote ranges and people round to "about 7,000", so a tight
# bound would fire on honest answers; 25% still catches the failure this exists
# for, which was out by a factor of 775.
TOLERANCE = 0.25


def contradicts_itself(count: int, gp_each: float, goal_gp: int) -> bool:
    """Does an answer's own quantity, price and goal fail to multiply out?

    The check the number-grounding guard cannot make. That guard asks where a
    figure came from, and "9 sharks" for a 5,000,000 gp goal passes it whenever
    a 9 appears anywhere on the pages read -- provenance is satisfied and the
    arithmetic is still nonsense. This asks the other question.
    """
    if gp_each <= 0 or goal_gp <= 0:
        return False
    return abs(count * gp_each - goal_gp) / goal_gp > TOLERANCE


def funded_by(
    goal_gp: int,
    *,
    item_name: str,
    net_each: int,
    volume: int | None = None,
) -> list[str]:
    """How much of something you must sell to pay a bill, as lines.

    The other half of :func:`shopping_list`, and the half a 24B model reliably
    invents: asked what 20,202 mahogany planks cost and how many sharks that is,
    it answered 1,311 -- about 1.3M gp against a 38M gp bill, with no arithmetic
    anywhere connecting the two numbers it had just printed.

    Counted against the **net** price, because tax falls on the seller and this
    side of the trade is a sale. That is the difference between 39,229 sharks
    and 38,444, and it is 785,000 gp of it.
    """
    if goal_gp <= 0 or net_each <= 0:
        return []
    count = items_for_goal(goal_gp, net_each)
    lines = [
        f"{count:,} {item_name}s sold at {net_each:,} gp each after tax pays for that"
    ]
    if volume is not None and volume < count:
        lines.append(
            f"only {volume:,} {item_name}s trade in a day, so that is several "
            "days of selling"
        )
    return lines
