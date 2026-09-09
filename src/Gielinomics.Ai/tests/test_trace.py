"""The trace has one job: still be readable when something went wrong.

So the tests are mostly about failure -- an ingest that is down, a file that
cannot be written, an Answer missing a field. None of those may reach the caller,
because a trace that breaks the thing it observes is worse than no trace.
"""

from __future__ import annotations

import json

import httpx

from reldo.trace import Exchange, Tracer, from_answer


class _Answer:
    text = "Ruby necklace, love."
    searches = ["gold bar"]
    pages_read = ["Gold bar"]
    citations = ["https://oldschool.runescape.wiki/w/Gold_bar"]
    passes_fired = ["the read nudge"]


def test_writes_one_json_line_per_exchange(tmp_path):
    path = tmp_path / "sub" / "exchanges.jsonl"
    tracer = Tracer(path, clock=lambda: "2026-08-12T00:00:00+00:00")

    import asyncio

    asyncio.run(tracer.write(Exchange(question="q1", answer="a1")))
    asyncio.run(tracer.write(Exchange(question="q2", answer="a2")))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["question"] for line in lines] == ["q1", "q2"]
    assert json.loads(lines[0])["at"] == "2026-08-12T00:00:00+00:00"


def test_records_whether_she_spoke_first(tmp_path):
    """The most important field: an unprompted remark had no question to be
    wrong about, so judging it as an answer to one is judging the wrong thing."""
    path = tmp_path / "e.jsonl"
    import asyncio

    asyncio.run(Tracer(path).write(Exchange(question="q", answer="a", prompted=False)))
    assert json.loads(path.read_text())["prompted"] is False


def test_keeps_the_rewritten_question_separately(tmp_path):
    """When a follow-up was rewritten, a wrong answer may be the rewrite's fault."""
    path = tmp_path / "e.jsonl"
    import asyncio

    asyncio.run(
        Tracer(path).write(
            Exchange(question="what about at 99", answer="a", asked="how long to 99 Fishing")
        )
    )
    row = json.loads(path.read_text())
    assert row["question"] == "what about at 99"
    assert row["asked"] == "how long to 99 Fishing"


def test_asked_defaults_to_the_question(tmp_path):
    path = tmp_path / "e.jsonl"
    import asyncio

    asyncio.run(Tracer(path).write(Exchange(question="q", answer="a")))
    assert json.loads(path.read_text())["asked"] == "q"


def test_from_answer_carries_the_passes_that_fired():
    """Which passes fired is the first thing worth knowing about a bad answer:
    it says whether the perimeter noticed anything at all."""
    exchange = from_answer(_Answer(), "what sells best", player="TimmyZero")
    assert exchange.passes_fired == ["the read nudge"]
    assert exchange.pages_read == ["Gold bar"]
    assert exchange.player == "TimmyZero"


def test_from_answer_survives_an_answer_missing_fields():
    """Answer grows fields; a trace that raises on a missing one takes down the
    answer it was observing."""

    class Bare:
        text = "hi"

    assert from_answer(Bare(), "q").answer == "hi"
    assert from_answer(Bare(), "q").citations == []


def test_an_unwritable_path_does_not_raise(tmp_path):
    blocker = tmp_path / "notadir"
    blocker.write_text("")
    import asyncio

    asyncio.run(Tracer(blocker / "e.jsonl").write(Exchange(question="q", answer="a")))


def test_a_refusing_ingest_does_not_raise(tmp_path):
    """The record shape is this module's; whatever listens may disagree."""
    import asyncio

    async def go():
        tracer = Tracer(tmp_path / "e.jsonl", ingest_url="http://ingest.invalid")
        async with tracer:
            tracer._http = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(422, text="unknown field")
                )
            )
            await tracer.write(Exchange(question="q", answer="a"))
        # The file still got it, which is the point of having two sinks.
        assert json.loads((tmp_path / "e.jsonl").read_text())["question"] == "q"

    asyncio.run(go())


def test_an_unreachable_ingest_does_not_raise(tmp_path):
    import asyncio

    def boom(_):
        raise httpx.ConnectError("refused")

    async def go():
        tracer = Tracer(tmp_path / "e.jsonl", ingest_url="http://ingest.invalid")
        async with tracer:
            tracer._http = httpx.AsyncClient(transport=httpx.MockTransport(boom))
            await tracer.write(Exchange(question="q", answer="a"))
        assert (tmp_path / "e.jsonl").exists()

    asyncio.run(go())


def test_posts_the_record_to_the_ingest(tmp_path):
    import asyncio

    seen: list[dict] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    async def go():
        tracer = Tracer(tmp_path / "e.jsonl", ingest_url="http://ingest.invalid")
        async with tracer:
            tracer._http = httpx.AsyncClient(transport=httpx.MockTransport(capture))
            await tracer.write(Exchange(question="q", answer="a", player="TimmyZero"))

    asyncio.run(go())
    assert seen and seen[0]["source"] == "reldo"
    assert seen[0]["player"] == "TimmyZero"


def test_off_when_nothing_is_configured():
    assert not Tracer().on
    assert Tracer("x.jsonl").on
    assert Tracer(ingest_url="http://x").on
