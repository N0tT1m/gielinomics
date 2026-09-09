"""Retrieval tests: index persistence and rank fusion, both without network."""

from __future__ import annotations

import numpy as np
import pytest

from reldo.index import DEFAULT_OLLAMA_URL, ChunkIndex, SemanticIndex, load_index
from reldo.retrieval import RRF_K, HybridRetriever, page_url
from reldo.wiki import SearchHit


class FakeIndex:
    """Stands in for SemanticIndex so fusion can be tested without an embedder."""

    def __init__(self, ranking):
        self._ranking = ranking

    def shortlist(self, query, *, k=20):
        from reldo.index import Candidate

        return [
            Candidate(t, f"summary of {t}", 1.0 - i * 0.1) for i, t in enumerate(self._ranking)
        ][:k]


class FakeWiki:
    def __init__(self, ranking, bodies=None):
        self._ranking = ranking
        self._bodies = bodies or {}

    async def search(self, query, *, limit=10):
        return [SearchHit(t, f"snippet of {t}", 100) for t in self._ranking][:limit]

    async def page_text(self, title):
        if title not in self._bodies:
            raise LookupError(title)
        return self._bodies[title]


def test_page_url_encodes_spaces():
    assert page_url("Abyssal whip") == "https://oldschool.runescape.wiki/w/Abyssal_whip"


async def test_fusion_promotes_pages_found_by_both_rankers():
    """A page ranked 2nd by both should beat a page ranked 1st by only one."""
    retriever = HybridRetriever(
        FakeWiki(["keyword-only", "agreed"]), FakeIndex(["semantic-only", "agreed"])
    )
    results = await retriever.shortlist("q", k=3)
    assert results[0].title == "agreed"
    assert results[0].found_by == ("keyword", "semantic")


async def test_fusion_scores_match_rrf_formula():
    retriever = HybridRetriever(FakeWiki(["a"]), FakeIndex(["a"]))
    results = await retriever.shortlist("q", k=1)
    assert results[0].score == pytest.approx(2.0 / RRF_K)


async def test_single_ranker_results_still_surface():
    retriever = HybridRetriever(FakeWiki(["only-keyword"]), FakeIndex(["only-semantic"]))
    titles = {r.title for r in await retriever.shortlist("q", k=5)}
    assert titles == {"only-keyword", "only-semantic"}


async def test_fetch_skips_pages_that_disappeared():
    """A stale index entry must not sink the whole query."""
    retriever = HybridRetriever(FakeWiki([], bodies={"Live": "body"}), FakeIndex([]))
    passages = await retriever.fetch(["Live", "Deleted"])
    assert [p.title for p in passages] == ["Live"]


async def test_fetch_truncates_long_bodies():
    retriever = HybridRetriever(FakeWiki([], bodies={"Long": "x" * 50_000}), FakeIndex([]))
    passages = await retriever.fetch(["Long"], max_chars=100)
    assert len(passages[0].text) == 100


def test_index_roundtrips_through_disk(tmp_path):
    vectors = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    index = SemanticIndex(["A", "B"], ["summary a", "summary b"], vectors, "test-model")
    path = tmp_path / "idx.npz"
    index.save(path)

    loaded = SemanticIndex.load(path)
    assert loaded.titles == ["A", "B"]
    assert loaded.summaries == ["summary a", "summary b"]
    assert loaded.model_name == "test-model"
    np.testing.assert_allclose(loaded.vectors, index.vectors)


def test_index_rejects_mismatched_lengths():
    with pytest.raises(ValueError, match="same length"):
        SemanticIndex(["A", "B"], ["only one"], np.zeros((2, 2), dtype=np.float32))


def test_index_load_gives_actionable_error_when_absent(tmp_path):
    with pytest.raises(FileNotFoundError, match="reldo index build|reldo build"):
        SemanticIndex.load(tmp_path / "missing.npz")


def test_vectors_are_normalised_on_construction():
    index = SemanticIndex(["A"], ["s"], np.array([[3.0, 4.0]], dtype=np.float32))
    assert np.linalg.norm(index.vectors[0]) == pytest.approx(1.0)


def test_generic_headings_are_dropped_by_document_frequency():
    """Furniture like 'Trivia' appears everywhere and must not reach the vector."""
    from reldo.index import _filter_generic_headings

    corpus = {f"Page {i}": ["Trivia", "Changes"] for i in range(500)}
    corpus["Herblore"] = ["Trivia", "Changes", "Training activities"]

    filtered = _filter_generic_headings(corpus)
    assert filtered["Herblore"] == ["Training activities"]
    assert filtered["Page 0"] == []


def test_rare_headings_survive_filtering():
    from reldo.index import _filter_generic_headings

    corpus = {f"Page {i}": ["Common"] for i in range(500)}
    corpus["Odd"] = ["Common", "Nylocas Ischyros"]
    assert "Nylocas Ischyros" in _filter_generic_headings(corpus)["Odd"]


def test_document_appends_headings_and_caps_them():
    from reldo.index import MAX_HEADINGS_PER_DOC, _document

    plain = _document("Vorkath", "A dragon.", [])
    assert plain == "Vorkath. A dragon."

    with_heads = _document("Vorkath", "A dragon.", ["Fight overview", "Drops"])
    assert with_heads == "Vorkath. A dragon. Topics covered: Fight overview, Drops."

    many = _document("X", "s.", [f"H{i}" for i in range(30)])
    assert many.count(",") == MAX_HEADINGS_PER_DOC - 1


# -- the embedder address survives a save/load round trip -------------------
# It did not, and nothing caught it: `reldo doctor` passes the configured URL
# explicitly and therefore passed, while `reldo ask` embedded every query
# against whatever address happened to be compiled in.


def test_the_embedder_address_is_persisted(tmp_path):
    index = SemanticIndex(
        ["A"], ["a"], np.ones((1, 4), dtype=np.float32), "nomic-embed-text",
        "ollama", "http://10.0.0.9:11434",
    )
    index.save(tmp_path / "i.npz")
    assert load_index(tmp_path / "i.npz").ollama_url == "http://10.0.0.9:11434"


def test_a_chunk_index_persists_it_too(tmp_path):
    index = ChunkIndex(
        ["A"], [""], ["a"], np.ones((1, 4), dtype=np.float32), "nomic-embed-text",
        "ollama", "http://10.0.0.9:11434",
    )
    index.save(tmp_path / "i.npz")
    assert load_index(tmp_path / "i.npz").ollama_url == "http://10.0.0.9:11434"


def test_the_configured_address_beats_the_stored_one(tmp_path):
    """Where the embedder runs is runtime configuration. Moving the GPU box
    must not mean rebuilding 35k vectors."""
    index = SemanticIndex(
        ["A"], ["a"], np.ones((1, 4), dtype=np.float32), "nomic-embed-text",
        "ollama", "http://10.0.0.9:11434",
    )
    index.save(tmp_path / "i.npz")
    loaded = load_index(tmp_path / "i.npz", "http://192.168.5.5:11434")
    assert loaded.ollama_url == "http://192.168.5.5:11434"


def test_an_index_written_before_this_fix_still_loads(tmp_path):
    """meta.json from an older build has no ollama_url key at all."""
    import json

    path = tmp_path / "i.npz"
    np.savez_compressed(path, vectors=np.ones((1, 4), dtype=np.float32))
    path.with_suffix(".meta.json").write_text(
        json.dumps({"model": "m", "backend": "local", "titles": ["A"], "summaries": ["a"]})
    )
    assert load_index(path).ollama_url == DEFAULT_OLLAMA_URL
