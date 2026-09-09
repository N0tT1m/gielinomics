"""The coach decides when to interrupt. That is the part worth testing.

Not what it says -- the agent says that, and the grounding passes cover it --
but whether it speaks at all, and how often. A coach that talks too much gets
muted, and every rule here exists to stop that.
"""

from __future__ import annotations

from reldo.coach import (
    COOLDOWN_SECONDS,
    Coach,
    CoachState,
    Observation,
    choose,
    observe,
)
from reldo.skills import xp_for_level


def _live(**over) -> dict:
    base = {
        "player": "TimmyZero",
        "fresh": True,
        "activity": "",
        "skills": {},
        "session": {"minutes": 1, "gains": {"Fishing": 400}},
    }
    return base | over


def test_says_nothing_when_nothing_changed():
    assert observe(CoachState(skills={"Fishing": 1000}), _live(skills={"Fishing": 1000}), 0) == []


def test_notices_a_level():
    before = CoachState(skills={"Fishing": xp_for_level(70)})
    seen = observe(before, _live(skills={"Fishing": xp_for_level(71)}), 0)
    level = next(o for o in seen if o.key == "level:Fishing:71")
    assert "71" in level.question
    # A level also looks like "started training Fishing" from a standing start.
    # Both are true; only one gets said, and it is the level.
    assert choose(seen) is level


def test_xp_that_does_not_cross_a_level_is_not_an_event():
    """Most XP is not news. Only the drop that changes what you can do is."""
    before = CoachState(skills={"Fishing": xp_for_level(70)})
    seen = observe(before, _live(skills={"Fishing": xp_for_level(70) + 50_000}), 0)
    assert [o for o in seen if o.key.startswith("level:")] == []


def test_a_tiny_drop_across_a_boundary_is_still_a_level():
    """The drop that crosses a level is often one fish.

    There used to be a 500 XP floor here and it silenced the coach completely:
    at 49,165 XP/hr, measured live, a fifteen-second poll moves about 205 XP, so
    every delta fell under it and nothing ever fired.
    """
    before = CoachState(skills={"Fishing": xp_for_level(71) - 100})
    seen = observe(before, _live(skills={"Fishing": xp_for_level(71)}), 0)
    assert any(o.key == "level:Fishing:71" for o in seen)


def test_a_realistic_poll_registers_as_activity():
    """205 XP is what fifteen seconds of ordinary fishing looks like."""
    before = CoachState(skills={"Fishing": 9_653_759, "Hitpoints": 36_637})
    seen = observe(before, _live(skills={"Fishing": 9_653_964, "Hitpoints": 36_637}), 0)
    assert any(o.key == "doing:Fishing" for o in seen)


def test_notices_a_change_of_activity():
    before = CoachState(activity="Fishing")
    seen = observe(before, _live(activity="Mining"), 0)
    assert any(o.key == "doing:Mining" for o in seen)


def test_idle_only_counts_once_per_stretch():
    live = _live(session={"minutes": 20, "gains": {}})
    state = CoachState(idle_since=0.0)
    first = observe(state, live, 0)
    assert any(o.key.startswith("idle:") for o in first)

    # Having said it, the same stretch of idleness is not news again.
    state.said.add(next(o.key for o in first if o.key.startswith("idle:")))
    assert [o for o in observe(state, live, 0) if o.key.startswith("idle:")] == []


def test_gaining_xp_is_never_idle():
    seen = observe(CoachState(), _live(session={"minutes": 30, "gains": {"Fishing": 9}}), 0)
    assert [o for o in seen if o.key.startswith("idle:")] == []


def test_choose_takes_the_most_urgent():
    picked = choose([Observation("a", "?", 1), Observation("b", "?", 3), Observation("c", "?", 2)])
    assert picked is not None and picked.key == "b"


def test_choose_of_nothing_is_nothing():
    assert choose([]) is None


def test_cooldown_silences_a_real_observation():
    """The observation is dropped, not queued: stale advice is worse than none."""
    coach = Coach("http://x", "TimmyZero", cooldown=COOLDOWN_SECONDS)
    coach.state.skills = {"Fishing": xp_for_level(70)}
    spoke = coach.update(_live(skills={"Fishing": xp_for_level(71)}), 1_000.0)
    assert spoke is not None

    coach.state.skills = {"Mining": xp_for_level(50)}
    assert coach.update(_live(skills={"Mining": xp_for_level(51)}), 1_010.0) is None


def test_speaks_again_once_the_cooldown_passes():
    coach = Coach("http://x", "TimmyZero", cooldown=100.0)
    coach.state.skills = {"Fishing": xp_for_level(70)}
    assert coach.update(_live(skills={"Fishing": xp_for_level(71)}), 1_000.0) is not None
    coach.state.skills = {"Mining": xp_for_level(50)}
    assert coach.update(_live(skills={"Mining": xp_for_level(51)}), 1_200.0) is not None


def test_never_repeats_itself():
    coach = Coach("http://x", "TimmyZero", cooldown=0.0)
    coach.state.skills = {"Fishing": xp_for_level(70)}
    live = _live(skills={"Fishing": xp_for_level(71)})
    assert coach.update(live, 1_000.0) is not None
    # Same level, later poll, still the same fact about the world.
    coach.state.skills = {"Fishing": xp_for_level(70)}
    assert coach.update(live, 9_000.0) is None


