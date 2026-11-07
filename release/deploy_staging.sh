#!/usr/bin/env bash
# Step 4 Release - deploy the candidate and point the STAGING endpoint at it (the gate then runs on STAGING).
# Runs in its own CI stage (CodePipeline DeployStaging / GitHub job deploy_staging) with the deploy role; the Gate
# role cannot deploy and cannot move any endpoint.
#
#   1. endpoint pins in agentcore.json = the DEPLOYED template's pins  (CloudFormation leaves STAGING / PROD alone;
#      the PROD endpoint resource is also protected by a stack policy, release/protect_prod.sh)
#   2. agentcore deploy                                   (code change -> new immutable runtime version)
#   3. STAGING -> newest runtime version                  (UpdateAgentRuntimeEndpoint, wait READY)
#   4. PROD must be unchanged                             (live version before == after, else exit 1)
#   5. out/staging.json {runtimeVersion, bundleVersion, datasetVersion, gitCommit, gate_key, ...}
# DEFAULT keeps following the latest version (and is denied by release/harden_default.sh); PROD only moves in
# release/release.sh. The Config bundle is not deployed here: BUNDLE_ID + BUNDLE_VERSION name the version the gate
# sends as baggage and release.sh pins on the Gateway.
#
# Env: AWS_REGION, BUNDLE_ID + BUNDLE_VERSION (optional), DATASET_VERSION (default: newest in registry.json),
#      SKIP_DEPLOY=1 (only re-point STAGING at the newest existing version), RUNTIME_VERSION (pin explicitly),
#      STACK_NAME (default AgentCore-evaldeck-default).
# IAM (DeployRole in release/ci/pipeline.yaml): CDK deploy (sts:AssumeRole on the cdk-* bootstrap roles),
#      cloudformation:GetTemplate, bedrock-agentcore:ListAgentRuntimeVersions, ListAgentRuntimeEndpoints,
#      GetAgentRuntimeEndpoint, UpdateAgentRuntimeEndpoint on runtime-endpoint/STAGING only (PROD / DEFAULT denied),
#      GetConfigurationBundleVersion.
# Usage: BUNDLE_ID=<id> BUNDLE_VERSION=<version-id> bash release/deploy_staging.sh
set -euo pipefail
cd "$(dirname "$0")/.."
REGION="${AWS_REGION:-us-east-1}"
PY="${PYTHON:-python3}"
PROJECT=agent/evaldeck
mkdir -p out

RUNTIME_ID=$("$PY" -c 'from common import kit; print(kit.runtime_id())' 2>/dev/null || true)

# 1. agentcore.json declares STAGING / PROD with a creation version; afterwards CI / release move them with
#    UpdateAgentRuntimeEndpoint, so live versions differ from the template. Copy the pins of the DEPLOYED template
#    into agentcore.json: the endpoint resources then have no change-set entry and CloudFormation never re-points
#    them. (Writing the live PROD version would be a template change to PROD, which the stack policy denies.)
STACK_NAME="${STACK_NAME:-AgentCore-evaldeck-default}"
PROD_BEFORE=""
if [ -n "$RUNTIME_ID" ]; then
  PROD_BEFORE=$(aws bedrock-agentcore-control get-agent-runtime-endpoint --agent-runtime-id "$RUNTIME_ID" \
    --endpoint-name PROD --region "$REGION" --query liveVersion --output text 2>/dev/null || true)
  aws cloudformation get-template --stack-name "$STACK_NAME" --region "$REGION" --query TemplateBody \
    --output json > out/deployed-template.json 2>/dev/null || rm -f out/deployed-template.json
  "$PY" - <<'PY'
import json
from pathlib import Path
p = Path("agent/evaldeck/agentcore/agentcore.json"); d = json.loads(p.read_text())
t = Path("out/deployed-template.json")
if not t.exists():
    print(json.dumps({"endpoint_pins": "no deployed stack yet, keeping agentcore.json"})); raise SystemExit
body = json.loads(t.read_text())
body = json.loads(body) if isinstance(body, str) else body
deployed = {r["Properties"]["Name"]: int(r["Properties"]["AgentRuntimeVersion"])
            for r in body.get("Resources", {}).values() if r.get("Type") == "AWS::BedrockAgentCore::RuntimeEndpoint"}
changed = {}
for rt in d["runtimes"]:
    for name, ep in (rt.get("endpoints") or {}).items():
        if name in deployed and ep["version"] != deployed[name]:
            changed[name] = (ep["version"], deployed[name]); ep["version"] = deployed[name]
if changed:
    p.write_text(json.dumps(d, indent=2) + "\n")
