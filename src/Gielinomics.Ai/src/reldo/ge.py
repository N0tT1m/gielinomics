"""Grand Exchange prices, from the OSRS Wiki's real-time API.

The naive version of this module is four lines -- fetch ``/latest``, print the
price -- and it produces confidently wrong answers on exactly the items people
ask about. Measured against the live API while writing this:

    item                 latest low   latest high   spread   traded/day
    Sandstone (5kg)              10        20,000   2000x            13
    Granite (5kg)               713           713       1x           354
    Sandstone (10kg)          2,000         2,976     1.5x           127
    Coal                        149           149       1x     1,898,359

``/latest`` is *the single most recent trade on each side*, not a market price.
On Sandstone (5kg) somebody sold one for 10gp and somebody else bought one for
20,000gp, and neither number is what the item is worth. Report either as "the
price" and the answer is garbage -- with no error anywhere, which is the failure
mode this project cares about most.

So nothing here returns a bare number. Every price carries the 24-hour volume
that says whether it means anything, and :meth:`Price.liquidity` grades it.
Sandstone and granite are *dead markets* -- a few hundred trades a day against
coal's 1.9 million -- and the tool has to say so, because "sandstone sells for
2,976" is a worse answer than "sandstone barely sells at all".

API docs: https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

GE_API = "https://prices.runescape.wiki/api/v1/osrs"

# Item metadata changes on game updates, not minute to minute.
MAPPING_TTL = 3600.0
# The wiki aggregates in 5-minute buckets, so polling faster than this only
# re-fetches numbers that cannot have changed.
PRICE_TTL = 60.0

# Units traded per day, both sides summed. Calibrated against the live market
# rather than picked round: nature runes do 27.6M/day, coal 1.9M, abyssal whips
# 6,905, and the raw granite/sandstone that prompted this module do 13-354.
# Anything under a thousand a day is not a market you can reliably sell into.
LIQUID = 10_000
THIN = 1_000
ILLIQUID = 100

# Ratio between the two sides of ``/latest`` above which the pair is noise
# rather than a spread. Every liquid item measured sat at 1.00-1.05; the only
# item over 2x was Sandstone (5kg) at 2000x, on thirteen trades.
MAX_SANE_SPREAD = 2.0


# -- Grand Exchange tax ------------------------------------------------------
# Verified from the wiki's "Convenience fee and item sink" section, which is a
# table and therefore invisible to page_text -- read via section_text.
#
#   "Most transactions on the Grand Exchange are subject to a 2% tax, or
#    convenience fee, capped at a maximum of 5 million coins per item."
#
# Introduced 9 December 2021 at 1%, raised to 2% on 29 May 2025. Rate as data,
# not a branch: it has moved once already.
#
# The sub-50gp rule needs no special case. Tax rounds DOWN to the whole coin, so
# 2% of 49 is 0.98 -> 0. Integer arithmetic below gives that exactly; floats
# would put 50 * 0.02 at 1.0000000000000002 and invite a rounding bug at the
# one boundary players actually notice.
GE_TAX_PERCENT = 2
GE_TAX_CAP = 5_000_000

# From the wiki's "Exempt from tax" table, but spelled as the *prices API*
# spells them, which is not the same thing and matters more than it looks. The
# wiki lists "Energy potion" and "Varrock teleport"; /mapping has
# "Energy potion(4)" and "Varrock teleport (tablet)". Encoding the wiki's
# display names verbatim produces a set that silently never matches, and an
# exemption that never fires looks exactly like no bug at all.
TAX_EXEMPT: frozenset[str] = frozenset(
    name.lower()
    for name in (
        "Old school bond",
        "Energy potion(1)", "Energy potion(2)", "Energy potion(3)", "Energy potion(4)",
        # Low level combat consumables
        "Bronze arrow", "Bronze dart", "Iron arrow", "Iron dart",
        "Mind rune", "Steel arrow", "Steel dart",
        # Low level food
        "Bass", "Bread", "Cake", "Cooked chicken", "Cooked meat", "Herring",
        "Lobster", "Mackerel", "Meat pie", "Pike", "Salmon", "Shrimps", "Tuna",
        # Teleports. "West ardougne teleport (tablet)" is a different item and
        # is NOT exempt, so these match on the full name only.
        "Ardougne teleport (tablet)", "Camelot teleport (tablet)",
        "Civitas illa fortis teleport", "Falador teleport (tablet)",
        "Games necklace(8)", "Kourend castle teleport (tablet)",
        "Lumbridge teleport (tablet)", "Ring of dueling(8)",
        "Teleport to house (tablet)", "Varrock teleport (tablet)",
        # Tools
        "Chisel", "Gardening trowel", "Glassblowing pipe", "Hammer", "Needle",
        "Pestle and mortar", "Rake", "Saw", "Secateurs", "Seed dibber",
        "Shears", "Spade", "Watering can",
    )
)


def tax_on(price: int | None, item_name: str) -> int:
    """Tax a seller pays on one unit at ``price``."""
    if not price or price < 0:
        return 0
    if item_name.lower() in TAX_EXEMPT:
        return 0
    return min(price * GE_TAX_PERCENT // 100, GE_TAX_CAP)


def net_of_tax(price: int | None, item_name: str) -> int | None:
    """What the seller actually receives for one unit."""
    return None if price is None else price - tax_on(price, item_name)


def _squash(name: str) -> str:
    """Lowercased with spacing and punctuation stripped.

    "sword fish", "Sword-fish" and "Swordfish" are one thing to everybody except
    a string comparison. The catalogue closes the compound up and people do not,
    and every match tier above fails on the space -- "sword fish" is not a
    prefix of "swordfish" and not a substring of it either.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _same_item(catalogue: str, wanted: str) -> bool:
    """The same item named twice, allowing only a trailing plural to differ.

    The catalogue is inconsistent about it -- "Teak plank" and "Redwood logs" --
    and a wiki guide naming "2 redwood logs each" has to reach the second. One
    letter of latitude and no more: "Teak plank" must not match "Teak plank
    pack", which is a different item at a hundred times the price.
    """
    one, two = catalogue.strip().lower(), wanted.strip().lower()
    return one == two or one == f"{two}s" or f"{one}s" == two


