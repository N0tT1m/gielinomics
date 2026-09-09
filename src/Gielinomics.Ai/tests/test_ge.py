"""GE price tests against a stubbed prices API. No network.

The numbers in here are not invented. They were pulled from the live API while
the module was written, because the whole reason this code is more than a
dict lookup is that real thin markets produce absurd data: Sandstone (5kg)
really did report a last-sell of 10gp against a last-buy of 20,000gp on thirteen
trades in a day. A stub with tidy numbers would test nothing.
"""

from __future__ import annotations

import httpx
import pytest

from reldo.ge import GEClient, GEError, Item, Price, compare, tax_on

# Live snapshot. Coal is the liquid control; the rocks are the problem cases.
MAPPING = [
    {"id": 453, "name": "Coal", "limit": 13000, "highalch": 27, "members": False},
    {"id": 6971, "name": "Sandstone (1kg)", "limit": 100, "highalch": 0, "members": True},
    {"id": 6975, "name": "Sandstone (5kg)", "limit": 100, "highalch": 0, "members": True},
    {"id": 6977, "name": "Sandstone (10kg)", "limit": 100, "highalch": 0, "members": True},
    {"id": 6981, "name": "Granite (2kg)", "limit": 100, "highalch": 0, "members": True},
    {"id": 6983, "name": "Granite (5kg)", "limit": 100, "highalch": 0, "members": True},
    {"id": 4153, "name": "Granite maul", "limit": 70, "highalch": 50000, "members": True},
    {"id": 21742, "name": "Granite hammer", "limit": 8, "highalch": 64000, "members": True},
    {"id": 139, "name": "Prayer potion(4)", "limit": 2000, "highalch": 0, "members": True},
]

LATEST = {
    "453": {"high": 149, "low": 149},
    "6971": {"high": 4, "low": 4},
    "6975": {"high": 20000, "low": 10},  # 2000x spread on 13 trades
    "6977": {"high": 2976, "low": 2000},
    "6981": {"high": 60, "low": 60},
    "6983": {"high": 713, "low": 713},
    "4153": {"high": 98000, "low": 96001},
    "21742": {"high": 10500000, "low": 10400000},
    "139": {"high": 9018, "low": 9000},
}

DAY = {
    "453": {"avgHighPrice": 149, "avgLowPrice": 147,
            "highPriceVolume": 921_000, "lowPriceVolume": 977_359},
    "6971": {"avgHighPrice": None, "avgLowPrice": 7,
             "highPriceVolume": 1, "lowPriceVolume": 14},
    "6975": {"avgHighPrice": None, "avgLowPrice": 80,
             "highPriceVolume": 0, "lowPriceVolume": 13},
    "6977": {"avgHighPrice": 3890, "avgLowPrice": 2387,
             "highPriceVolume": 63, "lowPriceVolume": 64},
    "6981": {"avgHighPrice": 90, "avgLowPrice": 27,
             "highPriceVolume": 124, "lowPriceVolume": 51},
    "6983": {"avgHighPrice": 848, "avgLowPrice": 240,
             "highPriceVolume": 302, "lowPriceVolume": 52},
    "4153": {"avgHighPrice": 100913, "avgLowPrice": 98770,
             "highPriceVolume": 3300, "lowPriceVolume": 3399},
    "21742": {"avgHighPrice": 10500000, "avgLowPrice": 10487502,
              "highPriceVolume": 1170, "lowPriceVolume": 1170},
    "139": {"avgHighPrice": 9018, "avgLowPrice": 9000,
            "highPriceVolume": 705_000, "lowPriceVolume": 705_167},
}


def handler(request):
    path = request.url.path
    if path.endswith("/mapping"):
        return httpx.Response(200, json=MAPPING)
    if path.endswith("/latest"):
        return httpx.Response(200, json={"data": LATEST})
    if path.endswith("/24h"):
        return httpx.Response(200, json={"data": DAY})
    return httpx.Response(404, text="no such endpoint")


