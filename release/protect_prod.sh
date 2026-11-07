#!/usr/bin/env bash
# Step 4 Release - one-time platform hardening: protect the PROD endpoint inside the agent's CloudFormation stack.
#
# `agentcore deploy` is a CDK deploy. The deploy role hands the template to CloudFormation, which runs with the CDK
# bootstrap execution role, so IAM on the deploy role alone cannot stop a template change from re-pointing PROD.
# A stack policy can: it denies every update (modify, replace, delete) of the PROD RuntimeEndpoint resource, for
# whoever runs the stack update. The CDK deploy role has no cloudformation:SetStackPolicy, so CI cannot lift it.
# PROD still moves through release/release.sh (UpdateAgentRuntimeEndpoint API, not CloudFormation).
# Also enables termination protection on the stack.
#
# Run once by a platform admin after the first deploy (STAGING and PROD exist). Re-run if the endpoint's logical id
# changes. deploy_staging.sh keeps the template's PROD pin unchanged, so normal deploys never hit the policy.
# Env: AWS_REGION, STACK_NAME (default AgentCore-evaldeck-default). IAM: cloudformation:GetTemplate,
#      SetStackPolicy, UpdateTerminationProtection on the stack.
# Usage: bash release/protect_prod.sh            (prints the policy; DRY_RUN=1 skips the two write calls)
set -euo pipefail
cd "$(dirname "$0")/.."
REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-AgentCore-evaldeck-default}"
PY="${PYTHON:-python3}"
mkdir -p out

aws cloudformation get-template --stack-name "$STACK_NAME" --region "$REGION" --query TemplateBody --output json \
  > out/deployed-template.json
"$PY" - <<'PY' > out/stack-policy.json
import json
from pathlib import Path
body = json.loads(Path("out/deployed-template.json").read_text())
body = json.loads(body) if isinstance(body, str) else body
prod = [lid for lid, r in body["Resources"].items()
        if r.get("Type") == "AWS::BedrockAgentCore::RuntimeEndpoint" and r["Properties"].get("Name") == "PROD"]
if len(prod) != 1:
    raise SystemExit(f"expected one PROD RuntimeEndpoint in the stack, found {prod}")
print(json.dumps({"Statement": [
    {"Effect": "Allow", "Action": "Update:*", "Principal": "*", "Resource": "*"},
    {"Effect": "Deny", "Action": "Update:*", "Principal": "*", "Resource": f"LogicalResourceId/{prod[0]}"},
]}, indent=2))
PY
cat out/stack-policy.json
if [ "${DRY_RUN:-0}" != "1" ]; then
  aws cloudformation set-stack-policy --stack-name "$STACK_NAME" --region "$REGION" \
    --stack-policy-body file://out/stack-policy.json
  aws cloudformation update-termination-protection --stack-name "$STACK_NAME" --region "$REGION" \
    --enable-termination-protection --query StackId --output text >/dev/null
  aws cloudformation get-stack-policy --stack-name "$STACK_NAME" --region "$REGION" --query StackPolicyBody \
    --output text | "$PY" -c 'import json,sys; print(json.dumps({"stack_policy_set": json.load(sys.stdin)["Statement"][1]["Resource"]}))'
fi