class GEError(RuntimeError):
    """The prices API failed, or answered in a shape we can't use."""


@dataclass(frozen=True, slots=True)
class Item:
    """Static metadata for one tradeable item, from ``/mapping``."""

    id: int
    name: str
    limit: int | None  # GE buy limit per 4 hours; None for a few odd items
    high_alch: int | None
    members: bool
    examine: str = ""


@dataclass(frozen=True, slots=True)
class Price:
    """What one item is worth, with the evidence for how much to trust it.

    Two vocabularies collide here and mixing them up inverts the answer. The API
    calls them ``high`` and ``low``; what they *mean* is:

    * ``high`` -- the last price a *buy* offer completed at. You pay this to buy
      instantly, and a patient seller eventually gets near it.
    * ``low`` -- the last price a *sell* offer completed at. You receive this if
      you dump the item into the market right now.

    So the seller's floor is ``low`` and the seller's ceiling is ``high``. Named
    from the trader's side below, because "high price" reads like "the good one"
    and for a seller it's the one you only get by waiting.
    """

    item: Item
    instant_sell: int | None  # latest low -- what you get dumping it now
    instant_buy: int | None  # latest high -- what a buyer just paid
    avg_sell: int | None  # 24h avgLowPrice
    avg_buy: int | None  # 24h avgHighPrice
    volume: int  # 24h units traded, both sides

    @property
    def liquidity(self) -> str:
        if self.volume >= LIQUID:
            return "liquid"
        if self.volume >= THIN:
            return "thin"
        if self.volume >= ILLIQUID:
            return "illiquid"
        return "dead"

    @property
    def spread_ratio(self) -> float | None:
        """``instant_buy / instant_sell``. Above ~2 the pair is noise."""
        if not self.instant_sell or not self.instant_buy:
            return None
        return self.instant_buy / self.instant_sell

    @property
    def erratic(self) -> bool:
        """True when the two sides of ``/latest`` disagree implausibly."""
        ratio = self.spread_ratio
        return ratio is not None and ratio > MAX_SANE_SPREAD

    @property
    def estimate(self) -> int | None:
        """Best single guess at what a seller actually realises.

        Prefers the 24-hour average over ``/latest`` deliberately: an average
        over a day of trades survives one lunatic buying a rock for 20,000gp,
        and a single most-recent trade does not. Falls back to the instant
        figures only when the item didn't trade at all in 24 hours.
        """
        for candidate in (self.avg_sell, self.avg_buy, self.instant_sell, self.instant_buy):
            if candidate:
                return candidate
        return None

    @property
    def buy_estimate(self) -> int | None:
        """Best single guess at what a buyer actually pays.

        The mirror of :attr:`estimate`, and both exist because the two questions
        have different answers. What you realise selling planks is the low side
        of the book; what a 20,202-plank shopping list costs is the high one, and
        pricing a purchase off :attr:`estimate` understates it by the spread --
        which on a thin item is most of the number.

        Same preference for the 24-hour average over ``/latest``, for the same
        reason: one lunatic overpaying does not move a day of trades.
        """
        for candidate in (self.avg_buy, self.instant_buy, self.avg_sell, self.instant_sell):
            if candidate:
                return candidate
        return None

    @property
    def tax(self) -> int:
        """GE tax on one unit at :attr:`estimate`."""
        return tax_on(self.estimate, self.item.name)

    @property
    def net_estimate(self) -> int | None:
        """What a seller receives per unit, after tax.

        Distinct from :attr:`estimate` on purpose. "How much is an abyssal whip
        worth" wants the market price; "what should I sell" wants this. Ranking
        on the gross price compares taxed and untaxed items as though they were
        the same number -- Sandstone (10kg) at ~2,387gp is taxed and Granite
        (2kg) at ~27gp is not.
        """
        return net_of_tax(self.estimate, self.item.name)

    @property
    def net_daily_income(self) -> int | None:
        """Post-tax gp/day the market absorbs. The "best to sell" metric."""
        value = self.net_estimate
        return None if value is None else value * self.volume

    @property
    def daily_turnover(self) -> int | None:
        """Rough gp/day the whole market moves in this item.

        The number that answers "can I actually sell a thousand of these". A
        high unit price on a dead market is not income.
        """
        value = self.estimate
        return None if value is None else value * self.volume

    def liquidity_warning(self) -> str | None:
        """Why the volume makes this price hard to act on, if it does."""
        if self.volume == 0:
            return "did not trade at all in the last 24h -- price is stale"
        if self.liquidity in ("dead", "illiquid"):
            return (
                f"only {self.volume:,} traded in 24h -- you cannot reliably sell "
                "much of this at any price"
            )
        return None

    def price_warnings(self) -> list[str]:
        """Why the *number itself* may be wrong, separate from how thin the
        market is. Kept apart from :meth:`liquidity_warning` because a ranking
        table already shows volume in a column -- repeating it per row buries
        these, which are the ones a reader cannot infer from the table."""
        notes: list[str] = []
        if self.erratic:
            ratio = self.spread_ratio or 0
            notes.append(
                f"last buy and last sell differ by {ratio:,.0f}x "
                f"({self.instant_sell:,} vs {self.instant_buy:,}) -- the market is "
                "too thin for these to mean anything"
            )
        if self.avg_sell and self.avg_buy and self.avg_buy > self.avg_sell * MAX_SANE_SPREAD:
            notes.append(
                f"24h averages disagree ({self.avg_sell:,} sell vs {self.avg_buy:,} "
                "buy) -- what you get depends heavily on patience"
            )
        return notes

    def warnings(self) -> list[str]:
        """Everything that would make this price misleading if quoted alone."""
        liquidity = self.liquidity_warning()
        return ([liquidity] if liquidity else []) + self.price_warnings()

    def summary(self) -> str:
        """Model-readable block. Volume first, because it qualifies everything."""
        value = self.estimate
        head = f"{self.item.name} (id {self.item.id})"
        if value is None:
            return f"{head}: no price data at all in the last 24h."

        lines = [
            f"{head}: ~{value:,} gp each [{self.liquidity}, "
            f"{self.volume:,} traded/24h]"
        ]
        if self.instant_sell and self.instant_buy:
            lines.append(
                f"  sell instantly for ~{self.instant_sell:,}, or wait and get "
                f"nearer ~{self.instant_buy:,}"
            )
        if self.tax:
            lines.append(
                f"  you receive ~{self.net_estimate:,} after {self.tax:,} GE tax"
            )
        elif value >= 50:
            lines.append("  exempt from GE tax, so you receive the full price")
        if self.item.limit:
            lines.append(f"  GE buy limit {self.item.limit:,} per 4h")
        turnover = self.daily_turnover
        if turnover is not None:
            lines.append(f"  whole market moves ~{turnover:,} gp/day in this item")
        lines += [f"  WARNING: {w}" for w in self.warnings()]
        return "\n".join(lines)