def client(h=handler) -> GEClient:
    return GEClient("reldo/test (local)", transport=httpx.MockTransport(h))


def catalogue(rows) -> GEClient:
    """A client whose /mapping is exactly these items."""

    def h(request):
        if request.url.path.endswith("/mapping"):
            return httpx.Response(200, json=rows)
        return httpx.Response(200, json={"data": {}})

    return GEClient("reldo/test (local)", transport=httpx.MockTransport(h))


def price(**kw) -> Price:
    base = dict(
        item=Item(id=1, name="Thing", limit=100, high_alch=0, members=True),
        instant_sell=100, instant_buy=100, avg_sell=100, avg_buy=100, volume=50_000,
    )
    return Price(**{**base, **kw})


# -- liquidity grading -----------------------------------------------------


@pytest.mark.parametrize(
    "volume,expected",
    [(1_898_359, "liquid"), (10_000, "liquid"), (6_699, "thin"),
     (1_000, "thin"), (354, "illiquid"), (100, "illiquid"), (13, "dead"), (0, "dead")],
)
def test_liquidity_grades(volume, expected):
    assert price(volume=volume).liquidity == expected


# -- the garbage-price problem ---------------------------------------------


def test_erratic_spread_is_flagged():
    """Sandstone (5kg), live: sold for 10, bought for 20,000, on 13 trades."""
    p = price(instant_sell=10, instant_buy=20_000, volume=13)
    assert p.erratic
    assert any("2,000x" in w for w in p.price_warnings())


def test_normal_spread_is_not_flagged():
    """Every liquid item measured sat between 1.00x and 1.05x."""
    assert not price(instant_sell=766_763, instant_buy=783_994).erratic


def test_estimate_prefers_the_daily_average_over_a_single_trade():
    """The core anti-garbage decision: one lunatic paying 20,000 for a rock
    must not become the reported price."""
    p = price(instant_sell=10, instant_buy=20_000, avg_sell=80, avg_buy=None, volume=13)
    assert p.estimate == 80


def test_estimate_falls_back_to_latest_when_the_item_did_not_trade():
    p = price(avg_sell=None, avg_buy=None, instant_sell=500, instant_buy=600)
    assert p.estimate == 500


def test_estimate_is_none_when_there_is_no_data_at_all():
    p = price(avg_sell=None, avg_buy=None, instant_sell=None, instant_buy=None)
    assert p.estimate is None
    assert p.daily_turnover is None
    assert "no price data" in p.summary()


def test_zero_volume_is_reported_as_stale_not_as_a_price():
    p = price(volume=0)
    assert "stale" in (p.liquidity_warning() or "")


def test_diverging_daily_averages_are_flagged():
    """Granite (5kg), live: 240 average sell against 848 average buy."""
    p = price(avg_sell=240, avg_buy=848, instant_sell=713, instant_buy=713, volume=354)
    assert any("patience" in w for w in p.price_warnings())


def test_liquidity_and_price_warnings_stay_separate():
    """compare() shows volume as a column, so it must be able to drop the
    volume warning without losing the ones a table cannot express."""
    p = price(instant_sell=10, instant_buy=20_000, avg_sell=80, avg_buy=None, volume=13)
    assert p.liquidity_warning() is not None
    assert p.liquidity_warning() not in p.price_warnings()
    assert p.warnings() == [p.liquidity_warning(), *p.price_warnings()]


def test_turnover_multiplies_price_by_volume():
    assert price(avg_sell=2387, volume=127).daily_turnover == 2387 * 127


# -- name resolution -------------------------------------------------------


async def test_exact_name_wins_over_longer_matches():
    async with client() as ge:
        found = await ge.find("Coal")
    assert [i.name for i in found] == ["Coal"]


