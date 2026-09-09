"""The slash commands.

Split from :mod:`reldo.bot`, which is now about *routing* -- who the bot answers
and how a conversation is threaded -- while this is about the commands themselves.

**Most of these deliberately never touch the model.** `ge.py` computes its verdict
in code because a 24B model handed the same table ranked by unit count and got the
answer backwards; `unlocks.py` filters requirement tables in code because the model
invented an Ironwood mast at Sailing 20; `skills.py` does the arithmetic because
seven-digit division is where it is confidently wrong. All three were reachable
from the CLI and, on Discord, only *through* the model -- which is the one
component they exist to work around. A slash command is the direct route to the
exact answer, and it returns in about a second rather than twenty.

``/wiki`` and the conversational paths in :mod:`reldo.bot` are the exceptions, and
should be: reading and synthesising prose is the job only the model can do.
"""

from __future__ import annotations

import io
import logging

import discord

from .ge import GEError, cost_lines, rank, verdict
from .maps import for_pages, linked_from
from .money import from_market, parse_goal
from .progress import DAY, summarise
from .retrieval import page_url
from .skills import MAX_LEVEL, SKILLS, cheapest_gains, level_at_xp, plan
from .training import brackets_for, header
from .unlocks import dedupe, render
from .voice import VoiceError, retime, spoken_form
from .wom import PERIODS, WomError

log = logging.getLogger(__name__)

# discord.Embed.description caps at 4096. Monospace blocks need the fence too.
DESCRIPTION_LIMIT = 4096
BLOCK_BUDGET = DESCRIPTION_LIMIT - 20

# Discord shows at most 25 autocomplete suggestions and 25 choices per option.
MAX_CHOICES = 25

# Offered by /trend's autocomplete. Not a validation list -- the platform parses
# the window itself and says so when it cannot -- just the ones worth suggesting.
TREND_WINDOWS = (
    ("Last 24 hours", "24h"),
    ("Last 7 days", "7d"),
    ("Last 30 days", "30d"),
)

PARCHMENT = discord.Colour.from_rgb(94, 77, 48)

WIKI_ATTRIBUTION = "Data from the OSRS Wiki · CC BY-NC-SA 3.0"


def _block(text: str, *, budget: int = BLOCK_BUDGET) -> str:
    """A monospace block, truncated on a line boundary rather than mid-row.

    Cutting a fixed-width table mid-row produces a line that looks like data and
    is not, which is worse than visibly losing the tail.
    """
    if len(text) > budget:
        text = text[:budget].rsplit("\n", 1)[0] + "\n…"
    return f"```\n{text}\n```"


GE_ATTRIBUTION = "Live prices · prices.runescape.wiki"


async def _split_items(ge, raw: str) -> list[str]:
    """The item names in one ``/ge`` argument.

    Splitting on whitespace was the original rule and it cannot express half the
    catalogue: a great many OSRS items have a space in the name, so "granite
    maul" arrived as *two* lookups and came back as every granite rock ranked
    against every maul. The autocomplete made it worse rather than better --
    it completes to real names, so picking "Granite maul" from the dropdown
    produced a value the command then took apart.

    Commas separate. They cannot appear in an item name, so the split is
    unambiguous, and the autocomplete inserts them.

    Whitespace stays as a fallback for input with no comma in it, because
    ``/ge sandstone granite`` is what the command has always accepted and there
    is no reason to break it. The ambiguity that leaves -- is "granite maul" one
    item or two? -- is settled by asking the catalogue rather than guessing:
    "Granite maul" is an item and "sandstone granite" is not.
    """
    if "," in raw:
        return [name.strip() for name in raw.split(",") if name.strip()]
    whole = " ".join(raw.split())
    if not whole:
        return []
    if " " in whole:
        try:
            if await ge.find(whole, limit=1):
                return [whole]
        except Exception:
            # A catalogue that will not answer should not change how the input
            # is read; the lookup below reports the failure properly.
            pass
    return whole.split()

# Enough for "Money making guide/Catch…" to stay recognisable and for three
# columns to fit the narrowest client without wrapping.
GE_NAME_WIDTH = 22


