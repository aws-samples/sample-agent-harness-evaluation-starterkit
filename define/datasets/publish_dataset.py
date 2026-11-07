#!/usr/bin/env python3
"""Step 1 Define - create the AgentCore Dataset from a JSONL file and publish an immutable version.

Idempotent: a new version is published only when the file's content differs from the latest published
version (compared row by row, ignoring the service-assigned exampleId). CI then pins that version number.

Equivalent CLI (what this script runs the first time):
  agentcore eval dataset create --name evaldeck_acceptance --schema-type predefined \
      --source file://define/datasets/acceptance.jsonl --tags project=evaldeck
  agentcore eval dataset publish --id <dataset-id>      # returns while UPDATING: poll get until ACTIVE

Prerequisites: AgentCore CLI (npm i -g @aws/agentcore), boto3, IAM bedrock-agentcore:*Dataset*.
Usage:
  python define/datasets/publish_dataset.py [--file define/datasets/acceptance.jsonl] [--schema predefined]
Writes define/datasets/registry.json  {name: {id, versions: [{version, sha256, rows}]}}.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import kit  # noqa: E402

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "registry.json"
SCHEMA = {"predefined": "AGENTCORE_EVALUATION_PREDEFINED_V1", "simulated": "AGENTCORE_EVALUATION_SIMULATED_V1"}


def _norm(v):
    """The service stores 0.0 as 0 when a dataset is created from a file: compare integral floats as ints."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def canonical_rows(rows: list[dict]) -> list[str]:
    return sorted(json.dumps(_norm({k: v for k, v in r.items() if k != "exampleId"}), sort_keys=True,
                             ensure_ascii=False) for r in rows)


def find_dataset(name: str) -> dict | None:
    ctl = kit.client("bedrock-agentcore-control")
    token = None
    while True:
        r = ctl.list_datasets(**({"nextToken": token} if token else {}))
        for d in r.get("datasets", []):
            if d["datasetName"] == name:
                return d
        token = r.get("nextToken")
        if not token:
            return None


def wait_active(dataset_id: str, version: str | None = None, timeout: int = 300) -> dict:
    ctl = kit.client("bedrock-agentcore-control")
    for _ in range(timeout // 5):
        d = ctl.get_dataset(datasetId=dataset_id, **({"datasetVersion": version} if version else {}))
        if d["status"] == "ACTIVE":
            return d
        if d["status"].endswith("FAILED"):
            raise SystemExit(f"dataset {dataset_id} is {d['status']}: {d.get('failureReason')}")
        time.sleep(5)
    raise SystemExit(f"dataset {dataset_id} not ACTIVE after {timeout}s")


def version_rows(dataset_id: str, version: str) -> list[dict]:
    ctl = kit.client("bedrock-agentcore-control")
    rows, token = [], None
    while True:
        r = ctl.list_dataset_examples(datasetId=dataset_id, datasetVersion=version,
                                      **({"nextToken": token} if token else {}))
        rows += r.get("examples", [])
        token = r.get("nextToken")
        if not token:
            return rows


def latest_version(dataset_id: str) -> str | None:
    vs = kit.client("bedrock-agentcore-control").list_dataset_versions(datasetId=dataset_id).get("versions", [])
    nums = [int(v["datasetVersion"]) for v in vs if str(v.get("datasetVersion", "")).isdigit()]
    return str(max(nums)) if nums else None


def cli(*args: str) -> dict:
    out = subprocess.run(["agentcore", *args, "--region", kit.REGION, "--json"], capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"agentcore {' '.join(args)} failed:\n{out.stderr or out.stdout}")
    return json.loads(out.stdout)


def publish(name: str, path: Path, schema: str, dataset_id: str | None = None) -> dict:
    """Make the latest published version equal to `path`; returns {id, version, changed}."""
    rows = kit.load_jsonl(path)
    existing = {"datasetId": dataset_id} if dataset_id else find_dataset(name)
    if not existing:
        created = cli("eval", "dataset", "create", "--name", name, "--schema-type", schema,
                      "--source", f"file://{path}", "--tags", f"project={kit.PREFIX}")
        dataset_id, changed = created["datasetId"], True
        wait_active(dataset_id)
    else:
        dataset_id = existing["datasetId"]
        latest = latest_version(dataset_id)
        if latest and canonical_rows(version_rows(dataset_id, latest)) == canonical_rows(rows):
            return {"id": dataset_id, "version": latest, "changed": False, "rows": len(rows)}
        # replace the DRAFT with the file's rows (delete + add keeps exampleIds out of the local file)
        ctl = kit.client("bedrock-agentcore-control")
        wait_active(dataset_id)
        draft = version_rows(dataset_id, "DRAFT")
        if draft:
            ctl.delete_dataset_examples(datasetId=dataset_id, exampleIds=[r["exampleId"] for r in draft])
            wait_active(dataset_id)
        ctl.add_dataset_examples(datasetId=dataset_id, source={"inlineExamples": {"examples": rows}})
        wait_active(dataset_id)
        changed = True
    pub = cli("eval", "dataset", "publish", "--id", dataset_id)
    version = str(pub["datasetVersion"])
    wait_active(dataset_id, version)
    return {"id": dataset_id, "version": version, "changed": changed, "rows": len(rows)}


def record(name: str, path: Path, schema: str, res: dict) -> None:
    reg = json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}
    entry = reg.get(name)
    if not entry or entry.get("id") != res["id"]:
        # first publish in this account / region (or the dataset was re-created): start a fresh history
        entry = reg[name] = {"id": res["id"], "schema": SCHEMA[schema], "versions": []}
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if not any(v["version"] == res["version"] for v in entry["versions"]):
        entry["versions"].append({"version": res["version"], "sha256": sha, "rows": res["rows"],
                                  "published_at": kit.now_utc().isoformat()})
    REGISTRY.write_text(json.dumps(reg, indent=2) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=HERE / "acceptance.jsonl")
    ap.add_argument("--name", help="dataset name (default <prefix>_<file stem>)")
    ap.add_argument("--schema", choices=sorted(SCHEMA), default="predefined")
    a = ap.parse_args()
    name = a.name or f"{kit.PREFIX}_{a.file.stem}"
    res = publish(name, a.file, a.schema)
    record(name, a.file, a.schema, res)
    print(json.dumps({"dataset": name, **res}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