async def test_partial_name_expands_to_every_variant():
    async with client() as ge:
        found = await ge.find("sandstone")
    assert {i.name for i in found} == {
        "Sandstone (1kg)", "Sandstone (5kg)", "Sandstone (10kg)"
    }


async def test_parenthesised_variants_exclude_items_that_merely_contain_the_word():
    """The regression that made the answer nonsense: asked "sandstone or
    granite", the comparison ranked by gp/day and named the Granite hammer --
    a boss drop -- to someone plainly asking what to mine."""
    async with client() as ge:
        found = await ge.find("granite")
    assert [i.name for i in found] == ["Granite (2kg)", "Granite (5kg)"]


async def test_variant_tier_handles_the_no_space_potion_convention():
    async with client() as ge:
        found = await ge.find("prayer potion")
    assert [i.name for i in found] == ["Prayer potion(4)"]


async def test_substring_matching_still_works_when_nothing_better_matches():
    async with client() as ge:
        found = await ge.find("maul")
    assert [i.name for i in found] == ["Granite maul"]


async def test_prefix_beats_substring_when_there_are_no_variants():
    async with client() as ge:
        found = await ge.find("granite h")
    assert [i.name for i in found] == ["Granite hammer"]


async def test_empty_query_finds_nothing():
    async with client() as ge:
        assert await ge.find("  ") == []


async def test_unknown_item_finds_nothing():
    async with client() as ge:
        assert await ge.lookup("dragon claws of infinite gp") == []


# -- fetching --------------------------------------------------------------


async def test_lookup_joins_mapping_latest_and_volume():
    async with client() as ge:
        (coal,) = await ge.lookup("Coal")
    assert coal.item.limit == 13000
    assert coal.instant_sell == 149
    assert coal.avg_sell == 147
    assert coal.volume == 921_000 + 977_359
    assert coal.liquidity == "liquid"
    assert coal.warnings() == []


async def test_missing_price_rows_do_not_crash_the_join():
    """An item in /mapping with no entry in /latest is normal for dead items."""
    async def sparse(request):
        if request.url.path.endswith("/mapping"):
            return httpx.Response(200, json=MAPPING)
        return httpx.Response(200, json={"data": {}})

    async with client(sparse) as ge:
        (coal,) = await ge.lookup("Coal")
    assert coal.estimate is None
    assert coal.volume == 0


async def test_whole_market_endpoints_are_fetched_once_per_window():
    """Comparing eight items must not be sixteen requests."""
    seen: list[str] = []

    def counting(request):
        seen.append(request.url.path)
        return handler(request)

    async with client(counting) as ge:
        await ge.lookup("sandstone")
        await ge.lookup("granite")
    assert seen.count("/api/v1/osrs/latest") == 1
    assert seen.count("/api/v1/osrs/24h") == 1
    assert seen.count("/api/v1/osrs/mapping") == 1


async def test_http_error_becomes_a_readable_gerror():
    async with client(lambda r: httpx.Response(503, text="down")) as ge:
        with pytest.raises(GEError, match="503"):
            await ge.lookup("Coal")


async def test_transport_failure_becomes_a_readable_gerror():
    def boom(request):
        raise httpx.ConnectError("no route to host")

    async with client(boom) as ge:
        with pytest.raises(GEError, match="Could not reach"):
            await ge.lookup("Coal")


# -- the ranking table -----------------------------------------------------


async def test_compare_states_the_verdict_by_gp_per_day_not_unit_count():
    """The whole reason the verdict is computed here. mistral-small3.2:24b, given
    the table alone, picked 354 granite at 240gp over 127 sandstone at 2,387gp
    and cited "84,960 vs 303,149 gp/day" as its reasoning -- right numbers,
    opposite conclusion. Sandstone moves 3.5x the gp and must win."""
    async with client() as ge:
        rows = await ge.lookup("sandstone") + await ge.lookup("granite")
    verdict = compare(rows).splitlines()[0]
    assert verdict.startswith("ANSWER: Sandstone (10kg) is the best")
    # Post-tax: 2,387 - 47 = 2,340 net, x127 traded.
    assert "297,180 gp/day" in verdict


