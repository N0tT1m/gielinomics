"""Map extraction and the link format.

The link format is the part worth pinning: it was read out of the viewer's
``parseHash``, and every field is positional, so a transposition produces a URL
that loads a map of the wrong place rather than an error anybody would notice.
"""

from __future__ import annotations

import pytest

from reldo.maps import MAX_LINKS_FOLLOWED, MapLocation, for_pages, from_wikitext, linked_from

UNGAEL = "{{Infobox Location|name=Ungael}}\n{{Map|name=Ungael|x=2272|y=4064|zoom=2}}"
LUMBRIDGE = "{{Map|name=Lumbridge|width=300|height=400|zoom=1|x=3188|y=3220}}"


class FakeWiki:
    def __init__(self, pages: dict[str, str] | None = None, *, error: Exception | None = None):
        self._pages = pages or {}
        self._error = error
        self.requested: list[list[str]] = []

    async def wikitext(self, titles):
        self.requested.append(list(titles))
        if self._error:
            raise self._error
        return {t: self._pages[t] for t in titles if t in self._pages}


# -- the link ---------------------------------------------------------------


def test_the_fragment_is_zoom_mapid_plane_x_y():
    """Verified against the viewer's parseHash, which splits on '/' into
    [zoom, mapID, plane, x, y] -- x before y, despite it calling them lng/lat."""
    place = MapLocation(name="Ungael", x=2272, y=4064, zoom=2)
    assert place.url == "https://maps.runescape.wiki/osrs/#2/0/0/2272/4064"


def test_plane_and_map_id_ride_along():
    place = MapLocation(name="A dungeon", x=100, y=200, zoom=3, plane=1, map_id=7)
    assert place.url.endswith("#3/7/1/100/200")


# -- extraction -------------------------------------------------------------


def test_a_location_page_yields_its_map():
    place = from_wikitext(UNGAEL)
    assert (place.name, place.x, place.y, place.zoom) == ("Ungael", 2272, 4064, 2)


def test_fields_may_arrive_in_any_order():
    place = from_wikitext(LUMBRIDGE)
    assert (place.x, place.y, place.zoom) == (3188, 3220, 1)


def test_a_page_with_no_map_yields_nothing():
    """Most monsters and every item. The whip has no location."""
    assert from_wikitext("{{Infobox Item|name=Abyssal whip}} It is a whip.") is None


def test_inline_maplink_pins_are_ignored():
    """Lumbridge carries dozens of these, one per item spawn. Collecting them
    would bury the page's own location under where its bronze daggers are."""
    pins = "{{map|3208,3214|type=maplink|mtype=pin|group=Bowl}}" * 3
    assert from_wikitext(pins) is None


def test_the_infobox_map_wins_over_later_pins():
    assert from_wikitext(UNGAEL + "\n{{map|1,2|type=maplink}}").name == "Ungael"


def test_the_first_map_wins_when_a_page_has_several():
    """Later maps are of somewhere else the article mentions."""
    both = UNGAEL + "\n{{Map|name=Elsewhere|x=1000|y=1000}}"
    assert from_wikitext(both).name == "Ungael"


@pytest.mark.parametrize(
    "body",
    [
        "{{Map|name=Broken|x=notanumber|y=4064}}",
        "{{Map|name=Broken|y=4064}}",
        "{{Map|name=Broken}}",
    ],
)
def test_a_map_without_usable_coordinates_is_skipped(body):
    """Better no map than a link into the ocean."""
    assert from_wikitext(body) is None


def test_a_missing_name_falls_back_to_the_page_title():
    place = from_wikitext("{{Map|x=10|y=20}}", fallback_name="Somewhere")
    assert place.name == "Somewhere"


def test_zoom_defaults_when_the_template_omits_it():
    assert from_wikitext("{{Map|name=X|x=10|y=20}}").zoom == 2


# -- batching ---------------------------------------------------------------


async def test_pages_are_fetched_in_one_call():
    """One request however many pages were read; the map must not cost an
    extra round trip per citation."""
    wiki = FakeWiki({"Ungael": UNGAEL, "Lumbridge": LUMBRIDGE})
    places = await for_pages(wiki, ["Ungael", "Lumbridge"])
    assert len(wiki.requested) == 1
    assert [p.name for p in places] == ["Ungael", "Lumbridge"]


async def test_pages_without_maps_contribute_nothing():
    wiki = FakeWiki({"Ungael": UNGAEL, "Abyssal whip": "{{Infobox Item}}"})
    places = await for_pages(wiki, ["Abyssal whip", "Ungael"])
    assert [p.name for p in places] == ["Ungael"]


async def test_duplicate_titles_are_asked_for_once():
    wiki = FakeWiki({"Ungael": UNGAEL})
    await for_pages(wiki, ["Ungael", "Ungael"])
    assert wiki.requested == [["Ungael"]]


async def test_no_titles_means_no_request():
    wiki = FakeWiki()
    assert await for_pages(wiki, []) == []
    assert wiki.requested == []


async def test_a_wiki_failure_costs_the_map_and_not_the_answer():
    """A garnish must never turn a good answer into an error."""
    wiki = FakeWiki(error=RuntimeError("wiki on fire"))
    assert await for_pages(wiki, ["Ungael"]) == []


# -- following a page's own links -------------------------------------------
# A monster has no location, only somewhere it lives. The shortlist cannot
# supply that page: retrieval for "Vorkath" returns Vorkath/Strategies, the
# money-making guide and four combat achievements, and Ungael is not in the top
# twelve. Its own lead links to Ungael.


class LinkingWiki:
    """Serves wikitext for a page and for whatever it links to."""

    def __init__(self, pages):
        self._pages = pages
        self.asked: list[list[str]] = []

    async def wikitext(self, titles):
        self.asked.append(list(titles))
        return {t: self._pages[t] for t in titles if t in self._pages}


async def test_a_monsters_location_is_found_through_its_links():
    wiki = LinkingWiki({
        "Vorkath": "Vorkath is a boss on [[Ungael]], north of the [[Fremennik Province]].",
        "Ungael": "{{Map|name=Ungael|x=2272|y=4064|zoom=2}}",
    })
    found = await linked_from(wiki, "Vorkath")
    assert [m.name for m in found] == ["Ungael"]


async def test_namespaced_links_are_not_followed():
    """File:, Category: and friends are furniture, never places -- and each one
    followed is a title in a batched request that could have been a real page."""
    wiki = LinkingWiki({"Vorkath": "[[File:Vorkath.png]] [[Category:Bosses]] [[Ungael]]"})
    await linked_from(wiki, "Vorkath")
    followed = wiki.asked[-1]
    assert followed == ["Ungael"]


async def test_only_the_first_links_are_followed():
    """A busy page links to hundreds of things; the lead names the place and the
    navboxes at the bottom name everything else."""
    body = " ".join(f"[[Page {n}]]" for n in range(100))
    wiki = LinkingWiki({"Vorkath": body})
    await linked_from(wiki, "Vorkath")
    assert len(wiki.asked[-1]) == MAX_LINKS_FOLLOWED


async def test_a_page_that_cannot_be_fetched_costs_no_map_and_no_error():
    class Broken:
        async def wikitext(self, titles):
            raise RuntimeError("wiki is down")

    assert await linked_from(Broken(), "Vorkath") == []


async def test_a_page_with_no_links_yields_nothing():
    wiki = LinkingWiki({"Vorkath": "Vorkath is a boss."})
    assert await linked_from(wiki, "Vorkath") == []
