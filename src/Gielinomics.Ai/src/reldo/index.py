"""The semantic shortlist index: every article's title and lead paragraph, embedded
as one vector per article.

**There is deliberately no vector database here.** The wiki yields ~35k indexable
articles; at 384 dimensions that is a 54 MB float32 matrix, and a brute-force
cosine scan plus top-k selection measures at 0.8 ms on one core -- against a wiki
round-trip of several hundred milliseconds right after it. pgvector, Chroma, and
FAISS all solve a real problem, at the cost of a service to run, a schema to
migrate, and a recall/speed tradeoff to tune; here they would optimise 0.1% of the
query. A ``.npz`` file loaded into RAM is the whole store. Revisit at ~100x the
corpus, where it stops fitting comfortably in memory.

**Section headings are OFF by default, on measurement.** Appending them to each
article's document was tried (``with_headings=True``, ``reldo build
--with-headings``) on the theory that they widen what a page matches on. It cost a
question on the eval set: "the dragon boss you fight after Dragon Slayer 2" fell
from semantic rank 7 to outside the top 20, because the extra heading text diluted
the vector enough to lose the canonical page to its near-neighbours. One vector per
article means every word added competes with every other for the same 384
dimensions. A one-question swing at n=15 is within noise and does not prove
headings are harmful in general -- but the rank movement is large and directional,
so the default follows the measurement. The code stays for re-testing against a
bigger model or a bigger eval set.

The index holds titles and leads -- never article bodies. Full text is fetched live
at query time (see :mod:`reldo.retrieval`), which is what keeps answers correct the
day after a game update rather than the day after a re-index.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .wiki import WikiClient

log = logging.getLogger(__name__)

# 384-dim, ~130 MB of ONNX, no torch dependency. Chosen because shortlisting is a
# forgiving task: we only need the right page in the top ~40, and a hosted
# embedding API would add a per-query network hop plus a bill for no measurable
# gain at this corpus size. Swap it for a Voyage model here if that changes.
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

# GPU path. nomic-embed-text is 768-dim -- double bge-small -- and Ollama embeds
# ~370 docs/s on a 5090, indexing the whole wiki in ~1.6 min against ~13 min on
# CPU. The extra dimensions matter here: the section-heading experiment below
# failed partly because 384 dims left no room for the added text.
DEFAULT_OLLAMA_EMBED_MODEL = "nomic-embed-text"

# goose. Only a fallback: the real value comes from RELDO_OLLAMA_API_URL and is
# threaded through build, save, and load. It was written out at six call sites
# before, which is how a loaded index ended up embedding queries against a
# hardcoded address on somebody else's network.
DEFAULT_OLLAMA_URL = "http://192.168.1.78:11434"
OLLAMA_EMBED_BATCH = 256

# bge models want an instruction prefix on the *query* side only; documents are
# embedded bare. Getting this backwards silently costs a few points of recall.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# The lead section can run long; we only need enough to characterise the topic.
SUMMARY_CHARS = 600

# Section headings are appended to each article's document to widen topic coverage
# beyond the lead paragraph -- "Training activities" on Herblore, "Special attack"
# on a weapon. Two guards keep that from backfiring:
#
# 1. Most headings are navigational furniture. "Drops", "Trivia", "Changes",
#    "Gallery", and "References" appear on thousands of pages and carry no signal
#    about what a page is *about*; embedding them just pulls every article toward
#    a common centroid. Anything appearing on more than this fraction of the
#    corpus is dropped. A frequency cut beats a hand-written stopword list because
#    it re-derives itself when the wiki's conventions change.
HEADING_DF_CUTOFF = 0.01

# 2. A long page can have 30+ headings, and stuffing them all into one vector
#    blurs it into uselessness. Keep the first few in document order -- the
#    earliest sections are the ones about the subject rather than its appendices.
MAX_HEADINGS_PER_DOC = 10


@dataclass(frozen=True, slots=True)
class Candidate:
    """One shortlisted page, with the score that got it there."""

    title: str
    summary: str
    score: float


class SemanticIndex:
    """An in-memory embedding index over article titles, leads, and section headings."""

    def __init__(
        self,
        titles: list[str],
        summaries: list[str],
        vectors: np.ndarray,
        model_name: str = DEFAULT_MODEL,
        backend: str = "local",
        ollama_url: str = DEFAULT_OLLAMA_URL,
    ) -> None:
        if not (len(titles) == len(summaries) == vectors.shape[0]):
            raise ValueError("titles, summaries and vectors must be the same length")
        self.titles = titles
        self.summaries = summaries
        self.model_name = model_name
        self.backend = backend
        self.ollama_url = ollama_url
        # Pre-normalised, so cosine similarity is a plain dot product.
        self.vectors = _normalise(vectors.astype(np.float32))
        self._embedder = None

    # -- persistence -------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, vectors=self.vectors)
        path.with_suffix(".meta.json").write_text(
            json.dumps(
                {
                    "model": self.model_name,
                    "backend": self.backend,
                    # Persisted so a loaded index knows where its embedder lives.
                    # Omitting it sent every query embedding to the module default
                    # regardless of configuration -- silently, because `reldo
                    # doctor` passes the configured URL explicitly and therefore
                    # passed while `reldo ask` was talking to another machine.
                    "ollama_url": self.ollama_url,
                    "titles": self.titles,
                    "summaries": self.summaries,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        log.info("Wrote %d entries to %s", len(self.titles), path)

    @classmethod
    def load(cls, path: Path, ollama_url: str | None = None) -> SemanticIndex:
        """Load an index, preferring a caller-supplied embedder address.

        ``ollama_url`` wins over the stored one when given: the vectors are fixed
        at build time but *where the embedder runs* is runtime configuration, and
        moving the GPU box should not mean rebuilding 35k vectors.
        """
        meta_path = path.with_suffix(".meta.json")
        if not path.exists() or not meta_path.exists():
            raise FileNotFoundError(f"No index at {path}. Build one with: reldo build")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        vectors = np.load(path)["vectors"]
        return cls(
            meta["titles"],
            meta["summaries"],
            vectors,
            meta["model"],
            meta.get("backend", "local"),
            ollama_url or meta.get("ollama_url") or DEFAULT_OLLAMA_URL,
        )

    # -- query -------------------------------------------------------------

    def shortlist(self, query: str, *, k: int = 20) -> list[Candidate]:
        """The k articles whose indexed text is closest to the query."""
        prefix = QUERY_PREFIX if self.backend == "local" else ""
        vector = _normalise(self._embed([prefix + query]))[0]
        scores = self.vectors @ vector
        # argpartition beats a full sort: we want the top k out of 41k, not a
        # ranking of all 41k.
        k = min(k, len(self.titles))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [
            Candidate(self.titles[i], self.summaries[i], float(scores[i])) for i in top
        ]

    def _embed(self, texts: list[str]) -> np.ndarray:
        return embed_texts(texts, self.model_name, self.backend, self.ollama_url)


async def build(
    client: WikiClient,
    *,
    model_name: str = DEFAULT_MODEL,
    limit: int | None = None,
    progress: bool = True,
    with_headings: bool = False,
    backend: str = "local",
    ollama_url: str = DEFAULT_OLLAMA_URL,
) -> SemanticIndex:
    """Enumerate every article, fetch its lead section, and embed it.

    Measured on the full wiki: ~1,800 API calls, 18 minutes end to end on CPU,
    35,682 articles enumerated and 34,961 kept (the rest have no extractable lead
    section -- disambiguation pages and template-only stubs). Pass ``limit`` for a
    fast smoke test against a slice of the corpus.
    """
    log.info("Enumerating articles...")
    titles: list[str] = []
    async for title in client.iter_article_titles():
        titles.append(title)
        if limit and len(titles) >= limit:
            break
    log.info("Found %d articles; fetching summaries", len(titles))

    pairs: dict[str, str] = {}
    done = 0
    async for summary in client.iter_summaries(titles):
        text = " ".join(summary.summary.split())[:SUMMARY_CHARS]
        if text:
            pairs[summary.title] = text
        done += 1
        if progress and done % 1000 == 0:
            log.info("  %d/%d summaries", done, len(titles))

    kept_titles = list(pairs)

    headings: dict[str, list[str]] = {}
    if with_headings:
        log.info("Fetching section headings for %d articles", len(kept_titles))
        done = 0
        async for title, found in client.iter_headings(kept_titles):
            headings[title] = found
            done += 1
            if progress and done % 5000 == 0:
                log.info("  %d/%d heading sets", done, len(kept_titles))
        headings = _filter_generic_headings(headings)

    log.info(
        "Embedding %d documents with %s (%s backend)", len(kept_titles), model_name, backend
    )
    # Title is prepended to the body: for a wiki, the title is often the single
    # most discriminating signal ("Vorkath" vs a paragraph that never repeats it).
    documents = [_document(t, pairs[t], headings.get(t, [])) for t in kept_titles]
    vectors = embed_texts(documents, model_name, backend, ollama_url)

    return SemanticIndex(
        kept_titles,
        [pairs[t] for t in kept_titles],
        vectors,
        model_name,
        backend,
        ollama_url,
    )


def _document(title: str, summary: str, headings: list[str]) -> str:
    """The text that actually gets embedded for one article."""
    doc = f"{title}. {summary}"
    if headings:
        doc += " Topics covered: " + ", ".join(headings[:MAX_HEADINGS_PER_DOC]) + "."
    return doc


def _filter_generic_headings(headings: dict[str, list[str]]) -> dict[str, list[str]]:
    """Drop headings common enough across the corpus to carry no topical signal."""
    if not headings:
        return headings
    document_frequency: Counter[str] = Counter()
    for found in headings.values():
        document_frequency.update(set(found))

    cutoff = max(2, int(len(headings) * HEADING_DF_CUTOFF))
    generic = {h for h, n in document_frequency.items() if n > cutoff}
    log.info(
        "Dropping %d generic headings (on >%d pages), e.g. %s",
        len(generic),
        cutoff,
        [h for h, _ in document_frequency.most_common(6)],
    )
    return {t: [h for h in found if h not in generic] for t, found in headings.items()}


def embed_texts(
    texts: list[str],
    model_name: str,
    backend: str = "local",
    ollama_url: str = DEFAULT_OLLAMA_URL,
) -> np.ndarray:
    """Embed a list of strings with the selected backend.

    Synchronous on purpose: this is called from a worker thread during build and
    from ``asyncio.to_thread`` at query time, so an async client would just add a
    loop to manage for no benefit.
    """
    if backend == "ollama":
        return _embed_ollama(texts, model_name, ollama_url)
    return _embed_local(texts, model_name)


_LOCAL_EMBEDDERS: dict[str, object] = {}


def _embed_local(texts: list[str], model_name: str) -> np.ndarray:
    """CPU path: ONNX via fastembed. No torch, no GPU, works on a laptop."""
    from fastembed import TextEmbedding

    embedder = _LOCAL_EMBEDDERS.get(model_name)
    if embedder is None:
        embedder = _LOCAL_EMBEDDERS[model_name] = TextEmbedding(model_name=model_name)
    return np.array(list(embedder.embed(texts)), dtype=np.float32)


def _embed_ollama(texts: list[str], model_name: str, ollama_url: str) -> np.ndarray:
    """GPU path: Ollama's /api/embed, batched.

    Ollama accepts a list and returns vectors in the same order, so batching is
    the whole optimisation -- one request per 256 documents rather than per
    document turns a 35k-article build from hours into ~1.6 minutes.
    """
    import httpx

    base = ollama_url.rstrip("/").removesuffix("/v1")
    out: list[list[float]] = []
    with httpx.Client(timeout=600.0) as client:
        for start in range(0, len(texts), OLLAMA_EMBED_BATCH):
            batch = texts[start : start + OLLAMA_EMBED_BATCH]
            response = client.post(
                f"{base}/api/embed", json={"model": model_name, "input": batch}
            )
            response.raise_for_status()
            vectors = response.json().get("embeddings")
            if not vectors or len(vectors) != len(batch):
                raise RuntimeError(
                    f"Ollama returned {len(vectors or [])} vectors for {len(batch)} "
                    f"inputs -- is {model_name!r} an embedding model?"
                )
            out.extend(vectors)
    return np.array(out, dtype=np.float32)


def _normalise(vectors: np.ndarray) -> np.ndarray:
    if vectors.ndim == 1:
        vectors = vectors[None, :]
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


async def build_and_save(
    client: WikiClient,
    path: Path,
    *,
    limit: int | None = None,
    model_name: str = DEFAULT_MODEL,
    with_headings: bool = False,
    backend: str = "local",
    ollama_url: str = DEFAULT_OLLAMA_URL,
) -> SemanticIndex:
    index = await build(
        client,
        limit=limit,
        model_name=model_name,
        with_headings=with_headings,
        backend=backend,
        ollama_url=ollama_url,
    )
    await asyncio.to_thread(index.save, path)
    return index


class ChunkIndex:
    """An embedding index over article *passages* rather than lead paragraphs.

    Exposes the same ``shortlist(query, k=...)`` contract as
    :class:`SemanticIndex`, so :class:`~reldo.retrieval.HybridRetriever` accepts
    either without knowing which it has.

    Two things differ from the lead index, and both matter:

    * A page appears once per passage, so scores are **max-pooled per page**
      before ranking. Without that, a forty-chunk quest guide crowds out every
      other result by surface area alone.
    * The ``summary`` handed back is *the passage that matched*, not the page's
      lead. That is strictly more useful: it is the text that caused the hit, so
      it usually contains the answer rather than merely pointing at it.
    """

    def __init__(
        self,
        titles: list[str],
        sections: list[str],
        texts: list[str],
        vectors: np.ndarray,
        model_name: str = DEFAULT_OLLAMA_EMBED_MODEL,
        backend: str = "ollama",
        ollama_url: str = DEFAULT_OLLAMA_URL,
    ) -> None:
        if not (len(titles) == len(sections) == len(texts) == vectors.shape[0]):
            raise ValueError("titles, sections, texts and vectors must be the same length")
        self.titles = titles
        self.sections = sections
        self.texts = texts
        self.model_name = model_name
        self.backend = backend
        self.ollama_url = ollama_url
        self.vectors = _normalise(vectors.astype(np.float32))

    @property
    def pages(self) -> int:
        return len(set(self.titles))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, vectors=self.vectors)
        path.with_suffix(".meta.json").write_text(
            json.dumps(
                {
                    "kind": "chunk",
                    "model": self.model_name,
                    "backend": self.backend,
                    "ollama_url": self.ollama_url,
                    "titles": self.titles,
                    "sections": self.sections,
                    "texts": self.texts,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        log.info("Wrote %d chunks (%d pages) to %s", len(self.titles), self.pages, path)

    @classmethod
    def load(cls, path: Path, ollama_url: str | None = None) -> ChunkIndex:
        meta_path = path.with_suffix(".meta.json")
        if not path.exists() or not meta_path.exists():
            raise FileNotFoundError(f"No index at {path}. Build one with: reldo build")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return cls(
            meta["titles"],
            meta["sections"],
            meta["texts"],
            np.load(path)["vectors"],
            meta["model"],
            meta.get("backend", "ollama"),
            ollama_url or meta.get("ollama_url") or DEFAULT_OLLAMA_URL,
        )

    def shortlist(self, query: str, *, k: int = 20, pool: int = 400) -> list[Candidate]:
        """The k best *pages*, each ranked by its single best-matching passage."""
        prefix = QUERY_PREFIX if self.backend == "local" else ""
        vector = _normalise(
            embed_texts([prefix + query], self.model_name, self.backend, self.ollama_url)
        )[0]
        scores = self.vectors @ vector

        pool = min(pool, len(self.titles))
        top = np.argpartition(-scores, pool - 1)[:pool]
        top = top[np.argsort(-scores[top])]

        best: dict[str, Candidate] = {}
        for i in top:
            title = self.titles[i]
            if title in best:  # a better passage from this page already won
                continue
            where = f"[{self.sections[i]}] " if self.sections[i] else ""
            best[title] = Candidate(title, f"{where}{self.texts[i][:400]}", float(scores[i]))
            if len(best) >= k:
                break
        return list(best.values())


async def build_chunks(
    client: WikiClient,
    *,
    model_name: str = DEFAULT_OLLAMA_EMBED_MODEL,
    backend: str = "ollama",
    ollama_url: str = DEFAULT_OLLAMA_URL,
    limit: int | None = None,
    progress: bool = True,
) -> ChunkIndex:
    """Fetch every article's wikitext, split it into passages, and embed them.

    Measured on a 300-article sample and projected to the full wiki: ~85k chunks
    from ~35k articles, ~0.26 GB of float32 vectors, ~3 minutes of fetching and
    ~4 of embedding on a 5090. A brute-force scan at that size is 2.7 ms, so the
    no-vector-database call still holds.
    """
    from .chunks import Chunk, chunk_article

    log.info("Enumerating articles...")
    titles: list[str] = []
    async for title in client.iter_article_titles():
        titles.append(title)
        if limit and len(titles) >= limit:
            break
    log.info("Found %d articles; fetching wikitext", len(titles))

    chunk_titles: list[str] = []
    sections: list[str] = []
    texts: list[str] = []
    done = 0
    async for title, raw in client.iter_wikitext(titles):
        for chunk in chunk_article(title, raw):
            chunk_titles.append(chunk.title)
            sections.append(chunk.section)
            texts.append(chunk.text)
        done += 1
        if progress and done % 5000 == 0:
            log.info("  %d/%d articles -> %d chunks", done, len(titles), len(texts))

    log.info("Embedding %d chunks with %s (%s backend)", len(texts), model_name, backend)
    documents = [
        Chunk(t, s, x).document()
        for t, s, x in zip(chunk_titles, sections, texts, strict=True)
    ]
    vectors = embed_texts(documents, model_name, backend, ollama_url)
    return ChunkIndex(
        chunk_titles, sections, texts, vectors, model_name, backend, ollama_url
    )


def load_index(path: Path, ollama_url: str | None = None) -> SemanticIndex | ChunkIndex:
    """Load whichever index kind is on disk.

    The two are interchangeable to callers -- same ``shortlist`` contract -- so
    everything downstream goes through here rather than picking a class.

    Pass ``ollama_url`` from settings whenever the index might query: an index
    built on one machine and loaded on another otherwise embeds against whatever
    address it was built with, which fails in the one direction nobody checks.
    """
    meta_path = path.with_suffix(".meta.json")
    if not path.exists() or not meta_path.exists():
        raise FileNotFoundError(f"No index at {path}. Build one with: reldo build")
    kind = json.loads(meta_path.read_text(encoding="utf-8")).get("kind", "lead")
    loader = ChunkIndex.load if kind == "chunk" else SemanticIndex.load
    return loader(path, ollama_url)


async def build_chunks_and_save(
    client: WikiClient, path: Path, **kwargs
) -> ChunkIndex:
    index = await build_chunks(client, **kwargs)
    await asyncio.to_thread(index.save, path)
    return index
