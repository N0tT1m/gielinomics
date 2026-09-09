"""What was asked, and what came back.

Tracing here has meant one thing: point ``RELDO_TRACE_PROXY_URL`` at den-den-mushi
and let the chat calls flow through it. That captures prompts and tool calls,
which is the right altitude for debugging the model -- and the wrong one for the
question actually being asked, which is "what did she say to me, and was it
true". A proxy sees seventeen chat completions per answer and no answers.

So this records the *exchange*: the question, the answer, who was speaking, and
what the enforcement passes did on the way through. Two sinks, on purpose:

* A JSONL file, always. It is the one that cannot fail to be readable later --
  no auth, no schema, no service that has to be up at the moment something went
  wrong. Every analysis of a bad answer in this project has started with reading
  the actual text back.
* An HTTP ingest, when one is configured. Whatever is listening there decides
  what it does with the record; this does not know and must not care.

**Neither sink may ever break an answer.** A trace that takes down the thing it
is observing is worse than no trace, so every failure here is logged and
swallowed, and the HTTP post runs on a short timeout against a service that is
allowed to be down.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# Long enough for a local service, short enough that a hung one does not hold up
# the answer already on screen.
TIMEOUT = 4.0

# One answer can carry a lot of read pages. The whole point is readability later.
MAX_LIST = 20


@dataclass
class Exchange:
    """One question and its answer, with the machinery's fingerprints on it."""

    question: str
    answer: str
    player: str = ""
    persona: str = ""
    # False when she spoke first. The distinction matters more than anything
    # else here: an unprompted remark had no question from you to be wrong about.
    prompted: bool = True
    # The question actually asked, after a follow-up was rewritten standalone.
    # Different from `question` only when conversation.resolve changed it, which
    # is exactly when a wrong answer is worth blaming on the rewrite.
    asked: str = ""
    live: str = ""
    searches: list[str] = field(default_factory=list)
    pages_read: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    passes_fired: list[str] = field(default_factory=list)

    def record(self, at: str) -> dict:
        return {
            "source": "reldo",
            "at": at,
            "player": self.player,
            "persona": self.persona,
            "prompted": self.prompted,
            "question": self.question,
            "asked": self.asked or self.question,
            "answer": self.answer,
            "live": self.live,
            "searches": self.searches[:MAX_LIST],
            "pages_read": self.pages_read[:MAX_LIST],
            "citations": self.citations[:MAX_LIST],
            # Which enforcement passes fired is the single most useful field
            # when an answer is wrong: it says whether the perimeter noticed.
            "passes_fired": self.passes_fired[:MAX_LIST],
        }


def from_answer(answer, question: str, **over) -> Exchange:
    """Build an Exchange from a :class:`~reldo.agent.Answer`, defensively.

    getattr rather than attribute access throughout: Answer grows fields, and a
    trace that raises because one is missing takes down the answer it was
    observing. Nothing here is worth an exception.
    """
    return Exchange(
        question=question,
        answer=getattr(answer, "text", "") or "",
        searches=list(getattr(answer, "searches", []) or []),
        pages_read=list(getattr(answer, "pages_read", []) or []),
        citations=list(getattr(answer, "citations", []) or []),
        passes_fired=list(getattr(answer, "passes_fired", []) or []),
        **over,
    )


class Tracer:
    """Writes exchanges to a file, and posts them if an ingest is configured.

    Args:
        path: JSONL to append to. Created with its parents on first write.
        ingest_url: Something that accepts ``POST`` with a JSON body. The record
            shape is this module's; whatever is listening decides what it means.
    """

    def __init__(
        self,
        path: str | Path = "",
        *,
        ingest_url: str = "",
        clock=None,
    ) -> None:
        self._path = Path(path) if path else None
        self._ingest = ingest_url.rstrip("/")
        if clock is None:
            from datetime import datetime

            def clock() -> str:
                return datetime.now(UTC).isoformat(timespec="seconds")

        self._clock = clock
        self._http: httpx.AsyncClient | None = None

    @property
    def on(self) -> bool:
        return bool(self._path or self._ingest)

    async def __aenter__(self) -> Tracer:
        if self._ingest:
            self._http = httpx.AsyncClient(timeout=TIMEOUT)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def write(self, exchange: Exchange) -> None:
        """Record one exchange. Never raises."""
        record = exchange.record(self._clock())
        if self._path is not None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as out:
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError as exc:
                log.warning("Could not write the trace file: %r", exc)
        if self._http is not None:
            try:
                response = await self._http.post(f"{self._ingest}/", json=record)
                # Logged rather than raised, and at debug: a rejected record is
                # a schema disagreement with something else's API, not a fault
                # in the answer that was just given.
                if response.status_code >= 400:
                    log.warning(
                        "Trace ingest refused the record: %s %s",
                        response.status_code,
                        response.text[:200],
                    )
            except httpx.HTTPError as exc:
                log.warning("Could not reach the trace ingest: %r", exc)
