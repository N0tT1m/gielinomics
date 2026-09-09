"""Discord front end.

``/wiki`` for a one-shot question, and plain conversation for everything else:
mention the bot, reply to one of its answers, or DM it, and follow-ups work.
``@Reldo is the whip worth it at 70 attack`` then ``what about at 80?`` resolves
the second message against the first -- see :mod:`reldo.conversation` for why that
happens as a rewrite rather than as chat history.

**Conversation does not cost the privileged intent.** Discord withholds
``message.content`` from bots without ``message_content``, with three exceptions:
DMs, the bot's own messages, and *messages that mention the bot*. Every trigger
here is one of those, so ``Intents.default()`` still stands and the bot stays out
of the review queue it would otherwise join at 100 servers. Answering unaddressed
channel chatter is the thing that would need the intent, and is deliberately not
done.

Two Discord constraints shape everything here: a message caps at 2000 characters,
and an interaction must be acknowledged within 3 seconds or it is dead. A wiki
question takes several seconds of searching and reading, so every invocation defers
first and edits the response afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re

import discord

from .accounts import AccountStore
from .agent import WikiAgent

# Aliased: ReldoClient already has a `ge_client` property, and two different
# things called ge_client in one module is one too many.
from .clients import ge_client as build_ge
from .clients import hiscores_client as build_hiscores
from .clients import wom_client as build_wom
from .commands import register
from .conversation import ConversationStore, resolve
from .direct import answerer_for
from .index import load_index
from .live import LiveStore, serve
from .llm import ChatClient, client_for
from .maps import for_pages
from .persona import for_channel
from .progress import ProgressStore, poll
from .retrieval import HybridRetriever
from .voice import VoiceClient
from .wiki import WikiClient
from .wom import WomClient

log = logging.getLogger(__name__)

MESSAGE_LIMIT = 2000
# Leaves room for the sources block and the truncation marker.
ANSWER_BUDGET = 1500

# A user mention, in either of the two forms Discord has shipped.
_MENTION = re.compile(r"<@!?(\d+)>")

# Typed at the bot to drop the thread when a subject has carried over wrongly.
# Exact matches only: "reset" is a command, "reset my prayer" is a question.
_RESET_WORDS = frozenset({"reset", "start over", "new question", "forget it", "nevermind"})


class ReldoClient(discord.Client):
    """Slash-command client. ``/wiki <question>`` is the entire surface.

    Args:
        guild_id: Sync to this one guild instead of globally. **Set this.** A
            global sync can take up to an hour to appear in clients, and a
            missing ``/wiki`` looks exactly like a broken bot -- which is the
            single most common way to waste an afternoon here. A guild sync
            propagates immediately.
    """

    def __init__(
        self,
        guild_id: int | None = None,
        *,
        agent: WikiAgent | None = None,
        chat: ChatClient | None = None,
        store: ConversationStore | None = None,
        wiki: WikiClient | None = None,
        accounts: AccountStore | None = None,
        persona: str = "plain",
        persona_channels: dict[int, str] | None = None,
        progress: ProgressStore | None = None,
        voice: VoiceClient | None = None,
        live: LiveStore | None = None,
        wom: WomClient | None = None,
    ) -> None:
        # Default intents are enough. Slash commands need nothing privileged, and
        # the conversational paths below are exactly the three cases Discord
        # exempts from message_content: a mention, a reply (which mentions), and
        # a DM. Asking for the intent would gate the bot behind review to gain
        # only the ability to answer messages nobody addressed to it.
        super().__init__(intents=discord.Intents.default())
        self.tree = discord.app_commands.CommandTree(self)
        self._guild_id = guild_id
        self._agent = agent
        self._chat = chat
        self._wiki = wiki
        self._store = store if store is not None else ConversationStore()
        self._accounts = accounts
        self._persona = persona
        self._persona_channels = persona_channels or {}
        self._progress = progress
        self._voice = voice
        self._live = live
        self._wom = wom
        # One in-flight answer per conversation. A follow-up is meaningless until
        # the turn it refers to has been remembered, and a wiki question takes
        # long enough that an impatient user really does get there first.
        self._locks: dict[object, asyncio.Lock] = {}
        # How many messages are holding or queued on each lock above, so one can
        # be discarded when it is genuinely idle. See _claim_lock.
        self._lock_users: dict[object, int] = {}

    @property
    def agent(self) -> WikiAgent | None:
        return self._agent

    @property
    def accounts(self) -> AccountStore | None:
        return self._accounts

    @property
    def progress(self) -> ProgressStore | None:
        return self._progress

    @property
    def voice(self) -> VoiceClient | None:
        return self._voice

    @property
    def live(self) -> LiveStore | None:
        return self._live

    @property
    def wom(self) -> WomClient | None:
        return self._wom

    def rsn_for(self, user_id: int) -> str | None:
        """The RuneScape name linked to a Discord user, if any."""
        return self._accounts.get(user_id) if self._accounts else None

    def persona_for(self, channel_id: int | None):
        return for_channel(self._persona, self._persona_channels, channel_id)

    @property
    def wiki_client(self) -> WikiClient | None:
        """Explicit override first, else the one the agent already holds.

        getattr rather than attribute access: this is reached from the answer
        path to fetch maps, and maps.for_pages already refuses to fail an answer
        over a map. An agent that does not expose a wiki client should cost the
        garnish, not the reply.
        """
        return self._wiki or getattr(self._agent, "wiki", None)

    @property
    def ge_client(self):
        """The agent's GE client, so /ge shares its warm /mapping cache rather
        than refetching every tradeable item in the game per invocation."""
        return self._agent.ge if self._agent else None

    def conversation_key(self, message: discord.Message) -> object:
        """Which conversation a message belongs to.

        A reply resumes whatever produced the message being replied to, even
        across users -- somebody replying to an answer is continuing *that*
        exchange, which is the only reading of a reply that matches what the
        person doing it meant. Everything else is per channel and per author, so
        two people asking at once do not finish each other's sentences.
        """
        reference = message.reference
        if reference is not None and reference.message_id is not None:
            resumed = self._store.key_for_message(reference.message_id)
            if resumed is not None:
                return resumed
        return (message.channel.id, message.author.id)

    def addressed(self, message: discord.Message) -> bool:
        """Whether this message is for us, by a rule Discord will honour.

        Every branch is a case where content arrives without the privileged
        intent. Order matters only for readability; a reply to the bot also
        mentions it unless the author suppressed the ping, which is why the
        reference is checked separately rather than trusted to ``mentions``.
        """
        if message.author.bot or self.user is None:
            return False
        if message.guild is None:  # DM
            return True
        if any(user.id == self.user.id for user in message.mentions):
            return True
        reference = message.reference
        replied_to = getattr(reference, "resolved", None) if reference else None
        author = getattr(replied_to, "author", None)
        return getattr(author, "id", None) == self.user.id

    async def setup_hook(self) -> None:
        if self._guild_id:
            guild = discord.Object(id=self._guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info(
                "Synced %d command(s) to guild %s: %s",
                len(synced),
                self._guild_id,
                ", ".join(f"/{c.name}" for c in synced),
            )
        else:
            synced = await self.tree.sync()
            log.warning(
                "Synced %d command(s) GLOBALLY: %s. This can take up to an hour to "
                "appear. Set RELDO_DISCORD_GUILD_ID for instant registration while "
                "testing.",
                len(synced),
                ", ".join(f"/{c.name}" for c in synced),
            )

    async def on_ready(self) -> None:
        log.info(
            "Connected as %s in %d guild(s). Ask with /wiki",
            self.user,
            len(self.guilds),
        )
        for guild in self.guilds:
            log.info("  guild: %s (id=%s)", guild.name, guild.id)

    async def on_message(self, message: discord.Message) -> None:
        if self._agent is None or not self.addressed(message):
            return

        question = _strip_mention(message.content, self.user.id if self.user else 0)
        key = self.conversation_key(message)

        if question.lower().strip(" .!?") in _RESET_WORDS:
            self._store.forget(key)
            await message.reply("Forgotten. Ask me something new.", mention_author=False)
            return
        if not question:
            await message.reply(
                "Ask me something about Old School RuneScape -- mention me with a "
                "question, or reply to one of my answers to follow up.",
                mention_author=False,
            )
            return

        async with self.conversation(key):
            try:
                async with message.channel.typing():
                    asked, answer, places = await self.answer(
                        key,
                        question,
                        user_id=message.author.id,
                        channel_id=message.channel.id,
                    )
            except Exception:
                log.exception("Failed to answer %r", question)
                await message.reply(
                    "Something broke while I was reading the wiki. Try again in a "
                    "moment.",
                    mention_author=False,
                )
                return

            sent = await message.reply(
                embed=render_answer(asked, answer, places), mention_author=False
            )
            # Inside the lock, not after it. The next waiter is released the
            # instant this block ends, and a follow-up is meaningless until
            # the turn it refers to has been remembered -- so the store has
            # to be current before anybody else can read it. This used to sit
            # below, correct only because no await separated the two.
            self.remember(key, asked, answer.text, message_id=sent.id)

    @contextlib.asynccontextmanager
    async def conversation(self, key: object):
        """Hold this conversation for the duration of one exchange.

        Public, and the only supported way to answer, because the alternative
        was discovered rather than designed: ``/wiki`` reached past this into
        ``_answer`` and ``_store`` directly, so it shared the conversation key
        and the history with the mention path and shared none of the
        serialisation. Two ``/wiki`` in flight for one user, or a ``/wiki``
        racing a mention in the same channel, interleaved in exactly the way
        :meth:`_claim_lock` exists to prevent -- and the failure is invisible,
        because it surfaces one message later as a follow-up resolved against a
        turn that had not been remembered yet.

        A command that answers without entering this is the bug; making it a
        context manager is what makes the omission visible at the call site.
        """
        lock = self._claim_lock(key)
        try:
            async with lock:
                yield
        finally:
            self._drop_lock(key)

    def remember(self, key: object, question: str, answer: str, *, message_id=None) -> None:
        """Record a finished turn. Call inside :meth:`conversation`."""
        self._store.remember(key, question, answer, message_id=message_id)

    async def answer(
        self, key: object, question: str, *, user_id: int | None = None,
        channel_id: int | None = None,
    ):
        """Resolve a follow-up against its history, then answer it.

        Returns the question actually asked alongside the answer, because that
        is what belongs both in the embed title and in the stored turn -- storing
        "what about at 80?" would leave the next rewrite resolving a pronoun
        against a pronoun.
        """
        rsn = self.rsn_for(user_id) if user_id is not None else None
        history = self._store.history(key)
        asked = question
        if history and self._chat is not None:
            asked = await resolve(self._chat, history, question)
        answer = await self._agent.ask(
            asked,
            player=rsn,
            persona=self.persona_for(channel_id).prompt,
            live=self._live.summary(rsn) if (self._live and rsn) else "",
            # Quests, diaries and gear, straight from their client. Unlike the
            # live summary this is not folded into the prompt -- it is far too
            # big -- so the tools reach it through the Answer instead.
            profile=self._live.profile(rsn) if (self._live and rsn) else None,
        )
        places = []
        # wiki_client, not _wiki: the property falls back to the client the
        # agent already holds, and reading the raw field meant an instance built
        # without an explicit one silently dropped every map.
        wiki = self.wiki_client
        if wiki is not None:
            places = await for_pages(wiki, answer.pages_read)
        return asked, answer, places

    def _claim_lock(self, key: object) -> asyncio.Lock:
        """This conversation's lock, counting the caller as one of its users.

        Counted, rather than inferred from ``lock.locked()``. That test reads
        False in the window between a holder releasing and the next waiter being
        scheduled, so discarding on it dropped locks somebody was still queued
        on -- and the next message for that conversation then built a *second*
        lock and ran alongside the waiter, which is the one thing the lock is
        for. Reproduced with three messages: A holds, B queues, A finishes and
        prunes, C arrives to an empty dict and overlaps B.
        """
        self._lock_users[key] = self._lock_users.get(key, 0) + 1
        return self._locks.setdefault(key, asyncio.Lock())

    def _drop_lock(self, key: object) -> None:
        """Give up this caller's claim, discarding the lock once none are left.

        Keeps the bound the old pruning was after -- a busy server cannot grow
        this without limit -- without the window that made it wrong.
        """
        remaining = self._lock_users.get(key, 1) - 1
        if remaining > 0:
            self._lock_users[key] = remaining
            return
        self._lock_users.pop(key, None)
        self._locks.pop(key, None)


def _strip_mention(content: str, user_id: int) -> str:
    """The message with our own mention removed, other mentions left alone."""
    stripped = _MENTION.sub(
        lambda m: "" if m.group(1) == str(user_id) else m.group(0), content
    )
    return " ".join(stripped.split())


def build_client(
    agent: WikiAgent,
    guild_id: int | None = None,
    *,
    chat: ChatClient | None = None,
    store: ConversationStore | None = None,
    wiki: WikiClient | None = None,
    accounts: AccountStore | None = None,
    persona: str = "plain",
    persona_channels: dict[int, str] | None = None,
    progress: ProgressStore | None = None,
    voice: VoiceClient | None = None,
    live: LiveStore | None = None,
    wom: WomClient | None = None,
) -> ReldoClient:
    client = ReldoClient(
        guild_id,
        agent=agent,
        chat=chat,
        store=store,
        wiki=wiki,
        accounts=accounts,
        persona=persona,
        persona_channels=persona_channels,
        progress=progress,
        voice=voice,
        live=live,
        wom=wom,
    )

    register(client)
    return client


def render_answer(question: str, answer, places=()) -> discord.Embed:
    # An empty answer with pages on the record is a different failure from
    # finding nothing, and the two must not share a sentence. Asked
    # "Karambwans", this said "I couldn't find anything on the wiki for that"
    # directly above a Sources list naming Karambwan, the karambwan money-making
    # guide and a live GE price -- a contradiction the reader can see, which is
    # the one kind of wrong answer this project is least willing to ship.
    text = answer.text
    if not text:
        text = (
            "I read the pages below and could not put an answer together. Ask "
            "me something specific about it and I will do better."
            if answer.pages_read
            else "I couldn't find anything on the wiki for that."
        )
    if len(text) > ANSWER_BUDGET:
        text = text[:ANSWER_BUDGET].rsplit("\n", 1)[0] + "\n…"

    embed = discord.Embed(
        title=question[:250],
        description=text,
        colour=discord.Colour.from_rgb(94, 77, 48),  # RS parchment
    )
    if answer.citations:
        links = "\n".join(
            f"[{title}]({url})"
            for title, url in zip(answer.pages_read, answer.citations, strict=True)
        )
        embed.add_field(name="Sources", value=links[:1024], inline=False)
    if answer.prices_checked:
        # A price answer cites no wiki page, so without this the embed looks
        # sourceless exactly where "is this number current?" is the first thing
        # a reader wants to know.
        embed.add_field(
            name="Live GE prices",
            value=", ".join(dict.fromkeys(answer.prices_checked))[:1024],
            inline=False,
        )
    if places:
        # Only pages that declare a map contribute one, so this field is absent
        # for the whip and present for Ungael -- which is the right behaviour
        # rather than a gap. An item has no location to show.
        embed.add_field(
            name="Map" if len(places) == 1 else "Maps",
            value="\n".join(f"🗺 [{p.name}]({p.url})" for p in places)[:1024],
            inline=False,
        )
    embed.set_footer(text="Data from the OSRS Wiki · CC BY-NC-SA 3.0")
    return embed


async def run(settings) -> None:
    if not settings.discord_token:
        raise SystemExit("RELDO_DISCORD_TOKEN is not set.")

    user_agent = settings.require_user_agent()
    index = load_index(settings.index_path, settings.ollama_api_url)

    async with (
        WikiClient(
            user_agent, requests_per_second=settings.requests_per_second
        ) as wiki_client,
        client_for(settings) as chat,
    ):
        progress = ProgressStore(settings.progress_path)
        accounts = AccountStore(settings.accounts_path)
        agent = WikiAgent(
            HybridRetriever(wiki_client, index),
            chat,
            max_tokens=settings.max_tokens,
            user_agent=user_agent,
            progress=progress,
            ge=build_ge(settings),
            hiscores=build_hiscores(settings),
        )
        # The fast path, built after the agent so it can share the clients the
        # agent already holds -- a warm GE /mapping cache rather than a second
        # copy of every tradeable item in the game.
        agent.use_direct(answerer_for(settings, agent))
        # Empty URL disables every speech path rather than failing at call time.
        voice = (
            VoiceClient(settings.xtts_url, settings.xtts_voice)
            if settings.xtts_url
            else None
        )
        live = LiveStore() if settings.live_enabled else None
        wom = build_wom(settings)
        runner = None
        if live is not None:
            try:
                runner = await serve(
                    live,
                    host=settings.live_host,
                    port=settings.live_port,
                    token=settings.live_token,
                )
            except ValueError as exc:
                # serve() holds the invariant; this only turns it into something
                # you can act on, the way require_user_agent does for the wiki.
                raise SystemExit(str(exc)) from exc
        # The same chat client drives the follow-up rewrite, so it is traced and
        # configured identically to the answering loop rather than being a second
        # connection with its own settings to drift.
        # Sampling on a timer as well as on every question. Through agent.hiscores
        # so it shares the client the question path already holds rather than
        # opening a second one against Jagex.
        sampler = None
        if settings.progress_poll_seconds > 0:
            sampler = asyncio.create_task(
                poll(
                    accounts,
                    agent.hiscores,
                    progress,
                    interval=settings.progress_poll_seconds,
                )
            )

        client = build_client(
            agent,
            settings.discord_guild_id,
            chat=chat,
            wiki=wiki_client,
            accounts=accounts,
            persona=settings.persona,
            persona_channels=settings.persona_channels,
            progress=progress,
            voice=voice,
            live=live,
            wom=wom,
        )
        try:
            await client.start(settings.discord_token)
        finally:
            if sampler is not None:
                # Cancel before closing the agent: the sampler borrows its
                # hiscores client, and a round in flight against a closed one
                # would log a failure on the way out that means nothing.
                sampler.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sampler
            await agent.aclose()
            if voice is not None:
                await voice.aclose()
            if wom is not None:
                await wom.aclose()
            if runner is not None:
                await runner.cleanup()
