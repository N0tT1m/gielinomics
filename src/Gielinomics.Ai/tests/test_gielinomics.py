"""Reading through the platform instead of upstream. No network.

Two things are being tested and they are not the same thing. The first is that
the platform's responses parse -- ordinary wire-shape work. The second is the
claim the whole integration rests on: that swapping the data source changes
*nothing else*. ``ge.py`` decides that "granite" means the rocks and not the
granite hammer, that a 2000x spread is noise, that a sale is taxed; those
decisions have to survive the swap, and the way to show it is to run the same
assertions against the subclass that ``test_ge.py`` runs against the parent.

The fixtures below are the platform's shapes, taken from the OpenAPI document
the C# side generates: camelCase, decimals as JSON numbers, instants as ISO-8601
with an offset.
"""

from __future__ import annotations

import httpx
import pytest

from reldo import gielinomics as gx
from reldo.clients import ge_client, hiscores_client, using_platform, wom_client
from reldo.config import Settings
from reldo.ge import GEError
from reldo.hiscores import HiscoresError

BASE = "http://api:8080"

# Same items as test_ge.py, because half the point is that the same judgements
# still come out. Sandstone (5kg) is the thin-market problem case.
def _item(id_: int, name: str, limit: int, high_alch: int, members: bool) -> dict:
    return {
        "id": id_,
        "name": name,
        "limit": limit,
        "highalch": high_alch,
        "members": members,
        "examine": "",
    }


MAPPING = [
    _item(453, "Coal", 13000, 27, False),
    _item(6975, "Sandstone (5kg)", 100, 0, True),
    _item(6983, "Granite (5kg)", 100, 0, True),
    _item(21742, "Granite hammer", 8, 64000, True),
]

LATEST = {
    "data": {
        "453": {"high": 149, "highTime": 1_700_000_000, "low": 149, "lowTime": 1_700_000_000},
        # A 2000x spread on thirteen trades. Real, and the reason ge.py has a
        # sanity check at all.
        "6975": {"high": 20000, "highTime": 1_700_000_000, "low": 10, "lowTime": 1_699_000_000},
        "6983": {"high": 713, "highTime": 1_700_000_000, "low": 713, "lowTime": 1_700_000_000},
        "21742": {
            "high": 10_500_000,
            "highTime": 1_700_000_000,
            "low": 10_400_000,
            "lowTime": 1_700_000_000,
        },
    },
    "timestamp": None,
}

DAY = {
    "data": {
        "453": {
            "avgHighPrice": 149,
            "avgLowPrice": 147,
            "highPriceVolume": 921_000,
            "lowPriceVolume": 977_359,
        },
        # Seven trades in a day: dead, and it has to still read as dead.
        "6975": {
            "avgHighPrice": 3000,
            "avgLowPrice": 2500,
            "highPriceVolume": 7,
            "lowPriceVolume": 6,
        },
        "6983": {
            "avgHighPrice": 713,
            "avgLowPrice": 700,
            "highPriceVolume": 41_000,
            "lowPriceVolume": 39_500,
        },
        "21742": {
            "avgHighPrice": 10_500_000,
            "avgLowPrice": 10_400_000,
            "highPriceVolume": 4,
            "lowPriceVolume": 3,
        },
    },
    "timestamp": 1_699_913_600,
}

# A week of hourly bars for coal, rising 120 -> 180. Deliberately a real move:
# a 50% climb is unambiguous, so a test that fails here is the arithmetic and
# not the banding.
SERIES = {
    "itemId": 453,
    "stepSeconds": 3600,
    "from": "2026-09-01T00:00:00+00:00",
    "to": "2026-09-08T00:00:00+00:00",
    "points": [
        {
            "bucketTs": f"2026-09-0{1 + day}T00:00:00+00:00",
            "avgHigh": 120 + day * 10,
            "avgLow": 118 + day * 10,
            "highVolume": 1000,
            "lowVolume": 900,
        }
        for day in range(7)
    ],
}

# One unranked account, for the fallback tests. Jagex's shape, not the platform's.
JAGEX = {
    "name": "Nobody",
    "skills": [{"name": "Attack", "rank": 1, "level": 5, "xp": 400}],
    "activities": [],
}

