#!/usr/bin/env bash
# Step 4 Release - move PROD to exactly what the gate passed, and keep the release record for rollback.
#
#   1. recompute gate_key from out/staging.json (release/gate_key.py --check): content, runtime version, bundle
#      version, dataset version and gate.yaml must be the ones the gate decided on
#   2. require a PASS record /<prefix>/gate/<gate_key> (release/record.py) and an approver (APPROVED_BY; in CI the
#      approval is the GitHub environment reviewer / the CodePipeline Manual approval)
#   3. PROD -> runtimeVersion (UpdateAgentRuntimeEndpoint, wait READY)
#   4. bundleVersion set -> pin it on the front-door Gateway (release/ab_ship.sh pin = staticOverride rule)
#   5. /<prefix>/release/previous = old current, /<prefix>/release/current = this release
#      (rollback_lambda.py reads previous; one automatic rollback per release)
# Env: APPROVED_BY (required), STAGING_FILE (out/staging.json), GATEWAY_ID (optional; skip bundle pin with
#      PIN_BUNDLE=0), DRY_RUN=1 (checks only).
# IAM: ssm:GetParameter on parameter/<prefix>/gate/*, ssm:GetParameter/PutParameter on parameter/<prefix>/release/*,
#      bedrock-agentcore:GetAgentRuntimeEndpoint/UpdateAgentRuntimeEndpoint, Create/Update/ListGatewayRules.
# Usage: APPROVED_BY=jane@example.com bash release/release.sh
set -euo pipefail
cd "$(dirname "$0")/.."
REGION="${AWS_REGION:-us-east-1}"
PREFIX="${NAME_PREFIX:-evaldeck}"
PY="${PYTHON:-python3}"
STAGING_FILE="${STAGING_FILE:-out/staging.json}"
: "${APPROVED_BY:?set APPROVED_BY=<who approved this release>}"

# 1. the key the gate decided on must match what we are about to release
KEY=$("$PY" release/gate_key.py --staging "$STAGING_FILE" --check)
read -r RUNTIME_ID VERSION BUNDLE_ARN BUNDLE_VERSION < <("$PY" -c "
import json; d = json.load(open('$STAGING_FILE'))
print(d['runtimeId'], d['runtimeVersion'], d.get('bundleArn') or '-', d.get('bundleVersion') or '-')")

# 2. PASS record
VERDICT=$(aws ssm get-parameter --name "/${PREFIX}/gate/${KEY}" --region "$REGION" --query Parameter.Value \
  --output text 2>/dev/null | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["verdict"])' || echo MISSING)
if [ "$VERDICT" != PASS ]; then
  echo "refusing to release: gate record /${PREFIX}/gate/${KEY} is ${VERDICT}" >&2
  exit 1
fi
OLD=$(aws bedrock-agentcore-control get-agent-runtime-endpoint --agent-runtime-id "$RUNTIME_ID" --endpoint-name PROD \
  --region "$REGION" --query liveVersion --output text)
echo "{\"gate_key\": \"$KEY\", \"verdict\": \"$VERDICT\", \"prod\": \"$OLD -> $VERSION\", \"bundle\": \"$BUNDLE_VERSION\", \"approved_by\": \"$APPROVED_BY\"}"
[ "${DRY_RUN:-0}" = "1" ] && { echo "DRY_RUN: no change"; exit 0; }

# 3. PROD -> tested runtime version
aws bedrock-agentcore-control update-agent-runtime-endpoint --agent-runtime-id "$RUNTIME_ID" --endpoint-name PROD \
  --agent-runtime-version "$VERSION" --region "$REGION" --query '{live:liveVersion,target:targetVersion,status:status}' --output json
for _ in $(seq 1 60); do
  read -r LIVE STATUS < <(aws bedrock-agentcore-control get-agent-runtime-endpoint --agent-runtime-id "$RUNTIME_ID" \
    --endpoint-name PROD --region "$REGION" --query '[liveVersion,status]' --output text)
  [ "$STATUS" = READY ] && [ "$LIVE" = "$VERSION" ] && break
  sleep 5
done
[ "$LIVE" = "$VERSION" ] || { echo "PROD did not reach $VERSION (live $LIVE, $STATUS)" >&2; exit 1; }

# 4. tested bundle -> front-door Gateway
if [ "$BUNDLE_VERSION" != "-" ] && [ "${PIN_BUNDLE:-1}" = "1" ]; then
  bash release/ab_ship.sh pin "$BUNDLE_ARN" "$BUNDLE_VERSION"
fi

# 5. release records: previous <- current, current <- this release
"$PY" - "$KEY" "$OLD" <<'PY'
import json, os, sys
from datetime import UTC, datetime
from common import kit
key, old = sys.argv[1], sys.argv[2]
ssm, p = kit.client("ssm"), f"/{kit.PREFIX}/release"
st = json.load(open(os.getenv("STAGING_FILE", "out/staging.json")))
def get(name):
    try:
        return json.loads(ssm.get_parameter(Name=f"{p}/{name}")["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        return None
before = get("current") or {"runtimeVersion": old, "bundleArn": None, "bundleVersion": None, "gate_key": None,
                            "note": "release made before the kit's records"}
prev = get("previous") if before.get("gate_key") == key else before     # idempotent re-run keeps previous
cur = {"runtimeVersion": st["runtimeVersion"], "bundleArn": st.get("bundleArn"), "bundleVersion": st.get("bundleVersion"),
       "datasetVersion": st["datasetVersion"], "gate_key": key, "approvedBy": os.environ["APPROVED_BY"],
       "at": datetime.now(UTC).isoformat(), "rollbacks": []}
for name, val in (("previous", prev), ("current", cur)):
    ssm.put_parameter(Name=f"{p}/{name}", Value=json.dumps(val), Type="String", Overwrite=True)
    ssm.add_tags_to_resource(ResourceType="Parameter", ResourceId=f"{p}/{name}", Tags=[{"Key": "project", "Value": kit.PREFIX}])
print(json.dumps({"current": {k: cur[k] for k in ("runtimeVersion", "bundleVersion", "gate_key")},
                  "previous": prev and {k: prev.get(k) for k in ("runtimeVersion", "bundleVersion", "gate_key")}}))
PY
