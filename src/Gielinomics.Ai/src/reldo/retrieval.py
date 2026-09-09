"""Hybrid retrieval: shortlist from two rankers, then fetch the winners live.

The two rankers fail in opposite directions, which is the whole reason to run both:

* **CirrusSearch** is BM25 over the full article text. It nails exact terminology
  ("Bandos chestplate", "Ardougne Diary") and returns nothing useful when the user
  doesn't know the in-game name for the thing they're describing.
* **The semantic index** matches on meaning over titles and lead paragraphs. It
  handles "that boss that heals off its own poison" and is fuzzy about specifics.

Their rankings are merged with Reciprocal Rank Fusion, then the surviving pages
are fetched in full, live, so the text handed to the model is what the wiki says
right now rather than whatever it said when the index was last built.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .index import SemanticIndex
from .wiki import WikiClient

# Standard RRF constant. Larger values flatten the contribution of rank position,
# so a page has to place well in *both* rankers to climb. 60 is the value from the
# original Cormack et al. paper and it has never been the thing worth tuning here.
RRF_K = 60


@dataclass(frozen=True, slots=True)
class Passage:
    """A page body, ready to hand to the model."""

    title: str
    text: str
    url: str


@dataclass(frozen=True, slots=True)
class Shortlisted:
    """A page that survived fusion, before its body was fetched."""

    title: str
    summary: str
    score: float
    found_by: tuple[str, ...]


def page_url(title: str) -> str:
    return "https://oldschool.runescape.wiki/w/" + title.replace(" ", "_")


class HybridRetriever:
    """Shortlist with two rankers, fetch bodies for the top few."""

    def __init__(self, client: WikiClient, index: SemanticIndex) -> None:
        self._client = client
        self._index = index

    @property
    def client(self):
        """The underlying WikiClient. Exposed for callers that need the raw API
        rather than the retrieval pipeline -- table reads, for one."""
        return self._client

    async def shortlist(self, query: str, *, k: int = 8, pool: int = 20) -> list[Shortlisted]:
        """Fuse semantic and keyword rankings down to k candidates."""
        semantic_task = asyncio.to_thread(self._index.shortlist, query, k=pool)
        keyword_task = self._client.search(query, limit=pool)
        semantic, keyword = await asyncio.gather(semantic_task, keyword_task)

        scores: dict[str, float] = {}
        sources: dict[str, set[str]] = {}
        summaries: dict[str, str] = {}

        for rank, candidate in enumerate(semantic):
            scores[candidate.title] = scores.get(candidate.title, 0.0) + 1.0 / (RRF_K + rank)
            sources.setdefault(candidate.title, set()).add("semantic")
            summaries[candidate.title] = candidate.summary

        for rank, hit in enumerate(keyword):
            scores[hit.title] = scores.get(hit.title, 0.0) + 1.0 / (RRF_K + rank)
            sources.setdefault(hit.title, set()).add("keyword")
            summaries.setdefault(hit.title, hit.snippet)

        ranked = sorted(scores, key=lambda t: -scores[t])[:k]
        return [
            Shortlisted(
                title=title,
                summary=summaries.get(title, ""),
                score=scores[title],
                found_by=tuple(sorted(sources[title])),
            )
            for title in ranked
        ]

    async def fetch(self, titles: list[str], *, max_chars: int = 30_000) -> list[Passage]:
        """Fetch full page bodies concurrently, skipping any that 404.

        The cap covers most pages whole -- "Mining" is 15k characters and
        "Pay-to-play Mining training" is 27k. It does not cover everything:
        "Dragon Slayer II" is 45k. For those, read a section at a time via
        :meth:`sections` and :meth:`section_text` rather than losing the back
        half of the page silently.
        """

        async def one(title: str) -> Passage | None:
            try:
                text = await self._client.page_text(title)
            except Exception:  # a stale index entry shouldn't sink the whole query
                return None
            return Passage(title=title, text=text[:max_chars], url=page_url(title))

        results = await asyncio.gather(*(one(t) for t in titles))
        return [p for p in results if p is not None]

    async def sections(self, title: str) -> list[str]:
        """Section headings with their indices, indented by nesting level.

        The index is what :meth:`section_text` takes, so it has to be visible to
        whoever is choosing a section to read.
        """
        sections = await self._client.sections(title)
        return [f"[{s.index}] " + "  " * (s.level - 1) + s.line for s in sections]

    async def section_text(self, title: str, section: str) -> str:
        """Plaintext of one section of a page."""
        return await self._client.section_text(title, section)

    async def retrieve(self, query: str, *, k: int = 4) -> list[Passage]:
        """Shortlist, then fetch bodies for the top k. The one-call convenience path."""
        candidates = await self.shortlist(query, k=k)
        return await self.fetch([c.title for c in candidates])
