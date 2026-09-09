"""Structured wiki data, via the Bucket extension.

Everything else in this project reads the wiki as *prose* and pays for it.
``page_text`` cannot see tables at all, so :mod:`reldo.unlocks` parses rendered
HTML and then has to work out which column is which -- the shipbuilding tables
carry a ``Sailing level`` *and* a ``Construction level``, and picking the wrong
one answers a different question fluently. Bucket is the same facts as rows.

The wiki runs it: 74 extensions installed, ``Bucket`` among them, and 47 buckets
defined in the ``Bucket:`` namespace (id 9592) including ``quest``, ``dropsline``,
``recipe``, ``infobox_item`` and ``infobox_monster``.

**The query parameter is Lua source, not JSON.** That is not documented anywhere
obvious and the error messages are what give it away -- ``{"from":"items"}``
comes back with ``'}' expected near ':'``, which is a Lua parse error about a
table constructor. The shape is a builder::

    bucket('quest').select('page_name', 'official_difficulty').limit(2).run()

and the response is ``{"bucketQuery": ..., "bucket": [rows]}``, or the same with
a string ``error`` instead. A string, not the object every other MediaWiki
endpoint returns -- see :meth:`reldo.wiki.WikiClient._get`, which used to assume
otherwise and raised ``AttributeError`` on the first Bucket error it saw.

**Every value is escaped, because the query is code.** Quest names reach here
from the model, and an unescaped apostrophe in ``Cook's Assistant`` does not
merely break the query -- it ends the string literal and the rest is Lua. There
is no parameter binding to fall back on, so :func:`lua_string` is the whole
defence and is deliberately strict.

Two implicit details worth knowing, both learned by probing rather than from
documentation:

* ``page_name`` is a field on every bucket and is usually the only thing naming
  the row. ``Bucket:Quest`` defines nine fields and not one of them is the
  quest's name.
* ``Bucket:<Name>`` page content *is* the schema, as JSON. That makes
  :meth:`BucketClient.schema` a page fetch rather than an API call.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

from .wiki import WikiClient, WikiError

log = logging.getLogger(__name__)

# The `Bucket:` namespace, where each bucket's schema lives as a JSON page.
BUCKET_NAMESPACE = 9592

# Field and bucket names are identifiers in the query language. Validated rather
# than escaped: there is no legitimate reason for one to contain anything else,
# and a name that needs escaping is a bug rather than a value.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Skill requirements inside a quest's rendered requirement list. The wiki marks
# them up for its own scripts -- `data-skill="Magic" data-level="75"` -- which is
# the difference between reading a requirement and parsing a sentence about one.
_SKILL_REQUIREMENT = re.compile(
    r'data-skill="([^"]+)"[^>]*?data-level="(\d+)"', re.I
)


class BucketError(RuntimeError):
    """The Bucket API refused the query, or answered in a shape we can't use."""


def lua_string(value: str) -> str:
    """One Lua single-quoted string literal.

    The query parameter is executed as Lua, so this is the only thing standing
    between a page title and arbitrary code. Backslash first -- escaping the
    quote first would then have its own backslash escaped and reopen the hole.

    Control characters are rejected outright rather than escaped. Nothing that
    legitimately reaches here contains one, and a newline inside a string
    literal is a syntax error in Lua anyway.
    """
    text = str(value)
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise BucketError(f"Control character in Bucket value: {text!r}")
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


# Spellings tried before a lookup is called a miss. Each is a request, so this
# is the cost of a name that needs correcting rather than one paid every time.
MAX_NAME_VARIANTS = 4


def name_variants(name: str) -> list[str]:
    """The spellings a wiki page might use for what somebody typed.

    Bucket matches ``page_name`` exactly. Case is forgiven and nothing else is,
    so a plural or a space in a closed-up compound misses completely -- and a
    miss here is reported as "no recipe for that", which reads as a fact about
    the item rather than about the spelling. Measured: ``products_of("gold
    bars")`` returns nothing where ``"gold bar"`` returns forty.

    The same class of miss :func:`reldo.ge.GEClient.find` had, arriving the same
    way: the catalogue names one of a thing and people ask for several.

    Ordered most-likely-first, deduplicated case-insensitively, and capped.
    """
    base = " ".join(str(name).split())
    if not base:
        return []
    low = base.lower()
    # -es before -s: a fish, a bush and a box all take the longer ending, and
    # chopping one letter leaves a stem that matches nothing.
    forms = [base]
    for suffix, keep in (("es", -2), ("s", -1)):
        if low.endswith(suffix) and len(low) > len(suffix) + 2:
            forms.append(base[:keep])
    # Spacing applied to each, so "Sword fishes" reaches "Swordfish" and not
    # merely "Swordfishes". Singulars before closed-up forms: a plural is the
    # commoner slip, so it should cost the earlier request.
    out = forms + [f.replace(" ", "") for f in forms if " " in f]

    seen: set[str] = set()
    unique: list[str] = []
    for variant in out:
        key = variant.lower()
        if variant and key not in seen:
            seen.add(key)
            unique.append(variant)
    return unique[:MAX_NAME_VARIANTS]