SNAPSHOT = {
    "player": "Zezima",
    "capturedAt": "2026-09-08T12:00:00+00:00",
    "lastSeenAt": "2026-09-09T12:00:00+00:00",
    "mappingVersion": 1,
    "payload": {
        "skills": [
            {"name": "Overall", "rank": 1, "level": 2277, "xp": 4_600_000_000},
            {"name": "Attack", "rank": 12, "level": 99, "xp": 200_000_000},
            {"name": "Hitpoints", "rank": 9, "level": 99, "xp": 200_000_000},
            {"name": "Defence", "rank": 15, "level": 99, "xp": 200_000_000},
            {"name": "Strength", "rank": 11, "level": 99, "xp": 200_000_000},
            {"name": "Prayer", "rank": 20, "level": 99, "xp": 200_000_000},
            {"name": "Magic", "rank": 22, "level": 99, "xp": 200_000_000},
            {"name": "Ranged", "rank": 18, "level": 99, "xp": 200_000_000},
        ],
        # The half skill_samples does not retain, and therefore the reason the
        # snapshot route exists at all rather than the history route serving.
        "activities": [
            {"name": "Abyssal Sire", "rank": 400, "score": 1200},
            {"name": "Clue Scrolls (master)", "rank": 88, "score": 310},
        ],
    },
}

GAINS = {
    "player": "Zezima",
    "period": "7.00:00:00",
    "overall": {"skill": 0, "name": "Overall", "gainedXp": 1_500_000},
    "skills": [
        {"skill": 4, "name": "Ranged", "gainedXp": 900_000, "startLevel": 98, "endLevel": 99},
        {"skill": 2, "name": "Strength", "gainedXp": 600_000, "startLevel": 97, "endLevel": 98},
        # Zero gain, and it must not reach the summary as "Magic +0".
        {"skill": 6, "name": "Magic", "gainedXp": 0, "startLevel": 90, "endLevel": 90},
    ],
}