def _ge_one(price) -> discord.Embed:
    """One item, as embed fields rather than a monospace paragraph.

    ``Price.summary`` is written for the *model*: one fact per line, labels
    spelled out, no alignment. Rendered into a Discord code block those lines
    run past the width of the embed and wrap mid-number -- "435,433" ends up
    under "traded/24h" on the next row, and the block buys nothing, because
    there are no columns in it to align.

    Fields are what Discord has for this shape. Three to a row, each one a
    label and a figure, and they reflow instead of wrapping.
    """
    embed = discord.Embed(title=price.item.name[:256], colour=PARCHMENT)
    embed.set_footer(text=GE_ATTRIBUTION)
    if price.estimate is None:
        embed.description = "No price data at all in the last 24h."
        return embed

    embed.description = f"**{price.estimate:,} gp** each · {price.liquidity}"
    if price.instant_sell:
        embed.add_field(name="Sell instantly", value=f"{price.instant_sell:,}")
    if price.instant_buy:
        embed.add_field(name="Or wait for", value=f"{price.instant_buy:,}")
    if price.tax:
        embed.add_field(
            name="You receive", value=f"{price.net_estimate:,}\n-# {price.tax:,} tax"
        )
    elif price.estimate >= 50:
        embed.add_field(name="You receive", value=f"{price.estimate:,}\n-# no tax")
    if price.item.limit:
        embed.add_field(name="Buy limit", value=f"{price.item.limit:,}\n-# per 4h")
    embed.add_field(name="Traded", value=f"{price.volume:,}\n-# per 24h")
    if price.daily_turnover is not None:
        embed.add_field(name="Market moves", value=f"{price.daily_turnover:,}\n-# gp/day")
    # Not a field: a warning is a sentence, and a sentence in a third-width
    # column is a column of single words.
    for warning in price.warnings():
        embed.add_field(name="⚠", value=warning, inline=False)
    return embed


def _trend_embed(trend) -> discord.Embed:
    """A price movement, as a direction first and numbers second.

    Deliberately leads with the word rather than the percentage. "Rising
    sharply" is the answer to what was asked; +12.4% is the evidence for it, and
    a reader who wants only the answer should not have to do the comparison
    themselves. The bands behind that word are wide on purpose -- Grand Exchange
    prices wander a percent or two on nothing.
    """
    embed = discord.Embed(title=trend.item.name[:256], colour=PARCHMENT)
    embed.set_footer(text=GE_ATTRIBUTION)

    if trend.end is None or trend.samples == 0:
        embed.description = (
            f"No retained history for this over the last {trend.window}. "
            "The platform only knows what it has been running long enough to see."
        )
        return embed

    embed.description = f"**{trend.direction}** over {trend.window}"
    change = trend.change_percent
    if change is not None and trend.start is not None:
        embed.add_field(name="Was", value=f"{trend.start:,.0f} gp")
        embed.add_field(name="Now", value=f"{trend.end:,.0f} gp")
        embed.add_field(name="Change", value=f"{change:+.1f}%")
    else:
        embed.add_field(name="Now", value=f"{trend.end:,.0f} gp")

    if trend.low is not None and trend.high is not None and trend.high > trend.low:
        embed.add_field(name="Range", value=f"{trend.low:,.0f}-{trend.high:,.0f}")
    if trend.volume:
        embed.add_field(name="Traded", value=f"{trend.volume:,}")
    embed.add_field(name="Bars", value=f"{trend.samples:,}")
    return embed


def _ge_table(prices: list, title: str) -> discord.Embed:
    """Several items: the verdict, then the narrowest table that still answers.

    The CLI table carries six columns and runs to ~85 characters, which no
    Discord client shows without wrapping every row. Dropped here are the ones
    a reader can live without: gp/day is the column the verdict is computed
    from, so it stays, and unit price stays because it is what people picture.
    Volume survives as the liquidity word it already produces.
    """
    ranked = rank(prices)
    rows = [f"{'item':<{GE_NAME_WIDTH}} {'each':>9} {'gp/day':>13}"]
    for price in ranked:
        name = price.item.name
        name = name if len(name) <= GE_NAME_WIDTH else name[: GE_NAME_WIDTH - 1] + "…"
        each = f"{price.estimate:,}" if price.estimate else "-"
        income = price.net_daily_income
        rows.append(
            f"{name:<{GE_NAME_WIDTH}} {each:>9} "
            f"{(f'{income:,}' if income is not None else '-'):>13}"
        )

    embed = _embed(title, verdict(prices) + "\n" + _block("\n".join(rows)))
    embed.set_footer(text=GE_ATTRIBUTION)
    flagged = [(p, w) for p in ranked for w in p.price_warnings()]
    if flagged:
        embed.add_field(
            name="⚠",
            value="\n".join(f"**{p.item.name}** — {w}" for p, w in flagged)[:1024],
            inline=False,
        )
    return embed


# "Construction 48" is the longest cell at 15; three of them plus gaps is 49
# characters, which fits the narrowest client without wrapping.
SKILL_COLUMN = 12
SKILLS_PER_ROW = 3


def _stats_embed(
    player, *, title: str | None = None, footer: str | None = None
) -> discord.Embed:
    """A player's levels as a grid rather than one very long line.

    ``Player.summary`` joins all 23 skills with commas onto a single line -- 291
    characters for a normal account. That is right for the model, which has no
    layout, and unreadable in a code block, which wraps it into a paragraph of
    numbers with no way to find the skill you came for.

    A grid is what the data was always shaped like: name, level, three to a row,
    alphabetical so a skill is where you expect it.
    """
    overall = player.skills.get("Overall")
    head = f"**Total level {overall.level:,}**" if overall else f"**{player.name}**"
    if overall and overall.xp > 0:
        head += f" · {overall.xp:,} XP"
    head += f" · combat {player.combat_level}"

    ranked = sorted(
        (s for name, s in player.skills.items() if name != "Overall" and s.ranked),
        key=lambda s: s.name,
    )
    cells = [f"{s.name:<{SKILL_COLUMN}}{s.level:>3}" for s in ranked]
    rows = [
        "  ".join(cells[i : i + SKILLS_PER_ROW])
        for i in range(0, len(cells), SKILLS_PER_ROW)
    ]
    embed = _embed(title or player.name, head + "\n" + _block("\n".join(rows)), footer=footer)

    # Both of these are prose, so neither belongs in the grid or in a block.
    unranked = sorted(
        name for name, s in player.skills.items() if name != "Overall" and not s.ranked
    )
    if unranked:
        embed.add_field(name="Unranked", value=", ".join(unranked)[:1024], inline=False)
    done = player.done()
    if done:
        embed.add_field(
            name="Done",
            value=", ".join(f"{a.name} {a.score:,}" for a in done[:12])[:1024],
            inline=False,
        )
    return embed


