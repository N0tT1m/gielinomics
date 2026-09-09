"""Map links for pages that have a map.

"Vorkath is on Ungael, north of the Fremennik Province" is a worse answer than
the same sentence with somewhere to click. The wiki already knows where things
are -- location pages carry ``{{Map|name=Ungael|x=2272|y=4064|zoom=2}}`` in their
infobox -- so this is extraction, not inference, and nothing here guesses at a
coordinate it was not given.

**The link format was read out of the map viewer, not assumed.** The viewer at
maps.runescape.wiki is a Leaflet SPA that keeps its state in the URL fragment,
and its ``parseHash`` splits on ``/`` into exactly five fields::

    parseHash: s = t.split("/"); 5 === s.length && {
        zoom: s[0], mapID: s[1], plane: s[2], center: [s[4], s[3]]
    }

So the fragment is ``#zoom/mapID/plane/x/y`` -- note that x precedes y despite
the viewer calling them lng and lat internally, which is exactly the sort of
detail that produces a link pointing into the ocean if you guess it. Ungael is
``#2/0/0/2272/4064``.

Query parameters do not work here. The ``?m=&z=&p=&x=&y=`` form documented for
the RS3 map is a different application; this one reads only the fragment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAP_BASE = "https://maps.runescape.wiki/osrs/"

# Regional view. The template's own default varies by page -- Ungael ships
# zoom=2 and Lumbridge zoom=1 -- so this only applies when the page omits it.
DEFAULT_ZOOM = 2

# Only the capitalised infobox template, and only up to its first closing
# braces. Lowercase {{map|3208,3214|type=maplink|...}} is a different thing
# entirely: an inline pin for one spawn, and a busy page like Lumbridge carries
# dozens of them. Collecting those would bury the page's actual location under a
# list of where its bronze daggers are.
_MAP_TEMPLATE = re.compile(r"\{\{Map\s*\|([^{}]*?)\}\}", re.S)

_INT = re.compile(r"^-?\d{1,6}$")

# Link targets in wikitext, without the section anchor or the display label.
_WIKILINK = re.compile(r"\[\[([^\]|#]+)")

# Namespaced links are furniture, never places.
_NOT_A_PLACE = ("file:", "image:", "category:", "template:", "help:", "user:")

# A page's own links, in document order, are roughly most-relevant-first: the
# lead names the place, the navboxes at the bottom name everything else. Twenty
# is deep enough to clear the infobox and the first paragraphs and shallow
# enough to stay one batched request.
MAX_LINKS_FOLLOWED = 20


@dataclass(frozen=True, slots=True)
class MapLocation:
    """Somewhere on the world map, and the link that opens it there."""

    name: str
    x: int
    y: int
    zoom: int = DEFAULT_ZOOM
    plane: int = 0
    map_id: int = 0

    @property
    def url(self) -> str:
        return f"{MAP_BASE}#{self.zoom}/{self.map_id}/{self.plane}/{self.x}/{self.y}"


def _fields(body: str) -> dict[str, str]:
    """``name=Ungael|x=2272|y=4064`` as a dict, positional arguments dropped."""
    out: dict[str, str] = {}
    for part in body.split("|"):
        key, sep, value = part.partition("=")
        if sep:
            out[key.strip().lower()] = value.strip()
    return out


def _as_int(value: str | None, default: int | None = None) -> int | None:
    if value is None:
        return default
    return int(value) if _INT.match(value) else default


def from_wikitext(wikitext: str, *, fallback_name: str = "") -> MapLocation | None:
    """The page's primary map, if it declares one.

    First match only. The infobox map is the page's own location and comes
    first; anything later is a second map of somewhere else it mentions, and
    "here is where this article is about" is the only one worth a line in a
    Discord embed.
    """
    for match in _MAP_TEMPLATE.finditer(wikitext):
        fields = _fields(match.group(1))
        # A maplink pin carries type=; the infobox map never does. Checked
        # rather than assumed from the capital M, because template names are
        # case-insensitive on their first letter in MediaWiki.
        if "type" in fields:
            continue
        x, y = _as_int(fields.get("x")), _as_int(fields.get("y"))
        if x is None or y is None:
            continue
        return MapLocation(
            name=fields.get("name") or fallback_name,
            x=x,
            y=y,
            zoom=_as_int(fields.get("zoom"), DEFAULT_ZOOM) or DEFAULT_ZOOM,
            plane=_as_int(fields.get("plane"), 0) or 0,
            map_id=_as_int(fields.get("mapid"), 0) or 0,
        )
    return None


async def for_pages(client, titles: list[str]) -> list[MapLocation]:
    """Maps for whichever of these pages have one, in the order given.

    One batched call for up to 50 titles, so adding this to an answer costs a
    single extra request regardless of how many pages were read. Pages without a
    map -- most monsters and every item -- simply contribute nothing, which is
    the intended outcome rather than a failure: a whip has no location.
    """
    if not titles:
        return []
    wanted = list(dict.fromkeys(titles))[:50]
    try:
        pages = await client.wikitext(wanted)
    except Exception:  # a map is a garnish; never fail an answer over one
        return []

    found = []
    for title in wanted:
        location = from_wikitext(pages.get(title, ""), fallback_name=title)
        if location:
            found.append(location)
    return found


async def linked_from(client, title: str) -> list[MapLocation]:
    """Maps on the pages a page links to, for a page that has none of its own.

    A monster has no location: "Vorkath" declares no ``{{Map}}`` and the answer
    to "where is Vorkath" is Ungael, which its own lead links to.

    The shortlist cannot supply that, which is what this exists for. Retrieval
    for "Vorkath" returns ``Vorkath/Strategies``, the money-making guide, four
    combat achievements and ``Ava's assembler``; ``Ungael`` is not in the top
    twelve. The comment upstream in this module claimed the shortlist scan
    handled the Vorkath case, and it never did -- the page it needed was never
    a candidate.

    Two requests, so this is a fallback rather than the first move: worth it for
    ``/map``, where somebody explicitly asked where something is, and not worth
    adding to every answer about an item.
    """
    try:
        pages = await client.wikitext([title])
    except Exception:  # a map is a garnish; never fail an answer over one
        return []
    raw = pages.get(title, "")
    if not raw:
        return []

    linked = [
        name.strip()
        for name in dict.fromkeys(_WIKILINK.findall(raw))
        if name.strip() and not name.strip().lower().startswith(_NOT_A_PLACE)
    ]
    return await for_pages(client, linked[:MAX_LINKS_FOLLOWED])
