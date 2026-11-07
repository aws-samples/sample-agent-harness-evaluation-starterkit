#!/usr/bin/env bash
# Step 1 Define - deploy the CustomCode (Lambda) evaluator and register it with AgentCore Evaluations.
#
# Creates (idempotent: re-running updates the code, never duplicates):
#   IAM role       ${PREFIX}-code-eval-role   (Lambda basic execution only - the function reads nothing else)
#   Lambda         ${PREFIX}-redline-code     (python3.13, handler.handler)
#   Evaluator      ${PREFIX}_RedlineCode      (CustomCode, SESSION level)
# The Evaluations service invokes the function with the caller's credentials (on-demand / batch) or the online
# evaluation execution role, which need lambda:InvokeFunction + lambda:GetFunction on this function ARN.
#
# Prerequisites: AWS CLI v2, AgentCore CLI 1.0.x, zip; run python -m pytest define/evaluators/code_eval first.
# Usage: AWS_PROFILE=... AWS_REGION=us-east-1 bash define/evaluators/code_eval/deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
REGION="${AWS_REGION:-us-east-1}"
PREFIX="${NAME_PREFIX:-evaldeck}"
ROLE="${PREFIX}-code-eval-role"
FN="${PREFIX}-redline-code"
EVAL="${PREFIX}_RedlineCode"

if ! ROLE_ARN=$(aws iam get-role --role-name "$ROLE" --query Role.Arn --output text 2>/dev/null); then
  ROLE_ARN=$(aws iam create-role --role-name "$ROLE" --tags Key=project,Value="$PREFIX" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' \
    --query Role.Arn --output text)
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  sleep 10   # IAM propagation before Lambda can assume the new role
fi

rm -f /tmp/${FN}.zip && zip -q -j /tmp/${FN}.zip handler.py
if aws lambda get-function --function-name "$FN" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FN" --zip-file fileb:///tmp/${FN}.zip --region "$REGION" >/dev/null
else
  aws lambda create-function --function-name "$FN" --runtime python3.13 --handler handler.handler \
    --role "$ROLE_ARN" --zip-file fileb:///tmp/${FN}.zip --timeout 60 --memory-size 256 \
    --tags project="$PREFIX" --region "$REGION" >/dev/null
fi
aws lambda wait function-updated-v2 --function-name "$FN" --region "$REGION"
FN_ARN=$(aws lambda get-function --function-name "$FN" --region "$REGION" --query Configuration.FunctionArn --output text)

# register once; later runs only update the Lambda code (the evaluator just points at the ARN)
if ! agentcore eval evaluator list --region "$REGION" --json | grep -q "\"evaluatorName\": \"${EVAL}\""; then
  agentcore eval evaluator code-based create --name "$EVAL" --level SESSION --lambda-arn "$FN_ARN" \
    --timeout 60 --tags project="$PREFIX" --region "$REGION" --json
fi
echo "{\"lambda\": \"$FN_ARN\", \"evaluator\": \"$EVAL\"}"
