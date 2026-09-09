"""XP arithmetic tests. Pure functions, no network.

These matter more than they look. The whole point of `skills.py` is that a 24B
model cannot be trusted with seven-digit arithmetic -- so if the table is wrong,
we've replaced an unreliable estimate with a confident wrong answer, which is
worse. Reference values are Jagex's published XP table.
"""

from __future__ import annotations

import pytest

from reldo.skills import (
    MAX_LEVEL,
    actions_needed,
    hours_needed,
    level_at_xp,
    plan,
    xp_between,
    xp_for_level,
)

# Published OSRS XP table. If any of these drift, the table is broken.
REFERENCE = {
    1: 0, 2: 83, 3: 174, 10: 1_154, 20: 4_470, 30: 13_363, 40: 37_224,
    50: 101_333, 60: 273_742, 70: 737_627, 80: 1_986_068, 90: 5_346_332,
    92: 6_517_253, 99: 13_034_431,
}


@pytest.mark.parametrize(("level", "expected"), sorted(REFERENCE.items()))
def test_xp_table_matches_published_values(level, expected):
    assert xp_for_level(level) == expected


def test_level_92_is_half_of_99():
    """The well-known result: 92 is halfway to 99 in XP terms."""
    assert xp_for_level(92) == pytest.approx(xp_for_level(99) / 2, rel=0.001)


def test_level_at_xp_is_the_inverse_of_xp_for_level():
    for level in range(1, MAX_LEVEL + 1):
        assert level_at_xp(xp_for_level(level)) == level


def test_level_at_xp_boundary_is_exact():
    """One XP short of 99 must be 98, not 99."""
    assert level_at_xp(xp_for_level(99) - 1) == 98
    assert level_at_xp(xp_for_level(99)) == 99


def test_level_at_xp_handles_zero_and_the_cap():
    assert level_at_xp(0) == 1
    assert level_at_xp(200_000_000) == MAX_LEVEL


def test_xp_between_levels():
    assert xp_between(1, 99) == 13_034_431
    assert xp_between(60, 99) == 13_034_431 - 273_742
    assert xp_between(99, 99) == 0


def test_xp_between_rejects_backwards_range():
    with pytest.raises(ValueError, match="below"):
        xp_between(99, 60)


@pytest.mark.parametrize("level", [0, -1, MAX_LEVEL + 1])
def test_xp_for_level_rejects_out_of_range(level):
    with pytest.raises(ValueError, match="level must be"):
        xp_for_level(level)


def test_actions_needed_rounds_up():
    """A partial action doesn't level you."""
    assert actions_needed(1000, 300) == 4      # 3.33 -> 4
    assert actions_needed(900, 300) == 3       # exact stays exact
    assert actions_needed(1, 300) == 1


def test_actions_needed_handles_fractional_rates():
    assert actions_needed(100, 33.5) == 3


@pytest.mark.parametrize("rate", [0, -1])
def test_rates_must_be_positive(rate):
    with pytest.raises(ValueError, match="positive"):
        actions_needed(1000, rate)
    with pytest.raises(ValueError, match="positive"):
        hours_needed(1000, rate)


def test_hours_needed():
    assert hours_needed(120_000, 60_000) == pytest.approx(2.0)


def test_plan_reports_xp_actions_and_hours():
    out = plan(45, 99, xp_per_hour=120_000, xp_per_action=65)
    assert "45 -> 99" in out
    assert "XP/hr" in out and "hours" in out
    assert "XP/action" in out and "actions" in out


def test_plan_omits_rates_it_was_not_given():
    out = plan(1, 10)
    assert "1,154 XP" in out
    assert "hours" not in out and "actions" not in out
