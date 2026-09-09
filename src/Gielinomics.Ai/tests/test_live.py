"""The live-state receiver, driven over a real loopback socket.

aiohttp's test server is used rather than calling handlers directly, because the
things that break here are wire-level: a bad token, malformed JSON, a payload
from a plugin build that predates a field. All three are what actually arrives.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from reldo.live import (
    FRESH_SECONDS,
    MAX_BANK,
    MAX_QUESTS,
    MAX_TRACKED_PLAYERS,
    SESSION_GAP,
    LiveStore,
    build_app,
    parse_profile,
    parse_state,
    serve,
)


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


PAYLOAD = {
    "player": "TimmyZero",
    "skills": {"Mining": 158874, "Fishing": 9439707},
    "inventory": ["Rune pickaxe", "Granite x12"],
    "location": {"x": 3188, "y": 3220, "plane": 0},
    "region": 12850,
    "hitpoints": 34,
    "prayer": 11,
    "energy": 88,
    "activity": "Mining granite",
}


async def client_for(store: LiveStore, token: str = "") -> TestClient:
    app = build_app(store, token=token)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# -- parsing ----------------------------------------------------------------


def test_a_full_payload_parses():
    state = parse_state(PAYLOAD, at=1000.0)
    assert state.player == "TimmyZero"
    assert state.skills["Mining"] == 158874
    assert state.location == (3188, 3220, 0)
    assert state.activity == "Mining granite"


def test_a_payload_with_no_player_is_refused():
    with pytest.raises(ValueError, match="no player name"):
        parse_state({"skills": {}}, at=1000.0)


def test_an_older_plugin_sending_fewer_fields_still_works():
    """A plugin build that predates a field must degrade to reporting less, not
    be rejected -- the client updates on its own schedule, not this one's."""
    state = parse_state({"player": "T", "skills": {"Mining": 1}}, at=1000.0)
    assert state.location is None and state.energy is None
    assert state.skills == {"Mining": 1}


def test_a_broken_location_is_dropped_not_fatal():
    state = parse_state({"player": "T", "location": {"x": "nonsense"}}, at=1000.0)
    assert state.location is None


def test_inventory_cannot_exceed_a_real_one():
    state = parse_state({"player": "T", "inventory": ["x"] * 200}, at=1000.0)
    assert len(state.inventory) == 28


def test_absurd_strings_are_trimmed():
    state = parse_state({"player": "T", "activity": "x" * 5000}, at=1000.0)
    assert len(state.activity) <= 60


# -- freshness and sessions -------------------------------------------------


def test_stale_state_is_not_reported():
    """A closed client must not read as 'what you are doing'."""
    clock = Clock()
    store = LiveStore(clock=clock)
    store.update(parse_state(PAYLOAD, at=clock()))
    clock.advance(FRESH_SECONDS + 1)
    assert store.summary("TimmyZero") == ""


def test_fresh_state_is_reported():
    clock = Clock()
    store = LiveStore(clock=clock)
    store.update(parse_state(PAYLOAD, at=clock()))
    got = store.summary("TimmyZero")
    assert "Mining granite" in got and "Rune pickaxe" in got


def test_an_unknown_player_summarises_to_nothing():
    """Empty rather than 'no data': this gets folded into a prompt, and a line
    about the client being closed is noise on every question asked away from
    the game."""
    assert LiveStore().summary("Nobody") == ""


def test_session_gains_are_measured_from_where_it_started():
    """Reports arrive every few seconds, so a session is many small updates.
    Jumping the clock past SESSION_GAP between two of them would correctly be a
    new session, not a long one -- which is what the next test covers."""
    clock = Clock()
    store = LiveStore(clock=clock)
    store.update(parse_state(PAYLOAD, at=clock()))

    mined = 158874
    for _ in range(6):  # 6 x 5 min = 30 min of continuous play
        clock.advance(300)
        mined += 10_000
        store.update(
            parse_state(dict(PAYLOAD, skills={"Mining": mined, "Fishing": 9439707}), at=clock())
        )

    got = store.summary("TimmyZero")
    assert "Mining +60,000" in got
    assert "120,000/hr" in got  # 60k over 30 min
    assert "this session (30 min)" in got