print(json.dumps({"endpoint_pins_synced_to_deployed_template": changed}))
PY
fi

# 2. deploy (no-op when nothing changed: no new runtime version). CI checkouts have no aws-targets.json
#    (account-specific, gitignored): write the default target from the caller's credentials.
if [ "${SKIP_DEPLOY:-0}" != "1" ]; then
  TARGETS="$PROJECT/agentcore/aws-targets.json"
  [ -f "$TARGETS" ] || printf '[{"name": "default", "account": "%s", "region": "%s"}]\n' \
    "$(aws sts get-caller-identity --query Account --output text)" "$REGION" > "$TARGETS"
  (cd "$PROJECT" && agentcore deploy --region "$REGION") | tee out/deploy.log | tail -5
  RUNTIME_ID=$("$PY" -c 'from common import kit; print(kit.runtime_id())')
fi

# 3. STAGING -> the newest version (or RUNTIME_VERSION)
VERSION="${RUNTIME_VERSION:-$(aws bedrock-agentcore-control list-agent-runtime-versions --agent-runtime-id "$RUNTIME_ID" \
  --region "$REGION" --query 'max(agentRuntimes[].to_number(agentRuntimeVersion))' --output text)}"
aws bedrock-agentcore-control update-agent-runtime-endpoint --agent-runtime-id "$RUNTIME_ID" --endpoint-name STAGING \
  --agent-runtime-version "$VERSION" --region "$REGION" --query '{live:liveVersion,target:targetVersion,status:status}' --output json
for _ in $(seq 1 60); do
  read -r LIVE STATUS < <(aws bedrock-agentcore-control get-agent-runtime-endpoint --agent-runtime-id "$RUNTIME_ID" \
    --endpoint-name STAGING --region "$REGION" --query '[liveVersion,status]' --output text)
  [ "$STATUS" = READY ] && [ "$LIVE" = "$VERSION" ] && break
  sleep 5
done
[ "$LIVE" = "$VERSION" ] || { echo "STAGING did not reach version $VERSION (live $LIVE, $STATUS)" >&2; exit 1; }

# 4. PROD must not have moved (the deploy role is denied PROD; this also catches any other path)
if [ -n "$PROD_BEFORE" ] && [ "$PROD_BEFORE" != None ]; then
  PROD_AFTER=$(aws bedrock-agentcore-control get-agent-runtime-endpoint --agent-runtime-id "$RUNTIME_ID" \
    --endpoint-name PROD --region "$REGION" --query liveVersion --output text)
  [ "$PROD_AFTER" = "$PROD_BEFORE" ] || { echo "PROD moved during deploy: $PROD_BEFORE -> $PROD_AFTER" >&2; exit 1; }
  echo "{\"prod_unchanged\": \"$PROD_AFTER\"}"
fi

# 5. staging record + gate_key
"$PY" - "$RUNTIME_ID" "$VERSION" <<'PY'
import json, os, subprocess, sys
from datetime import UTC, datetime
from common import kit
rid, version = sys.argv[1], sys.argv[2]
reg_file = kit.KIT / "define/datasets/registry.json"
reg = (json.loads(reg_file.read_text()) if reg_file.exists() else {}).get(f"{kit.PREFIX}_acceptance")
if not reg or not reg.get("versions"):
    sys.exit("define/datasets/registry.json has no published acceptance version: run define/datasets/publish_dataset.py")
ds_version = os.getenv("DATASET_VERSION") or reg["versions"][-1]["version"]
bundle = None
if os.getenv("BUNDLE_VERSION"):
    v = kit.client("bedrock-agentcore-control").get_configuration_bundle_version(
        bundleId=os.environ["BUNDLE_ID"].rsplit("/", 1)[-1], versionId=os.environ["BUNDLE_VERSION"])
    bundle = {"arn": v["bundleArn"], "version": v["versionId"]}
sys.path.insert(0, str(kit.KIT / "release"))
import gate_key
doc = {"runtimeId": rid, "runtimeArn": kit.runtime_arn(), "endpoint": "STAGING", "runtimeVersion": version,
       "bundleArn": (bundle or {}).get("arn"), "bundleVersion": (bundle or {}).get("version"),
       "datasetName": f"{kit.PREFIX}_acceptance", "datasetId": reg["id"], "datasetVersion": str(ds_version),
       "gitCommit": gate_key.git_commit(), "at": datetime.now(UTC).isoformat()}
doc["gate_key"] = gate_key.from_staging(doc)
kit.write_json(kit.OUT / "staging.json", doc)
print(json.dumps({k: doc[k] for k in ("endpoint", "runtimeVersion", "bundleVersion", "datasetVersion", "gate_key")}))
PY
