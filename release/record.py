#!/usr/bin/env python3
"""Step 4 Release - store the gate decision in SSM Parameter Store under its gate_key.

/<prefix>/gate/<gate_key> = {gate_key, verdict, at, staging {runtimeVersion, bundleVersion, datasetVersion,
gitCommit}, segments, waived, reasons, metrics (segment/metric/status/mean/n), report_sha256}.
release.sh reads it: no PASS record for the recomputed gate_key -> no release. The latest decision for a key
overwrites the previous one (a re-run that BLOCKS revokes an earlier PASS for the same content).
The full report stays a CI artifact (out/gate-report.json); its sha256 is in the record.

IAM: ssm:PutParameter (+ ssm:AddTagsToResource for the tag) on parameter/<prefix>/gate/*.
Usage: python release/record.py [--report out/gate-report.json] [--staging out/staging.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import kit  # noqa: E402

MAX = 4096   # SSM Standard tier


def build(report: dict, staging: dict, report_bytes: bytes) -> dict:
    rec = {"gate_key": report["gate_key"], "verdict": report["decision"], "at": report["at"],
           "staging": {k: staging.get(k) for k in ("runtimeVersion", "bundleArn", "bundleVersion", "datasetVersion",
                                                   "gitCommit")},
           "segments": {s: v["decision"] for s, v in (report.get("segments") or {}).items()},
           "waived": report.get("waived", []), "reasons": [r[:160] for r in report.get("reasons", [])][:5],
           "metrics": [[m["segment"], m["metric"], m["status"], m["mean"], m["n"]] for m in report["metrics"]],
           "report_sha256": hashlib.sha256(report_bytes).hexdigest()}
    while len(json.dumps(rec)) > MAX and rec["metrics"]:
        rec["metrics"].pop()          # keep the verdict; the artifact has the full report
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path, default=kit.OUT / "gate-report.json")
    ap.add_argument("--staging", type=Path, default=kit.OUT / "staging.json")
    a = ap.parse_args()
    raw = a.report.read_bytes()
    report, staging = json.loads(raw), json.loads(a.staging.read_text())
    if report["gate_key"] != staging["gate_key"]:
        raise SystemExit(f"report gate_key {report['gate_key']} != staging gate_key {staging['gate_key']}")
    rec = build(report, staging, raw)
    name = f"/{kit.PREFIX}/gate/{rec['gate_key']}"
    ssm = kit.client("ssm")
    ssm.put_parameter(Name=name, Value=json.dumps(rec, separators=(",", ":")), Type="String", Overwrite=True,
                      Description=f"{kit.PREFIX} gate decision {rec['verdict']}")
    ssm.add_tags_to_resource(ResourceType="Parameter", ResourceId=name, Tags=[{"Key": "project", "Value": kit.PREFIX}])
    print(json.dumps({"parameter": name, "verdict": rec["verdict"], "runtimeVersion": rec["staging"]["runtimeVersion"],
                      "bundleVersion": rec["staging"]["bundleVersion"], "datasetVersion": rec["staging"]["datasetVersion"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
