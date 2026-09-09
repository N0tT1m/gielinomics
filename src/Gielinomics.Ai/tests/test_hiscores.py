"""Hiscores tests with mocked responses. No network.

The behaviour worth pinning is the unranked sentinel. Jagex returns -1 for rank,
level and xp on any skill the player isn't ranked in, and rendering that as "level
-1" (or worse, silently as 0) into a model's context is how you get an answer
telling someone they can't do a quest they can already do.
"""

from __future__ import annotations

import httpx
import pytest

from reldo.hiscores import Activity, HiscoresClient, HiscoresError, Player, Skill


def body(**levels):
    """A hiscores payload; unnamed skills come back unranked."""
    named = [("Overall", sum(levels.values()) or -1)] + list(levels.items())
    skills = [
        {"id": i, "name": n, "rank": 1000, "level": lv, "xp": lv * 1000}
        for i, (n, lv) in enumerate(named)
    ]
    skills.append({"id": 90, "name": "Runecraft", "rank": -1, "level": -1, "xp": -1})
    return {"name": "TestPlayer", "skills": skills, "activities": []}


def client_over(response):
    return HiscoresClient(transport=httpx.MockTransport(lambda r: response))


async def test_lookup_parses_skills_by_name():
    async with client_over(httpx.Response(200, json=body(Attack=70, Mining=45))) as c:
        player = await c.lookup("TestPlayer")
    assert player.name == "TestPlayer"
    assert player.level("Attack") == 70
    assert player.level("Mining") == 45


async def test_unranked_skill_reads_as_level_1_not_minus_1():
    async with client_over(httpx.Response(200, json=body(Attack=70))) as c:
        player = await c.lookup("x")
    assert player.level("Runecraft") == 1
    assert player.skills["Runecraft"].ranked is False


def test_unranked_hitpoints_reads_as_10_not_1():
    """The one skill that does not start at 1. Unranked is the *common* case for
    the accounts this feature exists to help -- the hiscores rank a slice of the
    population, so a low-level player comes back unranked in Hitpoints."""
    player = Player(
        name="x",
        skills={
            "Attack": Skill("Attack", 1, 20, 5_000),
            "Hitpoints": Skill("Hitpoints", -1, -1, -1),
        },
    )
    assert player.level("Hitpoints") == 10
    assert player.level("Attack") == 20  # everything else still starts at 1


def test_a_skill_missing_from_the_payload_entirely_still_starts_where_it_starts():
    """Absent and unranked have to agree: a payload predating a skill's release
    omits it rather than sending -1, and both mean "no rank"."""
    player = Player(name="x", skills={})
    assert player.level("Hitpoints") == 10
    assert player.level("Sailing") == 1


def test_combat_level_keeps_the_hitpoints_every_account_is_created_with():
    """Reading unranked Hitpoints as 1 put combat level 3 low, which is the
    difference between "you can do this" and "you cannot" on a wilderness or
    minigame requirement."""
    fresh = Player(
        name="x",
        skills={
            "Attack": Skill("Attack", 1, 20, 5_000),
            "Strength": Skill("Strength", 1, 20, 5_000),
            "Defence": Skill("Defence", 1, 10, 1_200),
            "Hitpoints": Skill("Hitpoints", -1, -1, -1),  # unranked
        },
    )
    ranked = Player(
        name="x",
        skills={**fresh.skills, "Hitpoints": Skill("Hitpoints", 900_000, 10, 1_154)},
    )
    assert fresh.combat_level == ranked.combat_level


def test_a_hitpoints_requirement_is_not_reported_as_unmet_for_a_fresh_account():
    player = Player(name="x", skills={"Hitpoints": Skill("Hitpoints", -1, -1, -1)})
    assert player.meets({"Hitpoints": 10})["Hitpoints"] == (10, 10, True)


async def test_skill_lookup_is_case_insensitive():
    async with client_over(httpx.Response(200, json=body(Mining=45))) as c:
        player = await c.lookup("x")
    assert player.level("mining") == 45
    assert player.level("MINING") == 45


async def test_unknown_skill_is_level_1_not_an_error():
    async with client_over(httpx.Response(200, json=body(Attack=70))) as c:
        player = await c.lookup("x")
    assert player.level("Sailing") == 1


async def test_404_is_an_actionable_message():
    async with client_over(httpx.Response(404, text="<html>404</html>")) as c:
        with pytest.raises(HiscoresError, match="No hiscores entry"):
            await c.lookup("zzznotreal")


async def test_empty_username_is_rejected_before_any_request():
    async with HiscoresClient() as c:
        with pytest.raises(HiscoresError, match="empty"):
            await c.lookup("   ")