def _verdict(ranked: list[Price]) -> str:
    """One sentence naming the winner, so the model has nothing left to decide.

    Phrased as a finding rather than a table header ("ANSWER: X is the best...")
    because the model reliably copies a stated conclusion and unreliably derives
    one. Says outright when the winner is still a bad idea -- on the question
    that prompted this module the honest verdict is that both options are dead
    markets, and a bare "sandstone wins" would be true and useless.
    """
    best = ranked[0]
    if best.net_daily_income is None:
        return "ANSWER: none of these have traded recently enough to rank."

    runner = next((p for p in ranked[1:] if p.net_daily_income), None)
    line = (
        f"ANSWER: {best.item.name} is the best of these to sell -- "
        f"{best.net_daily_income:,} gp/day after tax "
        f"({best.net_estimate:,} gp each net of {best.tax:,} tax "
        f"x {best.volume:,} traded/24h)."
    )
    if runner:
        line += (
            f" Next best is {runner.item.name} at "
            f"{runner.net_daily_income:,} gp/day."
        )
    if best.liquidity in ("dead", "illiquid"):
        line += (
            " Say this out loud in your answer: even the winner is an illiquid "
            "market, so this is the least-bad option rather than a good one, and "
            "nobody should go gather these expecting to sell them in quantity."
        )
    return line


