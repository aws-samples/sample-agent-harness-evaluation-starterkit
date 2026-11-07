#!/usr/bin/env python3
"""Step 6 Feedback - mine bad-case candidates from scores you already paid for (online / batch / on-demand /
simulation results), rebuild each conversation from the runtime traces, and write candidate dataset rows with
lineage metadata for human review. Mining costs no evaluator calls.

Candidates (per session): any red-line result < 1.0, then gate metrics below their threshold (define/gate.yaml),
lowest score first. Evaluator errors are not candidates (they are evaluation-side problems, not agent ones).
Each row is AGENTCORE_EVALUATION_PREDEFINED_V1: the real user turns, NO expected_response yet (the reviewer
writes it from the tool facts), and metadata.lineage pointing back to the session / trace / evaluator / score /
judge explanation so it can be opened again in Omni Web or the CloudWatch console.

Review = edit the file: set metadata.review.decision to accept (agent defect -> regression row; fill
turns[].expected_response and assertions), judge_error (goes to the judge-regression set, not the agent's
dataset) or reject. Then feedback/promote.py.

Usage:
  python feedback/mine_badcases.py --online-config <config-id> [--hours 6]      # online evaluation results
  python feedback/mine_badcases.py --results out/simulate-<ts>.results.json    # any evaldeck results file(s)
Writes out/badcases-<ts>.jsonl.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import yaml
from bedrock_agentcore.evaluation import CloudWatchAgentSpanCollector

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import kit  # noqa: E402


def online_items(config_id: str, hours: int) -> list[dict]:
    """Result events of an online evaluation config (OTel GenAI `gen_ai.evaluation.result` events, one JSON per
    log line). Read stream by stream with GetLogEvents (FilterLogEvents can lag behind fresh writes)."""
    logs = kit.client("logs")
    group = f"/aws/bedrock-agentcore/evaluations/results/{config_id}"
    start = int((kit.now_utc() - timedelta(hours=hours)).timestamp() * 1000)
    items = []
    for page in logs.get_paginator("describe_log_streams").paginate(logGroupName=group):
        for stream in page.get("logStreams", []):
            kw, token = {"logGroupName": group, "logStreamName": stream["logStreamName"], "startTime": start,
                         "startFromHead": True}, None
            while True:
                r = logs.get_log_events(**kw, **({"nextToken": token} if token else {}))
                for e in r.get("events", []):
                    try:
                        doc = json.loads(e["message"])
                    except json.JSONDecodeError:
                        continue
                    at = doc.get("attributes") or {}
                    if at.get("gen_ai.evaluation.name"):
                        items.append({"session_id": at.get("session.id"), "scenario_id": None,
                                      "evaluator": at["gen_ai.evaluation.name"],
                                      "value": at.get("gen_ai.evaluation.score.value"),
                                      "label": at.get("gen_ai.evaluation.score.label"),
                                      "trace_id": doc.get("traceId") or at.get("aws.bedrock_agentcore.evaluation_start_trace_id"),
                                      "explanation": at.get("gen_ai.evaluation.explanation"),
                                      "error": at.get("error.type")})
                if r.get("nextForwardToken") in (None, token):
                    break
                token = r["nextForwardToken"]
    return items


def pick(items: list[dict], gate: dict) -> list[dict]:
    specs = {m["evaluator"]: m for m in gate["metrics"]}
    bad = []
    for it in items:
        spec = specs.get(kit.canonical(it["evaluator"]))
        if not spec or it.get("error") or it.get("value") is None:
            continue
        v = float(it["value"])
        if spec["tier"] == "redline" and v < 1.0:
            bad.append({**it, "kind": "redline", "rank": 0})
        elif spec["tier"] == "gate" and v < spec.get("threshold", 0):
            bad.append({**it, "kind": "low_score", "rank": 1})
    return sorted(bad, key=lambda b: (b["rank"], float(b["value"])))


def conversation(collector, sid: str, hours: int) -> list[dict]:
    end = kit.now_utc()
    spans = collector.collect(session_id=sid, start_time=end - timedelta(hours=hours), end_time=end)
    turns = []
    for s in sorted(spans, key=lambda s: int(s.get("startTimeUnixNano") or 0)):
        if (s.get("attributes") or {}).get("gen_ai.operation.name") != "invoke_agent":
            continue
        ev = {e["name"]: e.get("attributes") or {} for e in s.get("events") or []}
        user = ev.get("gen_ai.user.message", {}).get("content", "")
        try:
            user = " ".join(b.get("text", "") for b in json.loads(user))
        except (ValueError, TypeError, AttributeError):
            pass
        turns.append({"input": user, "answer": ev.get("gen_ai.choice", {}).get("message", ""), "trace_id": s.get("traceId")})
    return turns


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--online-config")
    ap.add_argument("--results", type=Path, action="append", default=[])
    ap.add_argument("--hours", type=int, default=6)
    ap.add_argument("--max", type=int, default=20)
    a = ap.parse_args()
    gate = yaml.safe_load((kit.KIT / "define/gate.yaml").read_text())
    items, source = [], []
    if a.online_config:
        items += online_items(a.online_config, a.hours)
        source.append(f"online:{a.online_config}")
    for p in a.results:
        items += json.loads(p.read_text())["items"]
        source.append(p.name)
    by_session: dict[str, list[dict]] = defaultdict(list)
    for b in pick(items, gate):
        by_session[b["session_id"]].append(b)
    collector = CloudWatchAgentSpanCollector(log_group_name=kit.runtime_log_group(), region=kit.REGION)
    day, rows = kit.now_utc().strftime("%Y%m%d"), []
    for sid, flags in list(by_session.items())[: a.max]:
        turns = conversation(collector, sid, a.hours)
        if not turns:
            continue
        worst = flags[0]
        rows.append({
            "scenario_id": f"BAD-{day}-{hashlib.sha256(sid.encode()).hexdigest()[:6]}",   # stable per session
            "turns": [{"input": t["input"]} for t in turns],
            "assertions": [],
            "metadata": {
                "type": "badcase", "lifecycle_step": "feedback", "req_ids": [],
                "source": f"{'+'.join(source)}#{kit.canonical(worst['evaluator'])}",
                "lineage": {"origin_session_id": sid, "origin_scenario_id": worst.get("scenario_id"),
                            "flags": [{"evaluator": kit.canonical(f["evaluator"]), "kind": f["kind"], "value": f["value"],
                                       "trace_id": f.get("trace_id"), "explanation": (f.get("explanation") or "")[:400]}
                                      for f in flags],
                            "actual_responses": [t["answer"][:600] for t in turns],
                            "mined_at": kit.now_utc().isoformat()},
                "review": {"decision": "pending", "reviewer": None, "note": ""}}})
    path = kit.OUT / f"badcases-{kit.stamp()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(json.dumps({"scored_results": len(items), "flagged_sessions": len(by_session), "candidates": len(rows),
                      "file": str(path)}))
    for r in rows:
        f = r["metadata"]["lineage"]["flags"][0]
        print(f"  {r['scenario_id']} <- {r['metadata']['lineage']['origin_session_id'][:48]} {f['evaluator']}={f['value']} ({f['kind']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
