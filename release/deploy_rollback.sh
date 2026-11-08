#!/usr/bin/env bash
# Step 4 Release / 5 Observe - package release/rollback_lambda.py and deploy release/rollback.yaml
# (alarm -> EventBridge -> Lambda -> PROD back to /<prefix>/release/previous).
#   1. zip the handler (no dependencies: the Lambda runtime's boto3)
#   2. upload it to the CDK bootstrap assets bucket (exists because `agentcore deploy` needs a CDK bootstrap);
#      CODE_BUCKET overrides
#   3. aws cloudformation deploy --stack-name <prefix>-rollback
# Drill: DRY_RUN=true (default) logs the decision only; then
#   aws cloudwatch set-alarm-state --alarm-name <prefix>-shopagent-redline --state-value ALARM --state-reason drill
#   aws logs tail /aws/lambda/<prefix>-rollback --since 10m        # {"event": "rollback-decision", ...}
# and DRY_RUN=false bash release/deploy_rollback.sh to arm it. One automatic rollback per release; re-release with
# release/release.sh (or the pipeline) afterwards.
# Env: AWS_REGION, NAME_PREFIX, DRY_RUN (true), GATEWAY_ID, AB_TEST_ID, ALLOWED_ALARMS (optional), CODE_BUCKET.
# IAM: s3:PutObject on the code bucket, cloudformation deploy + iam:CreateRole/PutRolePolicy/PassRole for the stack.
# Usage: bash release/deploy_rollback.sh
set -euo pipefail
cd "$(dirname "$0")/.."
REGION="${AWS_REGION:-us-east-1}"
PREFIX="${NAME_PREFIX:-evaldeck}"
PY="${PYTHON:-python3}"
mkdir -p out

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${CODE_BUCKET:-cdk-hnb659fds-assets-${ACCOUNT}-${REGION}}"
ARN=$("$PY" -c 'from common import kit; print(kit.runtime_arn())')
SHA=$("$PY" -c "import hashlib; print(hashlib.sha256(open('release/rollback_lambda.py','rb').read()).hexdigest()[:12])")
KEY="${PREFIX}/rollback-lambda-${SHA}.zip"
rm -f out/rollback-lambda.zip && (cd release && zip -q -X ../out/rollback-lambda.zip rollback_lambda.py)
aws s3 cp out/rollback-lambda.zip "s3://${BUCKET}/${KEY}" --region "$REGION" --only-show-errors
aws cloudformation deploy --stack-name "${PREFIX}-rollback" --template-file release/rollback.yaml --region "$REGION" \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset --tags project="$PREFIX" \
  --parameter-overrides NamePrefix="$PREFIX" AgentRuntimeArn="$ARN" DryRun="${DRY_RUN:-true}" \
    GatewayId="${GATEWAY_ID:-}" AbTestId="${AB_TEST_ID:-}" AllowedAlarms="${ALLOWED_ALARMS:-}" CodeS3Bucket="$BUCKET" CodeS3Key="$KEY"
aws lambda get-function-configuration --function-name "${PREFIX}-rollback" --region "$REGION" \
  --query '{function:FunctionName,state:State,dryRun:Environment.Variables.DRY_RUN,runtime:Environment.Variables.AGENT_RUNTIME_ID}' \
  --output json