def test_idle_clock_resets_when_xp_moves():
    coach = Coach("http://x", "TimmyZero", cooldown=0.0)
    coach.update(_live(session={"minutes": 9, "gains": {}}), 100.0)
    assert coach.state.idle_since is not None
    coach.update(_live(session={"minutes": 10, "gains": {"Fishing": 500}}), 200.0)
    assert coach.state.idle_since is None


def test_activity_is_derived_when_the_plugin_does_not_report_one():
    """The plugin never fills in `activity`, so waiting for one waits forever."""
    before = CoachState(skills={"Fishing": 100_000, "Hitpoints": 50_000})
    seen = observe(before, _live(skills={"Fishing": 140_000, "Hitpoints": 51_000}), 0)
    assert any(o.key == "doing:Fishing" for o in seen)


def test_derived_activity_ignores_the_skills_that_come_along_for_the_ride():
    """Nobody has ever started Hitpoints."""
    before = CoachState(skills={"Attack": 100_000, "Hitpoints": 50_000})
    seen = observe(before, _live(skills={"Attack": 160_000, "Hitpoints": 70_000}), 0)
    assert [o.key for o in seen if o.key.startswith("doing:")] == ["doing:Attack"]


def test_no_movement_is_no_activity():
    """Genuinely none. Any gain counts now -- the floor is what broke this."""
    before = CoachState(skills={"Fishing": 100_000})
    seen = observe(before, _live(skills={"Fishing": 100_000}), 0)
    assert [o for o in seen if o.key.startswith("doing:")] == []


def test_a_reported_activity_still_wins_if_one_ever_appears():
    before = CoachState(skills={"Fishing": 100_000})
    seen = observe(before, _live(activity="Zulrah", skills={"Fishing": 140_000}), 0)
    assert any(o.key == "doing:Zulrah" for o in seen)


def test_switching_what_you_train_is_an_event():
    coach = Coach("http://x", "TimmyZero", cooldown=0.0)
    # Every skill in every payload, the way the plugin actually sends them: a
    # skill appearing from nowhere has no delta to measure and is skipped.
    coach.update(_live(skills={"Fishing": 5_000_000, "Mining": 340_000}), 0.0)
    assert coach.update(_live(skills={"Fishing": 5_040_000, "Mining": 340_000}), 20.0) is not None
    assert coach.state.activity == "Fishing"
    # Still fishing, still inside level 88: not news again.
    assert coach.update(_live(skills={"Fishing": 5_080_000, "Mining": 340_000}), 40.0) is None
    # Now mining, which is.
    spoke = coach.update(
        _live(skills={"Fishing": 5_080_000, "Mining": 360_000}), 60.0
    )
    assert spoke is not None and spoke.key == "doing:Mining"


def test_the_activity_question_carries_the_level_and_the_rate():
    """Asked bare, the agent answers about the skill instead of about you.

    Observed live: "I have just started Fishing" came back as "Fishing requires
    level 96, you're at 95" -- a level requirement, which is not advice. The
    numbers were already in the payload; the question just did not carry them.
    """
    before = CoachState(skills={"Fishing": 9_653_759})
    live = _live(
        skills={"Fishing": 9_653_964},
        session={"minutes": 5, "gains": {"Fishing": 4672}, "rates": {"Fishing": 49165}},
    )
    doing = next(o for o in observe(before, live, 0) if o.key == "doing:Fishing")
    assert "level 95" in doing.question
    assert "49,165" in doing.question
    assert "faster" in doing.question


def test_the_activity_question_survives_a_missing_rate():
    """Under a minute there is no rate yet, and that must not read as zero."""
    before = CoachState(skills={"Fishing": 9_653_759})
    live = _live(
        skills={"Fishing": 9_653_964},
        session={"minutes": 0, "gains": {"Fishing": 205}, "rates": {"Fishing": 0}},
    )
    doing = next(o for o in observe(before, live, 0) if o.key == "doing:Fishing")
    assert "XP an hour" not in doing.question
    assert "level 95" in doing.question


def test_a_level_observation_names_its_skill_and_level():
    """So the caller can look the facts up before asking anything.

    The failure this exists for, from the trace: asked "I just reached Thieving
    level 17, what does that unlock", she read the general Thieving page and
    answered about level 91. searches was empty. The number was real and on the
    page, so the ungrounded-number pass had no objection -- provenance was fine,
    relevance was not.
    """
    before = CoachState(skills={"Thieving": xp_for_level(16)})
    seen = observe(before, _live(skills={"Thieving": xp_for_level(17)}), 0)
    level = next(o for o in seen if o.key == "level:Thieving:17")
    assert level.skill == "Thieving"
    assert level.level == 17


def test_an_activity_observation_names_its_skill_and_level_too():
    """So the guide's bracket for that level can be fetched before asking.

    Recalled instead, she told a level 18 account to pickpocket Master Farmers,
    which need 38 -- having reported the requirement as 94 one remark earlier,
    that being the level you stop failing at with the hard Ardougne Diary.
    """
    before = CoachState(skills={"Fishing": 9_653_759})
    doing = next(
        o for o in observe(before, _live(skills={"Fishing": 9_653_964}), 0)
        if o.key.startswith("doing:")
    )
    assert doing.skill == "Fishing"
    assert doing.level == 95


def test_a_reported_activity_that_is_not_a_skill_has_nothing_to_look_up():
    """"Zulrah" is not a skill, so there is no guide bracket to fetch."""
    before = CoachState(skills={"Fishing": 100_000})
    doing = next(
        o for o in observe(before, _live(activity="Zulrah", skills={"Fishing": 140_000}), 0)
        if o.key.startswith("doing:")
    )
    assert doing.skill == "" and doing.level == 0