async def test_verdict_admits_when_even_the_winner_is_illiquid():
    async with client() as ge:
        rows = await ge.lookup("sandstone") + await ge.lookup("granite")
    assert "least-bad option" in compare(rows).splitlines()[0]


async def test_verdict_does_not_hedge_on_a_genuinely_liquid_winner():
    async with client() as ge:
        rows = await ge.lookup("Coal") + await ge.lookup("prayer potion")
    verdict = compare(rows).splitlines()[0]
    assert "Prayer potion(4)" in verdict
    assert "least-bad" not in verdict


async def test_compare_sorts_the_table_the_same_way_the_verdict_ranks():
    """A table ordered one way with a verdict chosen another invites the model
    to 'correct' the verdict from the rows."""
    async with client() as ge:
        rows = await ge.lookup("sandstone") + await ge.lookup("granite")
    table = compare(rows)
    body = [ln for ln in table.splitlines() if ln and not ln.startswith(("ANSWER", "!", "item"))]
    turnovers = [int(ln.split()[-2].replace(",", "")) for ln in body[:4]]
    assert turnovers == sorted(turnovers, reverse=True)


async def test_compare_of_a_single_item_returns_its_full_summary():
    """No verdict line for a one-item 'comparison' -- there is nothing to rank,
    and "ANSWER: Coal is the best of these" reads as a recommendation."""
    async with client() as ge:
        rows = await ge.lookup("Coal")
    out = compare(rows)
    assert out == rows[0].summary()
    assert "ANSWER" not in out


async def test_compare_omits_the_volume_warning_it_already_shows_as_a_column():
    async with client() as ge:
        rows = await ge.lookup("sandstone")
    table = compare(rows)
    assert "traded in 24h -- you cannot reliably sell" not in table
    assert "2,000x" in table  # the one the table cannot express survives


async def test_compare_calls_out_an_all_illiquid_field():
    async with client() as ge:
        rows = await ge.lookup("sandstone")
    assert "Every item here is an illiquid market" in compare(rows)


async def test_compare_counts_the_thin_ones_in_a_mixed_field():
    async with client() as ge:
        rows = await ge.lookup("sandstone") + await ge.lookup("Coal")
    table = compare(rows)
    assert "3 of 4 barely trade" in table


def test_compare_handles_an_empty_result():
    assert compare([]) == "No matching tradeable items."



# -- Grand Exchange tax ----------------------------------------------------
# Rules verified from the wiki's "Convenience fee and item sink" section: 2%,
# capped at 5,000,000/item, rounding down so sub-50gp items pay nothing.


def test_tax_is_two_percent_rounded_down():
    assert tax_on(2387, "Sandstone (10kg)") == 47   # 47.74 floors to 47
    assert tax_on(1000, "Coal") == 20


def test_items_under_fifty_gp_pay_nothing():
    """Not a special case in the code -- 2% of 49 is 0.98 and tax floors to the
    whole coin. The boundary is exact and players notice it."""
    assert tax_on(49, "Granite (2kg)") == 0
    assert tax_on(50, "Granite (2kg)") == 1


def test_tax_is_capped_per_item():
    assert tax_on(1_000_000_000, "Twisted bow") == 5_000_000


def test_exempt_items_pay_nothing_at_any_price():
    assert tax_on(10_000_000, "Old school bond") == 0
    assert tax_on(200, "Lobster") == 0


def test_exempt_names_are_spelled_the_way_the_prices_api_spells_them():
    """The wiki lists "Energy potion" and "Varrock teleport"; /mapping has
    "Energy potion(4)" and "Varrock teleport (tablet)". Encoding the wiki's
    display names produces a set that silently never matches."""
    assert tax_on(300, "Energy potion(4)") == 0
    assert tax_on(300, "Varrock teleport (tablet)") == 0
    # A different item that merely looks similar is not exempt.
    assert tax_on(300, "West ardougne teleport (tablet)") > 0


