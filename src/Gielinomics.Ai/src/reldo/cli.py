"""Command line: doctor | build | search | ask | ge | unlocks | bot."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys

from . import config
from .clients import ge_client, hiscores_client, using_platform
from .index import build_and_save as build_index
from .index import build_chunks_and_save, load_index
from .retrieval import HybridRetriever
from .wiki import WikiClient

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reldo", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check the wiki and the local model server")

    p_build = sub.add_parser("build", help="build the semantic index")
    p_build.add_argument(
        "--limit", type=int, help="only index the first N articles (smoke test)"
    )
    p_build.add_argument(
        "--chunks",
        action="store_true",
        help=(
            "index article passages instead of lead paragraphs. Slower to build "
            "(~85k vectors) but finds facts buried in body prose."
        ),
    )
    p_build.add_argument(
        "--with-headings",
        action="store_true",
        help=(
            "also index section headings. Measured WORSE than the lead-only "
            "default (see README); kept for experimentation."
        ),
    )

    p_search = sub.add_parser("search", help="show what retrieval returns, no LLM")
    p_search.add_argument("query", nargs="+")
    p_search.add_argument("-k", type=int, default=8)

    p_trend = sub.add_parser(
        "trend",
        help="what an item's price has been doing, from the platform's history",
    )
    p_trend.add_argument("item", nargs="+", help="one item name")
    p_trend.add_argument(
        "--window", default="7d", help="how far back: 24h, 7d, 2w. Default 7d."
    )
    p_trend.add_argument(
        "--interval", default="1h", help="bar granularity: 5m, 1h, 1d. Default 1h."
    )

    p_serve = sub.add_parser(
        "serve", help="serve search and ask over HTTP, for the web frontend"
    )
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8100)
    p_serve.add_argument("--allow-origin", default="*", help="CORS origin for the browser")
    p_serve.add_argument(
        "--no-model",
        action="store_true",
        help="serve search only; do not connect to the model server",
    )

    p_ge = sub.add_parser(
        "ge", help="live GE prices and volume for one or more items, no LLM"
    )
    p_ge.add_argument(
        "items",
        nargs="+",
        help="item names, exact or partial: 'granite' expands to every variant",
    )

    p_unlocks = sub.add_parser(
        "unlocks",
        help="list everything a skill unlocks at or below a level, from the wiki's tables",
    )
    p_unlocks.add_argument("skill", help="e.g. Sailing")
    p_unlocks.add_argument("level", type=int, help="maximum level, inclusive")
    p_unlocks.add_argument(
        "--page",
        action="append",
        help="page to scan; repeatable. Defaults to searching the wiki for the skill.",
    )

    p_ask = sub.add_parser("ask", help="ask a question and get an answer")
    p_ask.add_argument("question", nargs="+")
    # Speaking is the default, not a flag. She is the assistant; having to ask
    # for her voice every time is the same mistake as having to ask for the
    # answer. --quiet is for when you are in a meeting.
    p_ask.add_argument(
        "--quiet",
        action="store_true",
        help="print the answer without saying it out loud",
    )
    p_ask.add_argument(
        "--persona",
        default="plain",
        help=(
            "who answers. Only 'plain' is defined in this repository; any other "
            "name resolves to it. Sets both how it writes and how it sounds."
        ),
    )
    p_ask.add_argument(
        "--voice",
        help="override the performer_id the persona would use",
    )
    p_ask.add_argument(
        "--tempo",
        type=float,
        help=(
            "override her speaking pace. Applied to the finished audio, so any "
            "value works -- unlike the server's speed, where 1.35 is broken and "
            "1.33 is not."
        ),
    )

    p_say = sub.add_parser("say", help="speak text out loud, no model involved")
    p_say.add_argument("text", nargs="+")
    p_say.add_argument("--voice", help="performer_id; omit for the configured one")
    p_say.add_argument(
        "--persona", default="", help="the persona's voice and pace"
    )
    p_say.add_argument("--tempo", type=float, help="speaking pace; 1.0 leaves it alone")
    p_say.add_argument(
        "--list", action="store_true", help="list the voices the server offers"
    )

    p_coach = sub.add_parser(
        "coach", help="watch what you are doing and speak up, unprompted"
    )
    p_coach.add_argument("player", help="your RSN, as the plugin reports it")
    p_coach.add_argument(
        "--from",
        dest="source",
        default="",
        help="live receiver base URL. Defaults to RELDO_LIVE_URL, else the endpoint host.",
    )
    p_coach.add_argument("--persona", default="plain", help="only 'plain' is defined")
    p_coach.add_argument("--quiet", action="store_true", help="print, do not speak")
    p_coach.add_argument(
        "--every", type=float, default=None, help="seconds between polls"
    )
    p_coach.add_argument(
        "--cooldown",
        type=float,
        default=None,
        help="minimum seconds between remarks. The point of the whole thing.",
    )

    p_web = sub.add_parser(
        "web", help="the same coach, in a window instead of a terminal"
    )
    p_web.add_argument("player", help="your RSN, as the plugin reports it")
    p_web.add_argument("--from", dest="source", default="", help="live receiver base URL")
    p_web.add_argument("--persona", default="plain", help="only 'plain' is defined")
    p_web.add_argument("--port", type=int, default=None, help="default 8100")
    p_web.add_argument(
        "--host",
        default="127.0.0.1",
        help="loopback by default. This is a window, not a service.",
    )
    p_web.add_argument(
        "--every", type=float, default=None, help="seconds between polls"
    )
    p_web.add_argument("--cooldown", type=float, default=None, help="seconds between remarks")

    sub.add_parser("bot", help="run the Discord bot")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=(
            logging.INFO
            if args.verbose or args.command in ("build", "bot")
            else logging.WARNING
        ),
        format="%(message)s",
    )
    try:
        return asyncio.run(_dispatch(args, config.load()))
    except FileNotFoundError as exc:
        # Almost always a missing index on a fresh checkout: data/ is gitignored
        # because it is a regenerable build artifact, so a clone has code but no
        # index. That is an instruction, not a crash.
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


async def _dispatch(args: argparse.Namespace, settings: config.Settings) -> int:
    if args.command == "doctor":
        return await _doctor(settings)

    if args.command == "bot":
        from .bot import run

        await run(settings)
        return 0

    if args.command == "trend":
        # Beside `ge` and above the index load on purpose: this is an API call,
        # so it works on a fresh clone before `reldo build` has ever run.
        settings.require_user_agent()
        if not using_platform(settings):
            print(
                "Reading prices straight from the wiki, which keeps no history. "
                "Set RELDO_GIELINOMICS_URL to point at the platform.",
                file=sys.stderr,
            )
            return 1

        async with ge_client(settings) as ge:
            try:
                found = await ge.trend(
                    " ".join(args.item), window=args.window, interval=args.interval
                )
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 1
            except Exception as exc:
                print(f"Could not read that trend: {exc}", file=sys.stderr)
                return 1

        if found is None:
            print(f"No tradeable item matching {' '.join(args.item)!r}.", file=sys.stderr)
            return 1
        print(found.summary())
        return 0

    if args.command == "ge":
        # No index and no model needed: this is a live API lookup, so it works
        # on a fresh clone before `reldo build` has ever run.
        from .ge import GEError, compare

        settings.require_user_agent()
        async with ge_client(settings) as ge:
            try:
                found = []
                for name in args.items:
                    matched = await ge.lookup(name)
                    if not matched:
                        print(f"No tradeable item matching {name!r}.", file=sys.stderr)
                    found.extend(matched)
            except GEError as exc:
                print(exc, file=sys.stderr)
                return 1
        if not found:
            return 1
        print(compare(list({p.item.id: p for p in found}.values())))
        return 0

    async with WikiClient(
        settings.require_user_agent(), requests_per_second=settings.requests_per_second
    ) as client:
        if args.command == "unlocks":
            from .unlocks import dedupe, render, scan_page

            pages = args.page
            if not pages:
                # No page given: let CirrusSearch nominate candidates. Scanning a
                # page with no "<skill> level" column costs a request and yields
                # nothing, which is the right failure -- better than guessing at
                # a page name and reporting an empty list as fact.
                hits = await client.search(f"{args.skill} level requirements", limit=5)
                pages = [h.title for h in hits]
                print(f"scanning: {', '.join(pages)}\n", file=sys.stderr)

            found = []
            for title in pages:
                found += await scan_page(client, title, args.skill, args.level)
            print(render(dedupe(found), args.skill, args.level))
            return 0

        # Before the index loads: speaking needs no retrieval, and paying ~95 MB
        # of index load to say one sentence would make `reldo say` feel broken.
        if args.command == "say":
            who = _persona_for(args.persona) if args.persona else None
            return await _say(
                settings,
                " ".join(args.text),
                args.voice or (who.voice if who else None),
                args.list,
                args.tempo if args.tempo is not None else (who.tempo if who else None),
            )

        if args.command == "build":
            if args.chunks:
                chunked = await build_chunks_and_save(
                    client,
                    settings.index_path,
                    limit=args.limit,
                    model_name=(
                        settings.ollama_embed_model
                        if settings.embed_backend == "ollama"
                        else settings.local_embed_model
                    ),
                    backend=settings.embed_backend,
                    ollama_url=settings.ollama_api_url,
                )
                print(
                    f"Indexed {len(chunked.titles)} chunks from {chunked.pages} "
                    f"articles -> {settings.index_path}"
                )
                return 0
            index = await build_index(
                client,
                settings.index_path,
                limit=args.limit,
                model_name=(
                    settings.ollama_embed_model
                    if settings.embed_backend == "ollama"
                    else settings.local_embed_model
                ),
                with_headings=args.with_headings,
                backend=settings.embed_backend,
                ollama_url=settings.ollama_api_url,
            )
            print(f"Indexed {len(index.titles)} articles -> {settings.index_path}")
            return 0

        # `serve` is the one command that must survive a missing index. Every
        # other one is a person at a terminal who can read "build one" and do
        # it; serve is a long-running container, and exiting 1 under
        # `restart: unless-stopped` is a crash loop rather than a message. It
        # comes up without an index instead, reports search as unavailable on
        # /health, and says so on the search route.
        if args.command == "serve":
            try:
                retriever = HybridRetriever(
                    client, load_index(settings.index_path, settings.ollama_api_url)
                )
            except FileNotFoundError as exc:
                log.warning("Serving without search: %s", exc)
                retriever = None
            return await _serve(args, settings, retriever)

        index = load_index(settings.index_path, settings.ollama_api_url)
        retriever = HybridRetriever(client, index)

        # After the index, because the advice comes from the agent: the triggers
        # decide when to speak, the agent decides what is true.
        if args.command == "coach":
            return await _coach(args, settings, retriever, client)

        if args.command == "web":
            return await _web(args, settings, retriever, client)

        if args.command == "search":
            for hit in await retriever.shortlist(" ".join(args.query), k=args.k):
                print(f"{hit.score:.4f}  [{'+'.join(hit.found_by):<16}] {hit.title}")
                print(f"          {hit.summary[:110]}")
            return 0

        if args.command == "ask":
            from .agent import WikiAgent
            from .direct import answerer_for
            from .llm import client_for

            async with (
                client_for(settings) as chat,
                WikiAgent(
                    retriever,
                    chat,
                    max_tokens=settings.max_tokens,
                    user_agent=settings.require_user_agent(),
                    ge=ge_client(settings),
                    hiscores=hiscores_client(settings),
                ) as agent,
            ):
                agent.use_direct(answerer_for(settings, agent))
                # The persona is passed into ask() rather than applied to the
                # finished text, which is the whole safety property: every
                # enforcement pass runs on what the model actually produced, so
                # being in character cannot smuggle a number past the checks.
                who = _persona_for(args.persona)
                answer = await agent.ask(
                    " ".join(args.question), persona=who.prompt
                )

            print(answer.text)

            # Show every grounding source, not just wiki search. A stats question
            # answered from the hiscores IS grounded, and reporting it as
            # ungrounded trains you to ignore the warning that matters.
            if answer.searches:
                print(f"\n[searched: {'; '.join(answer.searches)}]")
            if answer.players_checked:
                print(f"[hiscores: {', '.join(answer.players_checked)}]")
            if answer.prices_checked:
                print(f"[GE prices: {', '.join(answer.prices_checked)}]")
            if not (
                answer.searches
                or answer.pages_read
                or answer.players_checked
                or answer.prices_checked
            ):
                print(
                    "\n[WARNING: answered from memory -- no wiki page, no hiscores, "
                    "no GE prices]"
                )

            if answer.citations:
                print("Sources:")
                for url in answer.citations:
                    print(f"  {url}")

            from .maps import for_pages

            for place in await for_pages(client, answer.pages_read):
                print(f"Map: {place.name} -- {place.url}")

            # Last, and after the citations: the text and its sources are the
            # answer, speech is a convenience on top. Failing to speak must not
            # cost you an answer you already have on screen, so this reports and
            # returns 0 rather than raising.
            if not args.quiet:
                await _speak(
                    settings,
                    answer.text,
                    args.voice or who.voice,
                    args.tempo if args.tempo is not None else who.tempo,
                )
            return 0

    return 1


def _persona_for(name: str):
    """The persona to answer as.

    No characters ship with this repository, so every name resolves to
    :data:`~reldo.persona.PLAIN`. Resolving rather than rejecting is deliberate:
    an existing script or ``.env`` naming a persona that is not defined should
    cost the tone, not the command. See :mod:`reldo.persona` for why the hook is
    kept at all.
    """
    from .persona import PERSONAS, PLAIN

    return PERSONAS.get(name, PLAIN)


def _voice_client(settings: config.Settings, voice: str | None):
    """A VoiceClient from settings, or None with a reason printed.

    The URL is required rather than defaulted: voice.DEFAULT_URL happens to be
    right on this network, and silently falling back to it would make a typo'd
    RELDO_XTTS_URL look like a working configuration pointing somewhere else.
    """
    from .voice import DEFAULT_VOICE, VoiceClient

    if not settings.xtts_url:
        print(
            "No speech server configured. Set RELDO_XTTS_URL in .env "
            "(e.g. RELDO_XTTS_URL=http://192.168.1.78:8020).",
            file=sys.stderr,
        )
        return None
    return VoiceClient(settings.xtts_url, voice or settings.xtts_voice or DEFAULT_VOICE)


async def _speak(
    settings: config.Settings,
    text: str,
    voice: str | None,
    tempo: float | None = None,
) -> None:
    """Say an answer out loud, reporting rather than raising on failure."""
    from .voice import VoiceError, play, spoken_form

    client = _voice_client(settings, voice)
    if client is None:
        return
    async with client:
        try:
            audio = await client.speak(text, voice=voice)
        except VoiceError as exc:
            print(f"[speech failed: {exc}]", file=sys.stderr)
            return
    print(f'\n[speaking: "{spoken_form(text)}"]', file=sys.stderr)
    try:
        play(audio, tempo=tempo)
    except VoiceError as exc:
        print(f"[playback failed: {exc}]", file=sys.stderr)


async def _say(
    settings: config.Settings,
    text: str,
    voice: str | None,
    listing: bool,
    tempo: float | None = None,
) -> int:
    """``reldo say`` -- synthesis with no model and no retrieval in the way."""
    from .voice import VoiceError, play

    client = _voice_client(settings, voice)
    if client is None:
        return 1
    async with client:
        if listing:
            available = await client.voices()
            if not available:
                print(f"No voices from {settings.xtts_url}.", file=sys.stderr)
                return 1
            for name in available:
                print(f"{'* ' if name == (voice or settings.xtts_voice) else '  '}{name}")
            return 0
        try:
            audio = await client.speak(text, voice=voice)
        except VoiceError as exc:
            print(f"Speech failed: {exc}", file=sys.stderr)
            return 1
    try:
        played = play(audio, tempo=tempo)
    except VoiceError as exc:
        print(f"Playback failed: {exc}", file=sys.stderr)
        return 1
    print(f"{len(audio):,} bytes through {played}")
    return 0


async def _doctor(settings: config.Settings) -> int:
    """Check every external dependency before you need it at 2am.

    Includes the tool-calling probe specifically, because a model that cannot emit
    tool calls produces an agent that answers fluently from memory and never
    touches the wiki -- with no error anywhere to tell you.
    """
    from .llm import supports_tool_calling

    ok = True

    print(f"{'wiki':<15}{'':<30}", end="")
    try:
        async with WikiClient(
            settings.user_agent or "reldo/doctor (local)",
            requests_per_second=settings.requests_per_second,
        ) as client:
            hits = await client.search("abyssal whip", limit=1)
        print(f"OK    ({hits[0].title!r})" if hits else "OK    (no results?)")
    except Exception as exc:
        ok = False
        print(f"FAIL  {type(exc).__name__}: {exc}")

    print(f"{'index':<15}{str(settings.index_path):<30}", end="")
    try:
        index = load_index(settings.index_path, settings.ollama_api_url)
        kind = "chunks" if hasattr(index, "sections") else "articles"
        print(
            f"OK    ({len(index.titles)} {kind}, "
            f"{index.vectors.shape[1]}d, {index.backend})"
        )
    except Exception as exc:
        ok = False
        print(f"FAIL  {exc}")

    print(f"{'chat model':<15}{settings.ai_backend + ':' + settings.chat_model:<30}", end="")
    probed = False
    try:
        if await supports_tool_calling(
            settings.chat_base_url, settings.chat_model, headers=settings.trace_headers
        ):
            probed = True
            print("OK    (emits tool_calls)")
        else:
            ok = False
            print("FAIL  reachable but NO tool_calls -- cannot drive the agent")
    except Exception as exc:
        ok = False
        # With tracing on, this failure has two causes that look identical from
        # here -- the model box is down, or the proxy in front of it is. Say
        # which address actually refused so it isn't a guess.
        print(f"FAIL  {type(exc).__name__}: {exc}")
        if settings.trace_proxy_url:
            await _explain_trace_failure(settings)

    if settings.trace_proxy_url:
        print(f"{'traces':<15}{settings.trace_proxy_url:<30}", end="")
        if not probed:
            print("SKIP  (chat probe failed; nothing to record)")
        else:
            ok = await _check_traces(settings) and ok

    if settings.embed_backend == "ollama":
        print(f"{'embeddings':<15}{settings.ollama_embed_model:<30}", end="")
        try:
            from .index import embed_texts

            vectors = await asyncio.to_thread(
                embed_texts,
                ["ping"],
                settings.ollama_embed_model,
                "ollama",
                settings.ollama_api_url,
            )
            print(f"OK    ({vectors.shape[1]}d)")
        except Exception as exc:
            ok = False
            print(f"FAIL  {type(exc).__name__}: {exc}")

    print("\n" + ("all good" if ok else "something is broken -- see above"))
    return 0 if ok else 1


async def _explain_trace_failure(settings: config.Settings) -> None:
    """Say which component is broken, having actually checked.

    denden listens on two ports and they are not interchangeable: the proxy
    forwards chat, the hub serves the UI and the events API. Pointing
    RELDO_TRACE_PROXY_URL at the hub is the easy mistake, and the resulting
    error blames the model server. They are trivially distinguishable -- the
    hub answers /health with JSON, the proxy 404s it and instead forwards
    /api/tags to Ollama -- so check rather than list possibilities.
    """
    import httpx

    pad = f"{'':<15}{'':<30}      "
    proxy = settings.trace_proxy_url.rstrip("/")

    async def responds(url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                return (await http.get(url)).status_code == 200
        except Exception:
            return False

    print(f"{pad}Chat goes via the trace proxy at {proxy}, not straight to")
    print(f"{pad}{settings.ollama_api_url}.")

    if await responds(f"{proxy}/health"):
        # It answered /health, so something is alive there -- just the wrong half.
        print(f"{pad}")
        print(f"{pad}That address is denden's HUB, not its proxy. They are")
        print(f"{pad}different ports: the hub serves the UI and events API, the")
        print(f"{pad}proxy is what forwards chat. Check proxy_listen_addr in")
        print(f"{pad}denden's config.toml and point RELDO_TRACE_PROXY_URL there,")
        print(f"{pad}leaving RELDO_TRACE_HUB_URL on {proxy}.")
        return

    if await responds(f"{settings.trace_hub_url.rstrip('/')}/health"):
        print(f"{pad}")
        print(f"{pad}denden IS running (its hub answered on {settings.trace_hub_url})")
        print(f"{pad}but nothing is serving {proxy}. Its proxy_listen_addr is not")
        print(f"{pad}where RELDO_TRACE_PROXY_URL says -- check denden's config.toml.")
        return

    # Deliberately not "denden is not running": only the two addresses
    # configured here were checked, and a denden on some other port would land
    # in this branch too. Saying it is not running sends you to start a second
    # copy of something already running on a port you have forgotten.
    print(f"{pad}Nothing answered at {proxy} or at the hub")
    print(f"{pad}({settings.trace_hub_url}). If embeddings passed above then the")
    print(f"{pad}model box is fine -- this is denden, not Ollama.")
    print(f"{pad}")
    print(f"{pad}Either it is not running, or it is on ports other than these two.")
    print(f"{pad}Find out which before starting another copy:")
    print(f"{pad}  Windows  Get-Process denden | Select Id,Path")
    print(f"{pad}           Get-NetTCPConnection -State Listen |")
    print(f"{pad}             ? OwningProcess -in (Get-Process denden).Id")
    print(f"{pad}  macOS    pgrep -fl denden; lsof -nP -iTCP -sTCP:LISTEN | grep denden")
    print(f"{pad}")
    print(f"{pad}Its startup log prints both: 'black-snail proxy listening on ...'")
    print(f"{pad}is the one RELDO_TRACE_PROXY_URL needs. If it is genuinely not")
    print(f"{pad}running, start it with `denden server` and check default_upstream")
    print(f"{pad}is {settings.ollama_api_url}.")
    print(f"{pad}Or comment out RELDO_TRACE_PROXY_URL to bypass tracing entirely.")


async def _check_traces(settings: config.Settings) -> bool:
    """Confirm the probe we just sent actually landed in den-den-mushi.

    A proxy that forwards but never records is the failure worth catching here:
    answers keep working, so nothing looks broken, and you discover the traces
    were never written on the day you sit down to debug a bad answer.
    """
    import httpx

    hub = settings.trace_hub_url.rstrip("/")
    agent = settings.trace_agent or "reldo"
    pad = f"{'':<15}{'':<30}      "
    events: list[dict] = []

    # record() runs *after* the response is flushed back to us, so the row can
    # land a beat after the probe returns. Poll instead of racing it.
    for attempt in range(6):
        if attempt:
            await asyncio.sleep(0.4)
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                response = await http.get(
                    f"{hub}/api/v1/events", params={"kind": "trace", "limit": 50}
                )
                response.raise_for_status()
                events = response.json() or []
        except Exception as exc:
            print(f"FAIL  hub unreachable: {type(exc).__name__}: {exc}")
            print(f"{pad}proxy forwarded fine, so `denden proxy` is up but the hub")
            print(f"{pad}at {hub} is not -- run `denden server` for both.")
            return False

        if any(e.get("agent") == agent for e in events):
            n = sum(1 for e in events if e.get("agent") == agent)
            print(f'OK    (hub has {n} trace(s) tagged "{agent}")')
            return True

    print(f'FAIL  hub is up but no trace tagged "{agent}"')
    untagged = sum(1 for e in events if e.get("agent") in (None, "", "untagged"))
    if untagged:
        print(f"{pad}{untagged} untagged trace(s) present -- something is eating")
        print(f"{pad}the X-DenDen-Agent header before it reaches the proxy.")
    else:
        print(f"{pad}no traces at all: is RELDO_TRACE_PROXY_URL pointing at the")
        print(f"{pad}proxy port (8443) rather than the hub UI port (8765)?")
    return False


if __name__ == "__main__":
    sys.exit(main())


async def _serve(args, settings: config.Settings, retriever) -> int:
    """Run the JSON service until interrupted.

    ``retriever`` may be None when no index has been built. The service comes up
    anyway and reports search as unavailable, because this is the one command
    that runs as a container rather than as a person at a terminal.

    The model is optional and the flag is not a convenience. Search needs the
    index and the wiki; asking needs a 24B model on a GPU that may be a
    different machine, or off. Making the second a precondition for the first
    would mean the frontend's search box goes dark whenever the GPU box does,
    for no reason -- nothing in retrieval ever touches the model.
    """
    from .service import serve

    agent = None
    stack = contextlib.AsyncExitStack()
    async with stack:
        if not args.no_model:
            from .agent import WikiAgent
            from .direct import answerer_for
            from .llm import client_for

            chat = await stack.enter_async_context(client_for(settings))
            agent = await stack.enter_async_context(
                WikiAgent(
                    retriever,
                    chat,
                    max_tokens=settings.max_tokens,
                    user_agent=settings.require_user_agent(),
                    ge=ge_client(settings),
                    hiscores=hiscores_client(settings),
                )
            )
            agent.use_direct(answerer_for(settings, agent))

        runner = await serve(
            retriever,
            agent=agent,
            host=args.host,
            port=args.port,
            allow_origin=args.allow_origin,
        )
        print(
            f"Serving on http://{args.host}:{args.port} "
            f"(search: {retriever is not None}, ask: {agent is not None})"
        )
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await runner.cleanup()
    return 0


async def _coach(args, settings: config.Settings, retriever, client) -> int:
    """Watch, speak up, and answer back.

    Three things share one agent here: the poll loop that notices what changed,
    whatever you type, and the conversation that threads the two together. They
    take turns rather than run at once -- a coach that starts a remark on top of
    the answer you are listening to is a coach you turn off.
    """
    import time

    import httpx

    from .agent import WikiAgent
    from .coach import COOLDOWN_SECONDS, POLL_SECONDS, Coach
    from .conversation import ConversationStore, resolve
    from .direct import answerer_for
    from .llm import client_for
    from .trace import Tracer, from_answer

    base = args.source or settings.live_url
    if not base:
        print(
            "Nowhere to read live state from. Set RELDO_LIVE_URL in .env "
            "(e.g. RELDO_LIVE_URL=http://192.168.1.74:8099) or pass --from.",
            file=sys.stderr,
        )
        return 1

    coach = Coach(
        base,
        args.player,
        token=settings.live_token,
        poll_seconds=args.every or POLL_SECONDS,
        cooldown=args.cooldown if args.cooldown is not None else COOLDOWN_SECONDS,
    )
    # One conversation, because there is one of you. The Discord layer keys by
    # (channel, user) for the same reason this does not need to.
    store = ConversationStore()
    KEY = "cli"
    print(
        f"Coaching {args.player} from {base}, as {args.persona}. "
        "Type to ask something; Ctrl-C to stop.",
        file=sys.stderr,
    )

    typed: asyncio.Queue[str] = asyncio.Queue()

    async def read_stdin() -> None:
        """Feed typed lines in without blocking the poll loop.

        A thread per readline rather than a reader transport: stdin here may be a
        terminal, a pipe or closed entirely, and to_thread behaves the same for
        all three. EOF simply ends this task and leaves the watching running,
        which is what `reldo coach ... < /dev/null &` should do.
        """
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                return
            if line.strip():
                await typed.put(line.strip())

    async with (
        httpx.AsyncClient(timeout=15.0) as http,
        client_for(settings) as chat,
        Tracer(settings.trace_path, ingest_url=settings.trace_ingest_url) as tracer,
        WikiAgent(
            retriever,
            chat,
            max_tokens=settings.max_tokens,
            user_agent=settings.require_user_agent(),
            ge=ge_client(settings),
            hiscores=hiscores_client(settings),
        ) as agent,
    ):
        agent.use_direct(answerer_for(settings, agent))
        speaking = asyncio.Lock()

        async def answer(question: str, live: dict, *, asked: bool) -> None:
            """Ask, print, speak, remember. The one path both triggers use."""
            who = _persona_for(args.persona)
            async with speaking:
                # Follow-ups are rewritten into standalone questions rather than
                # handed to the model as chat history -- see conversation.py for
                # why: every enforcement pass in ask() checks what happened
                # during one ask(), and prior turns in the context would let a
                # follow-up be answered from text nothing verified this time.
                standalone = (
                    await resolve(chat, store.history(KEY), question)
                    if asked
                    else question
                )
                result = await agent.ask(
                    standalone,
                    persona=who.prompt,
                    player=args.player,
                    live=live.get("summary") or "",
                )
            print(f"\n{result.text}\n")
            # Remembered whether you asked or she volunteered, so "why?" works
            # on something she said unprompted -- which is most of what you
            # would want to say back to a coach.
            store.remember(KEY, standalone, result.text)
            await tracer.write(
                from_answer(
                    result,
                    question,
                    asked=standalone,
                    player=args.player,
                    persona=who.name,
                    prompted=asked,
                    live=live.get("summary") or "",
                )
            )
            if not args.quiet:
                await _speak(settings, result.text, who.voice, who.tempo)

        async def watch() -> None:
            offline = False
            while True:
                live = await coach.fetch(http)
                if live is None or not live.get("fresh"):
                    if live is not None and not offline:
                        print(f"{args.player} is not logged in; waiting.", file=sys.stderr)
                        offline = True
                    await asyncio.sleep(coach._poll)
                    continue
                offline = False
                pick = coach.update(live, time.monotonic())
                if pick is not None:
                    question, facts = await _facts_for(client, pick)
                    await answer(_with_facts(question, facts), live, asked=False)
                await asyncio.sleep(coach._poll)

        async def listen() -> None:
            while True:
                question = await typed.get()
                # Fresh state for the question actually being asked: answering
                # "what should I do now" against a snapshot from four minutes ago
                # is how a live coach becomes a stale one.
                live = await coach.fetch(http) or {}
                await answer(question, live, asked=True)

        tasks = [
            asyncio.create_task(watch()),
            asyncio.create_task(listen()),
            asyncio.create_task(read_stdin()),
        ]
        try:
            # read_stdin returning on EOF must not end the session; the other two
            # run until interrupted.
            await asyncio.gather(tasks[0], tasks[1])
        finally:
            for task in tasks:
                task.cancel()
    return 0


async def _web(args, settings: config.Settings, retriever, client) -> int:
    """The coach, served as a page instead of a shell.

    Same agent, same personas, same triggers as `reldo coach`. The differences
    are where the words come out and where the audio plays: the browser, so the
    machine running this does not have to be the machine you are sitting at.
    """
    import time
    import webbrowser

    import httpx

    from .agent import WikiAgent
    from .coach import COOLDOWN_SECONDS, POLL_SECONDS, Coach
    from .conversation import ConversationStore, resolve
    from .direct import answerer_for
    from .llm import client_for
    from .trace import Tracer, from_answer
    from .voice import VoiceError
    from .web import DEFAULT_PORT, build_app, keep, levels_for, serve

    # Falls back to loopback rather than refusing: `web` most often runs on the
    # same box as the receiver, and the machine that is not that box is the one
    # with RELDO_LIVE_URL set.
    base = args.source or settings.live_url or f"http://127.0.0.1:{settings.live_port}"

    coach = Coach(
        base,
        args.player,
        token=settings.live_token,
        poll_seconds=args.every or POLL_SECONDS,
        cooldown=args.cooldown if args.cooldown is not None else COOLDOWN_SECONDS,
    )
    store = ConversationStore()
    KEY = "web"
    clips: dict[str, bytes] = {}
    events: asyncio.Queue = asyncio.Queue()
    latest: dict = {}

    async def synth(text: str, who) -> str | None:
        """WAV for the page to play, or None. Never fatal: the text is the answer."""
        client = _voice_client(settings, who.voice)
        if client is None:
            return None
        try:
            async with client:
                audio = await client.speak(text, voice=who.voice)
        except VoiceError as exc:
            log.warning("Speech failed: %s", exc)
            return None
        from .voice import retime

        return keep(clips, retime(audio, who.tempo))

    async with (
        httpx.AsyncClient(timeout=15.0) as http,
        client_for(settings) as chat,
        Tracer(settings.trace_path, ingest_url=settings.trace_ingest_url) as tracer,
        WikiAgent(
            retriever,
            chat,
            max_tokens=settings.max_tokens,
            user_agent=settings.require_user_agent(),
            ge=ge_client(settings),
            hiscores=hiscores_client(settings),
        ) as agent,
    ):
        agent.use_direct(answerer_for(settings, agent))
        speaking = asyncio.Lock()

        async def respond(question: str, *, asked: bool) -> dict:
            who = _persona_for(args.persona)
            async with speaking:
                standalone = (
                    await resolve(chat, store.history(KEY), question) if asked else question
                )
                result = await agent.ask(
                    standalone,
                    persona=who.prompt,
                    player=args.player,
                    live=latest.get("summary") or "",
                )
            store.remember(KEY, standalone, result.text)
            await tracer.write(
                from_answer(
                    result,
                    question,
                    asked=standalone,
                    player=args.player,
                    persona=who.name,
                    prompted=asked,
                    live=latest.get("summary") or "",
                )
            )
            return {
                "text": result.text,
                "persona": who.name.title(),
                "audio": await synth(result.text, who),
            }

        async def live_snapshot() -> dict:
            skills = latest.get("skills") or {}
            return {
                "player": args.player,
                "fresh": bool(latest.get("fresh")),
                "skills": skills,
                "levels": levels_for(skills),
                "session": latest.get("session"),
            }

        async def watch() -> None:
            while True:
                live = await coach.fetch(http)
                if live is not None:
                    latest.clear()
                    latest.update(live)
                    await events.put({"kind": "state", **(await live_snapshot())})
                    if live.get("fresh"):
                        pick = coach.update(live, time.monotonic())
                        if pick is not None:
                            question, facts = await _facts_for(client, pick)
                            said = await respond(
                                _with_facts(question, facts), asked=False
                            )
                            await events.put({"kind": "remark", **said})
                await asyncio.sleep(coach._poll)

        # Off loopback the UI is token-gated; on loopback it is not, because
        # then the only thing that can reach it is already you.
        gate = settings.live_token if args.host not in ("127.0.0.1", "localhost") else ""
        app = build_app(
            answer=lambda question: respond(question, asked=True),
            live_snapshot=live_snapshot,
            events_queue=events,
            clips=clips,
            token=gate,
        )
        port = args.port or DEFAULT_PORT
        try:
            runner = await serve(app, host=args.host, port=port, token=gate)
        except ValueError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1
        shown = args.host if args.host != "0.0.0.0" else "<this machine's LAN address>"
        url = f"http://{shown}:{port}" + (f"?t={gate}" if gate else "")
        print(f"Reldo is at {url} — Ctrl-C to stop.", file=sys.stderr)
        if not gate:
            webbrowser.open(url)
        watching = asyncio.create_task(watch())
        try:
            await watching
        finally:
            watching.cancel()
            await runner.cleanup()
    return 0


async def _unlock_facts(client, skill: str, level: int) -> str:
    """What the wiki's tables say unlocks at exactly this level, or "".

    The coach asked the model "what does level 17 unlock" and got back a
    paragraph about level 91, off the general Thieving page, with an empty
    search list. The trace is unambiguous: it read an overview page, found a
    number on it, and answered a different question with it -- the
    ungrounded-number pass could not object, because 91 genuinely appears on the
    page it read. Provenance was fine; relevance was not.

    unlocks.py exists for exactly this. Its own module note says so: it filters
    requirement tables in code "because the model invented an Ironwood mast at
    Sailing 20". Handing her the rows instead of the question turns recall into
    relay.

    Returns "" on any failure, which puts the question back the way it was --
    a worse answer, not a missing one.
    """
    from .unlocks import Unlock, dedupe, render, scan_page

    try:
        found: list[Unlock] = dedupe(await scan_page(client, skill, skill, level))
    except Exception as exc:  # noqa: BLE001 - a coach remark must not die on this
        log.warning("Could not read %s's unlock tables: %r", skill, exc)
        return ""
    # Exactly this level, not everything below it: "what did I just unlock" is a
    # question about the step, and 200 rows of everything since level 1 is how
    # the useful line gets lost.
    at = [u for u in found if u.level == level]
    if not at:
        return ""
    return render(at, skill, level)


def _with_facts(question: str, facts: str) -> str:
    """Put the table rows in front of the question, or leave it alone.

    Relay, not recall. The instruction is explicit about the empty case because
    the failure it replaces was a confident paragraph about a level nobody asked
    about -- given nothing to relay, "nothing new unlocks" is the answer, and
    inventing one is not.
    """
    if not facts:
        return question
    return (
        f"{question}\n\n"
        "These rows come from the wiki's own requirement tables, already "
        "filtered to that exact level:\n\n"
        f"{facts}\n\n"
        "Relay what is above and nothing else. Do not add items, levels or "
        "figures from memory. If the list is empty, say that nothing notable "
        "unlocks at this level and move on to how to train it."
    )


async def _method_facts(client, skill: str, level: int) -> str:
    """The training guide's own bracket for this level, or "".

    Same fix as :func:`_unlock_facts`, for the other trigger. Asked whether
    training Thieving at 18 was any good, she read Master Farmer -- a real page,
    correctly fetched -- and recommended pickpocketing them. They need 38. On the
    previous remark she had read the same page and reported the requirement as
    94, which is on it: 94 is where you stop *failing* with the hard Ardougne
    Diary, and 38 is the requirement. Three answers, three numbers lifted off
    pages she genuinely read, none of them the number the question was about.

    The guide already brackets methods by level. Handing her the bracket that
    covers the level she is actually at removes the step where she chooses.
    """
    from .training import brackets_for

    try:
        legs, page = await brackets_for(client, skill, level, min(99, level + 1))
    except Exception as exc:  # noqa: BLE001 - never worth losing the remark over
        log.warning("Could not read %s's training guide: %r", skill, exc)
        return ""
    if not legs:
        return ""
    lines = []
    for leg in legs[:4]:
        rate = f", about {int(leg.xp_hour):,} xp/hr" if leg.xp_hour else ""
        lines.append(f"  {leg.heading} (levels {leg.start}-{leg.end}{rate})")
    return f"From {page}, the brackets covering level {level}:\n" + "\n".join(lines)


async def _facts_for(client, pick) -> tuple[str, str]:
    """The question to ask and the rows to answer it from.

    Returns the question too, because sometimes the honest thing is to ask a
    different one. Most levels unlock nothing: 19 Thieving unlocks nothing, and
    handing that question back unchanged with no rows is what produced "the real
    challenge starts at Thieving 91" -- twice, for a level 19 account. An empty
    table is an answer, not a failure to find one.
    """
    if not pick.skill or not pick.level:
        return pick.question, ""
    if pick.key.startswith("level:"):
        rows = await _unlock_facts(client, pick.skill, pick.level)
        if rows:
            return pick.question, rows
        return (
            f"Nothing new unlocks at {pick.skill} level {pick.level}. Say that "
            f"in one short line without dressing it up, then tell me the best "
            f"way to train {pick.skill} from here.",
            await _method_facts(client, pick.skill, pick.level),
        )
    if pick.key.startswith("doing:"):
        return pick.question, await _method_facts(client, pick.skill, pick.level)
    return pick.question, ""