# What turns a quest *mentioned* on a page into the quest that *gates* it, in
# two strengths, because a page names quests for more than one reason and the
# strong form has to win. Fossil Island is the case that proves it: its lead
# says players "visit during the quest Dragon Slayer II" -- of a neighbouring
# island, one sentence before -- and then "must have completed the Bone Voyage",
# which is the actual gate. Reading either kind of mention as equal, or taking
# whichever comes first, answers Dragon Slayer II.
_GATE_STATED = re.compile(
    r"\b(?:must (?:be |have )?complet\w+|requires? (?:the )?completion|requires?|"
    r"after complet\w+|completing|unlocked by|need(?:s|ed)? to (?:have )?complet\w+)\b",
    re.I,
)
# Weaker: the thing is *encountered* in a quest, which usually means the quest
# gates it and sometimes only means they share a story. Vorkath's own lead is
# this form -- "first encountered during the Dragon Slayer II quest" -- so it
# cannot simply be dropped; it is the fallback when nothing states a gate.
_GATE_IMPLIED = re.compile(r"\b(?:during|after|partway|access\w*|unlock\w+)\b", re.I)

# The sentence is the unit, not a character window. A window wide enough to
# hold "must have completed" reaches into the next sentence, and on Fossil
# Island the next sentence is where the real gate lives -- so a 120-character
# window credited Dragon Slayer II with Bone Voyage's words.
_SENTENCE = re.compile(r"[^.!?]+[.!?]*")


def gating_quest(text: str, quests: Sequence[str]) -> str | None:
    """The quest a page says gates the thing it is about, or None.

    Prose in, but not prose *parsing*: every candidate is matched against the
    wiki's own list of quests, so the only thing being decided here is which
    known quest is named and whether it is named as a gate. A name that is not
    on that list cannot come out of this, which is the difference between
    reading a page and inventing a quest that sounds like one.

    A stated gate beats an implied one wherever both appear, rather than
    whichever is mentioned first; see the patterns above for why.

    Longest first, and that is not a tidiness preference -- "Dragon Slayer I"
    and "Dragon Slayer II" are both quests and one is a prefix of the other.
    Word boundaries stop the prefix matching inside the longer name, and the
    ordering means the more specific answer is reached first either way. This
    case had already been answered "Dragon Slayer I" once.
    """
    sentences = _SENTENCE.findall(text)
    by_length = sorted(quests, key=len, reverse=True)
    for pattern in (_GATE_STATED, _GATE_IMPLIED):
        for sentence in sentences:
            if not pattern.search(sentence):
                continue
            for quest in by_length:
                if re.search(rf"\b{re.escape(quest)}\b", sentence):
                    return quest
    return None


def _parse_recipe(row: dict, fallback_name: str) -> dict | None:
    """One recipe row's production template, as a dict. None if it has none.

    Shared by every recipe lookup here: an empty body and unparseable JSON are
    both "no recipe" rather than an error, since a page can carry a recipe row
    with nothing in it.
    """
    try:
        body = json.loads(row.get("production_json") or "{}")
    except ValueError:
        log.warning("Recipe json for %r was not JSON", fallback_name)
        return None
    if not body:
        return None
    body["page_name"] = row.get("page_name") or fallback_name
    return body


def _identifier(name: str, *, what: str) -> str:
    if not _IDENTIFIER.match(name or ""):
        raise BucketError(f"Not a valid {what}: {name!r}")
    return name


def build_query(
    bucket: str,
    fields: list[str],
    *,
    where: dict[str, object] | None = None,
    limit: int | None = None,
) -> str:
    """The Lua one-liner for a straightforward select.

    Exposed and tested separately from the request because the escaping is the
    part worth pinning: a test that goes through HTTP proves the round trip and
    tells you nothing about what would happen to ``Cook's Assistant``.
    """
    _identifier(bucket, what="bucket name")
    if not fields:
        raise BucketError("Bucket requires at least one field to select.")
    for field in fields:
        _identifier(field, what="field name")

    query = f"bucket({lua_string(bucket)})"
    query += ".select(" + ", ".join(lua_string(f) for f in fields) + ")"
    for field, value in (where or {}).items():
        _identifier(field, what="field name")
        query += f".where({lua_string(field)}, {lua_string(str(value))})"
    if limit is not None:
        query += f".limit({int(limit)})"
    return query + ".run()"


