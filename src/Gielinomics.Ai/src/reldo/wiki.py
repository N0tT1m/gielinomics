"""Async client for the Old School RuneScape wiki's MediaWiki API.

The wiki runs CirrusSearch (Elasticsearch) and TextExtracts, which is what makes
this project tractable: we get real full-text search and clean plaintext page
bodies without parsing wikitext or scraping HTML.

Content is CC BY-NC-SA 3.0. Attribute the wiki; don't build a commercial product
on it.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Iterable, Iterator, Sequence
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser

import httpx

API_URL = "https://oldschool.runescape.wiki/api.php"

# Anonymous API limits. Exceeding them isn't an error -- MediaWiki silently
# clamps and emits a warning -- so batching code must respect them or it will
# quietly drop titles.
MAX_TITLES_PER_EXTRACT_CALL = 20
MAX_PAGES_PER_LIST_CALL = 500

# Raw wikitext batches far better than rendered content: `prop=extracts` without
# `exintro` silently clamps to ONE title per call (and returns empty extracts for
# the rest), which would make a heading sweep 35k sequential requests. Pulling
# wikitext 50 pages at a time and regexing the headings out costs ~700 calls.
MAX_TITLES_PER_CONTENT_CALL = 50

# Virtual levels go to 126. Used to tell a requirement apart from the XP rates
# and quantities the same wiki template also renders. Deliberately not imported
# from skills.py: this module has no reldo imports and is usable standalone.
MAX_SKILL_LEVEL = 126

# == Heading ==, === Subheading ===, up to level 6.
_HEADING_RE = re.compile(r"^[ \t]*(={2,6})[ \t]*(.+?)[ \t]*\1[ \t]*$", re.MULTILINE)

# Markup that shows up inside headings: [[Link|Label]], '''bold'', <ref>, {{template}}.
_WIKILINK_RE = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]|]+)\]\]")
_TEMPLATE_RE = re.compile(r"\{\{[^}]*\}\}")
_TAG_RE = re.compile(r"<[^>]+>")

# Sent on every request. When the wiki's replication lag exceeds this many
# seconds it returns an error instead of adding load, and we back off. Being a
# good citizen here is what keeps a bulk index build from getting the UA blocked.
MAXLAG_SECONDS = 5


class WikiError(RuntimeError):
    """The API returned an error, or returned something we can't use."""


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One CirrusSearch result."""

    title: str
    snippet: str
    word_count: int


@dataclass(frozen=True, slots=True)
class PageSummary:
    """A page's title and its lead section as plaintext."""

    title: str
    summary: str


@dataclass(frozen=True, slots=True)
class Section:
    """One section heading within a page."""

    index: str
    level: int
    line: str


