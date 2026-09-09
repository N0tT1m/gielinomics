"""Coin arithmetic tests. Pure functions, no network.

Anchored on the answer that produced the module. Asked "how many sharks do I
need to get 5 mill and how long will that take farming minnows", the agent said
375 minnows, 9 sharks, 29.1 hours. The real figures are below, and they are the
regression test: not "does it return a number" but "does it return *these*".
"""

from __future__ import annotations

import pytest

from reldo.money import (
    contradicts_itself,
    from_market,
    funded_by,
    hours_for_goal,
    items_for_goal,
    parse_goal,
    plan,
    shopping_list,
)

# The minnow guide's own numbers, from the page the agent had already read.
SHARK_PRICE = 717
MINNOWS_PER_SHARK = 40
MINNOW_GP_HR = 448_125


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("how many sharks do i need to get 5 mill", 5_000_000),
        ("i want 5m", 5_000_000),
        ("5 million gp", 5_000_000),
        ("500k", 500_000),
        ("1.5b", 1_500_000_000),
        ("save up 5,000,000 gp", 5_000_000),
        ("how do i make 100k gold", 100_000),
    ],
)
def test_reads_the_goal_people_actually_type(text, expected):
    assert parse_goal(text) == expected


def test_mill_with_two_ls_is_five_million():
    """The spelling that broke it. "5 mill" parsed as five coins, because the
    suffix pattern could match "mil" but not the trailing l, so it backtracked
    to no suffix at all -- and five coins divides into a gp/hr rate to give an
    answer that looks like a rounding error rather than a factor of a million."""
    assert parse_goal("5 mill") == 5_000_000
    assert parse_goal("5 mil") == 5_000_000
    assert parse_goal("5 million") == 5_000_000


@pytest.mark.parametrize(
    "text",
    [
        "How many planks are needed to go from level 37 to 70 Construction?",
        "what sailing level do i need to catch marlin",
        "how long from 45 to 99 mining",
        "what should i train next",
    ],
)
def test_questions_without_a_coin_goal_return_none(text):
    """The routing depends on this. A false positive here hands a Construction
    question to the money path and reads "70" as a seventy-coin target."""
    assert parse_goal(text) is None


def test_the_shark_answer():
    """The question that started it, computed rather than recalled."""
    assert items_for_goal(5_000_000, SHARK_PRICE) == 6_974
    assert hours_for_goal(5_000_000, MINNOW_GP_HR) == pytest.approx(11.16, rel=0.01)


def test_the_wrong_answer_is_caught_as_self_contradictory():
    """9 sharks at 717 gp is 6,453 gp, not 5,000,000. The number-grounding
    guard passes this whenever a 9 appears anywhere on the pages read; this is
    the check that does not."""
    assert contradicts_itself(9, SHARK_PRICE, 5_000_000)
    assert not contradicts_itself(6_974, SHARK_PRICE, 5_000_000)


def test_rounding_and_ranges_are_not_called_contradictions():
    """People round to "about 7,000" and guides quote ranges. Firing on those
    would make the check noise, and noise gets switched off."""
    assert not contradicts_itself(7_000, SHARK_PRICE, 5_000_000)


def test_plan_states_the_exchange_rate_leg():
    out = plan(
        5_000_000,
        gp_each=SHARK_PRICE,
        gp_per_hour=MINNOW_GP_HR,
        item_name="sharks",
        inputs_per_item=MINNOWS_PER_SHARK,
        input_name="minnows",
    )
    assert "6,974 sharks" in out
    assert "278,960 minnows" in out
    assert "11.2 hours" in out


def test_plan_does_not_emit_the_original_wrong_numbers():
    out = plan(5_000_000, gp_each=SHARK_PRICE, gp_per_hour=MINNOW_GP_HR, item_name="sharks")
    assert "375" not in out
    assert "29.1" not in out


def test_from_market_counts_against_the_net_price():
    """Tax is per item, so a goal met in cheap items costs more units than the
    gross price says. Counting on gross is the bug plan.md calls Phase 0."""
    gross = from_market(1_000_000, item_name="Shark", net_each=1_000, gross_each=1_000)
    taxed = from_market(1_000_000, item_name="Shark", net_each=980, gross_each=1_000)
    assert "1,000 x Shark" in gross
    assert "1,021 x Shark" in taxed


