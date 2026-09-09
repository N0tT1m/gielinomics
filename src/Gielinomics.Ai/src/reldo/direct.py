"""Answering an OSRS question without asking a model anything.

:mod:`reldo.intents` decides what a question wants; this fetches it. Between
them they answer twenty-six of the thirty-four answer-eval questions in about a
second each, against twenty to forty seconds and a fourteen-pass safety net for
the same answers through :class:`~reldo.agent.WikiAgent`.

The other eight are the model's and should be: "what is the fastest way to train
mining", "what is the lore behind the elven civil war" and their like are prose
to be read and summarised, which is the one thing here it does better. Coverage
is not the target -- being right is -- and four of those eight became *more*
reliable when handlers learned to decline them.

Nothing here is new capability. Every figure comes from code that already
existed -- :mod:`reldo.bucket` for the wiki's structured rows, :mod:`reldo.ge`
for live prices, :mod:`reldo.skills` and :mod:`reldo.money` for arithmetic,
:mod:`reldo.hiscores` for accounts. The agent reaches all of it too; it just
reaches it by persuading a 24B model to pick the right tool, which is the part
that goes wrong.

**Three exits, and the third is the one that keeps this honest.**

1. The router declines -- an unrecognised question goes to the model.
2. A handler runs and the data is there -- answer, in about a second.
3. A handler runs and finds **nothing** -- go to the model as well.

Exit 3 is not a detail. The router will misroute; every classifier does. If a
misroute produced "no data for that" the mistake would reach the asker as a
statement about Old School RuneScape rather than as a failure to understand
them -- which is precisely what ``_force_requirements`` did when it answered a
Defence question with a page stating only Construction and Smithing. A handler
that comes back empty has demonstrated it was the wrong handler.

**It returns an Answer, not a string, and that is what keeps the eval honest.**
Every handler records what it consulted -- the page it read, the price it
fetched, the arithmetic it did -- in the same fields
:class:`~reldo.agent.Answer` uses. Bypassing that and handing back bare prose
would make ``cite``, ``must_calc_xp`` and ``must_price`` in answer_eval pass
vacuously the moment a question routed here, which would read as an improvement
and would be the measurement quietly switching itself off.

**The text here is for a person, not for a model.** The renderers in
:mod:`reldo.agent` are written to be handed *to* the model and carry
instructions with them -- "report both numbers", "do not recompute". Those
belong in a handback and read as noise in an answer, so the sentences below are
written fresh. Only the prose differs; every number comes from the same call.
"""

from __future__ import annotations

import logging
import math

from .agent import Answer
from .bucket import BucketError, name_variants
from .ge import GEError, cost_lines
from .intents import Match, Router
from .money import from_market
from .skills import actions_needed, hours_needed, xp_between

log = logging.getLogger(__name__)

# Requirements to list before saying "and more". A quest gate runs to a dozen
# skills and a wall of them buries the one that was asked about.
MAX_LISTED = 8


def _number(value: object) -> float | None:
    """A Bucket quantity as a number, or None when it is not one.

    Quantities arrive as strings and are not all numeric -- a recipe can say
    "1-3" or "2 (noted)". Guessing 1 for those multiplies a wrong number by a
    thousand and prints it with a comma in it.
    """
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _about(name: str, asked: str) -> bool:
    """Does this page or recipe name the thing that was asked about?

    One shared word is the test, because equality is too strict and anything
    looser is no test at all: "yew logs" against "Yew longbow (u)" is the same
    question asked twice, "piety" against "Money making guide/combat" is not,
    and "mahogany planks" against "Rocking chair" is the index answering a
    question of its own.

    This is the check the three exits did not have. They cover a lookup that
    found *nothing*; a lookup that found something irrelevant is worse, because
    it looks like success -- the Answer is populated, the caller returns, and
    every enforcement pass that would have caught it is skipped.
    """
    from .training import terms

    return bool(terms(name) & terms(asked))


def _made_of(name: str, material: str) -> bool:
    """Is this page the material itself, rather than a thing built out of it?

    Every word, not one shared word. A material's name is two words that do
    different jobs -- "mahogany" says which and "plank" says what -- and half a
    match is how "mahogany planks" reaches "Mahogany hull parts". :func:`_about`
    is the looser test and is right where a page name and a question are two
    spellings of one thing; this is the strict one, for deciding whether a count
    of objects can stand in for a count of materials.
    """
    from .training import terms

    return bool(material) and terms(material) <= terms(name)


def _a(noun: str) -> str:
    """"a Furnace", "an Ammo mould". Cosmetic, and the answers are for people."""
    return f"{'an' if noun[:1].lower() in 'aeiou' else 'a'} {noun}"


