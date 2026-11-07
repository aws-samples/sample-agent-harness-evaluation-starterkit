#!/usr/bin/env python3
"""Step 4 Release - gate_key: the fingerprint that ties a gate decision to exactly what gets released.

gate_key = sha256(git commit, runtimeVersion, bundleVersion, datasetVersion, sha256(define/gate.yaml))[:16]
  - git commit      GIT_COMMIT / GITHUB_SHA / CODEBUILD_RESOLVED_SOURCE_VERSION / `git rev-parse HEAD`
                    (+ "+dirty:<content hash>" when starter-kit/ has uncommitted changes; "nogit:<content hash>"
                    without git, e.g. an S3 source zip)
  - runtimeVersion  the immutable AgentCore Runtime version STAGING points at (deploy_staging.sh)
  - bundleVersion   the Config bundle version id sent as baggage during the gate ("none" = code default prompt)
  - datasetVersion  the pinned AgentCore Dataset version the acceptance segment replayed
  - gate.yaml       thresholds / tiers / policy the decision used
release.sh recomputes the key from out/staging.json and refuses to release without a PASS record for it.

Usage:
  python release/gate_key.py --runtime-version 4 --bundle-version <id|none> --dataset-version 4
  python release/gate_key.py --staging out/staging.json [--check]   # recompute from the staging record
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / "release"))
from gate import content_key  # noqa: E402


def git_commit() -> str:
    for var in ("GIT_COMMIT", "GITHUB_SHA", "CODEBUILD_RESOLVED_SOURCE_VERSION"):
        if os.getenv(var):
            return os.environ[var]
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=KIT, capture_output=True, text=True,
                              check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=KIT, capture_output=True,
                               text=True, check=True).stdout.strip()
        return f"{head}+dirty:{content_key()}" if dirty else head
    except (subprocess.CalledProcessError, FileNotFoundError):
        return f"nogit:{content_key()}"


def compute(commit: str, runtime_version: str, bundle_version: str | None, dataset_version: str,
            gate_file: Path = KIT / "define/gate.yaml") -> str:
    gate_sha = hashlib.sha256(gate_file.read_bytes()).hexdigest()
    parts = [commit, str(runtime_version), bundle_version or "none", str(dataset_version), gate_sha]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]


def from_staging(doc: dict) -> str:
    return compute(doc["gitCommit"], doc["runtimeVersion"], doc.get("bundleVersion"), doc["datasetVersion"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staging", type=Path)
    ap.add_argument("--check", action="store_true", help="exit 1 unless the recomputed key equals staging gate_key")
    ap.add_argument("--commit")
    ap.add_argument("--runtime-version")
    ap.add_argument("--bundle-version")
    ap.add_argument("--dataset-version")
    a = ap.parse_args()
    if a.staging:
        doc = json.loads(a.staging.read_text())
        key = from_staging(doc)
        if a.check and key != doc.get("gate_key"):
            print(f"gate_key mismatch: staging.json says {doc.get('gate_key')}, inputs give {key}", file=sys.stderr)
            return 1
    else:
        if not (a.runtime_version and a.dataset_version):
            ap.error("give --staging or --runtime-version and --dataset-version")
        key = compute(a.commit or git_commit(), a.runtime_version, a.bundle_version, a.dataset_version)
    print(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