def test_net_estimate_is_what_the_seller_receives():
    p = price(avg_sell=2387, item=Item(6977, "Sandstone (10kg)", 100, 0, True))
    assert p.estimate == 2387          # market value, for "what is it worth"
    assert p.net_estimate == 2340      # what you get, for "what should I sell"
    assert p.tax == 47


def test_ranking_uses_post_tax_income():
    """A taxed item and an untaxed one ranked on gross price are two different
    numbers compared as though they were one."""
    taxed = price(avg_sell=100, volume=1000, item=Item(1, "Coal", 13000, 27, False))
    exempt = price(avg_sell=100, volume=1000, item=Item(2, "Lobster", 13000, 0, False))
    assert exempt.net_daily_income > taxed.net_daily_income


def test_summary_reports_the_tax_when_it_bites():
    p = price(avg_sell=2387, item=Item(6977, "Sandstone (10kg)", 100, 0, True))
    assert "after 47 GE tax" in p.summary()


def test_summary_says_so_when_an_item_is_exempt():
    p = price(avg_sell=200, item=Item(379, "Lobster", 13000, 0, False))
    assert "exempt from GE tax" in p.summary()


async def test_a_plural_finds_the_singular_item():
    """The catalogue names one of a thing and people ask for several. Every
    match tier fails on the trailing s -- "shark" does not start with "sharks"
    and "sharks" is not a substring of "shark" -- and the miss was then reported
    as "Sharks are untradeable and have no GE price"."""
    ge = catalogue([
        {"id": 385, "name": "Shark", "limit": 11000},
        {"id": 379, "name": "Lobster", "limit": 13000},
    ])
    async with ge:
        assert [i.name for i in await ge.find("sharks")] == ["Shark"]
        assert [i.name for i in await ge.find("lobsters")] == ["Lobster"]


async def test_an_item_whose_name_ends_in_s_is_untouched():
    """'Yew logs' is the item's actual name. It matches exactly on the first
    tier and must never reach the singular fallback, which would look for
    'Yew log' and find nothing."""
    ge = catalogue([
        {"id": 1515, "name": "Yew logs", "limit": 15000},
        {"id": 315, "name": "Shrimps", "limit": 6000},
    ])
    async with ge:
        assert [i.name for i in await ge.find("yew logs")] == ["Yew logs"]
        assert [i.name for i in await ge.find("shrimps")] == ["Shrimps"]


async def test_a_genuine_miss_still_returns_nothing():
    """The fallback must not invent a match by chopping letters off."""
    ge = catalogue([{"id": 385, "name": "Shark"}])
    async with ge:
        assert await ge.find("gragglefloop") == []


async def test_a_space_in_a_compound_name_still_finds_it():
    """The catalogue closes the compound up and people do not. "sword fish" is
    not a prefix of "swordfish" and not a substring of it either, so every tier
    fails on the space."""
    ge = catalogue([{"id": 373, "name": "Swordfish"}, {"id": 371, "name": "Raw swordfish"}])
    async with ge:
        assert [i.name for i in await ge.find("sword fish")] == ["Swordfish"]
        assert [i.name for i in await ge.find("Sword-Fish")] == ["Swordfish"]


async def test_an_es_plural_finds_the_singular():
    """A fish, a bush and a box all take the longer ending. Chopping one letter
    leaves 'swordfishe', which matches nothing, and the miss is indistinguishable
    from a real one."""
    ge = catalogue([{"id": 373, "name": "Swordfish"}, {"id": 1963, "name": "Banana"}])
    async with ge:
        assert [i.name for i in await ge.find("swordfishes")] == ["Swordfish"]
        assert [i.name for i in await ge.find("sword fishes")] == ["Swordfish"]


