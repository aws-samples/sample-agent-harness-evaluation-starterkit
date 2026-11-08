#!/usr/bin/env python3
"""Step 1 Define - translate requirements.yaml into gate.yaml (the file release/gate.py reads).

It only translates and validates; it never changes a threshold:
  - every requirement is covered by at least one release criterion, every criterion names known requirements
  - gate metrics need a threshold, redline metrics use violations_eq0
  - adds the evaluator level and the ground truth each built-in needs (so run scripts can skip rows without it)

Prerequisites: python3 + pyyaml.
Usage:
  python define/gen_gate.py            # write define/gate.yaml
  python define/gen_gate.py --check    # CI: exit 1 when gate.yaml is stale or requirements.yaml is invalid
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TIERS = {"redline", "gate", "observe"}
RULES = {"mean_gte", "violations_eq0"}
# level + ground truth of the built-ins this kit uses (docs: prompt-templates-builtin / ground-truth-evaluations)
BUILTIN = {
    "Builtin.GoalSuccessRate": ("SESSION", ["assertions"]),
    "Builtin.Correctness": ("TRACE", ["expected_response"]),
    "Builtin.Helpfulness": ("TRACE", []),
    "Builtin.Faithfulness": ("TRACE", []),
    "Builtin.ToolSelectionAccuracy": ("TOOL_CALL", []),
    "Builtin.TrajectoryAnyOrderMatch": ("SESSION", ["expected_trajectory"]),
    "Builtin.TrajectoryInOrderMatch": ("SESSION", ["expected_trajectory"]),
    "Builtin.Harmfulness": ("TRACE", []),
}
CUSTOM_LEVEL = {"evaldeck_ScopeJudge": "TRACE", "evaldeck_RedlineCode": "SESSION"}


def build(req: dict) -> tuple[dict, list[str]]:
    errs: list[str] = []
    req_ids = {r["id"] for r in req.get("requirements", [])}
    covered: set[str] = set()
    metrics = []
    for c in req.get("release_criteria", []):
        ev, tier, rule = c.get("evaluator"), c.get("tier"), c.get("rule")
        if tier not in TIERS:
            errs.append(f"{ev}: tier {tier!r} not in {sorted(TIERS)}")
        if rule not in RULES:
            errs.append(f"{ev}: rule {rule!r} not in {sorted(RULES)}")
        if tier == "redline" and rule != "violations_eq0":
            errs.append(f"{ev}: a redline must use rule violations_eq0")
        if tier != "redline" and rule == "violations_eq0":
            errs.append(f"{ev}: violations_eq0 is a red-line rule: use tier redline")
        if rule == "mean_gte" and not isinstance(c.get("threshold"), (int, float)):
            errs.append(f"{ev}: mean_gte needs a numeric threshold")
        unknown = set(c.get("req", [])) - req_ids
        if unknown:
            errs.append(f"{ev}: unknown requirements {sorted(unknown)}")
        covered |= set(c.get("req", []))
        level, gt = BUILTIN.get(ev, (CUSTOM_LEVEL.get(ev, "TRACE"), []))
        m = {"evaluator": ev, "tier": tier, "rule": rule, "level": level, "needs_gt": gt, "req": c.get("req", [])}
        if rule == "mean_gte":
            m["threshold"] = float(c["threshold"])
        if c.get("score_step") is not None:
            m["score_step"] = float(c["score_step"])
        seg = c.get("segment_tiers") or {}
        for s, t in seg.items():
            if t not in TIERS - {"redline"}:
                errs.append(f"{ev}: segment_tiers[{s}] = {t!r} must be gate or observe")
        if seg and tier == "redline":
            errs.append(f"{ev}: a redline gates in every segment (no segment_tiers)")
        if seg:
            m["segment_tiers"] = dict(seg)
        metrics.append(m)
    for missing in sorted(req_ids - covered):
        errs.append(f"{missing}: no release criterion covers this requirement")
    gate = {"generated_from": "define/requirements.yaml (do not edit by hand: python define/gen_gate.py)",
            "agent": req.get("agent"), "dataset": req.get("dataset"),
            "policy": req.get("gate_policy", {}), "metrics": metrics}
    return gate, errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requirements", default=HERE / "requirements.yaml", type=Path)
    ap.add_argument("--out", default=HERE / "gate.yaml", type=Path)
    ap.add_argument("--check", action="store_true", help="fail if --out is stale instead of writing it")
    a = ap.parse_args()
    gate, errs = build(yaml.safe_load(a.requirements.read_text()))
    if errs:
        print("requirements.yaml is invalid:\n  " + "\n  ".join(errs), file=sys.stderr)
        return 1
    text = yaml.safe_dump(gate, sort_keys=False, allow_unicode=True)
    if a.check:
        if not a.out.exists() or a.out.read_text() != text:
            print(f"{a.out} is stale: run python define/gen_gate.py", file=sys.stderr)
            return 1
        print(f"{a.out} is up to date ({len(gate['metrics'])} metrics)")
        return 0
    a.out.write_text(text)
    tiers = {t: sum(m["tier"] == t for m in gate["metrics"]) for t in sorted(TIERS)}
    print(f"wrote {a.out}: {len(gate['metrics'])} metrics {tiers}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