def _recipe_xp(recipe: dict, skill: str) -> tuple[str, float] | None:
    """The skill a recipe trains and the XP one of it gives.

    A named skill is a requirement rather than a preference. Gold ore's own
    recipe trains Mining at 65 XP, so a Smithing question answered off it
    produces a confident count of ores for the wrong skill.
    """
    for entry in recipe.get("skills") or []:
        name, experience = entry.get("name"), _number(entry.get("experience"))
        if not name or not experience:
            continue
        if skill and str(name).lower() != skill.lower():
            continue
        return str(name), experience
    return None


class DirectAnswerer:
    """Answer what can be answered exactly; decline everything else.

    Args:
        router: decides the intent. Injected so tests need no embedder.
        retriever: resolves a loosely-named thing to a page. "marlin" is not a
            page and "Raw marlin" is; "gold" is not a page and "Gold bar" is.
            name_variants covers spelling and not identity, so something has to
            look the name up, and retrieval is the thing that does that well.
        wiki: a :class:`~reldo.wiki.WikiClient`, for marked-up requirements.
        bucket: a :class:`~reldo.bucket.BucketClient`, for recipes and quests.
        ge: a :class:`~reldo.ge.GEClient`, for live prices.
        hiscores: a :class:`~reldo.hiscores.HiscoresClient`, for accounts.

    Every client is optional. A handler whose client is absent returns None and
    the question goes to the model, which is the same exit as a lookup that
    found nothing -- there is no configuration in which this answers worse than
    the agent, only ones in which it answers less often.
    """

    def __init__(self, router: Router, *, wiki=None, bucket=None, ge=None,
                 hiscores=None, retriever=None):
        self._router = router
        self._retriever = retriever
        self._wiki = wiki
        self._bucket = bucket
        self._ge = ge
        self._hiscores = hiscores

    async def answer(self, question: str) -> Answer | None:
        """An exact answer, or None to let the model have the question."""
        match = self._router.classify(question)
        if match is None:
            return None
        handler = getattr(self, f"_do_{match.intent}", None)
        if handler is None:
            log.debug("No handler for intent %r", match.intent)
            return None
        found = Answer(text="")
        try:
            found.text = await handler(match, found) or ""
        except (BucketError, GEError) as exc:
            # A source that will not answer is not a fact about the game.
            log.warning("Direct answer for %r failed: %s", match.intent, exc)
            return None
        if not found.text:
            return None
        # Everything it produced came from a source it recorded, so it is
        # grounded by construction rather than by inspection afterwards.
        found.seen.append(found.text)
        found.passes_fired.append(f"direct:{match.intent}")
        log.info("Answered %r directly as %s", question[:50], match.intent)
        return found

    # -- handlers ------------------------------------------------------------
    # Each returns finished prose, or None to fall through. Named _do_<intent>
    # so adding an intent and adding its handler are the same act of naming.

    async def _do_skill_requirement(self, m: Match, a: Answer) -> str | None:
        """"what sailing level do i need to catch marlin" -> Sailing 78.

        Read from the ``data-skill``/``data-level`` attributes the wiki marks
        each requirement up with, so this is a field lookup rather than a guess
        at which bare number beside which icon belongs to which skill. That
        ambiguity is the whole reason the case exists: marlin states Fishing 91
        and Sailing 78 as identical-looking numbers.
        """
        if self._wiki is None:
            return None
        skill, thing = m.slots["skill"], m.slots["thing"]
        # "marlin" is a question; "Raw marlin" is the page. Same spelling gap
        # name_variants was written for, and a miss here reads as "the game
        # does not require that" rather than "I looked in the wrong place".
        candidates: list[str] = []
        for name in name_variants(thing):
            candidates.append(name)
        candidates += await self._pages_for(thing)

        levels: dict[str, int] = {}
        for candidate in list(dict.fromkeys(candidates))[:5]:
            # Stating the skill is not enough to be the answer. Retrieval put
            # "Money making guide/combat" first for "piety", that page states
            # Prayer 74, and the requirement it states is a fact about a
            # money-making method rather than about the prayer.
            if not _about(candidate, thing):
                log.debug("Page %r is not about %r", candidate, thing)
                continue
            found = await self._requirements(candidate)
            if skill in found:
                levels, thing = found, candidate
                break
        if not levels or skill not in levels:
            # Nothing in the markup, which does not mean nothing on the page.
            # The Sorceress's Garden states its four Thieving levels in a table
            # and marks up none of them, so this declined and the model
            # answered unaided -- badly, and about a minigame whose own page
            # says N/A, 25, 45, 65.
            # Retrieval's titles rather than the shortlist's: name_variants
            # spells the thing the way the asker did, and this sentence prints
            # the page name back at them.
            spelled = await self._levels_from_tables(
                await self._pages_for(thing), skill, thing
            )
            if spelled:
                a.pages_read.append(spelled[0])
                return spelled[1]
            # The page does not state that skill. Saying so would be a claim
            # about the game; falling through is a claim about this lookup.
            return None
        a.pages_read.append(thing)
        a.skill_requirements.update(levels)
        return f"{thing.capitalize()} requires {skill} {levels[skill]}."

    async def _levels_from_tables(
        self, candidates: list[str], skill: str, thing: str
    ) -> tuple[str, str] | None:
        """``(page, prose)`` for a skill level stated in a table, not in markup.

        Every level on the page rather than one, because a table of them is a
        table of *alternatives*: the Sorceress's Garden has four gardens at
        four Thieving levels and "what level do I need" has four answers, the
        lowest of which is none at all. Picking the largest -- which is what
        the markup path does when a page repeats a skill -- would answer 65 to
        somebody who can walk in today.
        """
        from .skills import MAX_LEVEL
        from .unlocks import from_table, unlevelled

        for page in candidates[:3]:
            if not _about(page, thing):
                continue
            try:
                tables = await self._wiki.tables(page)
            except Exception as exc:
                log.warning("Could not read tables on %r: %s", page, exc)
                continue
            for table in tables:
                rows = from_table(table, skill, MAX_LEVEL, page)
                if not rows:
                    continue
                named = ", ".join(f"{r.name} {r.level}" for r in rows[:MAX_LISTED])
                free = unlevelled(table, skill)
                tail = ""
                if free:
                    named_free = " and ".join(free[:3])
                    verb = "states" if len(free[:3]) == 1 else "state"
                    tail = (
                        f" {named_free} {verb} no level, so the lowest way in "
                        f"needs no {skill} at all."
                    )
                return page, f"{page} lists {skill} by section: {named}.{tail}"
        return None

    async def _do_price(self, m: Match, a: Answer) -> str | None:
        if self._ge is None:
            return None
        asked = m.slots["item"]
        prices = await self._ge.lookup(asked)
        # Named, not merely returned. find() falls back to substring matching,
        # so a short or wrong needle matches items that have nothing to do with
        # the question -- "it" reaches kiteshield and adamantite, and the
        # highest-volume of those answered a question about a facility bottle.
        prices = [p for p in prices if _about(p.item.name, asked)]
        if not prices:
            return None
        best = max(prices, key=lambda p: p.volume)
        if best.estimate is None:
            return None
        a.prices_checked.append(best.item.name)
        line = f"{best.item.name} is worth about {best.estimate:,} gp"
        if best.net_estimate is not None and best.net_estimate != best.estimate:
            line += f", and you receive {best.net_estimate:,} after the {best.tax:,} gp tax"
        line += f". {best.volume:,} traded in the last 24h ({best.liquidity})."
        # The warning is the useful half on a thin market -- a price nobody
        # trades at is not money you can make.
        for warning in best.warnings():
            line += f"\nWarning: {warning}"
        return line

    async def _do_xp_between(self, m: Match, a: Answer) -> str | None:
        """The gap, or the hours when the question asked for hours.

        "how long does it take to get from 45 to 99 mining at granite rates"
        was answered with the XP gap, which is a true sentence and not the
        answer -- the question asked for a duration and got a quantity. A
        duration needs a rate; when the question does not state one the rate is
        on a wiki page, which is the model's job, so this declines rather than
        answering the question it can answer instead of the one asked.
        """
        low, high = m.slots["from_level"], m.slots["to_level"]
        skill = m.slots.get("skill") or ""
        gap = xp_between(low, high)
        a.xp_calculations.append(f"{low}->{high}")
        what = f"{skill} " if skill else ""
        if not m.slots.get("wants_hours"):
            return f"{what}{low} to {high} is {gap:,} XP."
        rate = m.slots.get("rate")
        if rate:
            return (
                f"{what}{low} to {high} is {gap:,} XP, which at {rate:,g} XP/hour "
                f"is {hours_needed(gap, rate):,.1f} hours."
            )
        return await self._hours_from_the_guide(m, a, gap)

    async def _hours_from_the_guide(self, m: Match, a: Answer, gap: int) -> str | None:
        """A duration for a method the question names but states no rate for.

        "at granite rates" is the asker pointing at a figure on a page rather
        than supplying one, so it can be looked up -- but only the one they
        pointed at. The Mining guide states twelve rates across the brackets
        overlapping 45 to 99, and choosing among them unprompted is the same act
        as inventing one, which is why a question naming no method still
        declines here.

        The bracket must cover the whole range asked about. A rate quoted for
        levels 45-70 does not describe 45 to 99, and an answer pairing the full
        gap with a partial bracket's rate would contradict itself in exactly the
        way ``consistent_hours_from`` in the eval exists to catch.
        """
        from .training import brackets_for

        skill, method = m.slots.get("skill") or "", m.slots.get("method") or ""
        if self._wiki is None or not skill or not method:
            return None
        low, high = m.slots["from_level"], m.slots["to_level"]
        legs, source = await brackets_for(
            self._wiki, skill, low, high, method=method
        )
        covering = [
            leg for leg in legs
            if leg.xp_hour and leg.start <= low and leg.end >= high
        ]
        if not covering:
            return None
        leg = covering[0]
        a.pages_read.append(source)
        return (
            f"{skill} {low} to {high} is {gap:,} XP. At {leg.xp_hour:,g} XP/hour "
            f"that is {hours_needed(gap, leg.xp_hour):,.1f} hours.\n"
            # The sentence, not just the number. A rate is a claim about
            # equipment and attention, and this guide states four for granite
            # between 63,000 and 134,000 -- so which one this is matters as much
            # as what it is.
            f'{source}, {leg.heading.strip()}: "{leg.rate_note}"'
        )

    async def _do_training_count(self, m: Match, a: Answer) -> str | None:
        """"how much gold to smelt from 48 to 50" -> 815 bars, and the XP.

        Both halves, always. The count is what was asked and the XP is the
        figure nobody can check by eye -- and the failure this replaces was
        reporting that second one as something the wiki does not give, which it
        never will, because it is a formula rather than a page.
        """
        if self._bucket is None:
            return None
        skill = m.slots.get("skill") or ""
        low, high = m.slots["from_level"], m.slots["to_level"]
        gap = xp_between(low, high)
        a.xp_calculations.append(f"{low}->{high}")

        recipe, found = None, None
        item = m.slots.get("item") or ""
        if item:
            pages = list(dict.fromkeys(name_variants(item) + await self._pages_for(item)))
            recipe = await self._bucket.first_recipe(pages, skill=skill or None)
            found = _recipe_xp(recipe, skill) if recipe else None
            if found is None and skill:
                # The shortlist holds what the question is about, which is not
                # always the page the recipe is on: "how much gold to smelt"
                # finds Gold ore and Furnace, and the recipe is on Gold bar. So
                # try each as the *material* instead -- one indexed query
                # asking what the named skill makes out of it.
                for page in pages[:4]:
                    recipe = await self._bucket.recipe_from_material(page, skill)
                    found = _recipe_xp(recipe, skill) if recipe else None
                    if found:
                        break
        material = m.slots.get("material") or ""
        # A question naming a material is answered by the guide, not by whichever
        # of the hundred things that material builds the index happened to rank
        # first. "how many mahogany planks 37 to 70" came back "8,163 x Rocking
        # chair", and with a shared-word test to stop it, "8,080 x Mahogany hull
        # parts" -- which passes that test on the word "mahogany" while being a
        # different object at half the XP.
        #
        # So the page has to name the *whole* material to keep the recipe. "Gold
        # bar" answers a question about gold bars because it is the thing asked
        # for; "Mahogany hull parts" does not answer one about mahogany planks,
        # however many words the two have in common.
        if found and material and not _made_of(str(recipe.get("page_name") or ""), material):
            log.debug("Recipe %r is not what %r asked about", recipe.get("page_name"), material)
            found = None

        if found is None:
            # No single recipe covers it, which for a wide range is the normal
            # case rather than a failure -- the method changes as you level. The
            # guide is written in exactly those brackets, so try it before
            # settling for the bare gap.
            if skill:
                spanned = await self._do_training_plan(
                    Match("training_plan",
                          {"skill": skill, "material": material,
                           "funding": m.slots.get("funding") or "",
                           "from_level": low, "to_level": high}, 1.0),
                    a,
                )
                if spanned:
                    return spanned
            what = f"{skill} " if skill else ""
            return f"{what}{low} to {high} is {gap:,} XP."

        trained, per_action = found
        count = actions_needed(gap, per_action)
        name = recipe.get("page_name", "them")
        a.pages_read.append(name)
        a.recipes_checked.append(name)
        return (
            f"{count:,} x {name}, which is {gap:,} {trained} XP from {low} to "
            f"{high} at {per_action:,g} each."
        )

    async def _do_training_plan(self, m: Match, a: Answer) -> str | None:
        """The guide's own brackets, clipped to the range asked for.

        The switch points are the wiki's, not mine: oak larders stop being the
        method at 52 because the guide says so, and reading that off the page
        is the difference between a plan and a guess about one.
        """
        if self._wiki is None:
            return None
        from .training import brackets_for

        skill = m.slots["skill"]
        low, high = m.slots["from_level"], m.slots["to_level"]
        material = m.slots.get("material") or ""
        legs, source = await brackets_for(
            self._wiki, skill, low, high, material=material
        )
        if not legs:
            # Including the case where the guide has brackets but none of them
            # are about the material asked for. Answering off the others would
            # be answering in teak a question that said mahogany.
            return None
        a.pages_read.append(source)
        a.xp_calculations.append(f"{low}->{high}")
        gap = xp_between(low, high)
        # Priced per bracket, because each one is a different material and the
        # cheap method is not the fast one -- which is the comparison somebody
        # asking what this costs is actually trying to make.
        funding = m.slots.get("funding") or ""
        rendered = [leg.render(extra=await self._cost_of(leg, a, funding)) for leg in legs]
        # Alternatives, not legs, and the distinction is not cosmetic. Several
        # of these brackets span the whole range -- Mahogany Homes and Fishing
        # crane repair each cover 1-99 -- so listing them as a plan reads as
        # "do all of these" and triple-counts the XP. The guide offers methods
        # that overlap; choosing between them needs a criterion it does not
        # publish per bracket, so the honest rendering names them as options.
        #
        # Saying "2 mahogany methods" rather than "2 methods" is what makes the
        # narrowing visible. A filtered list that does not say it is filtered
        # reads as the guide's whole answer, and the reader has no way to tell
        # that the cheaper teak route was dropped because they asked in mahogany.
        what = f"{material} method" if material else "method"
        head = (
            f"{skill} {low} to {high} is {gap:,} XP. The guide lists "
            f"{len(legs)} {what}{'s' if len(legs) != 1 else ''} covering part or "
            "all of that -- these are alternatives, not steps, so pick per "
            "bracket rather than doing them all:"
        )
        return head + "\n\n" + "\n\n".join(rendered) + f"\n\nFrom {source}."

    async def _cost_of(self, leg, a: Answer, funding: str = "") -> list[str]:
        lines = await cost_lines(
            self._ge, leg.materials or 0, leg.material, funding=funding
        )
        if lines:
            a.prices_checked.append(leg.material)
            if funding and len(lines) > 1:
                a.prices_checked.append(funding)
                a.gp_calculations.append(f"{leg.materials} x {leg.material}")
        return lines

    async def _do_player_stats(self, m: Match, a: Answer) -> str | None:
        if self._hiscores is None:
            return None
        player = await self._hiscores.lookup(m.slots["player"])
        a.players_checked.append(player.name)
        return player.summary()

    async def _do_recipe(self, m: Match, a: Answer) -> str | None:
        if self._bucket is None:
            return None
        item = m.slots["item"]
        recipe = await self._bucket.recipe(item)
        if not recipe:
            # The recipe is not always filed under the name of the thing. Asked
            # how to make cannonballs, the bucket has no "Cannonball" row at all
            # -- the recipe lives on "Steel cannonball" -- and name_variants
            # covers plurals and spacing rather than a missing qualifier. Every
            # other handler resolves a loose name through retrieval; this was
            # the one that did not, and it cost the case eighty seconds of the
            # model reading a page whose tables it cannot see.
            for page in await self._pages_for(item):
                if not _about(page, item):
                    continue
                recipe = await self._bucket.recipe(page, variants=False)
                if recipe:
                    break
        if not recipe:
            return None
        name = recipe.get("page_name", item)
        a.pages_read.append(name)
        a.recipes_checked.append(name)
        parts = []
        for entry in recipe.get("skills") or []:
            level, skill = entry.get("level"), entry.get("name")
            if level and skill:
                parts.append(f"{skill} {level}")
        materials = [
            f"{mat.get('quantity', '')} x {mat.get('name')}".strip(" x")
            for mat in (recipe.get("materials") or [])
            if mat.get("name")
        ]
        if not parts and not materials:
            return None
        line = f"{name} needs " + (", ".join(parts) if parts else "no skill level")
        if materials:
            line += ". Made from " + ", ".join(materials[:MAX_LISTED])
        if recipe.get("facilities"):
            line += f", at {_a(recipe['facilities'])}"
        # The tool is half the answer to "how do I make X" and the bucket has
        # carried it all along: cannonballs need an ammo mould, and a recipe
        # rendered without it tells somebody to take steel bars to a furnace and
        # stand there.
        if recipe.get("tools"):
            line += f", using {_a(recipe['tools'])}"
        made = (recipe.get("output") or {}).get("quantity")
        if made and str(made) != "1":
            line += f". One of those makes {made}"
        return line + "."

    async def _do_quest_requirements(self, m: Match, a: Answer) -> str | None:
        if self._bucket is None:
            return None
        found = await self._bucket.quest_requirements(m.slots["quest"])
        if not found:
            return None
        title, levels = found
        a.pages_read.append(title)
        a.quests_checked.append(title)
        a.skill_requirements.update(levels)
        if not levels:
            return f"{title} has no skill or quest-point requirements."
        listed = ", ".join(
            f"{skill} {level}"
            for skill, level in sorted(levels.items(), key=lambda kv: -kv[1])[:MAX_LISTED]
        )
        return f"{title} requires {listed}."

    async def _do_quantity_for_goal(self, m: Match, a: Answer) -> str | None:
        if self._ge is None:
            return None
        prices = await self._ge.lookup(m.slots["item"])
        if not prices:
            return None
        best = max(prices, key=lambda p: p.volume)
        if not best.net_estimate:
            return None
        a.prices_checked.append(best.item.name)
        a.gp_calculations.append(str(m.slots["goal"]))
        # "and how long will that take farming minnows" is the second half of
        # the question and the half the model got wrong by three orders of
        # magnitude. The guide publishes a gp/hr for the method, so the hours
        # are a division rather than an estimate.
        rate, source = await self._earning_rate(m.slots.get("doing") or "")
        if source:
            a.pages_read.append(source)
            a.money_ranked.append(source)
        return from_market(
            m.slots["goal"],
            item_name=best.item.name,
            net_each=best.net_estimate,
            gross_each=best.estimate,
            volume=best.volume,
            buy_limit=best.item.limit,
            gp_per_hour=rate or None,
        )

    async def _earning_rate(self, doing: str) -> tuple[float, str]:
        """``(gp/hr, the guide page)`` for a method named loosely, or ``(0, "")``.

        Matched against the guide's own 224 method names rather than parsed out
        of the question, and weighted so that a rare word decides it: "farming
        minnows" shares "farming" with thirty methods and "minnow" with two, and
        an unweighted overlap makes those count the same. The wiki names the
        methods; this only picks which one was meant.
        """
        if self._wiki is None or not doing:
            return 0.0, ""
        from .earnings import SKILLING_GUIDE_PAGE, read_guide
        from .training import terms

        wanted = terms(doing)
        if not wanted:
            return 0.0, ""
        try:
            methods = await read_guide(self._wiki, SKILLING_GUIDE_PAGE)
        except Exception as exc:
            log.warning("Could not read the money-making guide: %s", exc)
            return 0.0, ""
        common: dict[str, int] = {}
        for method in methods:
            for word in terms(method.name):
                common[word] = common.get(word, 0) + 1
        best, score = None, 0.0
        for method in methods:
            shared = wanted & terms(method.name)
            weight = sum(1 / common[word] for word in shared)
            if weight > score:
                best, score = method, weight
        if best is None:
            return 0.0, ""
        # The guide's own subpage, which is what the answer should cite: the
        # overview page it was read from says "Catching minnows" in a row and
        # the detail lives one page down.
        return float(best.gp_per_hour), f"{SKILLING_GUIDE_PAGE.rsplit('/', 1)[0]}/{best.name}"

    async def _do_exchange(self, m: Match, a: Answer) -> str | None:
        """"how many minnows for 5,103 sharks" -> 204,120, and where it says so.

        A fixed swap between two items is neither a price nor a recipe, and it
        had no path here at all: Kylie Minnow sells nothing, she trades 40 for
        1. Asked this, the model answered "the wiki does not say how much a
        shark costs. Ask for a price per shark" -- a refusal about a question
        nobody had asked, and the rate was in the first sentence of the page.

        :mod:`reldo.money`'s own docstring has cited 40 minnows per shark as the
        arithmetic the model gets wrong since the day it was written, so this is
        less a new capability than a debt.
        """
        if self._wiki is None:
            return None
        from .wiki import exchange_rate

        wanted, per = m.slots["wanted"], m.slots["per"]
        count = m.slots["count"]
        for page in await self._pages_for(wanted) + await self._pages_for(per):
            if not (_about(page, wanted) or _about(page, per)):
                continue
            try:
                text = await self._wiki.page_text(page)
            except Exception as exc:
                log.warning("Could not read %r: %s", page, exc)
                continue
            rate = exchange_rate(text, wanted, per)
            if not rate:
                continue
            each, per_each, called, said = rate
            total = math.ceil(count * each / per_each)
            a.pages_read.append(page)
            a.gp_calculations.append(f"{count} x {each}/{per_each}")
            # The page's word for it, not the asker's. "Sharks" from Kylie
            # Minnow are raw sharks, and the two are 300 gp apart. Singularised
            # first because the page's wording may already be plural and
            # "minnowss" is not a fish.
            from .training import singular

            words = called.split()
            words[-1] = singular(words[-1])
            one = " ".join(words)
            head = (
                f"{total:,} {one}s."
                if count != 1
                else f"{each:,} {one}s per {per_each if per_each != 1 else ''} {per}."
                .replace("  ", " ")
            )
            lines = [head, f'{page} says: "{said}"']
            worth = await self._worth_of(total, one, a) if m.slots.get("worth") else ""
            if worth:
                lines.insert(1, worth)
            return "\n".join(lines)
        return None

    async def _worth_of(self, count: int, item: str, a: Answer) -> str:
        """What ``count`` of something sells for, when the question also asked.

        "...and how much money on grand exchange is that" is a second question
        stapled to the first and answerable off the same number, which is the
        whole reason to do it here: the model given the count separately turned
        200,000 minnows into "205 sharks ... which is 200,000 gp", reusing the
        asker's own figure as the answer to a different question.
        """
        if self._ge is None or count <= 0:
            return ""
        try:
            price = await self._ge.exactly(item)
        except GEError as exc:
            log.warning("Could not price %r: %s", item, exc)
            return ""
        if price is None or not price.net_estimate:
            return ""
        a.prices_checked.append(price.item.name)
        return (
            f"At {price.net_estimate:,} gp each after tax that is "
            f"{count * price.net_estimate:,} gp."
        )

    async def _do_best_seller(self, m: Match, a: Answer) -> str | None:
        """Which of the things made from a material is worth most to sell.

        The failure this replaces is not arithmetic, it is the *set*. Asked what
        jewellery made from gold bars sells best, the model compared three items
        it had thought of and answered "Gold necklace" -- which is true of those
        three. The recipe bucket lists forty, and ranked over all of them the
        answer is Ruby necklace at 1.6bn gp/day against the gold necklace's
        114M. Nothing was miscalculated; the question was answered about a
        smaller world than the one it asked about.

        So the set comes from the bucket, the prices from the market, and the
        ordering from :func:`~reldo.ge.rank` -- the same call ``/ge`` makes,
        which already ranks by what a seller realises in a day rather than by
        unit price.
        """
        if self._bucket is None or self._ge is None:
            return None
        from .ge import rank, verdict

        material = m.slots["material"]
        products = await self._bucket.products_of(material)
        if len(products) < 2:
            # One product is not a comparison, and none means the material is
            # misspelled or makes nothing -- either way this is not the handler.
            return None
        items = []
        for name in products:
            items += await self._ge.find(name, limit=1)
        prices = [p for p in await self._ge.prices(items) if p.net_daily_income]
        if not prices:
            return None
        page = material[:1].upper() + material[1:]
        a.pages_read.append(page)
        a.money_ranked.append(page)
        a.prices_checked += [p.item.name for p in rank(prices)[:5]]
        top = rank(prices)[:5]
        listed = "\n".join(
            f"  {p.item.name:<24} {p.net_daily_income:>15,} gp/day"
            for p in top
        )
        # The "ANSWER:" prefix is the CLI table's, where the verdict sits above
        # columns and has to announce itself. Here it is the first sentence.
        return (
            f"{verdict(prices).removeprefix('ANSWER: ')}\n\nRanked over all "
            f"{len(products)} things the "
            f"wiki lists as made from {material}, {len(prices)} of which trade:"
            f"\n{listed}"
        )

    async def _do_money_methods(self, m: Match, a: Answer) -> str | None:
        if self._wiki is None:
            return None
        from .earnings import SKILLING_GUIDE_PAGE, best_per_skill, read_guide, render

        methods = await read_guide(self._wiki, SKILLING_GUIDE_PAGE)
        if not methods:
            return None
        a.pages_read.append(SKILLING_GUIDE_PAGE)
        a.money_ranked.append(SKILLING_GUIDE_PAGE)
        skill = m.slots.get("skill") or ""
        if skill:
            mine = [x for x in methods if x.skill.lower() == skill.lower()]
            if not mine:
                return None
            top = max(mine, key=lambda x: x.gp_per_hour)
            return (
                f"The best {skill} money maker on the wiki's guide is {top.name} "
                f"at {top.gp_per_hour:,} gp/hour."
            )
        return render(best_per_skill(methods))

    async def _do_which_quest(self, m: Match, a: Answer) -> str | None:
        """Which quest gates a boss or an area.

        This used to decline on the grounds that the wiki carries no "what gates
        this" field, so the answer is prose and prose is the model's job. Half
        of that was right and the conclusion did not follow. Asked about
        Vorkath, the model read a *Combat Achievement* page and answered "the
        Vorkath Master achievement requires killing Vorkath 100 times, but it
        does not mention any quest requirements" -- which is the same failure
        the handlers here were taught to avoid, committed on the other side of
        the fence: a page that mentions the thing, standing in for the page
        about it.

        The gate is prose, but it is prose naming something *structured*. Every
        candidate is checked against the wiki's own 228 quests, so what comes
        out is a quest that exists, named on the page about the thing asked
        about, in a sentence that says it gates it. See
        :func:`~reldo.bucket.gating_quest` for what that last part costs.
        """
        if self._wiki is None or self._bucket is None:
            return None
        from .bucket import gating_quest

        thing = m.slots["thing"]
        quests = await self._bucket.quest_names()
        if not quests:
            return None
        # The literal name first, because "Vorkath" is the page about Vorkath
        # and "Vorkath Master" is a page that mentions him.
        candidates = list(dict.fromkeys(name_variants(thing) + await self._pages_for(thing)))
        # What the page states *instead*, kept in case no candidate names a
        # quest. Held rather than returned early, because a later candidate
        # naming one outranks an earlier one merely not naming one.
        instead: tuple[str, dict[str, int]] | None = None
        for candidate in candidates[:4]:
            if not _about(candidate, thing):
                continue
            try:
                pages = await self._wiki.summaries([candidate])
            except Exception as exc:
                log.warning("Could not read %r: %s", candidate, exc)
                continue
            for page in pages:
                gate = gating_quest(page.summary, quests)
                if gate:
                    a.pages_read.append(page.title)
                    a.quests_checked.append(gate)
                    return f"{page.title} requires {gate}."
                if instead is None:
                    levels = await self._requirements(candidate)
                    if levels:
                        instead = page.title, levels
        if instead is None:
            return None
        # Most things are gated by a skill or by nothing, and saying so beats
        # falling through: asked which quest the Alchemical Hydra needs, the
        # model answered "the elite Kourend & Kebos Diary", which is not a quest
        # and is not required. Phrased as a fact about the page rather than
        # about the game, because that is the one this can actually check -- a
        # page stating no quest is weaker evidence than a page stating one.
        title, levels = instead
        a.pages_read.append(title)
        a.skill_requirements.update(levels)
        listed = ", ".join(
            f"{skill} {level}"
            for skill, level in sorted(levels.items(), key=lambda kv: -kv[1])[:MAX_LISTED]
        )
        return f"{title}'s page names no quest, only {listed}."

    async def _pages_for(self, thing: str, *, limit: int = 4) -> list[str]:
        """Page titles this loosely-named thing might be, best first.

        The retriever's own order, untouched. Re-ranking it by how many words a
        title shares with the question is exactly what _best_title does in the
        agent, and it is measurably wrong: given a chunk index that put the
        answering page first, that heuristic discarded the ranking and read a
        different page. Retrieval is better at this than a word count.
        """
        if self._retriever is None:
            return [thing]
        try:
            hits = await self._retriever.shortlist(thing, k=limit)
        except Exception as exc:
            log.warning("Could not resolve %r: %s", thing, exc)
            return [thing]
        return [h.title for h in hits] or [thing]

    async def _requirements(self, page: str) -> dict[str, int]:
        """Skill -> level for one page, highest wins on a repeat.

        A page can state the same skill twice -- once to wield and once for a
        money-making method needing more of it -- and the binding one is larger.
        """
        try:
            found = await self._wiki.requirements(page)
        except Exception as exc:
            log.warning("Could not read requirements on %r: %s", page, exc)
            return {}
        levels: dict[str, int] = {}
        for skill, level in found:
            levels[skill] = max(levels.get(skill, 0), level)
        return levels


def answerer_for(settings, agent) -> DirectAnswerer:
    """A DirectAnswerer sharing the agent's already-warm clients.

    Through the agent rather than building its own, so the GE ``/mapping``
    cache and the Bucket schema cache are the ones already paid for.
    """
    from .intents import router_for

    return DirectAnswerer(
        router_for(settings),
        retriever=agent.retriever,
        wiki=agent.wiki,
        bucket=agent.bucket,
        ge=agent.ge,
        hiscores=agent.hiscores,
    )