def _embed(title: str, description: str, *, footer: str | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=title[:256], description=description[:DESCRIPTION_LIMIT], colour=PARCHMENT
    )
    if footer:
        embed.set_footer(text=footer)
    return embed


async def _is_monster(client, title: str) -> bool:
    """Whether a page is a monster, and so has somewhere it lives.

    Asked of the wiki's structured data rather than guessed from the title.
    False on any failure: the fallback this gates is a garnish, and a Bucket
    outage should cost a map link rather than the command.
    """
    agent = getattr(client, "agent", None)
    if agent is None:
        return False
    try:
        rows = await agent.bucket.select(
            "infobox_monster", ["page_name"], where={"page_name": title}, limit=1
        )
    except Exception:
        return False
    return bool(rows)




def register(client) -> None:
    """Attach every slash command to a :class:`~reldo.bot.ReldoClient`."""
    tree = client.tree

    # -- /wiki -------------------------------------------------------------
    # The one command that needs the model. Everything below is exact.

    @tree.command(name="wiki", description="Ask a question about Old School RuneScape")
    @discord.app_commands.describe(question="What do you want to know?")
    async def wiki(interaction: discord.Interaction, question: str) -> None:
        from .bot import render_answer

        # Must happen inside 3s; the agent will take longer than that.
        await interaction.response.defer(thinking=True)
        key = (interaction.channel_id, interaction.user.id)
        # Under the conversation lock, exactly as the mention path runs. This
        # key is the same one `ReldoClient.conversation_key` falls back to, so
        # answering outside the lock did not merely race other `/wiki` calls --
        # it raced mentions in the same channel, over the same history.
        async with client.conversation(key):
            try:
                # Both ids, exactly as the mention path passes them. Omitting
                # them did not fail, it degraded: `answer` defaults both to
                # None, so a `/wiki` question resolved no linked account,
                # fetched no stats, applied no persona and saw no live session.
                # `/link` closes by promising answers that start from your
                # actual level, and this is the command most people will then
                # use to ask for one.
                asked, answer, places = await client.answer(
                    key,
                    question,
                    user_id=interaction.user.id,
                    channel_id=interaction.channel_id,
                )
            except Exception:
                log.exception("Failed to answer %r", question)
                await interaction.followup.send(
                    "Something broke while I was reading the wiki. Try again in a moment."
                )
                return

            # wait=True so we get the message back: its id is what lets somebody
            # reply to a slash-command answer and have the follow-up land in the
            # same conversation rather than starting a new one.
            sent = await interaction.followup.send(
                embed=render_answer(asked, answer, places), wait=True
            )
            # Inside the lock, for the reason `on_message` documents: the next
            # waiter is released as this block ends and resolves its follow-up
            # against whatever history says by then.
            client.remember(key, asked, answer.text, message_id=getattr(sent, "id", None))

    # -- /ge ---------------------------------------------------------------

    @tree.command(name="ge", description="Live Grand Exchange prices, ranked. No model involved.")
    @discord.app_commands.describe(
        items="Item names. Separate several with commas: 'granite maul, sandstone'."
    )
    async def ge(interaction: discord.Interaction, items: str) -> None:
        await interaction.response.defer(thinking=True)
        names = await _split_items(client.ge_client, items)
        if not names:
            await interaction.followup.send("Give me at least one item name.")
            return

        found = []
        try:
            for name in names:
                found.extend(await client.ge_client.lookup(name))
        except GEError as exc:
            await interaction.followup.send(str(exc))
            return
        if not found:
            await interaction.followup.send(
                f"No tradeable item matching: {', '.join(names)}. Untradeable items "
                "have no GE price at all."
            )
            return

        # De-duplicate: overlapping queries would otherwise list the same row
        # twice and skew how the ranking reads.
        unique = list({p.item.id: p for p in found}.values())
        embed = (
            _ge_one(unique[0])
            if len(unique) == 1
            else _ge_table(unique, ", ".join(names)[:256])
        )
        await interaction.followup.send(embed=embed)

    @ge.autocomplete("items")
    async def ge_autocomplete(interaction: discord.Interaction, current: str):
        """Suggest real item names, so a typo never reaches the ranking.

        Completes the last *comma-separated* entry and keeps the ones before it,
        because the command takes several items in one string and replacing the
        whole value would throw away what has already been typed.

        Per comma rather than per word, which is what it used to do. Completing
        a word cannot suggest "Granite maul" at all -- it would be matching on
        "maul" with "granite" stranded in front of it -- and worse, whatever it
        did suggest was rejoined with a space and then taken apart again by the
        command. A dropdown that offers values its own command cannot parse is
        the shape of bug this is.
        """
        head, sep, tail = current.rpartition(",")
        tail = tail.strip()
        if len(tail) < 2:
            return []
        try:
            matches = await client.ge_client.find(tail, limit=MAX_CHOICES)
        except Exception:
            return []
        prefix = f"{head.strip()}, " if sep else ""
        return [
            discord.app_commands.Choice(
                name=f"{prefix}{item.name}"[:100], value=f"{prefix}{item.name}"[:100]
            )
            for item in matches
        ]

    # -- /trend ------------------------------------------------------------
    # The one question the upstream APIs cannot answer. /latest and /24h say what
    # a thing is worth now; only the platform's retained bars say what it has been
    # doing, so this command exists only when RELDO_GIELINOMICS_URL is set.

    @tree.command(
        name="trend",
        description="What an item's price has been doing. Needs the Gielinomics platform.",
    )
    @discord.app_commands.describe(
        item="One item name.",
        window="How far back: 24h, 7d, 30d. Defaults to 7d.",
    )
    async def trend(
        interaction: discord.Interaction, item: str, window: str = "7d"
    ) -> None:
        await interaction.response.defer(thinking=True)

        ge = client.ge_client
        # Duck-typed rather than isinstance: the platform client is a subclass of
        # the plain one, so the honest question is "can this answer history",
        # and the answer is whether the method is there.
        if ge is None or not hasattr(ge, "trend"):
            await interaction.followup.send(
                "I am reading prices straight from the wiki, which keeps no history. "
                "Set RELDO_GIELINOMICS_URL to point me at the platform."
            )
            return

        try:
            found = await ge.trend(item, window=window)
        except ValueError as exc:
            await interaction.followup.send(str(exc))
            return
        except GEError as exc:
            await interaction.followup.send(str(exc))
            return
        except Exception as exc:  # the platform is one more thing that can be down
            log.warning("Could not read a trend for %r: %r", item, exc)
            await interaction.followup.send(
                "The platform could not answer that just now."
            )
            return

        if found is None:
            await interaction.followup.send(
                f"No tradeable item matching {item!r}. Untradeable items have no GE price."
            )
            return

        await interaction.followup.send(embed=_trend_embed(found))

    @trend.autocomplete("item")
    async def trend_autocomplete(interaction: discord.Interaction, current: str):
        """One item, so this completes the whole value rather than the last comma."""
        if len(current.strip()) < 2:
            return []
        try:
            matches = await client.ge_client.find(current.strip(), limit=MAX_CHOICES)
        except Exception:
            return []
        return [
            discord.app_commands.Choice(name=item.name[:100], value=item.name[:100])
            for item in matches
        ]

    @trend.autocomplete("window")
    async def trend_window_autocomplete(interaction: discord.Interaction, current: str):
        return [
            discord.app_commands.Choice(name=label, value=value)
            for label, value in TREND_WINDOWS
            if current.lower() in value
        ]

    # -- /goal -------------------------------------------------------------
    # "How many sharks for 5 mill" -- the question the agent got wrong by three
    # orders of magnitude, answered by a division against the live price instead
    # of by a 24B model in its head. No model involved.

    @tree.command(
        name="goal",
        description="How many of an item you need to sell to hit a coin goal. No model involved.",
    )
    @discord.app_commands.describe(
        amount="How much money you want. '5m', '5 mill', '500k' and '5000000' all work.",
        item="What you would be selling",
        gp_per_hour="Optional gp/hr, if you know the rate, to get hours",
    )
    async def goal(
        interaction: discord.Interaction,
        amount: str,
        item: str,
        gp_per_hour: float | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        target = parse_goal(amount) or parse_goal(f"{amount} gp")
        if not target:
            await interaction.followup.send(
                f"I cannot read {amount!r} as an amount of money. Try '5m', '500k' "
                "or a plain number."
            )
            return

        try:
            found = await client.ge_client.lookup(item)
        except GEError as exc:
            await interaction.followup.send(str(exc))
            return
        if not found:
            await interaction.followup.send(
                f"No tradeable item matching {item!r}. Untradeable items have no GE price."
            )
            return

        # Most liquid match wins. A partial name expands to every variant, and
        # the one somebody means by "shark" is the one the market actually
        # trades, not the first alphabetically.
        price = max(found, key=lambda p: p.volume)
        net = price.net_estimate
        if not net:
            await interaction.followup.send(
                f"{price.item.name} has no usable price -- it did not trade in the last 24h."
            )
            return

        body = from_market(
            target,
            item_name=price.item.name,
            net_each=net,
            gross_each=price.estimate,
            volume=price.volume,
            buy_limit=price.item.limit,
            gp_per_hour=gp_per_hour,
        )
        warnings = "\n".join(f"⚠ {w}" for w in price.warnings())
        await interaction.followup.send(
            embed=_embed(
                f"{target:,} gp in {price.item.name}",
                _block(body) + (f"\n{warnings}" if warnings else ""),
                footer="Live prices · prices.runescape.wiki",
            )
        )

    @goal.autocomplete("item")
    async def goal_autocomplete(interaction: discord.Interaction, current: str):
        if len(current) < 2:
            return []
        try:
            matches = await client.ge_client.find(current, limit=MAX_CHOICES)
        except Exception:
            return []
        return [
            discord.app_commands.Choice(name=i.name[:100], value=i.name[:100])
            for i in matches
        ]

    # -- /unlocks ----------------------------------------------------------

    @tree.command(
        name="unlocks",
        description="Everything a skill unlocks at or below a level, read from the wiki's tables.",
    )
    @discord.app_commands.describe(skill="Which skill", level="Maximum level, inclusive")
    @discord.app_commands.choices(
        # 24 skills against Discord's 25-choice ceiling. A dropdown rather than
        # free text because a typo here returns "nothing found", which reads as
        # a fact about the game rather than as a misspelling.
        skill=[discord.app_commands.Choice(name=s, value=s) for s in SKILLS[:MAX_CHOICES]]
    )
    async def unlocks(
        interaction: discord.Interaction,
        skill: discord.app_commands.Choice[str],
        level: discord.app_commands.Range[int, 1, MAX_LEVEL],
    ) -> None:
        from .agent import gather_unlocks

        await interaction.response.defer(thinking=True)
        try:
            found, scanned = await gather_unlocks(client.agent.retriever, skill.value, level)
        except Exception:
            log.exception("Unlock scan failed for %s %s", skill.value, level)
            await interaction.followup.send("Something broke while reading the wiki tables.")
            return

        if not found:
            await interaction.followup.send(
                f"No table with a '{skill.value} level' column turned up. Either the "
                "requirements are not in a table, or the skill has no page listing them."
            )
            return

        await interaction.followup.send(
            embed=_embed(
                f"{skill.value} level {level} and below",
                _block(render(dedupe(found), skill.value, level)),
                footer=f"From {', '.join(scanned)[:200]} · {WIKI_ATTRIBUTION}",
            )
        )

    # -- /map --------------------------------------------------------------

    @tree.command(name="map", description="Where a place is, with a world-map link.")
    @discord.app_commands.describe(place="A location, dungeon or city")
    async def map_(interaction: discord.Interaction, place: str) -> None:
        await interaction.response.defer(thinking=True)
        hits = await client.agent.retriever.shortlist(place, k=4)
        if not hits:
            await interaction.followup.send(f"Nothing on the wiki for {place!r}.")
            return

        # Take the first shortlisted page that actually declares a map, rather
        # than only the top hit: "Vorkath" outranks "Ungael" for a question
        # about where Vorkath is, and has no map of its own.
        titles = [h.title for h in hits]
        located = await for_pages(client.wiki_client, titles)
        if not located and await _is_monster(client, titles[0]):
            # Nothing in the results declares a map, which is the normal case for
            # a monster: it has no location of its own, only somewhere it lives.
            # Follow the top page's own links -- Vorkath's lead links to Ungael.
            #
            # Only for a monster. An item has no location, and following a whip's
            # links finds one anyway: "abyssal whip" reached Slayer and returned
            # a map of the Slayer Tower, which answers a question nobody asked.
            located = await linked_from(client.wiki_client, titles[0])
        if not located:
            await interaction.followup.send(
                f"Found {titles[0]!r}, but no page in the results has a map. Not "
                "everything does -- items and most monsters have no location."
            )
            return

        found = located[0]
        summary = next((h.summary for h in hits if h.title == found.name), "")
        await interaction.followup.send(
            embed=_embed(
                found.name,
                f"{summary[:600]}\n\n🗺 [View on the world map]({found.url})",
                footer=WIKI_ATTRIBUTION,
            )
        )

    # -- /link and /unlink -------------------------------------------------

    @tree.command(name="link", description="Remember your RuneScape name, so answers know it.")
    @discord.app_commands.describe(username="Your OSRS account name")
    async def link(interaction: discord.Interaction, username: str) -> None:
        if client.accounts is None:
            await interaction.response.send_message("Account links are not configured.")
            return
        await interaction.response.defer(thinking=True)
        try:
            name = client.accounts.link(interaction.user.id, username)
        except ValueError as exc:
            await interaction.followup.send(str(exc))
            return

        # Look it up immediately. Storing a name that 404s means every later
        # answer quietly falls back to generic advice, which is a hard failure
        # to trace back to a typo made once, days earlier.
        try:
            player = await client.agent.hiscores.lookup(name)
        except Exception as exc:
            await interaction.followup.send(
                f"Linked you to {name!r}, but the hiscores did not recognise it: {exc} "
                "Run /link again with the exact spelling if that is wrong."
            )
            return
        await interaction.followup.send(
            embed=_stats_embed(
                player,
                title=f"Linked to {player.name}",
                footer="Now I know where you are — ask me how to train something and "
                "I will start from your actual level.",
            )
        )

    @tree.command(name="unlink", description="Forget your RuneScape name.")
    async def unlink(interaction: discord.Interaction) -> None:
        if client.accounts is None or not client.accounts.unlink(interaction.user.id):
            await interaction.response.send_message("You had no account linked.")
            return
        await interaction.response.send_message("Forgotten. Answers will be generic again.")

    # -- /stats ------------------------------------------------------------

    @tree.command(name="stats", description="A player's levels from the official hiscores.")
    @discord.app_commands.describe(username="OSRS account name. Omit to use your linked one.")
    async def stats(interaction: discord.Interaction, username: str | None = None) -> None:
        await interaction.response.defer(thinking=True)
        name = username or client.rsn_for(interaction.user.id)
        if not name:
            await interaction.followup.send(
                "Give me a username, or run `/link` once and I will remember yours."
            )
            return
        try:
            player = await client.agent.hiscores.lookup(name)
        except Exception as exc:
            await interaction.followup.send(str(exc))
            return
        await interaction.followup.send(
            embed=_stats_embed(
                player, footer="Official OSRS hiscores"
            )
        )

    # -- /next -------------------------------------------------------------

    @tree.command(
        name="next", description="Which levels are cheapest to get next. Exact XP, no model."
    )
    @discord.app_commands.describe(username="OSRS account name. Omit to use your linked one.")
    async def next_(interaction: discord.Interaction, username: str | None = None) -> None:
        await interaction.response.defer(thinking=True)
        name = username or client.rsn_for(interaction.user.id)
        if not name:
            await interaction.followup.send(
                "Give me a username, or run `/link` once and I will remember yours."
            )
            return
        try:
            player = await client.agent.hiscores.lookup(name)
        except Exception as exc:
            await interaction.followup.send(str(exc))
            return

        rows = cheapest_gains({s: player.level(s) for s in SKILLS})
        if not rows:
            await interaction.followup.send(f"{player.name} is maxed. Nothing left to chase.")
            return
        width = max(len(r[0]) for r in rows)
        listing = "\n".join(
            f"{skill:<{width}}  {level:>3} -> {target:<3}  {need:>10,} xp"
            for skill, level, target, need in rows
        )
        await interaction.followup.send(
            embed=_embed(
                f"Cheapest next levels for {player.name}",
                _block(listing),
                footer=f"combat {player.combat_level} · exact XP from the level table",
            )
        )

    # -- /xp ---------------------------------------------------------------

    @tree.command(name="xp", description="Exact XP between two levels, and how long it takes.")
    @discord.app_commands.describe(
        from_level="Current level",
        to_level="Target level",
        xp_per_hour="Optional XP/hr from a training guide, to get hours",
        xp_per_action="Optional XP per action, to get how many actions",
    )
    async def xp(
        interaction: discord.Interaction,
        from_level: discord.app_commands.Range[int, 1, MAX_LEVEL],
        to_level: discord.app_commands.Range[int, 1, MAX_LEVEL],
        xp_per_hour: float | None = None,
        xp_per_action: float | None = None,
    ) -> None:
        # No defer: this is a table lookup and two divisions, so it answers well
        # inside the 3s window and an ack would only add a round trip.
        try:
            result = plan(
                from_level, to_level, xp_per_hour=xp_per_hour, xp_per_action=xp_per_action
            )
        except ValueError as exc:
            await interaction.response.send_message(f"Cannot compute that: {exc}")
            return
        await interaction.response.send_message(
            embed=_embed(f"{from_level} → {to_level}", _block(result))
        )

    # -- /train ------------------------------------------------------------
    # /xp is the arithmetic alone. This is the arithmetic plus what the wiki
    # says you need to buy, per level bracket, for the range actually asked for.

    @tree.command(
        name="train",
        description="XP to a target level, and the materials each bracket needs.",
    )
    @discord.app_commands.describe(
        skill="Which skill",
        target_level="The level you want",
        current_xp="Your current XP in that skill. Omit to use your linked account.",
        material="Only methods using this, e.g. mahogany planks. Omit for all of them.",
    )
    @discord.app_commands.choices(
        skill=[discord.app_commands.Choice(name=s, value=s) for s in SKILLS[:MAX_CHOICES]]
    )
    async def train(
        interaction: discord.Interaction,
        skill: discord.app_commands.Choice[str],
        target_level: discord.app_commands.Range[int, 2, MAX_LEVEL],
        current_xp: int | None = None,
        material: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        if current_xp is None:
            rsn = client.rsn_for(interaction.user.id)
            if not rsn:
                await interaction.followup.send(
                    "Give me your current XP, or run `/link` once and I will read "
                    "it from the hiscores."
                )
                return
            try:
                player = await client.agent.hiscores.lookup(rsn)
            except Exception as exc:
                await interaction.followup.send(f"Could not read {rsn}'s hiscores: {exc}")
                return
            entry = player.skills.get(skill.value)
            if entry is None:
                await interaction.followup.send(
                    f"The hiscores have no {skill.value} entry for {rsn}."
                )
                return
            current_xp = max(entry.xp, 0)

        if current_xp < 0:
            await interaction.followup.send("XP cannot be negative.")
            return

        body = [header(skill.value, current_xp, target_level)]
        from_level = level_at_xp(current_xp)
        source = ""
        if target_level > from_level:
            brackets, source = await brackets_for(
                client.agent.retriever.client, skill.value, from_level, target_level,
                material=material or "",
            )
            for leg in brackets:
                # Priced per bracket rather than per plan: each method uses a
                # different material and the whole point of listing them
                # together is that the cheap one is not the fast one.
                cost = await cost_lines(
                    client.agent.ge, leg.materials or 0, leg.material
                )
                body.append(leg.render(extra=cost))
            if not brackets and material:
                # Distinguished from having no guide at all, because the two ask
                # for different things next: drop the filter, or accept that this
                # skill has no bracketed guide to read.
                body.append(
                    f"\nNo {material} method in the {skill.value} guide covers "
                    f"{from_level} to {target_level}. Run this without the "
                    "material to see every method it does list."
                )
            elif not brackets:
                body.append(
                    "\nNo training guide with level brackets turned up for "
                    f"{skill.value}, so the materials are not in this answer."
                )

        await interaction.followup.send(
            embed=_embed(
                f"{skill.value} to {target_level}",
                _block("\n\n".join(body)),
                footer=f"{source} · {WIKI_ATTRIBUTION}" if source else WIKI_ATTRIBUTION,
            )
        )

    # -- /search -----------------------------------------------------------

    @tree.command(name="search", description="What retrieval returns for a query. No model.")
    @discord.app_commands.describe(query="What to look for")
    async def search(interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True)
        hits = await client.agent.retriever.shortlist(query, k=8)
        if not hits:
            await interaction.followup.send(f"Nothing found for {query!r}.")
            return
        # NOT a monospace block. Page titles are long and vary wildly in width,
        # so a fixed-width layout wraps every second row on a normal-width
        # client and the columns stop lining up -- which is the entire reason to
        # use one. Rows that flow are the right shape for variable-length data.
        #
        # The found_by flags are the point: they show which half of the hybrid
        # earned each hit, which is what makes this worth having over /wiki.
        lines = []
        for position, hit in enumerate(hits, 1):
            # Titles are wiki-controlled and contain _ and * often enough to
            # matter; unescaped, one of them silently italicises the rest.
            title = discord.utils.escape_markdown(hit.title)
            found = " + ".join(hit.found_by)
            lines.append(
                f"**{position}.** [{title}]({page_url(hit.title)})\n"
                f"-# {found} · {hit.score:.4f}"
            )
        await interaction.followup.send(
            embed=_embed(query, "\n".join(lines), footer=WIKI_ATTRIBUTION)
        )

    # -- /say --------------------------------------------------------------

    @tree.command(name="say", description="Speak something out loud, in her voice.")
    @discord.app_commands.describe(
        text="What to say", voice="Which voice. Omit for the configured one."
    )
    async def say(
        interaction: discord.Interaction, text: str, voice: str | None = None
    ) -> None:
        if client.voice is None:
            await interaction.response.send_message(
                "No speech server configured. Set RELDO_XTTS_URL."
            )
            return
        await interaction.response.defer(thinking=True)
        # Whoever holds this channel is who you hear, unless overridden.
        who = client.persona_for(interaction.channel_id)
        chosen = voice or who.voice or None
        try:
            audio = await client.voice.speak(text, voice=chosen)
        except VoiceError as exc:
            await interaction.followup.send(str(exc))
            return
        # Pace is applied to the finished audio rather than asked of the
        # server: its speed parameter is model conditioning and not smooth --
        # 1.35 yields more audio than 1.33 on one reference voice, and sounds it.
        audio = retime(audio, who.tempo)

        # Uploaded as a file rather than played into a voice channel: that path
        # needs PyNaCl and ffmpeg on the host, and this one works everywhere and
        # can be scrolled back to. Voice-channel playback is the upgrade, not the
        # only option.
        # The transcript of what the file says, so the two cannot disagree.
        # Discord caps a message at 2,000 and the spoken budget is 1,500, so
        # this no longer needs a second cut of its own -- the 400 it used to
        # take clipped the quote well before the audio it was quoting ended.
        await interaction.followup.send(
            content=f"> {spoken_form(text)}",
            file=discord.File(io.BytesIO(audio), filename="reldo.wav"),
        )

    @say.autocomplete("voice")
    async def say_autocomplete(interaction: discord.Interaction, current: str):
        if client.voice is None:
            return []
        return [
            discord.app_commands.Choice(name=v, value=v)
            for v in await client.voice.voices()
            if current.lower() in v.lower()
        ][:MAX_CHOICES]

    # -- /progress ---------------------------------------------------------

    @tree.command(name="progress", description="What you have actually trained lately.")
    @discord.app_commands.describe(days="How far back to look. Default 7.")
    async def progress_(
        interaction: discord.Interaction,
        days: discord.app_commands.Range[int, 1, 365] = 7,
    ) -> None:
        if client.progress is None:
            await interaction.response.send_message("Progress tracking is not configured.")
            return
        name = client.rsn_for(interaction.user.id)
        if not name:
            await interaction.response.send_message("Run `/link` once and I will track you.")
            return
        line = summarise(client.progress.gains(name, since=days * DAY))
        if not line:
            await interaction.response.send_message(
                f"Not enough history for {name} yet — ask me something tomorrow and "
                "I will have two points to compare."
            )
            return
        await interaction.response.send_message(
            # Prose, not a table. "XP in the last 3 days: Fishing +1,200,000, …"
            # has nothing to align, and a block only makes it wrap mid-number.
            embed=_embed(f"{name} — recent progress", line)
        )

    # -- /wom ----------------------------------------------------------------

    @tree.command(
        name="wom", description="Long-run gains and efficiency, from Wise Old Man."
    )
    @discord.app_commands.describe(
        period="How far back", username="OSRS name. Omit to use your linked one."
    )
    @discord.app_commands.choices(
        period=[discord.app_commands.Choice(name=p, value=p) for p in PERIODS]
    )
    async def wom(
        interaction: discord.Interaction,
        period: discord.app_commands.Choice[str] | None = None,
        username: str | None = None,
    ) -> None:
        if client.wom is None:
            await interaction.response.send_message("Wise Old Man is not configured.")
            return
        await interaction.response.defer(thinking=True)
        name = username or client.rsn_for(interaction.user.id)
        if not name:
            await interaction.followup.send("Run `/link` once, or give me a username.")
            return

        window = period.value if period else "week"
        try:
            found = await client.wom.lookup(name)
            gains = await client.wom.gains(name, period=window)
        except WomError as exc:
            # An untracked account is the common case, and it is fixable in one
            # command -- so say which one rather than only what went wrong.
            await interaction.followup.send(
                f"{exc}\n\nTry `/womtrack` to register {name} first."
                if "Not tracked" in str(exc)
                else str(exc)
            )
            return

        await interaction.followup.send(
            embed=_embed(
                f"{found.name} — last {window}",
                # Both halves are labelled prose -- efficient hours, then a
                # comma-joined XP line. Nothing in either is aligned, so a block
                # only costs the width it wraps at.
                f"{found.summary()}\n\n{gains.summary()}",
                footer="wiseoldman.net",
            )
        )

    @tree.command(name="womtrack", description="Register an account with Wise Old Man.")
    @discord.app_commands.describe(username="OSRS name. Omit to use your linked one.")
    async def womtrack(interaction: discord.Interaction, username: str | None = None) -> None:
        if client.wom is None:
            await interaction.response.send_message("Wise Old Man is not configured.")
            return
        await interaction.response.defer(thinking=True)
        name = username or client.rsn_for(interaction.user.id)
        if not name:
            await interaction.followup.send("Run `/link` once, or give me a username.")
            return
        try:
            found = await client.wom.track(name)
        except WomError as exc:
            await interaction.followup.send(str(exc))
            return
        await interaction.followup.send(
            embed=_embed(
                f"Tracking {found.name}",
                f"{found.summary()}\n\nGains need two snapshots, so check "
                "back tomorrow for `/wom`.",
                footer="wiseoldman.net",
            )
        )

    # -- /help -------------------------------------------------------------

    @tree.command(name="help", description="What Reldo can do.")
    async def help_(interaction: discord.Interaction) -> None:
        # Worth a command despite Discord listing the others natively: the
        # conversational paths are the ones people will not find, because
        # nothing in the slash UI hints that you can just talk to it.
        voice = client.persona_for(interaction.channel_id)
        embed = _embed(
            "Reldo",
            f"{voice.greeting}\n\n**Just mention me:**\n"
            "> @Reldo is the abyssal whip worth it at 70 attack\n\n"
            "**Reply to an answer to follow up** (\"what about at 80?\"), or DM me. "
            "Say `reset` to start a fresh thread.",
            footer=WIKI_ATTRIBUTION,
        )
        embed.add_field(
            name="Reads the wiki (uses the model)",
            value="`/wiki` — ask a question and get a cited answer",
            inline=False,
        )
        embed.add_field(
            name="Exact answers (no model, ~1s)",
            value=(
                "`/ge` — live prices, ranked by gp/day after tax\n"
                "`/trend` — what a price has been doing, from retained history\n"
                "`/goal` — how many of an item to sell for a coin target\n"
                "`/unlocks` — everything a skill unlocks at or below a level\n"
                "`/xp` — XP between two levels, and hours at a given rate\n"
                "`/train` — XP to a target level, and the materials each bracket needs\n"
                "`/stats` — a player's levels from the hiscores\n"
                "`/map` — where a place is\n"
                "`/search` — which pages retrieval returns, and why"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed)
