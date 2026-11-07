#!/usr/bin/env python3
"""Step 3 Evaluate (also 2 Build self-test, 5 Observe case review) - on-demand `Evaluate` over real session ids.

For each session: collect its spans from the runtime log group (CloudWatchAgentSpanCollector), then call the
data-plane Evaluate API once per evaluator (the API takes exactly 1 evaluator per call), targeting the
right level: SESSION = whole session, TRACE = each turn (traceIds, <=10 per call), TOOL_CALL = tool spans.
Ground truth from the dataset row is attached as evaluationReferenceInputs (assertions + expected
trajectory at session level, expected_response per turn). Metrics that need ground truth the row does not
have are skipped for that row (they would only produce "ignored" results).

Prerequisites: sessions ingested (wait 2-5 min after send_traffic.py); bedrock-agentcore:Evaluate,
logs:StartQuery/GetQueryResults on the runtime log group; custom evaluators registered.
Usage:
  python evaluate/run_ondemand.py --sessions out/sessions-acc-<ts>.json [--evaluators A B ...]
  python evaluate/run_ondemand.py --session-ids <sid> [<sid> ...] --evaluators Builtin.Helpfulness
Writes out/ondemand-<ts>.results.json (evaldeck.results/v1, read by release/gate.py).
CLI equivalent for one session: agentcore eval ondemand evaluate --agent <runtime-id> --session-ids <sid>
  --evaluators Builtin.GoalSuccessRate evaldeck_ScopeJudge-<suffix> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from bedrock_agentcore.evaluation import CloudWatchAgentSpanCollector
from bedrock_agentcore.evaluation.spans import tool_span_ids

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import kit  # noqa: E402

GATE = kit.KIT / "define/gate.yaml"


def ordered_traces(spans: list[dict]) -> list[str]:
    first: dict[str, int] = {}
    for s in spans:
        t = s.get("traceId")
        if t:
            first[t] = min(first.get(t, 1 << 63), int(s.get("startTimeUnixNano") or 0))
    return sorted(first, key=first.get)


def reference_inputs(sid: str, row: dict | None, traces: list[str]) -> list[dict]:
    if not row:
        return []
    refs, session = [], {"context": {"spanContext": {"sessionId": sid}}}
    if row.get("assertions"):
        session["assertions"] = [{"text": a} for a in row["assertions"]]
    if row.get("expected_trajectory"):
        session["expectedTrajectory"] = {"toolNames": row["expected_trajectory"]}
    if len(session) > 1:
        refs.append(session)
    for turn, tid in zip(row.get("turns") or [], traces):   # turn i <-> i-th trace of the session
        if turn.get("expected_response"):
            refs.append({"context": {"spanContext": {"sessionId": sid, "traceId": tid}},
                         "expectedResponse": {"text": turn["expected_response"]}})
    return refs


def has_gt(row: dict | None, needs: list[str]) -> bool:
    if not needs:
        return True
    if not row:
        return False
    return all(row.get(n) if n != "expected_response" else any(t.get(n) for t in row.get("turns", [])) for n in needs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=Path, help="out/sessions-*.json written by send_traffic.py")
    ap.add_argument("--session-ids", nargs="*", default=[])
    ap.add_argument("--evaluators", nargs="*", help="default: every metric in define/gate.yaml")
    ap.add_argument("--dataset-file", type=Path, help="ground truth rows (default: the sessions file's source)")
    ap.add_argument("--lookback-hours", type=int, default=6)
    ap.add_argument("--min-age-seconds", type=int, default=300,
                    help="refuse to score sessions younger than this (CloudWatch ingestion, docs: 2-5 min)")
    kit.add_target_args(ap)
    a = ap.parse_args()

    sess = json.loads(a.sessions.read_text()) if a.sessions else {"sessions": {}}
    sessions = {**sess.get("sessions", {}), **{s: None for s in a.session_ids}}
    if not sessions:
        ap.error("give --sessions or --session-ids")
    arn = sess.get("runtime_arn") or kit.runtime_arn()
    if sess.get("endpoint"):          # score the endpoint the traffic went to (its own log group)
        a.endpoint = sess["endpoint"]
    target = kit.set_target(a)
    if sess.get("end"):
        age = (kit.now_utc() - datetime.fromisoformat(sess["end"])).total_seconds()
        if age < a.min_age_seconds:
            raise SystemExit(f"traffic ended {age:.0f}s ago; wait {a.min_age_seconds - age:.0f}s more for ingestion")
    gate = yaml.safe_load(GATE.read_text())
    needs = {m["evaluator"]: m.get("needs_gt", []) for m in gate["metrics"]}
    names = a.evaluators or [m["evaluator"] for m in gate["metrics"]]
    ids = kit.evaluator_ids(names)
    ds_file = a.dataset_file or (kit.KIT / sess["source_file"] if sess.get("source_file") else None)
    rows = {r["scenario_id"]: r for r in kit.load_jsonl(ds_file)} if ds_file else {}

    ctl, dp = kit.client("bedrock-agentcore-control"), kit.client("bedrock-agentcore")
    levels = {n: ctl.get_evaluator(evaluatorId=i)["level"] for n, i in ids.items()}
    collector = CloudWatchAgentSpanCollector(log_group_name=kit.runtime_log_group(arn), region=kit.REGION)
    end = kit.now_utc()
    items: list[dict] = []
    for sid, scenario in sessions.items():
        row = rows.get(scenario)
        spans = collector.collect(session_id=sid, start_time=end - timedelta(hours=a.lookback_hours), end_time=end)
        if not spans:
            items += [{"session_id": sid, "scenario_id": scenario, "evaluator": n, "value": None,
                       "error": "NoSpans: session not ingested yet"} for n in names]
            continue
        traces = ordered_traces(spans)
        turns = len((row or {}).get("turns") or [])
        agent_spans = sum(1 for s in spans if (s.get("attributes") or {}).get("gen_ai.operation.name") == "invoke_agent")
        if turns and agent_spans < turns:   # partially ingested session: scoring it would score half a conversation
            items += [{"session_id": sid, "scenario_id": scenario, "evaluator": n, "value": None,
                       "error": f"PartialSession: {agent_spans}/{turns} turns ingested"} for n in names]
            continue
        refs = reference_inputs(sid, row, traces)
        for name, eid in ids.items():
            if not has_gt(row, needs.get(name, [])):
                continue
            req = {"evaluatorId": eid, "evaluationInput": {"sessionSpans": spans}}
            if refs:
                req["evaluationReferenceInputs"] = refs
            if levels[name] == "TRACE":
                req["evaluationTarget"] = {"traceIds": traces[:10]}
            elif levels[name] == "TOOL_CALL":
                spans_ids = tool_span_ids(spans)
                if not spans_ids:
                    continue
                req["evaluationTarget"] = {"spanIds": spans_ids[:10]}
            try:
                results = dp.evaluate(**req)["evaluationResults"]
            except Exception as e:  # one evaluator failing must not hide the others
                results = [{"errorCode": type(e).__name__, "errorMessage": str(e)[:300]}]
            for r in results:
                ctx = (r.get("context") or {}).get("spanContext") or {}
                items.append({"session_id": sid, "scenario_id": scenario, "evaluator": name,
                              "value": r.get("value"), "label": r.get("label"),
                              "explanation": (r.get("explanation") or "")[:600], "trace_id": ctx.get("traceId"),
                              "error": r.get("errorCode") and f"{r['errorCode']}: {r.get('errorMessage', '')[:200]}",
                              "tokens": r.get("tokenUsage"),
                              "ignored_reference_fields": r.get("ignoredReferenceInputFields")})
    doc = kit.results_doc("ondemand", items, agent={"runtime_arn": arn, **target},
                          evaluators=ids, sessions=sessions)
    path = kit.write_json(kit.OUT / f"ondemand-{kit.stamp()}.results.json", doc)
    for n, m in doc["metrics"].items():
        print(f"{n:34s} mean={m['mean']} n={m['n']} errors={m['errors']} labels={m['labels']}")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