def test_a_long_gap_starts_a_new_session():
    """Two hours of granite with lunch in the middle is two sessions; averaging
    across the break reports a rate nobody achieved."""
    clock = Clock()
    store = LiveStore(clock=clock)
    store.update(parse_state(PAYLOAD, at=clock()))
    clock.advance(SESSION_GAP + 1)
    store.update(parse_state(PAYLOAD, at=clock()))
    assert store.session("TimmyZero").started == clock()


def test_no_rate_is_quoted_before_it_would_mean_anything():
    """Under a minute, one XP drop reads as hundreds of thousands per hour."""
    clock = Clock()
    store = LiveStore(clock=clock)
    store.update(parse_state(PAYLOAD, at=clock()))
    clock.advance(10)
    store.update(parse_state(dict(PAYLOAD, skills={"Mining": 158874 + 500}), at=clock()))
    assert "/hr" not in store.summary("TimmyZero")


def test_logged_in_and_gaining_nothing_is_said_out_loud():
    """Invisible to the hiscores and to progress history, and the single most
    actionable thing a coach can notice."""
    clock = Clock()
    store = LiveStore(clock=clock)
    store.update(parse_state(PAYLOAD, at=clock()))
    clock.advance(600)
    store.update(parse_state(PAYLOAD, at=clock()))
    assert "no XP gained at all" in store.summary("TimmyZero")


# -- the wire ---------------------------------------------------------------


async def test_a_post_is_accepted_and_stored():
    store = LiveStore()
    client = await client_for(store)
    response = await client.post("/state", json=PAYLOAD)
    assert response.status == 200
    assert (await response.json())["player"] == "TimmyZero"
    assert store.get("TimmyZero").activity == "Mining granite"
    await client.close()


async def test_a_missing_token_is_rejected():
    store = LiveStore()
    client = await client_for(store, token="secret")
    assert (await client.post("/state", json=PAYLOAD)).status == 401
    assert store.get("TimmyZero") is None
    await client.close()


async def test_the_right_token_is_accepted():
    store = LiveStore()
    client = await client_for(store, token="secret")
    response = await client.post(
        "/state", json=PAYLOAD, headers={"Authorization": "Bearer secret"}
    )
    assert response.status == 200
    await client.close()


