"""Account links, activity parsing, min-max arithmetic, and the persona hook.

The one with teeth is ``test_prefetched_stats_do_not_switch_off_the_forced_read``.
Putting the asker's stats in front of the question is ambient context, not the
model choosing to look somebody up -- and the forced-read check keys on exactly
that distinction. Recording it in ``players_checked`` would have silently
disabled grounding enforcement for every linked user, which is the kind of
regression that shows up months later as "the answers got worse somehow".
"""

from __future__ import annotations

import os

import pytest

from reldo.accounts import AccountStore
from reldo.hiscores import Activity, Player, Skill
from reldo.persona import GROUNDING_CLAUSE, PLAIN, for_channel
from reldo.skills import cheapest_gains, next_milestone


def player(**levels) -> Player:
    skills = {
        name.title(): Skill(name.title(), 1, level, 1000)
        for name, level in levels.items()
    }
    return Player(name="TimmyZero", skills=skills)


# -- linking ----------------------------------------------------------------


def test_a_link_survives_a_restart(tmp_path):
    path = tmp_path / "accounts.json"
    AccountStore(path).link(7, "TimmyZero")
    assert AccountStore(path).get(7) == "TimmyZero"


def test_an_unlinked_user_has_no_name(tmp_path):
    assert AccountStore(tmp_path / "a.json").get(7) is None


def test_unlinking_forgets(tmp_path):
    store = AccountStore(tmp_path / "a.json")
    store.link(7, "TimmyZero")
    assert store.unlink(7) is True
    assert store.get(7) is None


def test_unlinking_nothing_reports_nothing(tmp_path):
    assert AccountStore(tmp_path / "a.json").unlink(7) is False


def test_surrounding_whitespace_is_normalised(tmp_path):
    store = AccountStore(tmp_path / "a.json")
    assert store.link(7, "  Timmy   Zero  ") == "Timmy Zero"


@pytest.mark.parametrize("bad", ["", "   ", "x" * 13, "timmy<script>"])
def test_impossible_names_are_refused_at_link_time(tmp_path, bad):
    """A name the hiscores can never match would turn every later answer
    generic, days after the typo that caused it."""
    with pytest.raises(ValueError):
        AccountStore(tmp_path / "a.json").link(7, bad)


def test_a_corrupt_file_reads_as_empty_rather_than_failing_to_boot(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("{not json")
    assert AccountStore(path).get(7) is None


def test_a_failed_save_does_not_unlink_everybody(tmp_path, monkeypatch):
    """The other half of the test above, and the reason the write is atomic.

    Reading a corrupt file as empty is the right call on its own and a trap
    next to a save that truncates first: the two together turn one interrupted
    write into every user silently unlinked, with nothing but a log line
    against it and the first symptom days later.
    """
    path = tmp_path / "a.json"
    store = AccountStore(path)
    store.link(7, "Zezima")

    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.link(8, "Lynx Titan")

    # A fresh store reads the file the failed save left behind.
    assert AccountStore(path).get(7) == "Zezima"


# -- activities -------------------------------------------------------------


def test_an_untouched_activity_is_not_done():
    """Jagex sends score 0 / rank -1 for everything never done, so all 91 arrive
    for a fresh account. Testing score != -1 would report 91 completions."""
    assert Activity("Vorkath", -1, 0).done is False


def test_a_real_count_is_done():
    assert Activity("Vorkath", 12345, 40).done is True


def test_only_real_counts_are_listed():
    found = Player(
        name="T",
        skills={},
        activities={
            "Vorkath": Activity("Vorkath", -1, 0),
            "Collections Logged": Activity("Collections Logged", -1, 11),
        },
    )
    assert [a.name for a in found.done()] == ["Collections Logged"]


# -- combat level -----------------------------------------------------------


def test_combat_level_matches_the_wikis_formula():
    """TimmyZero's real stats. Hand-computed from the formula read off the wiki:
    base 18 + melee 21.775 = 39.775 -> 39."""
    found = player(
        attack=34, strength=33, defence=33, hitpoints=34, ranged=13, prayer=11, magic=26
    )
    assert found.combat_level == 39


def test_a_fresh_account_is_combat_three():
    assert player(hitpoints=10).combat_level == 3


# -- min-max ----------------------------------------------------------------


def test_the_next_milestone_is_the_next_round_goal():
    assert next_milestone(54) == 60
    assert next_milestone(90) == 92


def test_there_is_nothing_after_99():
    assert next_milestone(99) is None


def test_cheapest_gains_are_sorted_by_xp_not_by_level():
    """The point of the ranking: 1->10 is 1,154 XP and 54->60 is 114,000, so a
    level-1 skill outranks one four times higher."""
    rows = cheapest_gains({"Slayer": 1, "Mining": 54, "Fishing": 95})
    assert [r[0] for r in rows] == ["Slayer", "Mining", "Fishing"]
    assert rows[0][1:3] == (1, 10)


def test_a_maxed_skill_is_left_out():
    assert cheapest_gains({"Fishing": 99}) == []


# -- persona ----------------------------------------------------------------


def test_the_default_voice_is_plain():
    assert for_channel("plain", {}, 100) is PLAIN


def test_an_unlisted_channel_gets_the_neutral_voice():
    """A persona is opt-in per channel, never global.

    A voice configured globally would follow the bot into every server it joins
    and every general channel in them, which is not what anybody wants from a
    wiki lookup in #help.
    """
    assert for_channel("plain", {999: "plain"}, 999) is PLAIN
    assert for_channel("plain", {999: "plain"}, 100) is PLAIN


def test_an_unknown_name_falls_back_rather_than_raising():
    """No characters ship, so every name is unknown and must resolve safely.

    A stale .env naming a persona that no longer exists should cost the tone,
    not the bot.
    """
    assert for_channel("nonsense", {999: "nonsense"}, 999) is PLAIN
    assert for_channel("plain", {999: ""}, 999) is PLAIN


def test_the_plain_persona_adds_nothing_at_all():
    assert PLAIN.prompt == ""
    assert PLAIN.voice == ""


def test_the_grounding_clause_cannot_be_relaxed_by_a_voice():
    """Any persona added later inherits this, which is why the hook stays.

    A character bolted on at the call site would bypass it; one defined here
    cannot, because the clause is appended to the prompt rather than applied to
    the finished text.
    """
    assert "still comes from the tools" in GROUNDING_CLAUSE
    assert "never a reason to guess" in GROUNDING_CLAUSE
    assert "suitable for all ages" in GROUNDING_CLAUSE


def test_names_lists_every_linked_account_once(tmp_path):
    """The scheduled sampler iterates these. Two Discord users can hold the same
    account -- a shared ironman, or one person with a second Discord -- and
    polling it twice a round buys one player's history for two requests."""
    store = AccountStore(tmp_path / "a.json")
    store.link(1, "TimmyZero")
    store.link(2, "Zezima")
    store.link(3, "TimmyZero")
    assert store.names() == ["TimmyZero", "Zezima"]


def test_names_is_empty_before_anybody_links(tmp_path):
    assert AccountStore(tmp_path / "a.json").names() == []