class BucketClient:
    """Structured queries against the wiki's Bucket extension.

    Wraps a :class:`~reldo.wiki.WikiClient` rather than opening its own
    connection: same host, same rate limit, same User-Agent. Bucket queries are
    ordinary API calls and there is no reason for them to escape the politeness
    the rest of the project pays for.
    """

    def __init__(self, client: WikiClient) -> None:
        self._client = client
        self._schemas: dict[str, dict] = {}
        self._quests: list[str] | None = None

    async def query(self, lua: str) -> list[dict]:
        """Run a raw Bucket query and return its rows.

        Raises:
            BucketError: the query was rejected. The message is the extension's
                own, which is unusually good -- "Field name not found in bucket
                infobox_item" tells you exactly what to fix.
        """
        try:
            payload = await self._client._get({"action": "bucket", "query": lua})
        except WikiError as exc:
            raise BucketError(f"{exc} (query: {lua})") from exc
        rows = payload.get("bucket")
        if rows is None:
            raise BucketError(f"No rows in Bucket response: {str(payload)[:200]}")
        # A bare builder with no .run() returns the builder object itself, which
        # is a dict rather than a list and means the query was never executed.
        if isinstance(rows, dict):
            raise BucketError(f"Query was not run -- did you forget .run()? {lua}")
        return rows

    async def select(
        self,
        bucket: str,
        fields: list[str],
        *,
        where: dict[str, object] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Rows from one bucket. The ordinary entry point."""
        return await self.query(build_query(bucket, fields, where=where, limit=limit))

    async def buckets(self) -> list[str]:
        """Every bucket defined on the wiki, lowercased as queries spell them.

        The namespace lists them as page titles (``Bucket:Infobox item``) and
        queries take them as identifiers (``infobox_item``); the difference is
        exactly a case fold and a space swap, and getting it wrong produces
        "Bucket X does not exist" rather than anything that hints at why.
        """
        payload = await self._client._get(
            {
                "action": "query",
                "list": "allpages",
                "apnamespace": BUCKET_NAMESPACE,
                "aplimit": 500,
            }
        )
        return [
            page["title"].split(":", 1)[-1].lower().replace(" ", "_")
            for page in payload.get("query", {}).get("allpages", [])
        ]

    async def schema(self, bucket: str) -> dict[str, dict]:
        """Field definitions for one bucket, cached.

        The schema is the ``Bucket:`` page's content, which is JSON. Note that
        ``page_name`` is queryable on every bucket and appears in no schema, so
        it is added here -- it is usually the only field that names the row, and
        a caller reading the schema to decide what to select would otherwise
        never learn it exists.
        """
        key = bucket.lower()
        if key in self._schemas:
            return self._schemas[key]

        title = "Bucket:" + bucket.replace("_", " ").capitalize()
        pages = await self._client.wikitext([title])
        raw = pages.get(title)
        if not raw:
            raise BucketError(f"No schema page for bucket {bucket!r} (looked at {title!r})")
        try:
            fields = json.loads(raw)
        except ValueError as exc:
            raise BucketError(f"Schema for {bucket!r} is not JSON: {exc}") from exc
        fields.setdefault("page_name", {"type": "PAGE", "implicit": True})
        self._schemas[key] = fields
        return fields

    async def recipe(self, item: str, *, variants: bool = True) -> dict | None:
        """How an item is made: the skills it needs, and what it consumes.

        The same call as :meth:`quest_requirements`, for the other half of
        "what level do I need". Those levels live in a page's production
        template, which ``page_text`` strips along with every other table -- so
        the model reads the prose around it and picks up whichever nearby number
        looks like a requirement. Measured: asked what Cooking level a shark
        needs, it answered the level at which you stop *burning* them.

        Returns None for anything with no recipe, which is a real answer rather
        than a failure: a dragon pickaxe is a drop, not something you make.
        """
        # Variants cost a request each, so only where a name might need
        # correcting. A title off the shortlist came out of the wiki's own index
        # and is already exact -- first_recipe walks six of them and multiplying
        # that by four spellings would turn one lookup into twenty-four.
        for variant in name_variants(item) if variants else [item]:
            rows = await self.select(
                "recipe",
                ["page_name", "production_json"],
                where={"page_name": variant},
                limit=1,
            )
            if rows:
                return _parse_recipe(rows[0], variant)
        return None

    async def recipe_from_material(self, material: str, skill: str) -> dict | None:
        """What you turn a material into to train a skill.

        The other direction, and the one search cannot cover. Asked how much
        gold to smelt, the shortlist is "Smithing", "Furnace", "Gold ore",
        "Blast Furnace" -- everything about smelting gold and not the page that
        holds the recipe, because "Gold bar" is not what the question says.
        ``uses_material`` and ``uses_skill`` are indexed fields on the recipe
        bucket, so asking "what does Smithing make out of Gold ore" is one query
        and returns the bar.

        The skill is not optional here. Gold ore feeds Smithing and Crafting
        feeds off the bar; without it this returns whichever recipe happens to
        be first and answers a question nobody asked.
        """
        rows = await self.select(
            "recipe",
            ["page_name", "production_json"],
            where={"uses_skill": skill, "uses_material": material},
            # Several, because the same product repeats per facility -- gold bar
            # comes back three times, for the furnace, the Blast Furnace and the
            # Prifddinas one -- and only some rows carry a usable body.
            limit=5,
        )
        for row in rows:
            found = _parse_recipe(row, row.get("page_name", ""))
            if found and found.get("skills"):
                return found
        return None

    async def first_recipe(
        self, titles: list[str], *, limit: int = 6, skill: str | None = None
    ) -> dict | None:
        """The first of these pages that is actually made from something.

        Walking the shortlist rather than picking one title, because picking is
        what fails here. Word overlap chooses "Rune platebody (h4)" over the
        plain platebody, "Cannonball" over "Steel cannonball" -- which is the
        page carrying the level -- and a money-making guide over "Shark". The
        shortlist is already ordered by relevance and one of its entries has the
        recipe; the job is to find which.

        Same call :func:`reldo.maps.for_pages` makes over a shortlist for the
        same reason: the top hit is about the subject, and the page with the
        structured data on it is often the one below.

        Only recipes carrying a skill level count. A page that is technically
        made from something but requires no level answers nothing.

        ``skill`` names the skill the asker actually mentioned, and promotes any
        recipe that trains it over one that merely ranked higher. "How much gold
        do I need to smelt for Smithing" shortlists jewellery before the bar,
        and a gold necklace is a real recipe for the wrong skill -- answering
        from it is fluent, structured and about Crafting. A recipe for the named
        skill wins; with none, the best of the rest is still better than nothing.
        """
        fallback = None
        for title in titles[:limit]:
            found = await self.recipe(title, variants=False)
            if not (found and found.get("skills")):
                continue
            if skill is None or any(
                str(entry.get("name", "")).lower() == skill.lower()
                for entry in found["skills"]
            ):
                return found
            fallback = fallback or found
        return fallback

    async def products_of(self, material: str, *, limit: int = 60) -> list[str]:
        """Everything the wiki says is made from a material.

        The list half of "what made from gold bars sells best". Left to assemble
        that set itself the model named three items -- the gold necklace, amulet
        and bracelet -- and the wiki lists forty, because every sapphire, ruby
        and zenyte ring is a gold bar plus a gem. Ranking the three it thought of
        answers a question nobody asked, and the answer is wrong by 14x.

        The same lesson as :func:`reldo.unlocks.scan_page`: a list a model
        assembles from memory is a list it invented, and this one is an indexed
        field.

        Page names only, de-duplicated and sorted. A product repeats per
        facility and per recipe variant, and the caller wants the set.
        """
        for variant in name_variants(material):
            rows = await self.select(
                "recipe", ["page_name"], where={"uses_material": variant}, limit=limit
            )
            found = sorted({r["page_name"] for r in rows if r.get("page_name")})
            if found:
                return found
        return []

    async def quest_names(self) -> list[str]:
        """Every quest the wiki knows, cached for the life of this client.

        Used to decide whether a question is about a quest at all, so it must
        cost one request rather than one per question.
        """
        if self._quests is None:
            rows = await self.select("quest", ["page_name"], limit=500)
            self._quests = [r["page_name"] for r in rows if r.get("page_name")]
        return self._quests

    async def quest_requirements(self, quest: str) -> tuple[str, dict[str, int]] | None:
        """A quest's skill requirements as numbers, not as a sentence.

        Returns the wiki's page title alongside ``{skill: level}``, or None if
        no such quest is in the bucket.

        The levels come from the ``data-skill``/``data-level`` attributes the
        wiki puts on each requirement for its own scripts, so this reads a
        machine-readable field rather than parsing prose. That matters here more
        than usual: the plain ``requirements`` column says "None" for
        Cook's Assistant and holds rendered markup for everything harder, and a
        model asked to total up a requirement list is exactly where it drops one.

        "Quest points" comes through as a requirement like any other, because
        that is how the wiki marks it up and it gates quests the same way.
        """
        rows = await self.select(
            "quest", ["page_name", "json"], where={"page_name": quest}, limit=1
        )
        if not rows:
            return None
        title = rows[0].get("page_name") or quest
        try:
            body = json.loads(rows[0].get("json") or "{}")
        except ValueError:
            log.warning("Quest json for %r was not JSON", title)
            return title, {}

        found: dict[str, int] = {}
        for skill, level in _SKILL_REQUIREMENT.findall(str(body.get("requirements", ""))):
            # Highest wins. A quest can list the same skill twice -- once to
            # start and once to finish -- and the binding requirement is the
            # larger of the two.
            found[skill] = max(int(level), found.get(skill, 0))
        return title, found