async def test_malformed_json_is_a_400_not_a_crash():
    client = await client_for(LiveStore())
    response = await client.post(
        "/state", data=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status == 400
    await client.close()


async def test_a_json_array_is_refused():
    client = await client_for(LiveStore())
    assert (await client.post("/state", json=[1, 2, 3])).status == 400
    await client.close()


async def test_a_payload_with_no_player_is_a_400():
    client = await client_for(LiveStore())
    assert (await client.post("/state", json={"skills": {}})).status == 400
    await client.close()


async def test_health_reports_who_is_tracked_and_whether_auth_is_on():
    store = LiveStore()
    client = await client_for(store, token="secret")
    await client.post("/state", json=PAYLOAD, headers={"Authorization": "Bearer secret"})
    body = await (await client.get("/health")).json()
    assert body == {
        "ok": True,
        "tracking": ["TimmyZero"],
        # Empty rather than absent: state and profile arrive on separate
        # endpoints, so "posting state but no profile" is a real configuration
        # and health is where you would look to find that out.
        "profiles": [],
        "auth": True,
    }
    await client.close()


# -- the pair of defaults that had no guard ---------------------------------
# live_host defaults to 0.0.0.0 and live_token to "", and an empty token
# disables the check outright. Together that is an open endpoint whose payload
# is folded into a player's prompt, so serve() refuses the combination.


async def test_serving_on_every_interface_without_a_token_is_refused():
    with pytest.raises(ValueError, match="Refusing to listen"):
        await serve(LiveStore(), host="0.0.0.0", port=0, token="")


async def test_the_refusal_names_both_ways_out():
    """An error that only says no costs an hour. This one has to say which two
    settings fix it, because the fix depends on where the game runs."""
    with pytest.raises(ValueError) as caught:
        await serve(LiveStore(), host="0.0.0.0", port=0, token="   ")
    message = str(caught.value)
    assert "RELDO_LIVE_TOKEN" in message
    assert "RELDO_LIVE_HOST=127.0.0.1" in message


async def test_loopback_without_a_token_is_still_fine():
    """The local-sidecar case is the one that does not need a secret, and
    demanding one there would be security theatre with a setup cost."""
    runner = await serve(LiveStore(), host="127.0.0.1", port=0, token="")
    await runner.cleanup()


async def test_every_interface_with_a_token_is_the_supported_deployment():
    """RuneLite on one box, the bot on another -- what this feature is for."""
    runner = await serve(LiveStore(), host="127.0.0.1", port=0, token="secret")
    await runner.cleanup()


async def test_a_token_that_only_shares_a_prefix_is_rejected():
    """Guards the constant-time comparison against being written as a
    startswith, which is the plausible wrong way to make == slower."""
    store = LiveStore()
    client = await client_for(store, token="secret")
    for offered in ("Bearer sec", "Bearer secretx", "Bearer SECRET", "secret"):
        response = await client.post(
            "/state", json=PAYLOAD, headers={"Authorization": offered}
        )
        assert response.status == 401, offered
    assert store.get("TimmyZero") is None
    await client.close()


# -- the profile: what only the client knows --------------------------------
# Quests, diaries, equipment and bank are readable from no public API. Every
# tracker that knows your quests learned them from a plugin.


PROFILE = {
    "player": "TimmyZero",
    "quests": {"finished": ["Cook's Assistant", "Dragon Slayer I"],
               "started": ["Dragon Slayer II"]},
    "diaries": {"Varrock": ["easy", "medium"], "Karamja": ["easy"]},
    "equipment": ["Rune platebody", "Dragon scimitar"],
    "bank": ["Shark x300", "Rune platebody"],
}


async def test_a_profile_round_trips_over_the_wire():
    store = LiveStore()
    client = await client_for(store)
    assert (await client.post("/profile", json=PROFILE)).status == 200
    profile = store.profile("TimmyZero")
    assert profile.quest_state("Cook's Assistant") == "finished"
    assert profile.quest_state("Dragon Slayer II") == "started"
    assert profile.quest_state("Monkey Madness II") == "not started"
    await client.close()


async def test_quest_matching_ignores_case_and_spacing():
    """The model spells quests out of the question, not out of the quest log."""
    profile = parse_profile(PROFILE, at=0.0)
    assert profile.quest_state("  cook's   ASSISTANT ") == "finished"


async def test_an_unseen_bank_is_not_an_empty_one():
    """The client cannot read the bank until the player opens it, so absent is
    the normal state. Reporting it as empty would tell somebody they own
    nothing."""
    without = parse_profile({**PROFILE, "bank": None}, at=0.0)
    assert without.bank is None
    assert without.holding("shark") == []

    with_bank = parse_profile(PROFILE, at=0.0)
    assert with_bank.holding("shark") == ["Shark x300 (bank)"]


def test_holding_looks_in_worn_gear_and_bank_and_says_which():
    profile = parse_profile(PROFILE, at=0.0)
    assert profile.holding("rune platebody") == [
        "Rune platebody (worn)", "Rune platebody (bank)"
    ]


def test_a_profile_from_an_older_plugin_degrades_rather_than_being_rejected():
    """A build that predates diaries should report less, not fail."""
    profile = parse_profile({"player": "TimmyZero"}, at=0.0)
    assert profile.quests_finished == frozenset()
    assert profile.diaries == {} and profile.equipment == []


def test_a_profile_with_no_player_is_refused():
    with pytest.raises(ValueError, match="no player name"):
        parse_profile({"quests": {"finished": ["x"]}}, at=0.0)


def test_absurd_lists_are_capped_rather_than_stored():
    profile = parse_profile(
        {"player": "x", "bank": [f"item{n}" for n in range(5000)],
         "quests": {"finished": [f"q{n}" for n in range(5000)]}},
        at=0.0,
    )
    assert len(profile.bank) == MAX_BANK
    assert len(profile.quests_finished) == MAX_QUESTS


async def test_the_profile_endpoint_is_behind_the_same_token():
    store = LiveStore()
    client = await client_for(store, token="secret")
    assert (await client.post("/profile", json=PROFILE)).status == 401
    assert store.profile("TimmyZero") is None
    await client.close()


async def test_the_summary_carries_a_digest_not_the_whole_profile():
    """This string goes into the prompt for every question; 160 quest names
    would drown everything else in it."""
    store = LiveStore(clock=lambda: 100.0)
    store.update(parse_state(PAYLOAD, at=100.0))
    store.update_profile(parse_profile(PROFILE, at=100.0))
    out = store.summary("TimmyZero")
    assert "2 quests finished" in out and "3 diary tiers" in out
    assert "Cook's Assistant" not in out


async def test_a_profile_outlives_the_freshness_window_state_has():
    """A quest you finished is still finished when the client is closed.

    So a stale session drops the "right now" block and keeps the digest.
    Returning "" for both discarded the durable half with the perishable one,
    and a logged-out player looked like one with no quest data at all -- leaving
    the model no reason to reach for a tool it had not been told existed.
    """
    store = LiveStore(clock=lambda: 100.0)
    store.update(parse_state(PAYLOAD, at=0.0))  # stale state
    store.update_profile(parse_profile(PROFILE, at=0.0))

    out = store.summary("TimmyZero")
    assert "Mining granite" not in out           # the perishable half aged out
    assert "2 quests finished" in out            # the durable half did not
    assert store.profile("TimmyZero") is not None


async def test_a_player_with_no_profile_at_all_still_summarises_to_nothing():
    """The digest must not turn "nothing to say" into a sentence on every
    question asked away from the game."""
    assert LiveStore(clock=lambda: 100.0).summary("Nobody") == ""


# -- eviction ---------------------------------------------------------------
#
# The per-payload caps above bound one POST. Nothing bounded how many *players*
# could be in here: one entry per distinct name, held for the life of the
# process, and a profile with a bank in it runs to tens of kilobytes.


def _state(player: str, at: float, **skills):
    return parse_state({"player": player, "skills": skills or {"Mining": 1}}, at=at)


def _profile(player: str, at: float):
    return parse_profile({"player": player, "quests": {"finished": ["Cook's Assistant"]}}, at=at)


def test_states_do_not_grow_without_bound():
    store = LiveStore(max_players=3)
    for n in range(10):
        store.update(_state(f"p{n}", at=1000.0 + n))
    assert len(store._states) == 3


def test_the_coldest_player_is_the_one_dropped():
    store = LiveStore(max_players=2)
    store.update(_state("Alice", at=1000.0))
    store.update(_state("Bob", at=1001.0))
    store.update(_state("Alice", at=1002.0))   # Alice reports again; Bob goes cold
    store.update(_state("Carol", at=1003.0))

    assert store.get("Alice") is not None, "the active player was evicted"
    assert store.get("Carol") is not None
    assert store.get("Bob") is None


def test_an_evicted_player_takes_their_session_with_them():
    """_sessions is created in update() and nowhere else, so it has no cap of
    its own -- which is only safe if the two are evicted together."""
    store = LiveStore(max_players=1)
    store.update(_state("Alice", at=1000.0))
    store.update(_state("Bob", at=1001.0))

    assert store.session("Alice") is None
    assert set(store._sessions) == {"Bob"}


def test_profiles_do_not_grow_without_bound():
    store = LiveStore(max_players=3)
    for n in range(10):
        store.update_profile(_profile(f"p{n}", at=1000.0 + n))
    assert len(store._profiles) == 3


def test_a_profile_is_not_evicted_by_state_traffic():
    """The two halves have different lifecycles: state is perishable and a
    profile is the durable thing no public API carries. A player whose client
    is quiet must not lose their quest log because other people are playing."""
    store = LiveStore(max_players=2)
    store.update_profile(_profile("Alice", at=1000.0))
    for n in range(10):
        store.update(_state(f"p{n}", at=1001.0 + n))

    assert store.profile("Alice") is not None


def test_a_cap_of_zero_means_no_cap():
    """0 is 'off' everywhere else here, and a store that can hold nothing would
    silently disable live state for anyone who set it looking for that."""
    store = LiveStore(max_players=0)
    for n in range(50):
        store.update(_state(f"p{n}", at=1000.0 + n))
    assert len(store._states) == 50


def test_the_default_cap_is_far_above_a_real_population():
    """This is a Discord bot's linked accounts, which is dozens. The cap is a
    ceiling on what a client posting names in a loop costs, not a limit anyone
    should reach."""
    assert MAX_TRACKED_PLAYERS >= 100
