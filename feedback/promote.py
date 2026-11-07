#!/usr/bin/env python3
"""Step 6 Feedback - promote reviewed bad cases into the regression dataset and publish a new version
ONLY if the content changed.

Input: a reviewed badcases file (mine_badcases.py output after human review):
  metadata.review.decision = accept       -> appended to define/datasets/acceptance.jsonl (agent defect; the row
                                            must have an expected_response on every turn and >= 1 assertion)
                           = judge_error  -> appended to define/labels/judge_regression.jsonl (tests the judge,
                                            never the agent)
                           = reject / pending -> skipped
Rows keep their lineage metadata (origin session, evaluator, score, reviewer). Re-running with the same file
adds nothing and publishes nothing (dedupe by scenario_id + content comparison in publish_dataset.py).
A new dataset version changes the gate's dataset -> release/gate.py BLOCKS until the baseline is re-measured
on it ("re-baseline"), which is intended.

Usage:
  python feedback/promote.py --reviewed out/badcases-<ts>.jsonl [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import kit  # noqa: E402
from define.datasets.publish_dataset import publish, record  # noqa: E402

DATASET = kit.KIT / "define/datasets/acceptance.jsonl"
JUDGE_REGRESSION = kit.KIT / "define/labels/judge_regression.jsonl"


def validate(row: dict) -> list[str]:
    errs = []
    if not row.get("turns") or not all(t.get("expected_response") for t in row["turns"]):
        errs.append("every turn needs an expected_response written by the reviewer")
    if not row.get("assertions"):
        errs.append("at least one assertion is required")
    if not (row.get("metadata") or {}).get("review", {}).get("reviewer"):
        errs.append("metadata.review.reviewer is required")
    return errs


def append_new(path: Path, rows: list[dict]) -> int:
    have = {r["scenario_id"] for r in kit.load_jsonl(path)} if path.exists() else set()
    new = [r for r in rows if r["scenario_id"] not in have]
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in new)
    return len(new)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reviewed", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    rows = kit.load_jsonl(a.reviewed)
    accept = [r for r in rows if r["metadata"]["review"]["decision"] == "accept"]
    judge = [r for r in rows if r["metadata"]["review"]["decision"] == "judge_error"]
    bad = {r["scenario_id"]: validate(r) for r in accept if validate(r)}
    if bad:
        print(json.dumps({"refused": bad}, indent=2), file=sys.stderr)
        return 1
    for r in accept:
        r["metadata"]["lifecycle_step"] = "feedback->define"
    summary = {"accepted": len(accept), "judge_error": len(judge),
               "skipped": len(rows) - len(accept) - len(judge), "dry_run": a.dry_run}
    if a.dry_run:
        print(json.dumps(summary))
        return 0
    summary["added_to_dataset"] = append_new(DATASET, accept)
    summary["added_to_judge_regression"] = append_new(JUDGE_REGRESSION, judge)
    name = f"{kit.PREFIX}_{DATASET.stem}"
    res = publish(name, DATASET, "predefined")
    record(name, DATASET, "predefined", res)
    summary["dataset"] = {"name": name, **res}
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