async def test_unreachable_endpoint_raises_hiscores_error():
    def down(request):
        raise httpx.ConnectError("no route")

    async with HiscoresClient(transport=httpx.MockTransport(down)) as c:
        with pytest.raises(HiscoresError, match="Could not reach"):
            await c.lookup("x")


async def test_malformed_payload_raises_rather_than_half_parsing():
    async with client_over(httpx.Response(200, json={"nope": True})) as c:
        with pytest.raises(HiscoresError, match="Unexpected"):
            await c.lookup("x")


async def test_non_200_non_404_is_reported():
    async with client_over(httpx.Response(503, text="busy")) as c:
        with pytest.raises(HiscoresError, match="HTTP 503"):
            await c.lookup("x")


def test_meets_reports_each_requirement_separately():
    player = Player(
        name="x",
        skills={
            "Mining": Skill("Mining", 1, 45, 100),
            "Smithing": Skill("Smithing", 1, 30, 100),
        },
    )
    result = player.meets({"Mining": 40, "Smithing": 50})
    assert result["Mining"] == (45, 40, True)
    assert result["Smithing"] == (30, 50, False)


def test_summary_separates_ranked_from_unranked():
    player = Player(
        name="x",
        skills={
            "Overall": Skill("Overall", 1, 500, 1_000_000),
            "Attack": Skill("Attack", 1, 70, 737_627),
            "Runecraft": Skill("Runecraft", -1, -1, -1),
        },
    )
    out = player.summary()
    assert "Attack 70" in out
    assert "Runecraft" in out and "unranked" in out
    assert "-1" not in out


def test_summary_gives_unranked_skills_the_level_they_actually_have():
    """The block the model reads. Naming unranked skills without their level let
    'level 1 or close' stand in for Hitpoints 10 -- the same wrong number
    `level()` used to return, arriving by a different route."""
    player = Player(
        name="x",
        skills={
            "Overall": Skill("Overall", 1, 40, 10_000),
            "Attack": Skill("Attack", 1, 20, 5_000),
            "Hitpoints": Skill("Hitpoints", -1, -1, -1),
            "Runecraft": Skill("Runecraft", -1, -1, -1),
        },
    )
    out = player.summary()
    assert "Hitpoints 10" in out
    assert "Runecraft 1" in out


# -- what an activity score actually means ----------------------------------
# 91 counters come back on every lookup and they do not all mean the same thing.
# Most are tallies, two are ratings, one is the sum of six others.


def with_activities(*rows):
    return Player(
        name="x",
        skills={"Overall": Skill("Overall", 1, 500, 1_000_000)},
        activities={n: Activity(n, r, s) for n, r, s in rows},
    )


def test_a_rating_is_not_counted_as_something_done():
    """Verified against a live account: PvP Arena - Rank came back rank=-1,
    score=2500. Sorted in with the kill counts it led the list, so the stat
    block opened by telling the model this player had done the PvP Arena two and
    a half thousand times."""
    player = with_activities(
        ("PvP Arena - Rank", -1, 2500), ("Collections Logged", -1, 11)
    )
    assert [a.name for a in player.done()] == ["Collections Logged"]
    assert [a.name for a in player.ratings()] == ["PvP Arena - Rank"]


def test_the_clue_total_is_not_listed_beside_the_tiers_it_totals():
    """It is the sum of six entries already present, and being the largest it
    would lead the list -- the same double count wom.py drops "overall" for."""
    player = with_activities(
        ("Clue Scrolls (all)", 1, 30),
        ("Clue Scrolls (easy)", 1, 20),
        ("Clue Scrolls (hard)", 1, 10),
    )
    assert "Clue Scrolls (all)" not in [a.name for a in player.done()]
    assert sum(a.score for a in player.done()) == 30


def test_clues_read_in_tier_order_not_by_volume():
    """"master 3" means something different beside "beginner 200" than it does
    at the top of a list sorted by count."""
    player = with_activities(
        ("Clue Scrolls (master)", 1, 3),
        ("Clue Scrolls (beginner)", 1, 200),
        ("Clue Scrolls (hard)", 1, 40),
    )
    assert [a.name for a in player.clues()] == [
        "Clue Scrolls (beginner)", "Clue Scrolls (hard)", "Clue Scrolls (master)"
    ]


def test_a_zeroed_activity_is_still_not_done():
    """A fresh account has all 91 present and zeroed."""
    player = with_activities(("Vorkath", -1, 0), ("Zulrah", -1, 0))
    assert player.done() == [] and player.clues() == []


def test_the_summary_labels_a_rating_as_one():
    """The label is the only thing between "PvP Arena 2,500" and an answer that
    calls it two and a half thousand wins."""
    out = with_activities(
        ("PvP Arena - Rank", -1, 2500), ("Vorkath", 1, 40)
    ).summary()
    assert "Vorkath 40" in out
    assert "a score, not a number of games" in out
    assert "PvP Arena 2,500" in out
