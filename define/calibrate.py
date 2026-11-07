#!/usr/bin/env python3
"""Step 1 Define - calibrate an LLM judge against human labels before it is allowed to gate a release.

  sample : pick judge results for humans to label - low scores first (most likely violations, at most n/2),
           then a seeded random fill; writes a JSONL template with an empty `human_value`
  score  : agreement = share of labelled rows where human_value == judge value; exit 1 below --threshold
           (default 0.85). Disagreements are printed - they become judge-regression cases or rubric fixes.
Label blind: fill `human_value` before looking at `judge_value` / `judge_explanation`, or you inherit the
judge's opinion. Re-calibrate on a fresh sample after every rubric change (a rubric tuned on one batch can
drop on the next).

Usage:
  python define/calibrate.py sample --results out/simulate-<ts>.results.json --evaluator evaldeck_ScopeJudge \
      --n 20 --out define/labels/scope-r1.jsonl
  python define/calibrate.py score --labels define/labels/scope-r1.jsonl [--threshold 0.85]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path


def sample(items: list[dict], evaluator: str, n: int, seed: int) -> list[dict]:
    mine = [i for i in items if i["evaluator"] == evaluator and i.get("value") is not None and not i.get("error")]
    key = lambda i: (i.get("session_id") or "", i.get("trace_id") or "")  # noqa: E731
    low = sorted((i for i in mine if i["value"] < 1.0), key=key)[: n // 2]
    rest = sorted((i for i in mine if i not in low), key=key)
    picked = low + random.Random(seed).sample(rest, min(n - len(low), len(rest)))
    return [{"session_id": i.get("session_id"), "trace_id": i.get("trace_id"), "scenario_id": i.get("scenario_id"),
             "evaluator": evaluator, "human_value": None, "human_note": "",
             "judge_value": i["value"], "judge_label": i.get("label"), "judge_explanation": i.get("explanation")}
            for i in picked]


def agreement(rows: list[dict]) -> tuple[float | None, list[dict]]:
    scored = [r for r in rows if r.get("human_value") is not None]
    if not scored:
        return None, []
    diff = [r for r in scored if float(r["human_value"]) != float(r["judge_value"])]
    return round(1 - len(diff) / len(scored), 3), diff


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--results", type=Path, action="append", required=True)
    s.add_argument("--evaluator", required=True)
    s.add_argument("--n", type=int, default=20)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("score")
    c.add_argument("--labels", type=Path, required=True)
    c.add_argument("--threshold", type=float, default=0.85)
    a = ap.parse_args()

    if a.cmd == "sample":
        items = [i for p in a.results for i in json.loads(p.read_text())["items"]]
        rows = sample(items, a.evaluator, a.n, a.seed)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        print(f"wrote {len(rows)} rows to {a.out}: fill human_value (blind), then run `score`")
        return 0
    rows = [json.loads(line) for line in a.labels.read_text().splitlines() if line.strip()]
    agree, diff = agreement(rows)
    labelled = sum(r.get("human_value") is not None for r in rows)
    print(json.dumps({"labels": str(a.labels), "labelled": labelled, "of": len(rows), "agreement": agree,
                      "threshold": a.threshold, "calibrated": agree is not None and agree >= a.threshold}))
    for r in diff:
        print(f"  DISAGREE {r['scenario_id']} trace={str(r['trace_id'])[:12]} judge={r['judge_value']} "
              f"human={r['human_value']} {r.get('human_note', '')}")
    return 0 if agree is not None and agree >= a.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