def rank(prices: list[Price]) -> list[Price]:
    """By what a seller actually realises in a day, best first.

    Public because a caller rendering its own layout -- Discord, which cannot
    fit this table -- must not re-derive the order. A table sorted one way with
    a verdict chosen another is the exact invitation to "correct" it that
    :func:`compare` exists to remove.
    """
    return sorted(prices, key=lambda p: (p.net_daily_income or 0), reverse=True)


def verdict(prices: list[Price]) -> str:
    """The winner sentence on its own, for callers that draw their own table."""
    return _verdict(rank(prices))


def compare(prices: list[Price]) -> str:
    """Rank items by what a seller realises, and state the verdict outright.

    The leading verdict line is not decoration, it is the point. Handed the
    table alone, mistral-small3.2:24b compared *unit counts* across items with
    a 10x price difference and concluded that 354 granite at 240gp beat 127
    sandstone at 2,387gp -- then quoted "84,960 gp/day vs 303,149 gp/day" in the
    same sentence as its justification. It had the right numbers and drew the
    opposite conclusion from them.

    This is the same call :mod:`reldo.skills` makes about XP arithmetic: a 24B
    model at Q6 is the least reliable component in the system, so the comparison
    happens here, in code, and the model is left with nothing to do but report
    it. Sorted by gp/day for the same reason -- it is the metric the verdict
    uses, and a table ordered one way with a verdict chosen another invites the
    model to "correct" it.
    """
    if not prices:
        return "No matching tradeable items."
    if len(prices) == 1:
        return prices[0].summary()

    ranked = rank(prices)
    width = max(len(p.item.name) for p in ranked)

    lines = [_verdict(ranked), ""]

    # "gp/day" is the column that actually answers "best to sell": unit price
    # alone ranks a 10M item that trades twice a day above a 150gp one that
    # moves two million, which is backwards for anyone asking what to go gather.
    lines.append(
        f"{'item':<{width}}  {'each':>10}  {'after tax':>10}  {'traded/24h':>11}  "
        f"{'net gp/day':>15}  liquidity"
    )
    for price in ranked:
        value = f"{price.estimate:,}" if price.estimate else "no data"
        net = f"{price.net_estimate:,}" if price.net_estimate is not None else "-"
        income = price.net_daily_income
        moved = f"{income:,}" if income is not None else "-"
        lines.append(
            f"{price.item.name:<{width}}  {value:>10}  {net:>10}  {price.volume:>11,}  "
            f"{moved:>15}  {price.liquidity}"
        )

    # Only the warnings the table cannot show. Volume and liquidity are already
    # columns; repeating them per row is what buried these last time.
    flagged = [(p, w) for p in ranked for w in p.price_warnings()]
    if flagged:
        lines.append("")
        lines += [f"! {p.item.name}: {w}" for p, w in flagged]

    thin = [p for p in ranked if p.liquidity in ("dead", "illiquid")]
    if len(thin) == len(ranked):
        lines.append(
            "\nEvery item here is an illiquid market -- the prices above are what "
            "the last few trades happened at, not what you can expect to sell "
            "volume at. Treat them as indicative only."
        )
    elif thin:
        lines.append(
            f"\n{len(thin)} of {len(ranked)} barely trade (marked illiquid/dead). "
            "Their prices are indicative only; you will not move quantity at them."
        )
    return "\n".join(lines)


