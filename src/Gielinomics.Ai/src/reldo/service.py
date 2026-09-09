"""The HTTP surface, for callers that are not Discord and not a terminal.

``web.py`` already serves a page, but it serves *one* page -- the coach's own
window, on loopback, with its own layout and its own event stream. This is the
other thing: no page at all, just JSON, so the React frontend in ``web/`` can
put a search box next to the price charts without this service having an opinion
about how it looks.

Three routes, and the split between them is the point:

``GET  /api/ai/search``
    Retrieval only. No model is loaded, nothing is generated, and the response
    is the shortlist with the reason each entry is on it. Fast enough to run on
    a keystroke, which is what makes it a search box rather than a form.
    Returns 503 with the command to fix it when no index has been built yet.

``POST /api/ai/ask``
    The whole agent, enforcement passes and all. Seconds, not milliseconds.

``GET  /health`` (also ``/api/ai/health``)
    Whether the index actually loaded, and whether a model is attached. Served
    at both paths: the container healthcheck wants the short one, the browser
    wants it under the same prefix as everything else it calls.

**Search deliberately does not require the model server.** The index is a numpy
matrix and the keyword half is an HTTP call to the wiki; neither needs Ollama to
be up. A frontend that can still search while the GPU box is off is worth more
than one that fails uniformly.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from .retrieval import HybridRetriever, page_url

log = logging.getLogger(__name__)

# Enough to fill a dropdown twice over; more than this and the caller wants the
# wiki's own search, not a shortlist.
MAX_RESULTS = 25


def build_app(
    retriever: HybridRetriever | None,
    *,
    agent=None,
    allow_origin: str = "*",
) -> web.Application:
    """Wire the routes.

    Args:
        retriever: The hybrid retriever, or None when no index has been built.
            None rather than refusing to start: this runs as a container, and a
            fresh deployment has an empty index volume until somebody builds
            into it. Exiting would be a crash loop under `restart:
            unless-stopped`, where coming up and saying what is missing is a
            service you can actually query about its own state.
        agent: The answering agent, or None to serve search alone. Optional
            because the two have very different costs to stand up -- search
            needs an index, asking needs a 24B model on a GPU -- and a
            deployment that only wants the search box should not have to
            provide the second to get the first.
        allow_origin: CORS origin for the browser frontend. The service is
            expected to sit behind the same reverse proxy as the API, in which
            case this is never exercised; it is here for the dev setup, where
            Vite serves on 5173 and this does not.
    """

    async def search(request: web.Request) -> web.Response:
        if retriever is None:
            raise web.HTTPServiceUnavailable(
                text="No search index has been built yet. Run: reldo build"
            )

        query = (request.query.get("q") or "").strip()
        if not query:
            return _json({"query": "", "results": []})

        try:
            limit = min(int(request.query.get("limit", 8)), MAX_RESULTS)
        except ValueError:
            raise web.HTTPBadRequest(text="limit must be a number") from None

        hits = await retriever.shortlist(query, k=max(limit, 1))
        return _json(
            {
                "query": query,
                "results": [
                    {
                        "title": hit.title,
                        "summary": hit.summary,
                        "url": page_url(hit.title),
                        "score": round(hit.score, 6),
                        # Which ranker found it. Surfaced rather than hidden
                        # because the two fail in opposite directions, and a
                        # result found by both is a different kind of result
                        # from one found by neither's strong suit.
                        "foundBy": list(hit.found_by),
                    }
                    for hit in hits
                ],
            }
        )

    async def ask(request: web.Request) -> web.Response:
        if agent is None:
            raise web.HTTPServiceUnavailable(
                text="This service was started without a model. Search still works."
            )
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="Body must be JSON.") from None

        question = str(body.get("question") or "").strip()
        if not question:
            raise web.HTTPBadRequest(text="A question is required.")

        answer = await agent.ask(question)
        return _json(
            {
                "question": question,
                "answer": getattr(answer, "text", str(answer)),
                "citations": [
                    {"title": c.title, "url": c.url}
                    for c in getattr(answer, "citations", []) or []
                ],
            }
        )

    async def health(_: web.Request) -> web.Response:
        # Reports what is actually available rather than a flat "ok". A service
        # answering ok while it has no index is a service that 503s every search
        # and says it is fine.
        #
        # Still 200 when search is unavailable, deliberately: the container
        # healthcheck reads the status code, and a service that is up and
        # honestly reporting a missing index is healthy. Restarting it would not
        # produce an index.
        return _json(
            {
                "status": "ok",
                "search": retriever is not None,
                "ask": agent is not None,
            }
        )

    async def cors(request: web.Request, handler) -> web.StreamResponse:
        # The preflight is answered here rather than by a route per path: the
        # middleware runs before the handler either way, so a route would only
        # be a second place to forget one.
        if request.method == "OPTIONS":
            response: web.StreamResponse = web.Response(status=204)
        else:
            response = await handler(request)
        response.headers["Access-Control-Allow-Origin"] = allow_origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    app = web.Application(middlewares=[web.middleware(cors)])
    app.add_routes(
        [
            # Twice, on purpose. /health is what the container healthcheck hits,
            # where a prefix would be noise; /api/ai/health is what the browser
            # hits, where everything else it calls is under that prefix.
            web.get("/health", health),
            web.get("/api/ai/health", health),
            web.get("/api/ai/search", search),
            web.post("/api/ai/ask", ask),
        ]
    )
    return app


def _json(payload: dict[str, Any]) -> web.Response:
    return web.json_response(payload)


async def serve(
    retriever: HybridRetriever | None,
    *,
    agent=None,
    host: str = "0.0.0.0",
    port: int = 8100,
    allow_origin: str = "*",
) -> web.AppRunner:
    """Start the service and return the runner, for the caller to clean up.

    Binds all interfaces by default because the only deployment that matters is
    inside the compose network, where loopback would make it unreachable from
    the API container. There is nothing to authenticate here -- both routes are
    reads against a public wiki -- so this is not the same decision ``live.py``
    makes about its token.
    """
    runner = web.AppRunner(build_app(retriever, agent=agent, allow_origin=allow_origin))
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("AI service listening on http://%s:%d", host, port)
    return runner