def platform(*, fail: set[str] = frozenset(), seen: list[str] | None = None) -> httpx.MockTransport:
    """A stand-in for the C# API.

    ``fail`` names path fragments that should 503, which is how the fallback
    tests make the platform unavailable without making it unreachable -- the
    two are different code paths in ``_platform_get`` and both must fall back.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if seen is not None:
            seen.append(path)
        if any(fragment in path for fragment in fail):
            return httpx.Response(503, json={"title": "Unavailable"})
        if path == "/api/prices/mapping":
            return httpx.Response(200, json=MAPPING)
        if path == "/api/prices/latest":
            return httpx.Response(200, json=LATEST)
        if path == "/api/prices/24h":
            return httpx.Response(200, json=DAY)
        if path.endswith("/prices"):
            return httpx.Response(200, json=SERIES)
        if path.endswith("/snapshot"):
            return httpx.Response(200, json=SNAPSHOT)
        if path.endswith("/gains"):
            return httpx.Response(200, json=GAINS)
        if path.endswith("/track"):
            return httpx.Response(201, json={"displayName": "Zezima"})
        return httpx.Response(404, json={"title": "Not found"})

    return httpx.MockTransport(handle)


def upstream(payload: object = None, status: int = 200) -> httpx.MockTransport:
    """A stand-in for the wiki or Jagex, for the fallback path."""
    return httpx.MockTransport(lambda request: httpx.Response(status, json=payload))


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, user_agent="test (contact@example.com)", **overrides)


# ---------------------------------------------------------------------------
# The claim: swapping the source changes nothing above the transport.
# ---------------------------------------------------------------------------


async def test_prices_come_back_through_the_inherited_pipeline():
    async with gx.GEClient(BASE, transport=platform()) as ge:
        prices = await ge.lookup("coal")

    assert [p.item.name for p in prices] == ["Coal"]
    coal = prices[0]
    assert coal.instant_buy == 149
    assert coal.avg_sell == 147
    assert coal.volume == 921_000 + 977_359
    assert coal.liquidity == "liquid"


async def test_name_resolution_still_refuses_to_price_the_hammer():
    """The judgement that motivated the four-tier resolution in the first place.

    "granite" must mean the rock. Getting this wrong once had the bot answer a
    question about mining with a boss drop, and the tiers are the fix -- so the
    subclass has to inherit them intact, not merely compile.
    """
    async with gx.GEClient(BASE, transport=platform()) as ge:
        found = await ge.find("granite")

    assert [item.name for item in found] == ["Granite (5kg)"]


async def test_thin_market_nonsense_is_still_caught():
    async with gx.GEClient(BASE, transport=platform()) as ge:
        price = await ge.exactly("Sandstone (5kg)")

    assert price is not None
    assert price.erratic is True
    assert price.liquidity == "dead"


async def test_mapping_is_only_fetched_once():
    """The cache ge.py keeps must survive the subclass.

    It is the reason the bot shares one client: /mapping is every tradeable
    item in the game, and refetching it per price lookup is what the caching
    exists to stop.
    """
    seen: list[str] = []
    async with gx.GEClient(BASE, transport=platform(seen=seen)) as ge:
        await ge.lookup("coal")
        await ge.lookup("granite")

    assert seen.count("/api/prices/mapping") == 1


# ---------------------------------------------------------------------------
# The capability that is new: history.
# ---------------------------------------------------------------------------


async def test_trend_reads_a_direction_off_retained_bars():
    async with gx.GEClient(BASE, transport=platform()) as ge:
        trend = await ge.trend("coal", window="7d")

    assert trend is not None
    assert trend.samples == 7
    assert trend.start == pytest.approx(119)
    assert trend.end == pytest.approx(179)
    assert trend.change_percent == pytest.approx(50.4, abs=0.1)
    assert trend.direction == "rising sharply"


async def test_trend_resolves_the_name_the_same_way_pricing_does():
    async with gx.GEClient(BASE, transport=platform()) as ge:
        trend = await ge.trend("granite")

    assert trend is not None
    assert trend.item.name == "Granite (5kg)"


async def test_trend_is_none_for_something_that_is_not_an_item():
    async with gx.GEClient(BASE, transport=platform()) as ge:
        assert await ge.trend("a thing nobody sells") is None


async def test_a_flat_price_is_not_reported_as_a_trend():
    flat = dict(SERIES, points=[dict(point, avgHigh=100, avgLow=100) for point in SERIES["points"]])
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=MAPPING if request.url.path.endswith("mapping") else flat,
        )
    )
    async with gx.GEClient(BASE, transport=transport) as ge:
        trend = await ge.trend("coal")

    assert trend is not None
    assert trend.direction == "flat"


async def test_series_refuses_to_invent_an_empty_history():
    """No fallback on this one, and that is deliberate.

    Upstream has no history to fall back *to*, so an empty list would read as
    "this item has never traded" rather than "the platform is down".
    """
    async with gx.GEClient(BASE, transport=platform(fail={"/prices"})) as ge:
        with pytest.raises(gx.GielinomicsError):
            await ge.series(453)


@pytest.mark.parametrize("window,expected_hours", [("6h", 6), ("7d", 168), ("2w", 336)])
def test_windows_parse(window: str, expected_hours: int):
    assert gx._parse_window(window).total_seconds() == expected_hours * 3600


@pytest.mark.parametrize("window", ["", "d", "7", "7y", "seven days", "-7d"])
def test_an_unparseable_window_is_loud(window: str):
    """Silently defaulting would answer a question about the wrong period."""
    with pytest.raises(ValueError):
        gx._parse_window(window)


# ---------------------------------------------------------------------------
# Falling back.
# ---------------------------------------------------------------------------


async def test_a_dead_platform_costs_the_history_not_the_price():
    async with gx.GEClient(
        BASE,
        transport=platform(fail={"/api/prices"}),
        upstream_transport=upstream(MAPPING),
    ) as ge:
        found = await ge.find("coal")

    assert [item.name for item in found] == ["Coal"]


async def test_fallback_off_makes_a_misconfigured_url_loud():
    async with gx.GEClient(
        BASE, fallback=False, transport=platform(fail={"/api/prices"})
    ) as ge:
        with pytest.raises(GEError):
            await ge.find("coal")


async def test_an_unreachable_platform_falls_back_too():
    """Connection refused and HTTP 503 are different branches; both must fall back."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with gx.GEClient(
        BASE, transport=httpx.MockTransport(refuse), upstream_transport=upstream(MAPPING)
    ) as ge:
        assert await ge.find("coal")


# ---------------------------------------------------------------------------
# Hiscores.
# ---------------------------------------------------------------------------


async def test_stored_snapshots_keep_the_activity_counters():
    """The reason the snapshot route serves the payload rather than the samples.

    ``skill_samples`` retains index, rank, level and xp and drops activities
    entirely, so a caller reconstructing a Player from history would silently
    lose every boss kill and clue tier.
    """
    async with gx.HiscoresClient(BASE, transport=platform()) as hs:
        player = await hs.lookup("Zezima")

    assert player.name == "Zezima"
    assert player.level("Attack") == 99
    assert player.activities["Abyssal Sire"].score == 1200
    assert player.combat_level == 126


