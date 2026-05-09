"""Tiny CLI for housekeeping.

    python -m app.cli init-db
    python -m app.cli replay <llm_call_id>
    python -m app.cli demo "I want a beginner-friendly local RAG knowledge base"
"""

from __future__ import annotations

import json
import sys

from . import db, github, llm, parser, query_planner, ranker


def _init_db() -> None:
    db.init_db()
    print(f"OK — schema created at {db.get_engine().url}")


def _replay(call_id: int) -> None:
    out = llm.replay(call_id)
    print(json.dumps(out, ensure_ascii=False, indent=2))


def _demo(text: str) -> None:
    parsed = parser.parse_requirement(text)
    queries = query_planner.plan_queries(parsed)
    qid = db.save_query(raw_text=text, parsed=parsed, queries=queries)
    print(f"query_id = {qid}")
    print("parsed:")
    print(parsed.model_dump_json(indent=2))
    print(f"queries ({len(queries)}):")
    for q in queries:
        print(f"  - [{q.sort}] {q.q}  // {q.why}")
    cands = github.recall(queries)
    print(f"recall: {len(cands)} candidates")
    if not cands:
        return
    snaps = github.fetch_snapshots([c.full_name for c in cands])
    recs = ranker.rank(parsed, snaps, top_n=5)
    db.save_recommendations(qid, recs)
    print(f"\nTop {len(recs)}:")
    for r in recs:
        print(f"  #{r.final_rank} {r.repo.full_name}  ★{r.repo.stars}  "
              f"final={r.final_score:.1f}  level={r.rule_score.deployment_level}")
        print(f"     {r.rationale}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd == "init-db":
        _init_db()
        return 0
    if cmd == "replay":
        if len(argv) < 3:
            print("usage: replay <llm_call_id>")
            return 1
        _replay(int(argv[2]))
        return 0
    if cmd == "demo":
        if len(argv) < 3:
            print("usage: demo <quoted text>")
            return 1
        _demo(argv[2])
        return 0
    print(f"unknown command: {cmd}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
