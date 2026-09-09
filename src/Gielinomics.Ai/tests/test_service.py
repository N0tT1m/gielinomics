"""The JSON surface the web frontend calls. No network, no index, no model.

The retriever is stubbed rather than built, because what is under test here is
the contract -- what shape comes back, what a missing query does, what happens
when the model is absent -- and none of that is a question about retrieval
quality. ``test_retrieval.py`` already owns that question.

The one behaviour worth stating twice: **search must work without a model.**
The index is numpy and the keyword half is an HTTP call to the wiki; neither
needs a GPU. A frontend whose search box goes dark because the model server is
off would be failing for no reason, so there is a test that it does not.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from reldo.retrieval import Shortlisted
from reldo.service import build_app


class StubRetriever:
    """Records what it was asked, returns what it was told to."""

    def __init__(self, hits: list[Shortlisted] | None = None) -> None:
        self.hits = hits if hits is not None else [
            Shortlisted(
                title="Abyssal whip",
                summary="A weapon.",
                score=0.031,
                found_by=("keyword", "semantic"),
            ),
            Shortlisted(
                title="Abyssal demon",
                summary="A monster.",
                score=0.016,
                found_by=("semantic",),
            ),
        ]
        self.asked: list[tuple[str, int]] = []

    async def shortlist(self, query: str, *, k: int = 8, pool: int = 20) -> list[Shortlisted]:
        self.asked.append((query, k))
        return self.hits


class StubAnswer:
    text = "The whip is worth it at 70 Attack."
    citations = ()


class StubAgent:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def ask(self, question: str, **_) -> StubAnswer:
        self.asked.append(question)
        return StubAnswer()


async def client_for(retriever, agent=None) -> TestClient:
    app = build_app(retriever, agent=agent)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_search_returns_the_shortlist_with_its_provenance():
    retriever = StubRetriever()
    client = await client_for(retriever)
    try:
        response = await client.get("/api/ai/search", params={"q": "abyssal whip"})
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    assert body["query"] == "abyssal whip"
    assert [r["title"] for r in body["results"]] == ["Abyssal whip", "Abyssal demon"]
    # Which ranker found it, surfaced rather than hidden: the two fail in
    # opposite directions, so "both found it" is a different kind of hit.
    assert body["results"][0]["foundBy"] == ["keyword", "semantic"]
    assert body["results"][0]["url"] == "https://oldschool.runescape.wiki/w/Abyssal_whip"


async def test_search_needs_no_model():
    """The reason `agent` is optional at all."""
    client = await client_for(StubRetriever(), agent=None)
    try:
        response = await client.get("/api/ai/search", params={"q": "whip"})
        health = await (await client.get("/health")).json()
    finally:
        await client.close()

    assert response.status == 200
    assert health["search"] is True
    assert health["ask"] is False


async def test_an_empty_query_is_not_an_error():
    """A search box is empty before it is typed in, which is not a mistake."""
    retriever = StubRetriever()
    client = await client_for(retriever)
    try:
        body = await (await client.get("/api/ai/search", params={"q": "  "})).json()
    finally:
        await client.close()

    assert body["results"] == []
    assert retriever.asked == []


async def test_limit_is_capped_rather_than_honoured():
    retriever = StubRetriever()
    client = await client_for(retriever)
    try:
        await client.get("/api/ai/search", params={"q": "whip", "limit": "5000"})
    finally:
        await client.close()

    assert retriever.asked == [("whip", 25)]


async def test_a_nonsense_limit_is_rejected_rather_than_ignored():
    client = await client_for(StubRetriever())
    try:
        response = await client.get("/api/ai/search", params={"q": "whip", "limit": "lots"})
    finally:
        await client.close()

    assert response.status == 400


async def test_asking_without_a_model_says_search_still_works():
    client = await client_for(StubRetriever(), agent=None)
    try:
        response = await client.post("/api/ai/ask", json={"question": "is the whip worth it"})
        text = await response.text()
    finally:
        await client.close()

    assert response.status == 503
    assert "Search still works" in text


async def test_asking_with_a_model_returns_the_answer():
    agent = StubAgent()
    client = await client_for(StubRetriever(), agent=agent)
    try:
        body = await (
            await client.post("/api/ai/ask", json={"question": "is the whip worth it"})
        ).json()
    finally:
        await client.close()

    assert body["answer"] == StubAnswer.text
    assert agent.asked == ["is the whip worth it"]


@pytest.mark.parametrize("payload", [{}, {"question": "   "}])
async def test_a_question_is_required(payload: dict):
    client = await client_for(StubRetriever(), agent=StubAgent())
    try:
        response = await client.post("/api/ai/ask", json=payload)
    finally:
        await client.close()

    assert response.status == 400


async def test_a_non_json_body_is_a_400_not_a_500():
    client = await client_for(StubRetriever(), agent=StubAgent())
    try:
        response = await client.post(
            "/api/ai/ask", data="not json", headers={"Content-Type": "application/json"}
        )
    finally:
        await client.close()

    assert response.status == 400


async def test_the_browser_gets_cors_headers():
    """Vite serves the frontend on another port in development."""
    client = await client_for(StubRetriever())
    try:
        response = await client.get("/api/ai/search", params={"q": "whip"})
    finally:
        await client.close()

    assert response.headers["Access-Control-Allow-Origin"] == "*"


async def test_health_is_served_without_authentication():
    client = await client_for(StubRetriever())
    try:
        response = await client.get("/health")
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    assert body["status"] == "ok"


@pytest.mark.parametrize("path", ["/health", "/api/ai/health"])
async def test_health_is_served_at_both_paths(path: str):
    """The healthcheck and the browser want different prefixes for one answer."""
    client = await client_for(StubRetriever())
    try:
        response = await client.get(path)
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    assert body["status"] == "ok"


def test_the_app_builds_without_an_agent():
    assert isinstance(build_app(StubRetriever()), web.Application)


# ---------------------------------------------------------------------------
# No index. The state a fresh deployment is in until somebody builds one.
# ---------------------------------------------------------------------------


async def test_the_service_comes_up_without_an_index():
    """A crash loop is not a message.

    This runs as a container with `restart: unless-stopped`, and the index is a
    volume that starts empty. Exiting would restart forever without ever
    producing an index; coming up lets you query the service about its own
    state.
    """
    client = await client_for(None)
    try:
        response = await client.get("/health")
        body = await response.json()
    finally:
        await client.close()

    # 200, not 503: the container healthcheck reads the status code, and a
    # service honestly reporting a missing index is up. Restarting it would not
    # produce one.
    assert response.status == 200
    assert body["search"] is False


async def test_searching_without_an_index_says_how_to_fix_it():
    client = await client_for(None)
    try:
        response = await client.get("/api/ai/search", params={"q": "whip"})
        text = await response.text()
    finally:
        await client.close()

    assert response.status == 503
    assert "reldo build" in text