async def test_an_untracked_account_falls_through_to_jagex():
    async with gx.HiscoresClient(
        BASE,
        track=False,
        transport=platform(fail={"/snapshot"}),
        upstream_transport=upstream(JAGEX),
    ) as hs:
        player = await hs.lookup("Nobody")

    assert player.level("Attack") == 5


async def test_asking_about_a_stranger_enrols_them_for_next_time():
    seen: list[str] = []
    async with gx.HiscoresClient(
        BASE,
        token="secret",
        transport=platform(fail={"/snapshot"}, seen=seen),
        upstream_transport=upstream(JAGEX),
    ) as hs:
        await hs.lookup("Nobody")

    assert "/api/players/Nobody/track" in seen


async def test_a_failed_enrolment_does_not_cost_the_answer():
    async with gx.HiscoresClient(
        BASE,
        token="secret",
        transport=platform(fail={"/snapshot", "/track"}),
        upstream_transport=upstream(JAGEX),
    ) as hs:
        player = await hs.lookup("Nobody")

    assert player.level("Attack") == 5


async def test_an_empty_username_is_still_rejected_before_any_request():
    async with gx.HiscoresClient(BASE, transport=platform()) as hs:
        with pytest.raises(HiscoresError):
            await hs.lookup("   ")


# ---------------------------------------------------------------------------
# Wise Old Man: gains move, efficiency does not.
# ---------------------------------------------------------------------------


async def test_gains_come_from_the_platforms_own_snapshots():
    async with gx.WomClient(BASE, transport=platform()) as wom:
        gains = await wom.gains("Zezima", period="week")

    assert gains.skills == {"ranged": 900_000, "strength": 600_000}
    assert gains.ehp_gained == 0.0
    assert "Ranged +900,000" in gains.summary()


async def test_efficiency_is_not_routed_through_the_platform():
    """EHP is WOM's model, not an observation this platform records.

    The assertion is on ``seen``: the platform transport records every path it
    is asked for, so an empty list is proof the call went to Wise Old Man and
    not through an extra hop that could only have returned the same answer.
    """
    seen: list[str] = []
    async with gx.WomClient(
        BASE,
        transport=platform(seen=seen),
        upstream_transport=upstream(
            {
                "displayName": "Zezima",
                "type": "regular",
                "build": "main",
                "exp": 1,
                "latestSnapshot": None,
            }
        ),
    ) as wom:
        efficiency = await wom.lookup("Zezima")

    assert efficiency.name == "Zezima"
    assert seen == []


@pytest.mark.parametrize(
    "period,window", [("day", "1d"), ("week", "7d"), ("month", "30d"), ("year", "365d")]
)
def test_wom_periods_translate_to_platform_windows(period: str, window: str):
    assert gx._period_to_window(period) == window


# ---------------------------------------------------------------------------
# The factory: one decision, made once.
# ---------------------------------------------------------------------------


def test_no_url_configured_means_every_client_stays_upstream():
    plain = settings()
    assert using_platform(plain) is False
    assert type(ge_client(plain)) is not gx.GEClient
    assert type(hiscores_client(plain)) is not gx.HiscoresClient


def test_a_url_switches_all_of_them_together():
    """Together is the property worth testing.

    A configuration where the agent reads platform prices and /ge reads the
    wiki's is one where the same conversation can quote two prices for a whip.
    """
    wired = settings(gielinomics_url=BASE)
    assert using_platform(wired) is True
    assert isinstance(ge_client(wired), gx.GEClient)
    assert isinstance(hiscores_client(wired), gx.HiscoresClient)
    assert isinstance(wom_client(wired), gx.WomClient)


def test_wom_switched_off_yields_nothing_rather_than_a_broken_client():
    assert wom_client(settings(gielinomics_url=BASE, wom_enabled=False)) is None


def test_tracking_needs_a_token():
    """Enrolment is authenticated, so tracking without one would 401 every time."""
    client = hiscores_client(settings(gielinomics_url=BASE, gielinomics_track=True))
    assert client._track is False


def test_a_trailing_slash_does_not_double_up_in_paths():
    client = ge_client(settings(gielinomics_url=BASE + "/"))
    assert client._base_url == BASE
