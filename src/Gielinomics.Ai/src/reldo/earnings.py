"""What actually makes money, read out of the wiki's money-making guide.

The twin of :mod:`reldo.unlocks`, and it exists for the identical reason. "Which
skill is best for making money" is a ranking over a table, and the wiki has the
table -- ``Money making guide/Skilling`` carries Method, Hourly profit, Skills
and Category for a few hundred methods, and the wiki computes that profit column
from live Grand Exchange prices, which is exactly what somebody asking "based on
GE prices" means.

Two things stop the agent answering it unaided, and they are the two this
project keeps meeting:

* The figures are in a table, and ``page_text`` cannot see tables at all. The
  model reads the guide, finds a page with no numbers in it, and then either
  says the wiki does not say -- which is what it did -- or names a method it
  remembers. Measured on mistral-small3.2:24b: "The wiki does not say which
  skilling method makes the most money", citing the two pages that do; and on a
  second run, High Level Alchemy, which is not in the top thirty.
* Ranking a few hundred rows by a comma-formatted number and grouping them by
  skill is arithmetic, and arithmetic is where a 24B model is confidently wrong.

So the reading and the ranking happen here, in code, and the model gets a
finished list. The same call :mod:`reldo.ge` makes about comparing prices and
:mod:`reldo.unlocks` makes about filtering requirements.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .skills import SKILLS

# The guide and its skilling-only subpage. Both carry the same column layout;
# the subpage is the one a "which skill" question wants, and the parent adds
# combat and non-skilling methods for "what makes the most money overall".
GUIDE_PAGE = "Money making guide"
SKILLING_GUIDE_PAGE = "Money making guide/Skilling"

# Columns are matched on what the header *contains*, not on the exact string
# the guide happens to use today. An exact match is one wiki edit away from
# finding nothing, and finding nothing here is silent: the pass returns empty,
# the model goes back to answering "the wiki does not say which skilling method
# makes the most money", and there is no error to connect that to a renamed
# column. Tolerant matching costs nothing and removes a tripwire.


def _is_method(cell: str) -> bool:
    return "method" in cell or "activity" in cell


def _is_hourly_profit(cell: str) -> bool:
    """A profit column that is explicitly per hour, and only that.

    Both halves are load-bearing. Drop "profit" and this matches a Time column;
    drop the hour and it matches the parent page's Profit-over-Time table, where
    484,000 per 25 minutes would be ranked as an hourly rate against methods
    that really are hourly -- comparing two numbers that mean different things,
    which is the whole failure ge.compare exists to prevent one level up.
    """
    return "profit" in cell and any(unit in cell for unit in ("hour", "hr", "/h"))


def _is_skills(cell: str) -> bool:
    return "skill" in cell


def _is_category(cell: str) -> bool:
    return "categor" in cell

# "4,340,000" -> 4340000. Anything else in the column -- a range, a dash, an
# empty cell -- is not a number we can rank on and the row is dropped rather
# than guessed at.
_PROFIT = re.compile(r"^\s*(\d[\d,]*)\s*$")

# The guide files a skilling method as "Skilling/<Skill>". That is the wiki's
# own attribution and beats reading the Skills column, which lists everything a
# method benefits from -- "Thieving 85+, Agility 50, Magic 47+, Herblore 93" is
# one Thieving method, and taking the first name there is a coin toss.
_SKILLING_CATEGORY = re.compile(r"^\s*skilling\s*/\s*(.+?)\s*$", re.I)


@dataclass(frozen=True, slots=True)
class Method:
    """One row of the money-making guide, with its profit as a number."""

    name: str
    gp_per_hour: int
    skills: str
    category: str

    @property
    def skill(self) -> str:
        """The skill this trains, or "" when it is not a skilling method.

        The category first, because it is the wiki's own filing. Falling back to
        the Skills column is for the parent page, where a skilling method can be
        categorised by content rather than by skill.
        """
        found = _SKILLING_CATEGORY.match(self.category)
        if found:
            named = found.group(1).strip()
            for skill in SKILLS:
                if skill.lower() == named.lower():
                    return skill
        for skill in SKILLS:
            if re.search(rf"\b{re.escape(skill.lower())}\b", self.skills.lower()):
                return skill
        return ""


def _column(header: list[str], matches) -> int | None:
    for i, cell in enumerate(header):
        if matches(cell.strip().lower()):
            return i
    return None


def methods_from_table(table: list[list[str]]) -> list[Method]:
    """Every rankable row of one guide table. Empty for a table that is not one.

    Requires both a Method and an Hourly profit column, which is what keeps the
    navbox at the bottom of the page -- also a table, also full of method names
    -- from arriving as three hundred methods worth zero gp an hour.
    """
    if len(table) < 2:
        return []
    header = table[0]
    name_col = _column(header, _is_method)
    profit_col = _column(header, _is_hourly_profit)
    if name_col is None or profit_col is None:
        return []
    skills_col = _column(header, _is_skills)
    category_col = _column(header, _is_category)

    def cell(row: list[str], index: int | None) -> str:
        return row[index].strip() if index is not None and index < len(row) else ""

    found: list[Method] = []
    for row in table[1:]:
        if len(row) <= max(name_col, profit_col):
            continue
        profit = _PROFIT.match(row[profit_col])
        name = row[name_col].strip()
        if not profit or not name:
            continue
        found.append(
            Method(
                name=name,
                gp_per_hour=int(profit.group(1).replace(",", "")),
                skills=cell(row, skills_col),
                category=cell(row, category_col),
            )
        )
    return found


def best_per_skill(methods: list[Method]) -> list[Method]:
    """The single best-paying method for each skill, richest skill first.

    Which is what "what skill is best for making money" is actually asking. A
    flat top-twenty answers a different question -- it is nine Thieving rows and
    reads as though nothing else earns.
    """
    best: dict[str, Method] = {}
    for method in methods:
        skill = method.skill
        if not skill:
            continue
        if skill not in best or method.gp_per_hour > best[skill].gp_per_hour:
            best[skill] = method
    return sorted(best.values(), key=lambda m: -m.gp_per_hour)


def rank(methods: list[Method]) -> list[Method]:
    """Every method, best-paying first."""
    return sorted(methods, key=lambda m: -m.gp_per_hour)


def render(methods: list[Method], *, limit: int = 15, by_skill: bool = True) -> str:
    """The ranking as something the model can only report, not re-derive.

    Leads with the answer outright for the reason :func:`reldo.ge._verdict`
    does: handed a table alone, the model picks a row it recognises. Naming the
    winner in a sentence leaves it nothing to decide.
    """
    if not methods:
        return (
            "No money-making methods with an hourly profit column were found. "
            "The guide's tables may have changed shape."
        )
    shown = methods[:limit]
    top = shown[0]
    subject = f"{top.skill} " if by_skill and top.skill else ""
    head = (
        f"ANSWER: {subject}is the best of these for making money -- "
        f"{top.name} at {top.gp_per_hour:,} gp/hour. These figures are the "
        "wiki's own, computed from live Grand Exchange prices."
    )
    width = max(len(m.name) for m in shown)
    lines = [head, ""]
    if by_skill:
        lines.append(f"{'skill':<12}  {'method':<{width}}  {'gp/hour':>12}")
        for m in shown:
            lines.append(f"{m.skill:<12}  {m.name:<{width}}  {m.gp_per_hour:>12,}")
    else:
        lines.append(f"{'method':<{width}}  {'gp/hour':>12}")
        for m in shown:
            lines.append(f"{m.name:<{width}}  {m.gp_per_hour:>12,}")
    lines.append(
        "\nRequirements are per method and are not all skill levels -- read the "
        "Skills column on the guide before promising anybody these rates."
    )
    return "\n".join(lines)


async def read_guide(client, title: str = SKILLING_GUIDE_PAGE) -> list[Method]:
    """Every rankable method on one guide page.

    One request. The tables come through ``action=parse``, which is the route
    that survives table markup -- see :meth:`reldo.wiki.WikiClient.tables`.
    """
    tables = await client.tables(title)
    found: list[Method] = []
    for table in tables:
        found += methods_from_table(table)
    return found