class GEClient:
    """Async client for the wiki's real-time prices API.

    Args:
        user_agent: Sent on every request. The prices API asks for a descriptive
            agent with a contact route and rate-limits anonymous traffic; this is
            the same courtesy :mod:`reldo.wiki` already pays.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        user_agent: str = "reldo/0.1 (github.com/N0tT1m/nieve)",
        *,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            headers={"User-Agent": user_agent}, timeout=timeout, transport=transport
        )
        self._mapping: dict[int, Item] = {}
        self._mapping_at = 0.0
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def __aenter__(self) -> GEClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str) -> Any:
        try:
            response = await self._http.get(f"{GE_API}/{path}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise GEError(
                f"GE prices API returned HTTP {exc.response.status_code} for {path}."
            ) from exc
        except httpx.HTTPError as exc:
            raise GEError(f"Could not reach the GE prices API: {exc!r}") from exc
        except ValueError as exc:
            raise GEError(f"GE prices API sent malformed JSON for {path}: {exc!r}") from exc

    async def _window(self, path: str) -> dict[str, Any]:
        """Fetch and briefly cache one of the whole-market price endpoints.

        These return every item in one response, so a question comparing eight
        items costs two requests rather than sixteen. The TTL exists for the
        Discord bot, where the client outlives a single question.
        """
        hit = self._cache.get(path)
        now = time.monotonic()
        if hit and now - hit[0] < PRICE_TTL:
            return hit[1]
        payload = await self._get(path)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise GEError(f"GE prices API sent no data for {path}.")
        self._cache[path] = (now, data)
        return data

    async def mapping(self) -> dict[int, Item]:
        """Every tradeable item, by id. Cached for an hour."""
        if self._mapping and time.monotonic() - self._mapping_at < MAPPING_TTL:
            return self._mapping
        payload = await self._get("mapping")
        if not isinstance(payload, list):
            raise GEError("GE mapping endpoint did not return a list.")
        self._mapping = {
            int(row["id"]): Item(
                id=int(row["id"]),
                name=str(row["name"]),
                limit=row.get("limit"),
                high_alch=row.get("highalch"),
                members=bool(row.get("members")),
                examine=str(row.get("examine") or ""),
            )
            for row in payload
            if "id" in row and "name" in row
        }
        self._mapping_at = time.monotonic()
        return self._mapping

    async def find(self, query: str, *, limit: int = 12) -> list[Item]:
        """Resolve a loose name to tradeable items, best match first.

        Four tiers, narrowest first, and the first non-empty one wins outright.
        The tier that matters is the second: OSRS puts variants in parentheses
        ("Sandstone (5kg)", "Prayer potion(4)"), so ``granite`` naming every
        ``Granite (...)`` rock is the asker being precise, not vague.

        Returning the granite *gear* alongside them looks harmless and is not.
        Asked "sandstone or granite", the comparison ranked by gp/day and
        answered "Granite hammer" -- a boss drop -- to someone plainly asking
        what to mine. A word appearing inside a different item's name is a much
        weaker signal than a parenthesised variant of the name given, so the
        tiers never mix.
        """
        needle = query.strip().lower()
        if not needle:
            return []
        items = (await self.mapping()).values()

        def sort(matches: list[Item]) -> list[Item]:
            return sorted(matches, key=lambda i: i.name)[:limit]

        exact = [i for i in items if i.name.lower() == needle]
        if exact:
            return sort(exact)

        # "granite" -> "Granite (5kg)"; also catches the no-space convention
        # used by potion doses, "prayer potion" -> "Prayer potion(4)".
        variants = [
            i
            for i in items
            if i.name.lower().startswith((f"{needle} (", f"{needle}("))
        ]
        if variants:
            return sort(variants)

        prefix = [i for i in items if i.name.lower().startswith(needle)]
        if prefix:
            return sort(prefix)

        contains = [i for i in items if needle in i.name.lower()]
        if contains:
            return sort(contains)

        # Spacing and punctuation last, so it can never shadow a real name.
        # "sword fish" is Swordfish; "half plait" is not a thing but "Half plait
        # of jute fibre" is, and a prefix match on the squashed form finds it.
        squashed = _squash(needle)
        if squashed:
            same = [i for i in items if _squash(i.name) == squashed]
            if same:
                return sort(same)
            starts = [i for i in items if _squash(i.name).startswith(squashed)]
            if starts:
                return sort(starts)

        # Nothing matched, and the commonest reason is a plural. The catalogue
        # names one of a thing -- "Shark", "Lobster" -- and people, and models
        # relaying people, ask for "sharks". Every tier above fails on the
        # trailing s: "shark" does not start with "sharks" and "sharks" is not a
        # substring of "shark".
        #
        # The consequence was worse than an empty result. get_ge_price answers a
        # miss by explaining that untradeable items have no GE price, so a
        # question about 795 sharks came back "Sharks are untradeable and have
        # no GE price" -- confidently, about one of the most traded items in the
        # game.
        #
        # Last, so it can never shadow a real name. Items legitimately ending in
        # s -- "Yew logs", "Shrimps", "Bones" -- match exactly on the first tier
        # and never reach here.
        # -es before -s. "swordfishes" is not "swordfishe", and a fish, a bush
        # and a box all take the longer ending; chopping one letter leaves a
        # stem that matches nothing and the miss looks identical to a real one.
        for suffix, keep in (("es", -2), ("s", -1)):
            if needle.endswith(suffix) and len(needle) > len(suffix) + 2:
                found = await self.find(needle[:keep], limit=limit)
                if found:
                    return found
        return []

    async def prices(self, items: list[Item]) -> list[Price]:
        """Current prices and 24h volume for the given items."""
        if not items:
            return []
        latest = await self._window("latest")
        day = await self._window("24h")

        out = []
        for item in items:
            key = str(item.id)
            now = latest.get(key) or {}
            window = day.get(key) or {}
            out.append(
                Price(
                    item=item,
                    instant_sell=now.get("low"),
                    instant_buy=now.get("high"),
                    avg_sell=window.get("avgLowPrice"),
                    avg_buy=window.get("avgHighPrice"),
                    volume=(window.get("lowPriceVolume") or 0)
                    + (window.get("highPriceVolume") or 0),
                )
            )
        return out

    async def lookup(self, query: str, *, limit: int = 12) -> list[Price]:
        """Loose name straight to priced results. The usual entry point."""
        return await self.prices(await self.find(query, limit=limit))

    async def exactly(self, name: str) -> Price | None:
        """The item that *is* this name, or None. No near misses.

        For callers pricing something the wiki named rather than something a
        person typed. :meth:`find` is built to be generous, and generosity is
        wrong here: a training guide saying "teak plank" means that item, and
        the fallback tiers would happily price "Teak plank pack" or, for a guide
        naming "plank", the ordinary Plank at a fifth of the cost. A wrong price
        in a shopping list is worse than no price, because it multiplies.
        """
        if not name.strip():
            return None
        for price in await self.lookup(name, limit=4):
            if _same_item(price.item.name, name):
                return price
        return None


async def cost_lines(ge, count: int, material: str, *, funding: str = "") -> list[str]:
    """What ``count`` of ``material`` costs at the live buy price, as lines.

    ``funding`` names something the asker proposes to sell to pay for it, and
    adds the sale to the bill: "how many mahogany planks, how much money, and
    how many sharks" is one question, and the third part is the one a model
    answers by inventing a number -- 1,311 sharks against a 38M gp bill it had
    just printed.

    Both sides of the book in four lines, which is the reason they are computed
    together: the planks are bought at the high price and the sharks are sold at
    the low one net of tax, and getting either backwards is worth millions.

    Silent on every failure, and that is the point: in a training plan the
    materials are the answer and the price is a courtesy. A prices API that is
    down, an item the catalogue spells differently, a bracket the guide states
    no material for -- each of those costs the asker a line they did not ask
    for, and none of them should cost them the plan they did.

    Takes the client rather than living on it because a caller may not have one,
    and ``None`` here is the same nothing as a failed lookup.
    """
    if ge is None or count <= 0 or not material:
        return []
    try:
        price = await ge.exactly(material)
    except GEError as exc:
        log.warning("Could not price %r: %s", material, exc)
        return []
    if price is None or not price.buy_estimate:
        return []
    from .money import funded_by, shopping_list

    lines = shopping_list(
        count,
        item_name=material,
        gp_each=price.buy_estimate,
        volume=price.volume,
        buy_limit=price.item.limit,
        warnings=price.warnings(),
    )
    if not funding:
        return lines
    try:
        sold = await ge.exactly(funding)
    except GEError as exc:
        log.warning("Could not price %r: %s", funding, exc)
        return lines
    if sold is None or not sold.net_estimate:
        return lines
    return lines + funded_by(
        count * price.buy_estimate,
        item_name=funding,
        net_each=sold.net_estimate,
        volume=sold.volume,
    )