def test_from_market_warns_when_the_goal_exceeds_the_market():
    thin = from_market(
        5_000_000, item_name="Sandstone (10kg)", net_each=2_339, volume=127
    )
    assert "more than the market will absorb" in thin
    liquid = from_market(5_000_000, item_name="Shark", net_each=703, volume=21_315)
    assert "more than the market will absorb" not in liquid


def test_zero_and_negative_inputs_raise_rather_than_divide():
    with pytest.raises(ValueError):
        items_for_goal(5_000_000, 0)
    with pytest.raises(ValueError):
        hours_for_goal(5_000_000, 0)
    with pytest.raises(ValueError):
        plan(0, gp_each=717)


def test_a_tiny_share_of_a_huge_market_does_not_render_as_zero():
    """5,108 sharks against 3.4m traded a day is 0.15%, and "0% of daily volume"
    reads as a broken number rather than as the go-ahead it is."""
    out = from_market(5_000_000, item_name="Shark", net_each=979, volume=3_420_844)
    assert "under 1% of daily volume" in out
    assert "0% of daily volume" not in out


# -- what a training plan's materials cost -----------------------------------


def test_a_shopping_list_is_priced_and_totalled():
    """20,202 mahogany planks at the live buy price. The multiplication is the
    whole point: it is the step the model got wrong by three orders of
    magnitude, and the one nobody checks by eye."""
    lines = shopping_list(20_202, item_name="mahogany plank", gp_each=1_876)
    assert lines[0] == (
        "at 1,876 gp each: 37,898,952 gp for the mahogany planks"
    )


def test_nothing_to_price_prints_nothing():
    assert shopping_list(0, item_name="mahogany plank", gp_each=1_876) == []
    assert shopping_list(20_202, item_name="mahogany plank", gp_each=0) == []


def test_a_buy_limit_says_how_many_windows_it_takes():
    """13,000 planks per 4 hours against a 20,202-plank list is two sittings,
    which is a fact about the plan and not about the price."""
    lines = shopping_list(
        20_202, item_name="mahogany plank", gp_each=1_876, buy_limit=13_000
    )
    assert "2 windows to buy that many" in lines[1]


def test_one_window_is_not_worth_saying():
    lines = shopping_list(
        5_000, item_name="mahogany plank", gp_each=1_876, buy_limit=13_000
    )
    assert len(lines) == 1


def test_wanting_more_than_the_market_moves_in_a_day_is_said_plainly():
    """Arithmetic rather than a liquidity grade: 60,346 redwood logs against
    40,000 traded a day is several days of buying however that volume grades."""
    lines = shopping_list(
        60_346, item_name="redwood log", gp_each=340, volume=40_000
    )
    assert any("more than the 40,000 traded" in line for line in lines)


def test_a_liquid_market_gets_no_caveat():
    lines = shopping_list(
        20_202, item_name="mahogany plank", gp_each=1_876, volume=5_155_802
    )
    assert lines == ["at 1,876 gp each: 37,898,952 gp for the mahogany planks"]


def test_the_market_s_own_warnings_come_through():
    lines = shopping_list(
        100, item_name="granite (5kg)", gp_each=713, warnings=["only 354 traded in 24h"]
    )
    assert lines[-1] == "only 354 traded in 24h"


def test_a_bill_is_converted_into_something_to_sell():
    """The third part of "how many planks, how much money, how many sharks",
    and the part the model answered by inventing 1,311 against a 38M gp bill it
    had just printed."""
    lines = funded_by(38_444_406, item_name="shark", net_each=980)
    assert lines[0] == "39,229 sharks sold at 980 gp each after tax pays for that"


def test_the_sale_is_counted_net_of_tax():
    """Tax falls on the seller, so this side of the trade pays it -- 39,229
    sharks rather than 38,444, which is 785,000 gp of difference."""
    taxed = funded_by(38_444_406, item_name="shark", net_each=980)
    untaxed = funded_by(38_444_406, item_name="shark", net_each=1_000)
    assert "39,229" in taxed[0]
    assert "38,445" in untaxed[0]


def test_a_market_too_small_to_absorb_the_sale_says_so():
    lines = funded_by(
        38_444_406, item_name="mole claw", net_each=980, volume=1_200
    )
    assert "only 1,200 mole claws trade in a day" in lines[1]


def test_nothing_to_fund_prints_nothing():
    assert funded_by(0, item_name="shark", net_each=980) == []
    assert funded_by(38_444_406, item_name="shark", net_each=0) == []
