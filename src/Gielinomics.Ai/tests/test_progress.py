"""XP snapshots and speech, both without network.

The dedupe in ``record`` is the load-bearing part. Every question triggers a
hiscores lookup, so storing unconditionally would fill the file with identical
rows and make "you have been at this for 6 hours" a fact about the conversation
rather than about the game.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from reldo.progress import (
    DAY,
    ProgressStore,
    poll,
    sample_once,
    snapshot_of,
    summarise,
)
from reldo.voice import VoiceClient, VoiceError, spoken_form


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# -- snapshots --------------------------------------------------------------


def test_a_snapshot_survives_a_restart(tmp_path):
    path = tmp_path / "p.json"
    ProgressStore(path).record("TimmyZero", {"Fishing": 100})
    assert ProgressStore(path).snapshots("TimmyZero")[0].xp == {"Fishing": 100}


def test_an_identical_snapshot_is_not_stored_twice():
    """Asking questions is not playing. Without this, a day of chatting looks
    like a day of training."""
    store = ProgressStore("/dev/null")
    store._save = lambda: None
    assert store.record("T", {"Fishing": 100}) is True
    assert store.record("T", {"Fishing": 100}) is False
    assert len(store.snapshots("T")) == 1


def test_a_changed_snapshot_is_stored():
    store = ProgressStore("/dev/null")
    store._save = lambda: None
    store.record("T", {"Fishing": 100})
    assert store.record("T", {"Fishing": 200}) is True


def test_an_empty_snapshot_is_ignored():
    store = ProgressStore("/dev/null")
    store._save = lambda: None
    assert store.record("T", {}) is False


def test_history_is_capped(tmp_path):
    from reldo import progress

    store = ProgressStore(tmp_path / "p.json")
    for n in range(progress.MAX_SNAPSHOTS + 20):
        store.record("T", {"Fishing": n + 1})
    assert len(store.snapshots("T")) == progress.MAX_SNAPSHOTS


def test_one_snapshot_is_not_enough_to_compare(tmp_path):
    store = ProgressStore(tmp_path / "p.json")
    store.record("T", {"Fishing": 100})
    assert store.gains("T") is None


def test_gains_are_the_difference_between_snapshots(tmp_path):
    clock = Clock()
    store = ProgressStore(tmp_path / "p.json", clock=clock)
    store.record("T", {"Fishing": 100, "Mining": 50})
    clock.advance(2 * DAY)
    store.record("T", {"Fishing": 500, "Mining": 50})

    gained, elapsed = store.gains("T")
    assert gained == {"Fishing": 400}  # Mining did not move, so it is not listed
    assert elapsed == pytest.approx(2 * DAY)


def test_the_window_reported_is_the_real_one_not_the_one_asked_for(tmp_path):
    """Ask for a week of history from somebody tracked since yesterday and the
    honest answer is 'in 1 day'. Reporting the requested window would overstate
    how long they have been stuck."""
    clock = Clock()
    store = ProgressStore(tmp_path / "p.json", clock=clock)
    store.record("T", {"Fishing": 100})
    clock.advance(DAY)
    store.record("T", {"Fishing": 200})

    _, elapsed = store.gains("T", since=7 * DAY)
    assert elapsed == pytest.approx(DAY)


def test_snapshots_outside_the_window_are_not_the_baseline(tmp_path):
    clock = Clock()
    store = ProgressStore(tmp_path / "p.json", clock=clock)
    store.record("T", {"Fishing": 0})
    clock.advance(30 * DAY)
    store.record("T", {"Fishing": 1000})
    clock.advance(DAY)
    store.record("T", {"Fishing": 1100})

    gained, _ = store.gains("T", since=7 * DAY)
    assert gained == {"Fishing": 100}  # not 1,100 -- the month-old row is outside


def test_a_corrupt_file_reads_as_empty(tmp_path):
    path = tmp_path / "p.json"
    path.write_text("{not json")
    assert ProgressStore(path).snapshots("T") == []


# -- summarising ------------------------------------------------------------


def test_no_history_summarises_to_nothing():
    assert summarise(None) == ""


def test_no_gains_is_said_out_loud():
    """The most useful thing a coach can notice. An empty string would hide it."""
    assert summarise(({}, 3 * DAY)) == "No XP gained at all in the last 3 days."


def test_gains_are_listed_biggest_first():
    line = summarise(({"Mining": 10, "Fishing": 400_000}, DAY))
    assert line == "XP in the last 1 day: Fishing +400,000, Mining +10."


def test_hours_read_as_hours_not_fractions_of_a_day():
    assert "5 hours" in summarise(({"Fishing": 1}, 5 * 3600))


# -- speech -----------------------------------------------------------------


def test_a_short_answer_is_spoken_whole():
    assert spoken_form("You need 70 Attack.") == "You need 70 Attack."


def test_links_are_read_as_their_label():
    """'open bracket Abyssal whip close bracket http colon slash' is
    unlistenable."""
    got = spoken_form("See [Abyssal whip](https://oldschool.runescape.wiki/w/Abyssal_whip).")
    assert got == "See Abyssal whip."


def test_bare_urls_are_dropped():
    assert "http" not in spoken_form("Read https://example.com/thing for more.")


def test_markdown_furniture_is_stripped():
    assert spoken_form("**Mine** `granite`") == "Mine granite"


def test_a_long_answer_is_cut_on_a_sentence_boundary():
    """There is no scrollback in audio, so a clipped clause is simply lost."""
    text = " ".join(f"Sentence number {n} goes here and is fairly long." for n in range(30))
    got = spoken_form(text, limit=100)
    assert got.endswith(".")
    assert len(got) <= 100


def test_one_enormous_sentence_is_still_said():
    got = spoken_form("x" * 500, limit=100)
    assert len(got) == 100


# -- the speech client ------------------------------------------------------


def voice_client(handler) -> VoiceClient:
    return VoiceClient("http://xtts", transport=httpx.MockTransport(handler))


async def test_speak_follows_the_two_step_contract():
    """POST /speak returns a filename; the audio is a second request."""
    seen: list[str] = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/speak":
            return httpx.Response(200, json={"audio": "abc.wav"})
        return httpx.Response(200, content=b"RIFFwav")

    async with voice_client(handler) as client:
        assert await client.speak("You have one Slayer.") == b"RIFFwav"
    assert seen == ["/speak", "/audio/abc.wav"]


async def test_a_200_with_no_filename_is_an_error_not_a_success():
    """The failure worth catching: /speak answering 200 does not mean audio
    exists, and treating it as success gives a silent, successful-looking play."""

    def handler(request):
        return httpx.Response(200, json={})

    async with voice_client(handler) as client:
        with pytest.raises(VoiceError, match="no audio filename"):
            await client.speak("hello")


async def test_empty_audio_is_an_error():
    def handler(request):
        if request.url.path == "/speak":
            return httpx.Response(200, json={"audio": "abc.wav"})
        return httpx.Response(200, content=b"")

    async with voice_client(handler) as client:
        with pytest.raises(VoiceError, match="empty"):
            await client.speak("hello")


async def test_a_server_that_is_down_raises_rather_than_hanging():
    def handler(request):
        raise httpx.ConnectError("refused")

    async with voice_client(handler) as client:
        with pytest.raises(VoiceError, match="Could not reach"):
            await client.speak("hello")


async def test_nothing_to_say_is_refused_before_the_request():
    called = False

    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(200, json={"audio": "a.wav"})

    async with voice_client(handler) as client:
        with pytest.raises(VoiceError, match="Nothing to say"):
            await client.speak("   ")
    assert not called


async def test_the_voice_is_sent_and_overridable():
    sent: list[dict] = []

    def handler(request):
        import json

        if request.url.path == "/speak":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"audio": "a.wav"})
        return httpx.Response(200, content=b"RIFF")

    async with voice_client(handler) as client:
        await client.speak("hi", voice="yor_forger")
    assert sent[0]["performer_id"] == "yor_forger"


async def test_voices_degrade_to_empty_when_the_server_is_down():
    def handler(request):
        raise httpx.ConnectError("refused")

    async with voice_client(handler) as client:
        assert await client.voices() == []


# -- sampling on a timer ----------------------------------------------------
# History used to advance only when somebody asked something, so a week of
# playing without talking to the bot left nothing to compare against.


class FakeAccounts:
    def __init__(self, *names):
        self._names = list(names)

    def names(self):
        return list(self._names)


class FakeHiscores:
    """Serves a queue of XP maps per player, so a poll can watch one move."""

    def __init__(self, xp_by_name, *, fails=()):
        self._xp = {n: list(v) for n, v in xp_by_name.items()}
        self._fails = set(fails)
        self.looked_up: list[str] = []

    async def lookup(self, rsn):
        self.looked_up.append(rsn)
        if rsn in self._fails:
            raise RuntimeError(f"no hiscores entry for {rsn!r}")
        rows = self._xp[rsn]
        current = rows.pop(0) if len(rows) > 1 else rows[0]
        return SimpleNamespace(name=rsn, xp=lambda skill: current.get(skill, 0))


async def test_history_advances_with_nobody_asking_anything(tmp_path):
    """The whole point. record() is otherwise only reached from a question, so
    somebody who plays all week and asks nothing has one snapshot and no gains."""
    store = ProgressStore(tmp_path / "p.json")
    hiscores = FakeHiscores({"TimmyZero": [{"Fishing": 100}, {"Fishing": 5_000}]})
    accounts = FakeAccounts("TimmyZero")

    await sample_once(accounts, hiscores, store)
    await sample_once(accounts, hiscores, store)

    assert len(store.snapshots("TimmyZero")) == 2
    gained, _ = store.gains("TimmyZero")
    assert gained == {"Fishing": 4_900}


async def test_a_round_where_nobody_played_stores_nothing(tmp_path):
    store = ProgressStore(tmp_path / "p.json")
    hiscores = FakeHiscores({"TimmyZero": [{"Fishing": 100}]})
    accounts = FakeAccounts("TimmyZero")

    assert await sample_once(accounts, hiscores, store) == 1  # the baseline
    assert await sample_once(accounts, hiscores, store) == 0  # unchanged
    assert len(store.snapshots("TimmyZero")) == 1


async def test_one_dead_account_does_not_cost_everybody_else_their_history(tmp_path):
    """A renamed account 404s forever, and it must not take the round with it."""
    store = ProgressStore(tmp_path / "p.json")
    hiscores = FakeHiscores(
        {"Renamed": [{}], "TimmyZero": [{"Fishing": 100}]}, fails=["Renamed"]
    )

    await sample_once(FakeAccounts("Renamed", "TimmyZero"), hiscores, store)

    assert hiscores.looked_up == ["Renamed", "TimmyZero"]
    assert store.snapshots("TimmyZero")[0].xp == {"Fishing": 100}


async def test_the_scheduled_sample_stores_what_a_question_would_have(tmp_path):
    """Both paths go through snapshot_of. Built differently, record()'s dedupe
    would see every alternation as a change and fill the file with rows in which
    nothing happened."""
    player = SimpleNamespace(name="TimmyZero", xp=lambda s: 100 if s == "Fishing" else 0)
    store = ProgressStore(tmp_path / "p.json")

    store.record(player.name, snapshot_of(player))  # the question path
    await sample_once(
        FakeAccounts("TimmyZero"), FakeHiscores({"TimmyZero": [{"Fishing": 100}]}), store
    )

    assert len(store.snapshots("TimmyZero")) == 1


async def test_poll_samples_before_its_first_sleep(tmp_path):
    """A restart is the moment you most want a baseline; waiting out the
    interval leaves a hole in the history after every deploy."""
    store = ProgressStore(tmp_path / "p.json")
    hiscores = FakeHiscores({"TimmyZero": [{"Fishing": 100}]})

    async def sleep(_seconds):
        raise asyncio.CancelledError  # stop after the first round

    with pytest.raises(asyncio.CancelledError):
        await poll(FakeAccounts("TimmyZero"), hiscores, store, sleep=sleep)

    assert store.snapshots("TimmyZero")


async def test_poll_survives_a_round_that_raises(tmp_path):
    """A sampler that dies silently leaves a feature that looks enabled and
    does nothing -- the exact failure this function exists to remove."""
    rounds = []

    class Exploding:
        def names(self):
            rounds.append(1)
            if len(rounds) == 1:
                raise RuntimeError("boom")
            return []

    async def sleep(_seconds):
        if len(rounds) >= 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await poll(Exploding(), None, None, sleep=sleep)

    assert len(rounds) == 2  # kept going after the failure
