"""Score retrieval against a fixed question set. No model in the loop, no tokens spent.

Retrieval quality is the ceiling on answer quality: if the right page never makes
the shortlist, no amount of prompt work recovers it. This measures recall@k for the
combined ranker and for each half separately, so you can see *which* half earns its
keep on which kind of question -- the whole premise of the hybrid design is that
keyword and semantic fail on different inputs, and this is where that claim gets
checked rather than assumed.

    uv run python evals/retrieval_eval.py [-k 8]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reldo import config  # noqa: E402
from reldo.index import load_index  # noqa: E402
from reldo.retrieval import HybridRetriever  # noqa: E402
from reldo.wiki import WikiClient  # noqa: E402

QUESTIONS = Path(__file__).parent / "questions.jsonl"


def hit(expected: list[str], titles: list[str]) -> bool:
    """A question counts as answered if any expected page made the shortlist.

    An exact title, or one of its subpages: "Vorkath/Strategies" genuinely
    answers a question about Vorkath and should count.

    Substring matching does not, which is what this used to do. Expecting
    "Vorkath" was satisfied by "Vorkath's stuffed head" and "Vorkath display
    (Old School Museum)" -- a shortlist that would tell the model nothing,
    banked as a pass. A scorer that is looser than the task flatters the
    thing it is meant to measure, which is the one failure mode an eval
    cannot afford.
    """
    lowered = {t.lower() for t in titles}
    return any(
        want in lowered or any(t.startswith(f"{want}/") for t in lowered)
        for want in (e.lower() for e in expected)
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-k", type=int, default=8, help="shortlist depth")
    parser.add_argument("--verbose", action="store_true", help="show every miss")
    parser.add_argument(
        "--index", type=Path, help="index to score (defaults to RELDO_INDEX_PATH)"
    )
    args = parser.parse_args()

    settings = config.load()
    index_path = args.index or settings.index_path
    cases = [json.loads(line) for line in QUESTIONS.read_text().splitlines() if line.strip()]

    print(f"  index: {index_path}")
    index = load_index(index_path, settings.ollama_api_url)
    async with WikiClient(
        settings.require_user_agent(), requests_per_second=settings.requests_per_second
    ) as client:
        retriever = HybridRetriever(client, index)

        totals: dict[str, list[int]] = defaultdict(list)
        by_kind: dict[str, list[int]] = defaultdict(list)

        for case in cases:
            fused = await retriever.shortlist(case["q"], k=args.k)
            semantic = await asyncio.to_thread(index.shortlist, case["q"], k=args.k)
            keyword = await client.search(case["q"], limit=args.k)

            scores = {
                "hybrid": hit(case["expect"], [c.title for c in fused]),
                "semantic": hit(case["expect"], [c.title for c in semantic]),
                "keyword": hit(case["expect"], [h.title for h in keyword]),
            }
            for name, ok in scores.items():
                totals[name].append(int(ok))
            by_kind[case["kind"]].append(int(scores["hybrid"]))

            mark = "PASS" if scores["hybrid"] else "MISS"
            flags = "".join(
                letter if scores[name] else "-"
                for name, letter in (("semantic", "s"), ("keyword", "k"))
            )
            print(f"  [{mark}] [{flags}] {case['q'][:58]:<58}")
            if args.verbose and not scores["hybrid"]:
                print(f"         expected one of {case['expect']}")
                print(f"         got {[c.title for c in fused][:5]}")

    n = len(cases)
    print(f"\n  recall@{args.k} over {n} questions")
    for name in ("hybrid", "semantic", "keyword"):
        got = sum(totals[name])
        print(f"    {name:<9} {got}/{n}  ({got / n:.0%})")

    print("\n  hybrid, by question kind")
    for kind, results in sorted(by_kind.items()):
        print(f"    {kind:<12} {sum(results)}/{len(results)}")

    return 0 if sum(totals["hybrid"]) == n else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
