#!/usr/bin/env bash
# Step 4 Release - the CI gate step. GitHub Actions and CodeBuild both call this one script, so running it
# locally is running exactly what CI runs. Run release/deploy_staging.sh first: it points STAGING at the
# candidate and writes out/staging.json (runtime version, Config bundle version, dataset version, gate_key).
#   1. generated files are current      define/gen_gate.py --check
#   2. evaluator + gate unit tests       pytest (no AWS)
#   3. segment acceptance                evaluate/run_dataset.py --mode ondemand on the pinned dataset version
#   4. segment sim                       evaluate/simulate.py (personas)            [SIM=0 skips]
#      both on STAGING, both send the bundle version as baggage and verify it in the agent spans
#   5. decide                            release/gate.py -> exit 0 PASS / 1 BLOCKED (fails the CI job)
# record.py (SSM) runs after this step in CI whatever the decision, so a BLOCK also revokes an earlier PASS.
# Env: AWS credentials (OIDC role in CI), AWS_REGION, AGENT_RUNTIME_ARN (or a deployed agent/evaldeck project),
#      STAGING_FILE (out/staging.json), RESULTS / SIM_RESULTS (reuse existing results files, skip 3 / 4).
set -euo pipefail
cd "$(dirname "$0")/../.."
PY="${PYTHON:-python}"
STAGING_FILE="${STAGING_FILE:-out/staging.json}"
[ -f "$STAGING_FILE" ] || { echo "missing $STAGING_FILE: run release/deploy_staging.sh first" >&2; exit 1; }
"$PY" define/gen_gate.py --check
"$PY" -m pytest -q define/evaluators/code_eval release

read -r DATASET_VERSION BUNDLE_ID BUNDLE_VERSION < <("$PY" -c "
import json; d = json.load(open('$STAGING_FILE'))
print(d['datasetVersion'], (d.get('bundleArn') or '-').rsplit('/', 1)[-1], d.get('bundleVersion') or '-')")
TARGET=(--endpoint STAGING)
[ "$BUNDLE_VERSION" != "-" ] && TARGET+=(--bundle "$BUNDLE_ID" --bundle-version "$BUNDLE_VERSION")
if [ -z "${RESULTS:-}" ]; then
  "$PY" evaluate/run_dataset.py --mode ondemand --segment acceptance --dataset-version "$DATASET_VERSION" "${TARGET[@]}"
  RESULTS=$(ls -t out/dataset-ondemand-*.results.json | head -1)
fi
ARGS=(--results "$RESULTS")
if [ "${SIM:-1}" = "1" ]; then
  if [ -z "${SIM_RESULTS:-}" ]; then
    "$PY" evaluate/simulate.py "${TARGET[@]}"
    SIM_RESULTS=$(ls -t out/simulate-*.results.json | head -1)
  fi
  ARGS+=(--results "$SIM_RESULTS")
fi
"$PY" release/gate.py "${ARGS[@]}" --staging "$STAGING_FILE" --report out/gate-report.json
