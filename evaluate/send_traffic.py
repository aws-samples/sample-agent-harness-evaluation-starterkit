#!/usr/bin/env python3
"""Steps 3 / 5 - send real conversations to the deployed runtime and record the session ids.

Replays the turns of a predefined dataset file (one runtime session per scenario, turns in order) so that
on-demand / batch evaluation and online evaluation have real traces to score. Writes
out/sessions-<ts>.json: {"runtime_arn", "endpoint", "bundle", "start", "end", "sessions": {session_id: scenario_id}}.
Wait 2-5 minutes after it finishes before evaluating (CloudWatch ingestion).

Prerequisites: deployed agent (agent/evaldeck), boto3, bedrock-agentcore:InvokeAgentRuntime.
Usage:
  python evaluate/send_traffic.py [--file define/datasets/acceptance.jsonl] [--repeat 1] [--tag acc] \
      [--endpoint STAGING|PROD] [--bundle <bundle-id> --bundle-version <version-id>]
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import kit  # noqa: E402


def run_row(row: dict, tag: str, arn: str) -> tuple[str, str, list[str]]:
    sid = kit.new_session_id(f"{tag}-{row['scenario_id']}")
    answers = [kit.invoke(t["input"], sid, arn) for t in row.get("turns") or [{"input": row["input"]}]]
    return sid, row["scenario_id"], answers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=kit.KIT / "define/datasets/acceptance.jsonl")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--tag", default="traffic")
    kit.add_target_args(ap)
    a = ap.parse_args()
    target = kit.set_target(a)
    arn, rows = kit.runtime_arn(), kit.load_jsonl(a.file) * a.repeat
    start = kit.now_utc()
    with ThreadPoolExecutor(max_workers=5) as pool:
        done = list(pool.map(lambda r: run_row(r, a.tag, arn), rows))
    doc = {"runtime_arn": arn, **target, "start": start.isoformat(), "end": kit.now_utc().isoformat(),
           "source_file": str(a.file.relative_to(kit.KIT)) if a.file.is_relative_to(kit.KIT) else str(a.file),
           "sessions": {sid: sc for sid, sc, _ in done},
           "answers": {sid: ans for sid, _, ans in done}}
    path = kit.write_json(kit.OUT / f"sessions-{a.tag}-{kit.stamp()}.json", doc)
    for sid, sc, ans in done:
        print(f"{sc:28s} {sid}  -> {ans[-1][:90]!r}")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