async def test_squashing_never_shadows_a_real_name():
    """It runs after every ordinary tier, so an item that genuinely matches on
    its own spelling is never displaced by one that only matches squashed."""
    ge = catalogue([
        {"id": 1, "name": "Dragon bones"},
        {"id": 2, "name": "Dragonbone necklace"},
    ])
    async with ge:
        assert [i.name for i in await ge.find("dragon bones")] == ["Dragon bones"]


# -- pricing a purchase rather than a sale -----------------------------------


def test_buy_estimate_is_the_other_side_of_the_book():
    """Selling planks realises the low side; buying 20,202 of them costs the
    high one. Pricing a shopping list off estimate() understates it."""
    p = price(avg_sell=1_839, avg_buy=1_876, instant_sell=1_800, instant_buy=1_900)
    assert p.estimate == 1_839
    assert p.buy_estimate == 1_876


def test_buy_estimate_falls_back_through_to_the_sell_side():
    """A material that only trades one way is still worth pricing -- badly is
    what "no data at all" is for, and one side is not that."""
    assert price(avg_buy=None, instant_buy=None, avg_sell=700).buy_estimate == 700
    assert price(avg_buy=None, instant_buy=None, avg_sell=None,
                 instant_sell=None).buy_estimate is None


PLANKS = [
    {"id": 8782, "name": "Mahogany plank", "limit": 13000, "highalch": 1, "members": True},
    {"id": 8780, "name": "Teak plank", "limit": 13000, "highalch": 1, "members": True},
    {"id": 960, "name": "Plank", "limit": 13000, "highalch": 1, "members": False},
    {"id": 24884, "name": "Mahogany plank pack", "limit": 100, "highalch": 1,
     "members": True},
    {"id": 1515, "name": "Redwood logs", "limit": 15000, "highalch": 1, "members": True},
]


async def test_exactly_takes_the_item_that_is_the_name_and_no_near_miss():
    async with catalogue(PLANKS) as ge:
        found = await ge.exactly("mahogany plank")
        assert found is not None and found.item.name == "Mahogany plank"
        # ...and not the pack, which is a different item at a hundred times the
        # price. A wrong price in a shopping list multiplies.
        assert (await ge.exactly("mahogany plan")) is None


async def test_exactly_allows_a_plural_because_the_catalogue_is_inconsistent():
    """The guide says "2 redwood logs each" and the catalogue says "Redwood
    logs"; it says "6 teak planks" and the catalogue says "Teak plank"."""
    async with catalogue(PLANKS) as ge:
        assert (await ge.exactly("redwood log")).item.name == "Redwood logs"
        assert (await ge.exactly("teak plank")).item.name == "Teak plank"


async def test_a_generic_material_does_not_price_as_the_specific_one():
    """A guide naming "plank" means the ordinary plank, not the mahogany one.
    find() would offer both; exactly() takes the item that is the name."""
    async with catalogue(PLANKS) as ge:
        assert (await ge.exactly("plank")).item.name == "Plank"


async def test_cost_lines_are_silent_when_there_is_no_client_or_nothing_to_price():
    from reldo.ge import cost_lines

    assert await cost_lines(None, 20_202, "mahogany plank") == []
    async with catalogue(PLANKS) as ge:
        assert await cost_lines(ge, 0, "mahogany plank") == []
        assert await cost_lines(ge, 20_202, "") == []
        # An item the catalogue does not have is not a reason to lose the plan.
        assert await cost_lines(ge, 20_202, "adamantite plank") == []


async def test_a_prices_api_that_is_down_costs_the_price_and_not_the_plan():
    def boom(request):
        if request.url.path.endswith("/mapping"):
            return httpx.Response(200, json=PLANKS)
        return httpx.Response(503, text="down")

    from reldo.ge import cost_lines

    async with GEClient("reldo/test (local)", transport=httpx.MockTransport(boom)) as ge:
        assert await cost_lines(ge, 20_202, "mahogany plank") == []
