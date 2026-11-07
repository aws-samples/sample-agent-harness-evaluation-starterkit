#!/usr/bin/env python3
"""Step 3 Evaluate - replay a published Dataset version against the agent and score it (the CI gate input).

Two runners from the bedrock-agentcore SDK (Dataset evaluation is in Public Preview):
  --mode ondemand  OnDemandEvaluationDatasetRunner: invoke -> wait for ingestion -> SDK collects spans ->
                   one Evaluate call per evaluator per scenario. Fast feedback, small datasets, PR gates.
  --mode batch     BatchEvaluationRunner: invoke -> wait -> one StartBatchEvaluation job (<=10 evaluators,
                   <=500 sessions, 25% cheaper per token) -> poll. Baselines, release regression.
Ground truth in the rows is mapped automatically: assertions -> GoalSuccessRate, turns[].expected_response
-> Correctness, expected_trajectory -> Trajectory*Match. The dataset version is pinned (CI must never read
DRAFT); the registry written by define/datasets/publish_dataset.py supplies the default.

Prerequisites: deployed agent, published dataset, custom evaluators registered; IAM bedrock-agentcore:
InvokeAgentRuntime, Evaluate, StartBatchEvaluation/GetBatchEvaluation, GetDataset/ListDatasetExamples,
logs read on the runtime log group and the batch results log group.
Endpoint + Config bundle: --endpoint STAGING (default) and --bundle <id> --bundle-version <version-id> send the
bundle as W3C baggage on every turn; afterwards the invoke_agent spans of every scenario are checked for that
bundle version (results doc "bundle_check"; release/gate.py blocks when it is not verified).
Usage:
  python evaluate/run_dataset.py --mode ondemand [--dataset-version 2] [--evaluators ...] \
      [--endpoint STAGING] [--bundle <bundle-id> --bundle-version <version-id>] [--segment acceptance]
  python evaluate/run_dataset.py --mode batch --evaluators Builtin.GoalSuccessRate Builtin.FailureAnalysis
Writes out/dataset-<mode>-<ts>.results.json (evaldeck.results/v1, read by release/gate.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml
from bedrock_agentcore.evaluation import (
    AgentInvokerInput,
    AgentInvokerOutput,
    BatchEvaluationRunConfig,
    BatchEvaluationRunner,
    BatchEvaluatorConfig,
    CloudWatchAgentSpanCollector,
    CloudWatchDataSourceConfig,
    DatasetClient,
    DatasetManagementServiceProvider,
    EvaluationRunConfig,
    EvaluatorConfig,
    OnDemandEvaluationDatasetRunner,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import kit  # noqa: E402

REGISTRY = kit.KIT / "define/datasets/registry.json"


def pinned_dataset(name: str, version: str | None) -> tuple[str, str]:
    entry = (json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}).get(name)
    if not entry or not entry.get("versions"):
        raise SystemExit(f"dataset {name} is not in {REGISTRY.relative_to(kit.KIT)}: "
                         "run define/datasets/publish_dataset.py first")
    return entry["id"], version or entry["versions"][-1]["version"]


def make_invoker(arn: str):
    def invoke(inp: AgentInvokerInput) -> AgentInvokerOutput:   # called once per turn, same session per scenario
        prompt = inp.payload if isinstance(inp.payload, str) else json.dumps(inp.payload)
        return AgentInvokerOutput(agent_output=kit.invoke(prompt, inp.session_id, arn))
    return invoke


def ondemand_items(result) -> list[dict]:
    items = []
    for sc in result.scenario_results:
        if sc.status != "COMPLETED" and not sc.evaluator_results:
            items.append({"session_id": sc.session_id, "scenario_id": sc.scenario_id, "evaluator": "_invocation",
                          "value": None, "error": sc.error or sc.status})
        for er in sc.evaluator_results:
            for r in er.results:
                ctx = (r.get("context") or {}).get("spanContext") or {}
                items.append({"session_id": sc.session_id, "scenario_id": sc.scenario_id,
                              "evaluator": r.get("evaluatorName") or er.evaluator_id, "value": r.get("value"),
                              "label": r.get("label"), "explanation": (r.get("explanation") or "")[:600],
                              "trace_id": ctx.get("traceId"),
                              "error": r.get("errorCode") and f"{r['errorCode']}: {r.get('errorMessage', '')[:200]}"})
    return items


def batch_items(events: list[dict], sessions: dict[str, str]) -> list[dict]:
    items = []
    for e in events:
        at = e.get("attributes") or {}
        if not at.get("gen_ai.evaluation.name"):
            continue
        sid = at.get("session.id") or at.get("gen_ai.session.id")
        scenario = sessions.get(sid) or at.get("test.scenario.id") or re.sub(r"-[0-9a-f-]{36}$", "", sid or "")
        items.append({"session_id": sid, "scenario_id": scenario,
                      "evaluator": at["gen_ai.evaluation.name"], "value": at.get("gen_ai.evaluation.score.value"),
                      "label": at.get("gen_ai.evaluation.score.label"),
                      "explanation": (at.get("gen_ai.evaluation.explanation") or "")[:600],
                      "trace_id": e.get("traceId") or at.get("trace.id"),
                      "error": at.get("error.type") or at.get("gen_ai.evaluation.error")})
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["ondemand", "batch"], default="ondemand")
    ap.add_argument("--dataset", default=f"{kit.PREFIX}_acceptance")
    ap.add_argument("--dataset-version")
    ap.add_argument("--evaluators", nargs="*", help="default: every metric in define/gate.yaml")
    ap.add_argument("--ingestion-delay", type=int, default=180, help="seconds; do not lower (docs: 2-5 min)")
    ap.add_argument("--name", help="batch job name ([a-zA-Z][a-zA-Z0-9_]{0,47})")
    ap.add_argument("--segment", default="acceptance", help="gate segment this run feeds (release/gate.py)")
    ap.add_argument("--concurrency", type=int, default=int(os.getenv("EVAL_CONCURRENCY", "5")),
                    help="scenarios in parallel (macOS: 1 - concurrent TLS handshakes in the SDK runner segfaulted)")
    kit.add_target_args(ap)
    a = ap.parse_args()

    target = kit.set_target(a)
    arn = kit.runtime_arn()
    names = a.evaluators or [m["evaluator"] for m in yaml.safe_load((kit.KIT / "define/gate.yaml").read_text())["metrics"]]
    ids = kit.evaluator_ids(names)
    ds_id, version = pinned_dataset(a.dataset, a.dataset_version)
    dataset = DatasetManagementServiceProvider(dataset_id=ds_id, version_id=version,
                                               client=DatasetClient(region_name=kit.REGION)).get_dataset()
    start = kit.now_utc()
    if a.mode == "ondemand":
        config = EvaluationRunConfig(evaluator_config=EvaluatorConfig(evaluator_ids=list(ids.values())),
                                     evaluation_delay_seconds=a.ingestion_delay, max_concurrent_scenarios=a.concurrency)
        collector = CloudWatchAgentSpanCollector(log_group_name=kit.runtime_log_group(arn), region=kit.REGION)
        result = OnDemandEvaluationDatasetRunner(region=kit.REGION).run(
            config=config, dataset=dataset, agent_invoker=make_invoker(arn), span_collector=collector)
        items, extra = ondemand_items(result), {}
    else:
        name = a.name or f"{kit.PREFIX}_dataset_{kit.stamp()}"
        config = BatchEvaluationRunConfig(
            batch_evaluation_name=name,
            evaluator_config=BatchEvaluatorConfig(evaluator_ids=list(ids.values())),
            data_source=CloudWatchDataSourceConfig(service_names=[kit.service_name(arn)],
                                                   log_group_names=[kit.runtime_log_group(arn)],
                                                   ingestion_delay_seconds=a.ingestion_delay),
            max_concurrent_scenarios=a.concurrency)
        runner = BatchEvaluationRunner(region=kit.REGION)
        result = runner.run_dataset_evaluation(config=config, dataset=dataset, agent_invoker=make_invoker(arn))
        events = runner.fetch_evaluation_events(result) if result.output_data_config else []
        summary = result.evaluation_results.model_dump(mode="json") if result.evaluation_results else None
        items = batch_items(events, {})
        items += [{"session_id": None, "scenario_id": f.scenario_id, "evaluator": "_invocation", "value": None,
                   "error": f.error_message} for f in result.agent_invocation_failures]
        extra = {"batch": {"id": result.batch_evaluation_id, "status": result.status, "summary": summary,
                           "insights": [i.model_dump(mode="json") for i in (result.insight_results or [])],
                           "errors": result.error_details}}
    check = kit.bundle_check([i["session_id"] for i in items], arn)
    doc = kit.results_doc(f"dataset-{a.mode}", items, agent={"runtime_arn": arn, **target}, segment=a.segment,
                          dataset={"name": a.dataset, "id": ds_id, "version": version}, evaluators=ids,
                          bundle_check=check, started_at=start.isoformat(), **extra)
    path = kit.write_json(kit.OUT / f"dataset-{a.mode}-{kit.stamp()}.results.json", doc)
    for n, m in doc["metrics"].items():
        print(f"{n:34s} mean={m['mean']} n={m['n']} errors={m['errors']}")
    print("bundle_check " + json.dumps({k: check.get(k) for k in ("expected_version", "sessions", "matched_sessions",
                                                                  "by_source", "verified")}))
    if extra.get("batch"):
        print(json.dumps({k: extra["batch"][k] for k in ("id", "status", "errors")}))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
