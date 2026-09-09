"""What a skill unlocks at or below a level, read out of the wiki's tables.

"Everything I can build at Sailing 20 and below" is a filter over a table, and
the wiki has the table. Two things stop the agent answering it unaided: the
tables are invisible to ``page_text`` (``prop=extracts`` strips them), and
filtering thirty rows on a numeric column is the sort of task a 24B model does
*almost* right -- it drops a row, or includes the level-31 one, and the answer
looks complete either way.

So the filtering happens here, in code, the same call :mod:`reldo.skills` makes
about XP arithmetic and :mod:`reldo.ge` makes about ranking. The model gets a
finished list.

Column detection is deliberately conservative. The shipbuilding tables carry a
``Sailing level`` *and* a ``Construction level``, and picking the wrong one
answers a different question fluently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Headers that name the thing being unlocked rather than a statistic about it.
_NAME_HEADERS = ("tier", "name", "item", "component", "boat", "ship", "type", "part")

_INT = re.compile(r"^\s*(\d{1,3})\s*$")


@dataclass(frozen=True, slots=True)
class Unlock:
    """One row of a requirements table, filtered in."""

    name: str
    level: int
    source: str
    extras: dict[str, str] = field(default_factory=dict)


def _level_column(header: list[str], skill: str) -> int | None:
    """Index of the ``<skill> level`` column, or None if this table has none.

    Requires the skill name. A bare "Level" match would grab the Construction
    column on every shipbuilding table, which is a different requirement and
    reads perfectly plausibly.
    """
    want = skill.strip().lower()
    for i, cell in enumerate(header):
        low = cell.strip().lower()
        if low in (f"{want} level", f"{want} lvl", want):
            return i
    return None


def _name_column(header: list[str], rows: list[list[str]], level_col: int) -> int:
    """Which column holds the thing's name.

    Has to look at the data, not just the header. Expanding ``colspan`` gives
    the helm table two "Tier" columns -- the first is the icon cell, empty in
    every row -- so trusting the header alone names everything "" and the whole
    table filters out with no error anywhere.
    """

    def populated(index: int) -> int:
        return sum(
            1 for r in rows if index < len(r) and r[index].strip() and not _INT.match(r[index])
        )

    named = [
        i
        for i, cell in enumerate(header)
        if i != level_col and cell.strip().lower() in _NAME_HEADERS
    ]
    candidates = named or [i for i in range(len(header)) if i != level_col]
    return max(candidates, key=populated) if candidates else 0


def from_table(
    table: list[list[str]], skill: str, max_level: int, source: str
) -> list[Unlock]:
    """Rows of one table whose ``<skill> level`` is at or below ``max_level``."""
    if len(table) < 2:
        return []
    header = table[0]
    level_col = _level_column(header, skill)
    if level_col is None:
        return []
    name_col = _name_column(header, table[1:], level_col)

    found: list[Unlock] = []
    for row in table[1:]:
        if len(row) <= max(level_col, name_col):
            continue
        match = _INT.match(row[level_col])
        if not match:
            continue  # sub-headers, blank spacer rows, "N/A"
        level = int(match.group(1))
        if level > max_level:
            continue
        name = row[name_col].strip()
        if not name:
            continue
        extras = {
            header[i].strip(): cell.strip()
            for i, cell in enumerate(row)
            if i not in (level_col, name_col)
            and i < len(header)
            and cell.strip()
            and header[i].strip()
        }
        found.append(Unlock(name=name, level=level, source=source, extras=extras))
    return found


def unlevelled(table: list[list[str]], skill: str) -> list[str]:
    """Names of rows in a ``<skill> level`` table that state no level.

    :func:`from_table` drops them, which is right when the question is "what
    can I do at 25" -- a row with no number is not an unlock at any particular
    level. It is wrong when the question is "what level do I need", because a
    row stating no level is the answer that you need none.

    The Sorceress's Garden table is the case: Winter N/A, Spring 25, Autumn 45,
    Summer 65. Reported without the first row it reads as a minimum of 25, and
    the truth is that the minigame starts at 1.
    """
    if len(table) < 2:
        return []
    header = table[0]
    level_col = _level_column(header, skill)
    if level_col is None:
        return []
    name_col = _name_column(header, table[1:], level_col)
    out: list[str] = []
    for row in table[1:]:
        if len(row) <= max(level_col, name_col) or _INT.match(row[level_col]):
            continue
        name = row[name_col].strip()
        # A blank cell is a spacer; "N/A" is a statement.
        if name and row[level_col].strip():
            out.append(name)
    return out


def dedupe(unlocks: list[Unlock]) -> list[Unlock]:
    """Collapse repeats, keeping the lowest level each thing is available at.

    Tables repeat across sections -- the shipbuilding page lists hulls both in
    an overview and again per ship class -- and listing "Oak, level 20" three
    times reads as three unlocks.
    """
    # Stage one: identical rows appearing under two headings. Keyed on the whole
    # row, not the name -- a bronze keel and a bronze helm are both "Bronze" at
    # level 1 and are not the same unlock; the other columns tell them apart.
    # Last wins, because MediaWiki puts the overview first and the detailed
    # per-component sections after it, so the later source is the specific one
    # ("Shipbuilding - Helm" over "Shipbuilding - Core boat parts").
    rows: dict[tuple[str, int, frozenset[tuple[str, str]]], Unlock] = {}
    for u in unlocks:
        rows[(u.name.lower(), u.level, frozenset(u.extras.items()))] = u

    # Stage two: the same tier repeated within one section because the table has
    # a variant per ship class -- Wooden hull at level 1 appears three times with
    # different HP. One unlock, listed once; the per-class stats are detail, and
    # three "Wooden, level 1" lines read as three separate things you can build.
    best: dict[tuple[str, str, int], Unlock] = {}
    for u in rows.values():
        best.setdefault((u.source.lower(), u.name.lower(), u.level), u)
    return sorted(best.values(), key=lambda u: (u.level, u.source, u.name))


def render(unlocks: list[Unlock], skill: str, max_level: int) -> str:
    """Group by source and list by level. Model-readable and human-readable."""
    if not unlocks:
        return (
            f"Nothing found requiring {skill} level {max_level} or below. Either "
            f"the page has no '{skill} level' column, or the requirements are not "
            "in a table."
        )

    by_source: dict[str, list[Unlock]] = {}
    for u in unlocks:
        by_source.setdefault(u.source, []).append(u)

    lines = [f"Everything at {skill} level {max_level} or below ({len(unlocks)} total):"]
    for source, group in by_source.items():
        lines.append(f"\n{source}:")
        for u in group:
            detail = ""
            if u.extras:
                # A couple of the most useful columns, not the whole row.
                keep = list(u.extras.items())[:3]
                detail = "  (" + ", ".join(f"{k} {v}" for k, v in keep) + ")"
            lines.append(f"  {u.level:>3}  {u.name}{detail}")
    return "\n".join(lines)


async def scan_page(client, title: str, skill: str, max_level: int) -> list[Unlock]:
    """Every qualifying row from every table on one page.

    Section by section rather than whole-page, because the section heading is
    the only thing that says what a table *is* -- "Hull", "Helm", "Keel" are
    three tables with identical column names, and a flat list of tiers with no
    labels is not an answer.
    """
    found: list[Unlock] = []
    try:
        sections = await client.sections(title)
    except Exception:
        sections = []

    for section in sections:
        try:
            tables = await client.tables(title, section.index)
        except Exception:
            continue
        for table in tables:
            found += from_table(table, skill, max_level, f"{title} - {section.line}")

    if not found:
        # Pages with no section structure, or where the table sits above the
        # first heading and section reads therefore miss it.
        try:
            for table in await client.tables(title):
                found += from_table(table, skill, max_level, title)
        except Exception:
            pass
    return found


async def count_level_tables(client, title: str, skill: str) -> int:
    """Cheap triage: how many ``<skill> level`` tables does this page have?

    One request for the whole page, against one-per-section for a full scan.
    That is what makes it affordable to consider eight candidates: the pages
    that actually answer a Sailing question rank fourth and seventh across two
    searches, so a budget small enough to be cheap per page has to be spent on
    a wide net rather than a deep one.

    A count rather than a yes/no, because it doubles as a relevance ranking.
    "Shipbuilding" has nine such tables and the single-item page for one mast
    has one; scanning in search-rank order put the single-item page first and
    the hub page outside the budget, which is how a four-category answer came
    back as one mast.
    """
    try:
        tables = await client.tables(title)
    except Exception:
        return 0
    return sum(1 for t in tables if t and _level_column(t[0], skill) is not None)