class WikiClient:
    """Rate-limited async access to the OSRS wiki API.

    Args:
        user_agent: Required. The wiki blocks default agents (``python-requests``,
            ``curl``, and friends) outright, so we fail at construction rather than
            let you discover it as a 403 storm mid-index-build. Include a contact
            URL, e.g. ``"reldo/0.1 (github.com/you/reldo)"``.
        requests_per_second: Ceiling on outbound request rate.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        user_agent: str,
        *,
        requests_per_second: float = 4.0,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_base: float = 2.0,
    ) -> None:
        if not user_agent or not user_agent.strip():
            raise ValueError(
                "A descriptive User-Agent is required. The OSRS wiki blocks default "
                "agents; use something like 'reldo/0.1 (github.com/you/reldo)'."
            )
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        # Exposed so tests can collapse retry sleeps to zero.
        self._backoff_base = backoff_base
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip"},
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            # The rate limiter already serialises requests, so a large pool buys
            # nothing and just gives us more sockets to have go stale mid-build.
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def __aenter__(self) -> WikiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- transport ---------------------------------------------------------

    async def _get(self, params: dict[str, str | int]) -> dict:
        """One API call, rate-limited, with backoff on 429/5xx and maxlag.

        **Everything this raises is a WikiError**, and callers depend on that
        rather than on it being the usual case. :meth:`iter_wikitext` and
        :meth:`iter_headings` catch it per batch so one bad batch cannot abort a
        700-call sweep, and :meth:`reldo.bucket.BucketClient.query` translates it
        into its own error type. ``raise_for_status`` used to sit here instead,
        so a 500 came out as ``httpx.HTTPStatusError`` and sailed past all of
        them -- making the "one bad batch shouldn't abort the sweep" comments
        below true of a 503 and false of a 500, which is not a distinction
        anything upstream of here intends to draw.
        """
        query = {
            "format": "json",
            "formatversion": "2",
            "maxlag": MAXLAG_SECONDS,
            **params,
        }

        # What last sent us round again, so the give-up message below names the
        # thing that actually happened rather than listing all three and being
        # right about one of them.
        retrying_because = "rate-limiting or lagging"

        for attempt in range(5):
            async with self._lock:
                gap = self._min_interval - (time.monotonic() - self._last_request)
                if gap > 0:
                    await asyncio.sleep(gap)
                self._last_request = time.monotonic()

            try:
                response = await self._http.get(API_URL, params=query)
            except httpx.TransportError as exc:
                # Connection resets, DNS hiccups, read timeouts. An index build is
                # ~1,800 requests over 20 minutes; without this, a single blip
                # anywhere in that window throws the whole run away.
                if attempt == 4:
                    raise WikiError(f"Transport failure after 5 attempts: {exc!r}") from exc
                await asyncio.sleep(self._backoff(attempt))
                continue

            if response.status_code == 429:
                retrying_because = "rate-limiting"
                await asyncio.sleep(self._retry_delay(response, attempt))
                continue

            if response.status_code >= 500:
                # Every 5xx, not only the 503 the wiki itself sends when it is
                # shedding load: a CDN or reverse proxy in front of it answers
                # 502 and 504 for the same transient condition, and treating
                # those as fatal throws away a whole index build over one blip.
                retrying_because = f"answering HTTP {response.status_code}"
                await asyncio.sleep(self._retry_delay(response, attempt))
                continue

            if response.status_code >= 400:
                # A client error is ours and will not improve on a retry -- a
                # malformed query, a title the API rejects. Raised as the
                # module's own error rather than httpx's; see the docstring.
                raise WikiError(
                    f"Wiki API returned HTTP {response.status_code}: {response.text[:200]}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                # A 200 carrying something that is not JSON did not come from
                # the API -- it is an interstitial from whatever is in front of
                # it. Same contract as everything else here.
                raise WikiError(
                    f"Wiki API sent a non-JSON body: {response.text[:200]!r}"
                ) from exc

            # maxlag arrives as a 200 with an error body, not an HTTP error code.
            error = payload.get("error")
            if error:
                # Usually an object with code/info. The Bucket extension answers
                # with a bare string instead ("Field name not found in bucket
                # infobox_item"), and assuming the object shape turned every
                # Bucket error into an AttributeError inside the retry loop --
                # a crash in place of the extension's own, rather good, message.
                if not isinstance(error, dict):
                    raise WikiError(str(error))
                if error.get("code") == "maxlag":
                    retrying_because = "lagging behind its replicas"
                    await asyncio.sleep(self._retry_delay(response, attempt))
                    continue
                raise WikiError(f"{error.get('code')}: {error.get('info')}")

            return payload

        raise WikiError(f"Gave up after 5 attempts -- the wiki is {retrying_because}.")

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_base**attempt, 30.0) if self._backoff_base else 0.0

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        """Honour Retry-After when present, else exponential backoff."""
        header = response.headers.get("Retry-After")
        if header:
            try:
                return min(float(header), 60.0)
            except ValueError:
                pass
        return self._backoff(attempt)

    # -- reads -------------------------------------------------------------

    async def search(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        """Full-text search via CirrusSearch.

        This is keyword/BM25 search: excellent when the user's wording overlaps the
        article's, useless when it doesn't. Pair it with the semantic index -- see
        :mod:`reldo.retrieval`.
        """
        payload = await self._get(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": min(limit, 50),
                "srnamespace": 0,
                "srprop": "snippet|wordcount",
            }
        )
        return [
            SearchHit(
                title=hit["title"],
                snippet=_strip_html(hit.get("snippet", "")),
                word_count=hit.get("wordcount", 0),
            )
            for hit in payload.get("query", {}).get("search", [])
        ]

    async def summaries(self, titles: Sequence[str]) -> list[PageSummary]:
        """Lead-section plaintext for up to 20 pages in one call.

        Titles that don't resolve are dropped rather than raising -- callers are
        usually working from a search result set where a stale title is expected.
        """
        if not titles:
            return []
        if len(titles) > MAX_TITLES_PER_EXTRACT_CALL:
            raise ValueError(
                f"At most {MAX_TITLES_PER_EXTRACT_CALL} titles per call; "
                f"got {len(titles)}. Use iter_summaries() for bulk work."
            )
        payload = await self._get(
            {
                "action": "query",
                "prop": "extracts",
                "exintro": 1,
                "explaintext": 1,
                "exlimit": "max",
                "titles": "|".join(titles),
            }
        )
        out = []
        for page in payload.get("query", {}).get("pages", []):
            if page.get("missing") or not page.get("extract"):
                continue
            out.append(PageSummary(title=page["title"], summary=page["extract"].strip()))
        return out

    async def iter_summaries(
        self, titles: Iterable[str], *, concurrency: int = 4
    ) -> AsyncIterator[PageSummary]:
        """Stream summaries for arbitrarily many titles, batching and pipelining."""
        batches = list(_chunked(titles, MAX_TITLES_PER_EXTRACT_CALL))
        semaphore = asyncio.Semaphore(concurrency)

        async def run(batch: list[str]) -> list[PageSummary]:
            async with semaphore:
                return await self.summaries(batch)

        for coro in asyncio.as_completed([run(b) for b in batches]):
            for summary in await coro:
                yield summary

    async def headings(self, titles: Sequence[str]) -> dict[str, list[str]]:
        """Section headings for up to 50 pages, parsed out of raw wikitext.

        Headings are returned in document order, with wiki markup stripped. Pages
        with no revision content are omitted rather than raising.
        """
        if not titles:
            return {}
        if len(titles) > MAX_TITLES_PER_CONTENT_CALL:
            raise ValueError(
                f"At most {MAX_TITLES_PER_CONTENT_CALL} titles per call; got {len(titles)}. "
                "Use iter_headings() for bulk work."
            )
        payload = await self._get(
            {
                "action": "query",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "titles": "|".join(titles),
            }
        )
        out: dict[str, list[str]] = {}
        for page in payload.get("query", {}).get("pages", []):
            revisions = page.get("revisions")
            if not revisions:
                continue
            wikitext = revisions[0].get("slots", {}).get("main", {}).get("content", "")
            found = [_clean_heading(m.group(2)) for m in _HEADING_RE.finditer(wikitext)]
            out[page["title"]] = [h for h in found if h]
        return out

    async def wikitext(self, titles: Sequence[str]) -> dict[str, str]:
        """Raw wikitext for up to 50 pages, keyed by title.

        The only batched route to full article bodies. ``prop=extracts`` without
        ``exintro`` silently clamps to one title per call, so the clean-plaintext
        path would be 35k sequential requests where this is ~700.
        """
        if not titles:
            return {}
        if len(titles) > MAX_TITLES_PER_CONTENT_CALL:
            raise ValueError(
                f"At most {MAX_TITLES_PER_CONTENT_CALL} titles per call; got {len(titles)}. "
                "Use iter_wikitext() for bulk work."
            )
        payload = await self._get(
            {
                "action": "query",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "titles": "|".join(titles),
            }
        )
        out: dict[str, str] = {}
        for page in payload.get("query", {}).get("pages", []):
            revisions = page.get("revisions")
            if not revisions:
                continue
            content = revisions[0].get("slots", {}).get("main", {}).get("content")
            if content:
                out[page["title"]] = content
        return out

    async def iter_wikitext(
        self, titles: Iterable[str], *, concurrency: int = 4
    ) -> AsyncIterator[tuple[str, str]]:
        """Stream wikitext for arbitrarily many titles, batching and pipelining."""
        batches = list(_chunked(titles, MAX_TITLES_PER_CONTENT_CALL))
        semaphore = asyncio.Semaphore(concurrency)

        async def run(batch: list[str]) -> dict[str, str]:
            async with semaphore:
                try:
                    return await self.wikitext(batch)
                except WikiError:
                    # One bad batch shouldn't abort a 700-call sweep.
                    return {}

        for coro in asyncio.as_completed([run(b) for b in batches]):
            for title, content in (await coro).items():
                yield title, content

    async def iter_headings(
        self, titles: Iterable[str], *, concurrency: int = 4
    ) -> AsyncIterator[tuple[str, list[str]]]:
        """Stream headings for arbitrarily many titles, batching and pipelining."""
        batches = list(_chunked(titles, MAX_TITLES_PER_CONTENT_CALL))
        semaphore = asyncio.Semaphore(concurrency)

        async def run(batch: list[str]) -> dict[str, list[str]]:
            async with semaphore:
                try:
                    return await self.headings(batch)
                except WikiError:
                    # One bad batch shouldn't abort a 700-call sweep; those pages
                    # simply end up indexed on their summary alone.
                    return {}

        for coro in asyncio.as_completed([run(b) for b in batches]):
            for title, found in (await coro).items():
                yield title, found

    async def page_text(self, title: str) -> str:
        """Full page body as plaintext.

        Pages are small -- a boss page runs about 5 KB -- so fetching whole pages
        live is cheap enough that we don't cache article bodies at all.

        **This is blind to tables.** ``prop=extracts&explaintext`` drops them
        entirely, and a lot of OSRS facts live in one: level requirements, drop
        rates, item stats. "Cannonball" is the worked example -- its Smithing
        requirement is in a table, so ``page_text`` returns 7,325 characters that
        do not contain "35" anywhere, and a model asked for it either says it is
        not on the page or recites the number from memory.

        :meth:`section_text` does not have this problem: it goes through
        ``action=parse``, so tables survive ``_html_to_text``. Reading the
        section is the route to table data.

        Switching this method to the parse route was measured and rejected:
        it is 1.3x larger for Cannonball but 4.7x for Vorkath and 10.1x for
        Rune platebody, and it pushes "Pay-to-play Mining training" from 27k to
        39.8k -- past the 30k cap in HybridRetriever.fetch, which would silently
        lose the back half of the page a training question needs. Paying 3-10x
        the context on every read, to a 24B local model, for table data most
        questions do not want, is the wrong default.
        """
        payload = await self._get(
            {
                "action": "query",
                "prop": "extracts",
                "explaintext": 1,
                "redirects": 1,
                "titles": title,
            }
        )
        pages = payload.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            raise WikiError(f"No such page: {title!r}")
        return (pages[0].get("extract") or "").strip()

    async def section_text(self, title: str, section: str) -> str:
        """Plaintext of one section, by the index from :meth:`sections`.

        Guides are the pages this matters for. "Pay-to-play Mining training" is
        27k characters and "Dragon Slayer II" is 45k -- far past what is sensible
        to hand a local model in one go, and the part a training question needs
        (levels 45-99) is in the back half that a truncated read never reaches.

        Rendered HTML rather than wikitext: templates come out expanded, so the
        model reads "3-tick mining granite" instead of
        ``[[Tick manipulation|3-tick]] mining [[granite]]``, and it is ~15%
        shorter for the same content.
        """
        payload = await self._get(
            {
                "action": "parse",
                "page": title,
                "section": section,
                "prop": "text",
                "redirects": 1,
            }
        )
        html_text = payload.get("parse", {}).get("text", "")
        if not html_text:
            raise WikiError(f"No section {section!r} on {title!r}")
        return _html_to_text(html_text)

    async def tables(self, title: str, section: str | None = None) -> list[list[list[str]]]:
        """Structured tables from a page, or from one section of it.

        The route to everything ``page_text`` cannot see. A great many OSRS
        facts -- level requirements, item stats, drop rates, the GE tax
        exemptions -- live only in tables, and ``prop=extracts`` drops them.
        """
        params: dict[str, str | int] = {
            "action": "parse", "page": title, "prop": "text", "redirects": 1,
        }
        if section is not None:
            params["section"] = section
        payload = await self._get(params)
        html_text = payload.get("parse", {}).get("text", "")
        if not html_text:
            raise WikiError(f"No content for {title!r} section {section!r}")
        return parse_tables(html_text)

    async def requirements(self, title: str) -> list[tuple[str, int]]:
        """Every skill requirement a page states, as ``(skill, level)`` pairs.

        Exact where reading it out of the page is not: the wiki marks each one
        up with the skill's name and its level as attributes, so this needs no
        column-guessing and cannot attribute a level to the wrong skill. That is
        the whole failure it exists for -- "what Sailing level for marlin"
        answered 91, the Fishing level, because the rendered page shows both as
        bare numbers next to indistinguishable icons.
        """
        payload = await self._get(
            {"action": "parse", "page": title, "prop": "text", "redirects": 1}
        )
        html_text = payload.get("parse", {}).get("text", "")
        if not html_text:
            raise WikiError(f"No content for {title!r}")
        return parse_skill_requirements(html_text)

    async def sections(self, title: str) -> list[Section]:
        """Section headings for a page, so a caller can drill into one."""
        payload = await self._get({"action": "parse", "page": title, "prop": "sections"})
        return [
            Section(index=s["index"], level=int(s["level"]), line=s["line"])
            for s in payload.get("parse", {}).get("sections", [])
        ]

    async def iter_article_titles(self) -> AsyncIterator[str]:
        """Every non-redirect article in the main namespace, ~41k of them.

        Used once, to build the index.
        """
        params: dict[str, str | int] = {
            "action": "query",
            "list": "allpages",
            "apnamespace": 0,
            "apfilterredir": "nonredirects",
            "aplimit": MAX_PAGES_PER_LIST_CALL,
        }
        while True:
            payload = await self._get(params)
            for page in payload.get("query", {}).get("allpages", []):
                yield page["title"]
            cont = payload.get("continue")
            if not cont:
                return
            params = {**params, **cont}


def _chunked(items: Iterable[str], size: int) -> Iterator[list[str]]:
    batch: list[str] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


# The wiki's skill-clickpic template renders a requirement as an icon followed
# by a bare number: `<span class="scp" data-skill="Sailing" data-level="78">`,
# an <img>, then " 78 ". Strip the tags naively and the skill's *identity* goes
# with them -- "Sailing 78, Fishing 91, Construction 72" arrives as "78 91 72"
# and the model has to guess which number the question was about. Asked what
# Sailing level marlin needs, it guessed the one the prose spelled out, and
# answered 91: the Fishing level, cited, grounded, and not what was asked.
#
# The attributes are the fix, and they are exact rather than heuristic. Keep the
# skill name; the level is already in the span's text, so re-emitting it here
# would double every figure.
_SKILL_ICON = re.compile(r'(?is)<span class="scp"[^>]*?data-skill="([^"]*)"[^>]*>')

# Both attributes, for callers that want the pair as data rather than as prose.
_SKILL_REQ = re.compile(
    r'(?is)<span class="scp"[^>]*?data-skill="([^"]*)"[^>]*?data-level="([^"]*)"'
)


# "at a rate of 40 minnows for 1 shark", "exchanged for one raw shark per 40
# minnows". A fixed exchange between two items, which the wiki states in prose
# because it is not a recipe and not a price -- Kylie Minnow does not sell
# anything, she swaps.
#
# The item names are read as a run of words that stops at the next preposition
# rather than as a lazy blob: "40 minnows for one raw shark" ends the second
# name at "shark" only because "by" is what follows it. A lazy match stops at
# "raw", and "raw" is not the item -- so the pair failed to match a question
# about sharks on a page that answers it.
_NOT_A_NAME = (
    r"by|with|at|for|to|into|in|and|from|per|which|that|when|after|before|the|a|an"
)
_NAME = rf"(?!(?:{_NOT_A_NAME})\b)[a-z']+"
_EXCHANGED = re.compile(
    rf"([\d,]+)\s+({_NAME}(?:\s+{_NAME}){{0,3}})\s+(?:for|to|into)\s+"
    rf"(one|a|an|[\d,]+)\s+({_NAME}(?:\s+{_NAME}){{0,3}})",
    re.I,
)
_ONE = {"one": 1, "a": 1, "an": 1}

# The sentence has to be about swapping. Without this, "5 minnows for 1 hour"
# and any other two numbers in a sentence become an exchange rate.
_SWAPPING = re.compile(
    r"\b(?:exchang\w+|trad\w+|swap\w+|convert\w+|at a rate of|in return for)\b", re.I
)

_SENTENCE_W = re.compile(r"[^.!?]+[.!?]*")


def exchange_rate(
    text: str, wanted: str, per: str
) -> tuple[int, int, str, str] | None:
    """``(how many wanted, per how many of that, its name here, the sentence)``.

    The name comes back because the page's word for the thing is not the
    asker's, and the difference is money. Kylie Minnow gives *noted raw sharks*
    and somebody asking about "sharks" means the fish; priced as the cooked
    item that is 980 gp each against the raw 696, so 200,000 minnows came to
    4.9M when the answer is 3.5M. Whatever is priced downstream should be what
    the page says you get.

    Reads a fixed swap between two items off a page's own prose, oriented to
    the question rather than to the sentence: the wiki says "40 minnows for 1
    shark" and somebody may ask it either way round, so which number is the
    numerator is decided here and not by whoever wrote the page.

    Both names must appear in the same sentence and both must match what was
    asked. A sentence naming one of them is about something else -- the minnow
    page also says minnows have no use on their own, which is true and is not a
    rate.
    """
    from .training import terms

    want, have = terms(wanted), terms(per)
    if not want or not have:
        return None
    for sentence in _SENTENCE_W.findall(text):
        if not _SWAPPING.search(sentence):
            continue
        for found in _EXCHANGED.finditer(sentence):
            left_n, left, right_n, right = found.groups()
            left_count = int(left_n.replace(",", ""))
            right_count = _ONE.get(right_n.lower(), 0) or int(
                right_n.replace(",", "")
            )
            said = " ".join(sentence.split())
            if want <= terms(left) and have <= terms(right):
                return left_count, right_count, left.lower(), said
            if want <= terms(right) and have <= terms(left):
                return right_count, left_count, right.lower(), said
    return None


def parse_skill_requirements(raw: str) -> list[tuple[str, int]]:
    """Every ``(skill, level)`` pair the rendered HTML states, in page order.

    The same template carries XP rates and quantities as well as levels --
    a money-making guide's "30,274 Fishing XP per hour" is an ``scp`` span too --
    so anything that is not a plausible level is dropped rather than reported as
    a requirement. Comma-formatted values give that away by themselves.
    """
    out: list[tuple[str, int]] = []
    for skill, level in _SKILL_REQ.findall(raw):
        if not level.isdigit():
            continue
        value = int(level)
        if 1 <= value <= MAX_SKILL_LEVEL:
            out.append((skill.strip(), value))
    return out


def _html_to_text(raw: str) -> str:
    """Flatten rendered MediaWiki HTML into something a model can read.

    Table cells become ``|``-separated and rows newline-separated, which keeps
    the XP-rate and materials tables in skilling guides legible rather than
    collapsing them into one run-on line.
    """
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    text = re.sub(r'(?is)<sup class="reference".*?</sup>', "", text)
    text = re.sub(r"(?is)<span class=\"mw-editsection\".*?</span>", "", text)
    text = _SKILL_ICON.sub(r"\1 ", text)
    text = re.sub(r"(?i)</(td|th)>", " | ", text)
    text = re.sub(r"(?i)</(tr|p|div|li|h[1-6]|caption)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


class _TableParser(HTMLParser):
    """Pull ``<table>`` rows out of rendered MediaWiki HTML.

    ``_html_to_text`` flattens tables into pipe-separated prose, which is fine
    for a model to read and useless for filtering. "Everything buildable at
    Sailing 20 or below" needs the level column as a *number*, and reading it
    out of a run-on line is exactly the kind of thing a 24B model gets subtly
    wrong. So parse the structure and keep it.

    ``colspan`` is expanded and ``rowspan`` ignored: the shipbuilding hull table
    has grouped headers ("Defence bonuses" over four columns) and without the
    expansion every cell after it lands under the wrong name.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._span = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self.tables.append([])
        elif tag == "tr" and self._depth:
            self._row = []
        elif tag in ("td", "th") and self._depth:
            self._cell = []
            span = dict(attrs).get("colspan") or "1"
            self._span = int(span) if span.isdigit() and int(span) < 20 else 1
        elif tag == "span" and self._cell is not None:
            # A skill requirement is an icon plus a bare number, and the icon is
            # the only thing saying which skill. Without this the marlin row is
            # "78 91 72 62 66" -- five levels, no names. See _SKILL_ICON.
            skill = dict(attrs).get("data-skill")
            if skill:
                self._cell.append(f"{skill} ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            value = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            if self._row is not None:
                self._row.extend([value] * self._span)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(c for c in self._row) and self.tables:
                self.tables[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._depth:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_tables(html_text: str) -> list[list[list[str]]]:
    """Every table in some rendered HTML, as rows of cell strings."""
    parser = _TableParser()
    parser.feed(html_text)
    return [t for t in parser.tables if len(t) > 1]


# MediaWiki navigation boxes are tables too, and they are pure noise: twenty
# rows of every related page, no facts. They are recognisable by the "vte"
# (view/talk/edit) control the template puts in the first cell.
_NAVBOX = re.compile(r"^vte", re.I)


def tables_as_text(tables: list[list[list[str]]], *, max_chars: int = 6000) -> str:
    """Render parsed tables for a model to read.

    The point of handing over structure rather than prose: "Steel cannonball |
    35 | 30" is one row a model can quote, where the same fact in flattened
    text is a run-on line it has to count columns through.
    """
    out: list[str] = []
    budget = max_chars
    for number, table in enumerate(tables, 1):
        if not table or _NAVBOX.match(table[0][0] if table[0] else ""):
            continue
        # colspan expansion duplicates headers; collapse the repeats for display
        # only, so "Item | Item | Level" reads as "Item | Level".
        header = list(dict.fromkeys(c for c in table[0] if c))
        block = [f"Table {number}: " + " | ".join(header)]
        for row in table[1:]:
            cells = [c for c in row if c]
            if cells:
                block.append("  " + " | ".join(cells))
        rendered = "\n".join(block)
        if len(rendered) > budget:
            break
        budget -= len(rendered)
        out.append(rendered)
    return "\n\n".join(out) if out else "No tables on that page."


def _clean_heading(raw: str) -> str:
    """Strip wiki markup from a heading, keeping the human-readable label."""
    text = _TEMPLATE_RE.sub("", raw)
    text = _WIKILINK_RE.sub(r"\1", text)
    text = _TAG_RE.sub("", text)
    return text.replace("'''", "").replace("''", "").strip()


def _strip_html(text: str) -> str:
    """CirrusSearch snippets come back with <span class="searchmatch"> markup."""
    return re.sub(r"<[^>]+>", "", text).replace("&quot;", '"').replace("&amp;", "&")
