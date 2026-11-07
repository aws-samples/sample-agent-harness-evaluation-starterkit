#!/usr/bin/env python3
"""Step 3 Evaluate - user simulation: an LLM actor plays each persona (ActorProfile) for a multi-turn
conversation with the deployed agent, then the sessions are scored like any other.

Simulation runs in the SDK (SimulationConfig on the runner config); there is no separate Simulation API and
the CLI `simulate` subcommands replay predefined rows only. The actor calls Bedrock with YOUR credentials
(bedrock:InvokeModel on the actor model) and is billed as normal model usage. Public Preview.
Simulated rows support `assertions` only (no expected_response / expected_trajectory). Cap max_turns: the
actor decides when to stop, and long conversations inflate cost metrics.

Prerequisites: pip install 'bedrock-agentcore[simulation]'; deployed agent; evaluators registered.
Usage:
  python evaluate/simulate.py [--file define/datasets/personas.jsonl] [--actor-model <bedrock model id>]
         [--evaluators Builtin.GoalSuccessRate evaldeck_ScopeJudge evaldeck_RedlineCode] \
         [--endpoint STAGING] [--bundle <bundle-id> --bundle-version <version-id>]
Gate segment "sim": release/gate.py judges the metrics this run requested against the sim baseline.
Writes out/simulate-<ts>.results.json (evaldeck.results/v1).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from bedrock_agentcore.evaluation import (
    CloudWatchAgentSpanCollector,
    EvaluationRunConfig,
    EvaluatorConfig,
    FileDatasetProvider,
    OnDemandEvaluationDatasetRunner,
    SimulationConfig,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import kit  # noqa: E402
from evaluate.run_dataset import make_invoker, ondemand_items  # noqa: E402

DEFAULT_ACTOR = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(kit.KIT / "define/datasets/personas.jsonl"))
    ap.add_argument("--actor-model", default=DEFAULT_ACTOR)
    ap.add_argument("--evaluators", nargs="*",
                    default=["Builtin.GoalSuccessRate", f"{kit.PREFIX}_ScopeJudge", f"{kit.PREFIX}_RedlineCode"])
    ap.add_argument("--ingestion-delay", type=int, default=180)
    ap.add_argument("--segment", default="sim")
    ap.add_argument("--concurrency", type=int, default=min(2, int(os.getenv("EVAL_CONCURRENCY", "2"))))
    kit.add_target_args(ap)
    a = ap.parse_args()

    target = kit.set_target(a)
    arn = kit.runtime_arn()
    ids = kit.evaluator_ids(a.evaluators)
    dataset = FileDatasetProvider(a.file).get_dataset()       # rows with actor_profile -> SimulatedScenario
    config = EvaluationRunConfig(
        evaluator_config=EvaluatorConfig(evaluator_ids=list(ids.values())),
        evaluation_delay_seconds=a.ingestion_delay,
        max_concurrent_scenarios=a.concurrency,
        simulation_config=SimulationConfig(model_id=a.actor_model),
    )
    result = OnDemandEvaluationDatasetRunner(region=kit.REGION).run(
        config=config, dataset=dataset, agent_invoker=make_invoker(arn),
        span_collector=CloudWatchAgentSpanCollector(log_group_name=kit.runtime_log_group(arn), region=kit.REGION))
    items = ondemand_items(result)
    check = kit.bundle_check([s.session_id for s in result.scenario_results], arn)
    persona_file = Path(a.file)
    dataset = {"name": persona_file.stem, "file": persona_file.name,
               "version": hashlib.sha256(persona_file.read_bytes()).hexdigest()[:12]}   # baseline must match
    doc = kit.results_doc("simulate", items, agent={"runtime_arn": arn, **target}, segment=a.segment,
                          dataset=dataset, evaluators=ids, actor_model=a.actor_model, bundle_check=check,
                          scenarios={s.scenario_id: {"session_id": s.session_id, "status": s.status, "error": s.error}
                                     for s in result.scenario_results})
    path = kit.write_json(kit.OUT / f"simulate-{kit.stamp()}.results.json", doc)
    for sid, s in doc["scenarios"].items():
        print(f"{sid:26s} {s['status']:10s} {s['session_id']}")
    for n, m in doc["metrics"].items():
        print(f"{n:34s} mean={m['mean']} n={m['n']} errors={m['errors']}")
    print("bundle_check " + json.dumps({k: check.get(k) for k in ("expected_version", "sessions", "matched_sessions",
                                                                  "by_source", "verified")}))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
