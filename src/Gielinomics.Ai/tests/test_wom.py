"""Wise Old Man client, against recorded response shapes.

The shapes here were taken from the live API rather than invented, because the
one that matters is subtle: ``data.skills`` includes an ``overall`` row that is
the sum of every other row. Treating it as a skill double-counts every total and
puts "Overall" at the top of any ranking, which reads as a real finding.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from reldo.wom import Efficiency, Gains, WomClient, WomError

PLAYER = {
    "displayName": "TimmyZero",
    "type": "regular",
    "build": "main",
    "exp": 10_007_857,
    "combatLevel": 39,
    "ehp": 148.5,
    "ehb": 0.0,
    "ttm": 1620.4,
    "tt200m": 40_000.0,
}

GAINED = {
    "startsAt": "2026-08-02T00:00:00.000Z",
    "endsAt": "2026-08-09T00:00:00.000Z",
    "data": {
        "skills": {
            "overall": {
                "metric": "overall",
                "experience": {"gained": 412_000, "start": 9_595_857, "end": 10_007_857},
                "ehp": {"gained": 6.5, "start": 142.0, "end": 148.5},
            },
            "fishing": {
                "metric": "fishing",
                "experience": {"gained": 412_000, "start": 9_027_707, "end": 9_439_707},
                "ehp": {"gained": 6.5},
            },
            "mining": {
                "metric": "mining",
                "experience": {"gained": 0, "start": 158_874, "end": 158_874},
                "ehp": {"gained": 0},
            },
        }
    },
}


def client_for(handler) -> WomClient:
    return WomClient(transport=httpx.MockTransport(handler))


def responder(**by_path):
    def handler(request: httpx.Request) -> httpx.Response:
        for path, response in by_path.items():
            if path in str(request.url):
                return response() if callable(response) else response
        return httpx.Response(404, json={"message": "Player not found"})

    return handler


# -- lookup -----------------------------------------------------------------


async def test_efficiency_is_parsed():
    async with client_for(responder(**{"/players/": httpx.Response(200, json=PLAYER)})) as wom:
        found = await wom.lookup("TimmyZero")
    assert isinstance(found, Efficiency)
    assert (found.name, found.ehp, found.combat_level) == ("TimmyZero", 148.5, 39)


async def test_an_untracked_player_says_how_to_fix_it():
    """A bare 404 tells you nothing; the account simply has not been registered."""
    async with client_for(responder()) as wom:
        with pytest.raises(WomError, match="Not tracked"):
            await wom.lookup("Nobody")


async def test_rate_limiting_is_named_as_such():
    async with client_for(responder(**{"/players/": httpx.Response(429)})) as wom:
        with pytest.raises(WomError, match="rate-limiting"):
            await wom.lookup("TimmyZero")


async def test_an_unreachable_api_is_not_a_traceback():
    def boom(request):
        raise httpx.ConnectError("no route")

    async with client_for(boom) as wom:
        with pytest.raises(WomError, match="Could not reach"):
            await wom.lookup("TimmyZero")


# -- gains ------------------------------------------------------------------


async def test_gains_exclude_the_overall_row():
    """It is the sum of the others. Listing it alongside them doubles every
    total and tops every ranking."""
    async with client_for(responder(**{"/gained": httpx.Response(200, json=GAINED)})) as wom:
        gains = await wom.gains("TimmyZero", period="week")
    assert gains.skills == {"fishing": 412_000}
    assert "overall" not in gains.skills


async def test_skills_that_did_not_move_are_left_out():
    async with client_for(responder(**{"/gained": httpx.Response(200, json=GAINED)})) as wom:
        gains = await wom.gains("TimmyZero", period="week")
    assert "mining" not in gains.skills


async def test_efficient_hours_come_from_the_overall_row():
    async with client_for(responder(**{"/gained": httpx.Response(200, json=GAINED)})) as wom:
        gains = await wom.gains("TimmyZero", period="week")
    assert gains.ehp_gained == 6.5


async def test_the_period_is_sent_to_the_api():
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=GAINED)

    async with client_for(handler) as wom:
        await wom.gains("TimmyZero", period="month")
    assert "period=month" in seen[0]


async def test_a_bogus_period_is_refused_before_the_request():
    """WOM answers an unrecognised period with an empty result, which reads
    exactly like a player who did nothing."""
    called = False

    def handler(request):
        nonlocal called
        called = True
        return httpx.Response(200, json=GAINED)

    async with client_for(handler) as wom:
        with pytest.raises(WomError, match="Period must be one of"):
            await wom.gains("TimmyZero", period="fortnight")
    assert not called


# -- tracking ---------------------------------------------------------------


async def test_tracking_registers_then_reads_back():
    calls: list[str] = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=PLAYER)

    async with client_for(handler) as wom:
        found = await wom.track("TimmyZero")
    assert calls == ["POST", "GET"]
    assert found.name == "TimmyZero"


async def test_a_name_the_hiscores_do_not_know_is_reported_clearly():
    def handler(request):
        return httpx.Response(400, json={"message": "Failed to load hiscores"})

    async with client_for(handler) as wom:
        with pytest.raises(WomError, match="has to match the hiscores"):
            await wom.track("nosuchplayer")


# -- summaries --------------------------------------------------------------


def test_a_summary_leads_with_efficiency():
    line = Efficiency(
        name="TimmyZero", ehp=148.5, ehb=0.0, exp=10_007_857, combat_level=39,
        account_type="regular", build="main", ttm=1620.4, tt200m=40_000.0,
    ).summary()
    assert "148 efficient hours played" in line
    assert "1,620 hours to max" in line


def test_gains_read_biggest_first_with_the_hours_it_cost():
    line = Gains(period="week", skills={"mining": 10, "fishing": 412_000}, ehp_gained=6.5).summary()
    assert line.startswith("XP in the last week: Fishing +412,000, Mining +10.")
    assert "6.5 efficient hours" in line


def test_a_quiet_week_is_said_plainly():
    assert Gains(period="week", skills={}, ehp_gained=0).summary() == (
        "No XP gained in the last week."
    )


# -- the fields that were being parsed and dropped --------------------------
# The payload carries 19 top-level keys and a full snapshot; nine were used.


def player_payload(**over):
    body = {
        "displayName": "TimmyZero", "type": "regular", "build": "main",
        "exp": 10_000_000, "ehp": 82.3, "ehb": 0, "ttm": 900.0, "tt200m": 0.0,
        "combatLevel": 39,
        "lastChangedAt": "2026-08-01T06:25:37.670Z",
        "latestSnapshot": {"data": {"skills": {
            "overall": {"metric": "overall", "ehp": 82.3},
            "fishing": {"metric": "fishing", "ehp": 72.9},
            "mining": {"metric": "mining", "ehp": 4.2},
            "runecraft": {"metric": "runecraft", "ehp": 0},
        }}},
    }
    body.update(over)
    return body


async def test_per_skill_hours_come_from_the_snapshot_already_in_the_payload():
    """The thing the hiscores genuinely cannot say: not what you have, but what
    it cost. It rides along in the same response and was being discarded."""
    async with client_for(lambda r: httpx.Response(200, json=player_payload())) as c:
        found = await c.lookup("TimmyZero")
    assert found.skill_hours == {"fishing": 72.9, "mining": 4.2}


async def test_overall_is_dropped_from_the_hours_like_it_is_from_gains():
    """It is the sum of the rest, so beside them it is every hour counted twice
    and always the largest."""
    async with client_for(lambda r: httpx.Response(200, json=player_payload())) as c:
        found = await c.lookup("TimmyZero")
    assert "overall" not in found.skill_hours


async def test_the_summary_says_where_the_hours_went():
    async with client_for(lambda r: httpx.Response(200, json=player_payload())) as c:
        found = await c.lookup("TimmyZero")
    assert "Fishing 73" in found.summary()


async def test_idle_time_is_measured_from_when_the_account_last_changed():
    """Nothing in the hiscores says this -- they are a snapshot with no history
    attached -- so it answers "have you been playing?" on day one."""
    async with client_for(lambda r: httpx.Response(200, json=player_payload())) as c:
        found = await c.lookup("TimmyZero")
    now = datetime(2026, 8, 9, tzinfo=UTC)
    assert found.idle_for(now).days == 7
    assert "nothing gained in the last 7 day(s)" in found.summary(now=now)


async def test_an_account_that_changed_today_is_not_called_idle():
    async with client_for(lambda r: httpx.Response(200, json=player_payload())) as c:
        found = await c.lookup("TimmyZero")
    now = datetime(2026, 8, 1, 12, tzinfo=UTC)
    assert "nothing gained" not in found.summary(now=now)


async def test_an_unreadable_timestamp_costs_the_idle_line_and_nothing_else():
    """Everything it feeds is a nicety; the efficiency figures beside it are
    the point."""
    async with client_for(
        lambda r: httpx.Response(200, json=player_payload(lastChangedAt="not a date"))
    ) as c:
        found = await c.lookup("TimmyZero")
    assert found.last_changed_at is None
    assert found.idle_for() is None
    assert "82 efficient hours played" in found.summary()


async def test_a_payload_with_no_snapshot_still_parses():
    """An account tracked but never synced has no latestSnapshot at all."""
    async with client_for(
        lambda r: httpx.Response(200, json=player_payload(latestSnapshot=None))
    ) as c:
        found = await c.lookup("TimmyZero")
    assert found.skill_hours == {} and found.ehp == 82.3
