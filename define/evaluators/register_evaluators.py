#!/usr/bin/env python3
"""Step 1 Define - register custom evaluators from JSON definitions (CreateEvaluator request bodies).

Idempotent: an evaluator whose name already exists is updated in place (UpdateEvaluator) unless it is locked
by an enabled Online evaluation config - then the script says so and leaves it alone (pause the config or
register a new versioned name, e.g. evaldeck_ScopeJudgeV2; scores across judge versions are not comparable).

Files:
  judge_*.json    Custom (LLM-as-a-judge): instructions with {context} / {assistant_turn}, numerical ratingScale
  derived_*.json  CustomDerived: a Builtin/ThirdParty evaluator's logic on a model you pick
  code_eval/      CustomCode (Lambda): deploy.sh deploys the Lambda and registers it (this script is not needed)

Equivalent CLI for one judge (CLI judges are fixed at maxTokens 4096 / temperature 0, integer scales):
  agentcore eval evaluator llm-as-a-judge create --name evaldeck_ScopeJudge --level TRACE \
      --model us.anthropic.claude-haiku-4-5-20251001-v1:0 --instructions file://scope.txt --rating-scale pass-fail

Prerequisites: boto3; IAM bedrock-agentcore:CreateEvaluator/UpdateEvaluator/ListEvaluators/GetEvaluator.
Usage: python define/evaluators/register_evaluators.py [files ...]   (default: judge_*.json derived_*.json)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import kit  # noqa: E402

HERE = Path(__file__).resolve().parent


def existing() -> dict[str, dict]:
    out = {}
    for page in kit.client("bedrock-agentcore-control").get_paginator("list_evaluators").paginate():
        for e in page.get("evaluators", []):
            out[e["evaluatorName"]] = e
    return out


def register(spec: dict, have: dict[str, dict]) -> dict:
    ctl = kit.client("bedrock-agentcore-control")
    name = spec["evaluatorName"]
    cur = have.get(name)
    if cur is None:
        r = ctl.create_evaluator(**spec)
        return {"name": name, "id": r["evaluatorId"], "arn": r["evaluatorArn"], "action": "created"}
    if cur.get("lockedForModification"):
        return {"name": name, "id": cur["evaluatorId"], "arn": cur["evaluatorArn"], "action": "locked-skipped"}
    upd = {k: v for k, v in spec.items() if k in ("description", "evaluatorConfig", "level")}
    if "derived" in spec["evaluatorConfig"]:
        upd.pop("level", None)  # level of a derived evaluator comes from its base evaluator
    ctl.update_evaluator(evaluatorId=cur["evaluatorId"], **upd)
    return {"name": name, "id": cur["evaluatorId"], "arn": cur["evaluatorArn"], "action": "updated"}


def main(argv: list[str]) -> int:
    files = [Path(f) for f in argv] or sorted(HERE.glob("judge_*.json")) + sorted(HERE.glob("derived_*.json"))
    have = existing()
    for f in files:
        spec = json.loads(f.read_text())
        spec["evaluatorName"] = spec["evaluatorName"].replace("evaldeck", kit.PREFIX, 1)
        print(json.dumps({"file": f.name, **register(spec, have)}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
