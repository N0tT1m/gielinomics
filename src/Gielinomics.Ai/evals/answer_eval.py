"""End-to-end answer quality. Runs the real agent against the real model.

The retrieval eval scores whether the right page made the shortlist, and that
turned out to be the *easy* half. Every quality bug found so far got through it
untouched, because retrieval was already correct:

* "what Smithing level for a rune platebody" -- 'Rune platebody' ranked #1 by
  both rankers; the model read the general 'Smithing' page and answered 35.
* "attack level of the player Lynx Titan" -- searched the wiki, found the NPC
  'Lynx Tamer', answered about that.

Both are perfect retrieval scores and useless answers. This eval is slower and
noisier -- it runs a 24B model per question -- but it is the only thing that
measures what a user actually receives.

**One run tells you almost nothing.** Measured on the same eight cases and the
same code: 6, 6 and 8 out of 8 on three consecutive runs, against 7, 7, 6, 7, 7
on five runs of the commit before it. A single score cannot distinguish a
regression from the model having a bad afternoon, and two of those runs would
have "proved" opposite conclusions. Use ``--repeat`` and read the per-case rate;
a case that scores 2/3 is flaky, which is a different bug from one that scores
0/3 and wants a different fix.

Each case asserts some of:
  * the answer contains a fact it must contain
  * it does NOT contain a known wrong answer (the specific way it failed before)
  * it cited a page, when the question is answerable from one
  * it actually consulted the live source it needed (hiscores, GE, XP table)
  * any duration it states is arithmetically consistent with the rate it quotes

    uv run python evals/answer_eval.py [-n 3] [--repeat 3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reldo import config  # noqa: E402
from reldo.agent import WikiAgent  # noqa: E402
from reldo.index import load_index  # noqa: E402
from reldo.llm import client_for  # noqa: E402
from reldo.retrieval import HybridRetriever  # noqa: E402
from reldo.skills import xp_between  # noqa: E402
from reldo.wiki import WikiClient  # noqa: E402

CASES = Path(__file__).parent / "answers.jsonl"

# "126,000 xp/hr", "126k experience per hour", "110,000 XP an hour".
_RATE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*(k\b)?\s*(?:xp|experience)\s*(?:per|/|an|a)\s*(?:hour|hr)\b",
    re.I,
)
_HOURS = re.compile(r"([\d,]+(?:\.\d+)?)\s*hours?\b", re.I)

# How far a stated total may drift from the arithmetic before it is a lie
# rather than a rounding. The bug this catches was out by 54%.
HOURS_TOLERANCE = 0.15


def _number(raw: str, kilo: str | None) -> float:
    value = float(raw.replace(",", ""))
    return value * 1000 if kilo else value


def check_hours_consistent(case: dict, text: str) -> list[str]:
    """Catch a duration that does not follow from the rate quoted beside it.

    The failure this exists for: "200,000 XP per hour ... approximately 100
    hours" for 45->99 Mining, in one sentence. 100h at 200k/hr is 20M XP and the
    real gap is 12,972,919, so the two halves disagree by 54% -- and *neither*
    number came from the guide. Checking them against each other needs no
    knowledge of what the right answer is, which is what makes it robust as the
    wiki's own rates change.
    """
    levels = case.get("consistent_hours_from")
    if not levels:
        return []
    rate_match, hours_match = _RATE.search(text), _HOURS.search(text)
    if not rate_match or not hours_match:
        return []  # nothing claimed, nothing to contradict

    rate = _number(rate_match.group(1), rate_match.group(2))
    hours = _number(hours_match.group(1), None)
    if rate <= 0:
        return []

    expected = xp_between(levels[0], levels[1]) / rate
    if abs(hours - expected) / expected > HOURS_TOLERANCE:
        return [
            f"states {hours:,.0f}h at {rate:,.0f} xp/hr, but "
            f"{levels[0]}->{levels[1]} is {xp_between(*levels):,} XP = "
            f"{expected:,.1f}h -- the two numbers contradict each other"
        ]
    return []


def judge(case: dict, answer) -> tuple[bool, list[str]]:
    """Substring checks, deliberately. A model-as-judge would need a second
    model and introduce its own failure mode; these facts are exact strings."""
    text = answer.text.lower()
    problems: list[str] = []

    for needle in case.get("must_include", []):
        if needle.lower() not in text:
            problems.append(f"missing {needle!r}")
    for needle in case.get("must_not_include", []):
        if needle.lower() in text:
            problems.append(f"contains wrong value {needle!r}")

    wanted = case.get("cite", [])
    if wanted:
        cited = " ".join(answer.pages_read).lower()
        if not cited:
            problems.append("no page cited")
        elif not any(w.lower() in cited for w in wanted):
            problems.append(f"cited {answer.pages_read}, expected one of {wanted}")

    # Which live source was consulted. A price or duration answered from the
    # model's memory can be word-for-word plausible and a year out of date, and
    # no substring check would catch it -- only the absence of the tool call.
    if case.get("must_price") and not answer.prices_checked:
        problems.append("never called the GE tools -- price came from memory")
    if case.get("must_calc_xp") and not answer.xp_calculations:
        problems.append("never called calculate_xp -- the arithmetic was guessed")
    # The coin twin, and it needs to be a tool-call check rather than a
    # substring one: the right count moves with the live GE price, so there is
    # no fixed number to assert. What is stable is that a division happened in
    # code. "375 minnows, 9 raw sharks, 5,000,000 gp" is what happens when it
    # does not.
    if case.get("must_calc_gp") and not answer.gp_calculations:
        problems.append("never called calculate_gp -- the coin arithmetic was guessed")
    if case.get("must_get_requirements") and not answer.skill_requirements:
        problems.append("never called get_requirements -- the level may be another skill's")
    if case.get("must_check_player") and not answer.players_checked:
        problems.append("never called get_player_stats")

    problems += check_hours_consistent(case, answer.text)
    return not problems, problems


async def run_case(agent, case: dict):
    """Run one case. Returns (ok, problems, text, seconds, trace).

    ``trace`` is which enforcement passes fired and what the grounding excision
    removed. Without it a case that fails differently on each run reads as
    "flaky" and nothing more -- and flaky is not a diagnosis, it is the absence
    of one. Fourteen passes fire conditionally under `ask` and several can undo
    each other, so the sequence is usually the whole story: the shark question
    runs the forced read, then the coin calculation, and what comes out depends
    on which of them spoke last.
    """
    started = time.monotonic()
    try:
        answer = await agent.ask(case["q"])
    except Exception as exc:  # a crash is a failure, not a stack trace
        return False, [f"{type(exc).__name__}: {exc}"], "", time.monotonic() - started, {}
    ok, problems = judge(case, answer)
    trace = {
        "passes": list(answer.passes_fired),
        "excised": list(answer.excised),
        "budget": answer.budget_exhausted,
    }
    return ok, problems, answer.text, time.monotonic() - started, trace


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, help="only run the first N cases")
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run each case N times and report the pass rate. One run cannot "
        "tell a regression from noise; 3+ can.",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="TEXT",
        help="only run cases whose question contains TEXT, case-insensitively. "
        "Repeatable. The point is resolution: over the whole suite at --repeat 3 "
        "the noise band is a case or two either way, which cannot tell a real fix "
        "for one flaky case from a coin flip -- three consecutive runs each had a "
        "different pair go 2/3. A handful of cases at --repeat 10 costs what one "
        "full run costs and gives each of them a number worth arguing about.",
    )
    parser.add_argument(
        "--no-direct",
        action="store_true",
        help="skip the intent router and send every question to the model, for "
        "comparing the two paths on the same cases.",
    )
    parser.add_argument("--index", type=Path)
    args = parser.parse_args()

    settings = config.load()
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    if args.only:
        wanted = [t.lower() for t in args.only]
        cases = [c for c in cases if any(t in c["q"].lower() for t in wanted)]
        if not cases:
            print(f"No case matches {args.only!r}.", file=sys.stderr)
            return 1
    if args.n:
        cases = cases[: args.n]

    index = load_index(args.index or settings.index_path, settings.ollama_api_url)
    scores: dict[str, int] = defaultdict(int)
    seen_problems: dict[str, list[str]] = defaultdict(list)
    last_text: dict[str, str] = {}

    async with (
        WikiClient(
            settings.require_user_agent(), requests_per_second=settings.requests_per_second
        ) as wiki,
        client_for(settings) as chat,
    ):
        agent = WikiAgent(
            HybridRetriever(wiki, index),
            chat,
            max_tokens=settings.max_tokens,
            user_agent=settings.require_user_agent(),
        )
        # Scored through the same door the bot uses. The direct path returns a
        # fully populated Answer, so cite, must_calc_xp and must_price mean
        # exactly what they meant before -- which is the only reason routing
        # here is measurable rather than a way of switching the checks off.
        if not args.no_direct:
            from reldo.direct import answerer_for

            agent.use_direct(answerer_for(settings, agent))

        for case in cases:
            elapsed = 0.0
            traces: list[dict] = []
            for _ in range(args.repeat):
                ok, problems, text, took, trace = await run_case(agent, case)
                elapsed += took
                scores[case["q"]] += ok
                if not ok:
                    seen_problems[case["q"]] += [p for p in problems
                                                 if p not in seen_problems[case["q"]]]
                    last_text[case["q"]] = text
                    traces.append(trace)

            hits = scores[case["q"]]
            mark = "PASS" if hits == args.repeat else ("FAIL" if hits == 0 else "FLAKY")
            rate = f"{hits}/{args.repeat}" if args.repeat > 1 else ""
            print(f"  [{mark:5}] {case['q'][:52]:<52} {rate:>5} {elapsed:5.1f}s")
            for problem in seen_problems[case["q"]]:
                print(f"          {problem}")
            if hits < args.repeat:
                print(f"          got: {last_text[case['q']][:150]!r}")
                # One line per failing run. Printed per run rather than merged
                # because a case that fires a different sequence each time is
                # saying something different from one that fires the same
                # sequence and still gets it wrong, and merging hides which.
                for n, trace in enumerate(traces, 1):
                    # .get throughout: a case that raised returns an empty
                    # trace, and indexing it turned one unreachable model server
                    # into a KeyError that took the whole run's results with it
                    # -- 27 cases scored and nothing printed.
                    fired = " -> ".join(trace.get("passes") or []) or "no enforcement fired"
                    print(f"          run {n} passes: {fired}")
                    if trace.get("excised"):
                        cut = " | ".join(p.strip() for p in trace["excised"])
                        print(f"          run {n} excised: {cut[:160]}")
                    if trace.get("budget"):
                        print(f"          run {n} budget:  {trace['budget']}")
                print(f"          why this case exists: {case['why']}")

    solid = sum(1 for c in cases if scores[c["q"]] == args.repeat)
    dead = sum(1 for c in cases if scores[c["q"]] == 0)
    flaky = len(cases) - solid - dead
    total = sum(scores.values())

    print(f"\n  {solid}/{len(cases)} cases passed every run", end="")
    if args.repeat > 1:
        print(f"  ({total}/{len(cases) * args.repeat} answers overall)")
        if flaky:
            print(f"  {flaky} flaky -- passed sometimes. Not the same bug as a hard fail.")
        if dead:
            print(f"  {dead} failed every run.")
    else:
        print("\n  (one run only -- use --repeat 3 before concluding anything)")
    return 0 if dead == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
