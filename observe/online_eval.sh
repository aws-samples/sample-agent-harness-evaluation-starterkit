#!/usr/bin/env bash
# Step 5 Observe - Online evaluation: sample live sessions of one runtime endpoint and score them continuously.
#
# Only evaluators WITHOUT ground truth (online traffic has none): the same custom judge + code red line as
# the gate, plus GoalSuccessRate without assertions. Results -> log group
# /aws/bedrock-agentcore/evaluations/results/<config-id> and metrics namespace Bedrock-AgentCore/Evaluations
# (observe/alarms.yaml alarms on them). A custom evaluator referenced by an ENABLED config is locked: to change a
# judge, pause the config or register a new judge name.
# Idempotent: an existing config with the same name is left as is (use `agentcore eval online-eval update`).
#
# Prerequisites: AgentCore CLI 1.0.x; custom evaluators registered (define/evaluators); deployed runtime.
# The CLI auto-provisions the execution role (pass --role-arn to use your own; it needs logs read on the runtime
# log group, logs write on /aws/bedrock-agentcore/evaluations/*, bedrock:InvokeModel, lambda:InvokeFunction
# + lambda:GetFunction on the code evaluator).
# Usage: SAMPLING_RATE=10 ENDPOINT=PROD bash observe/online_eval.sh   (production traffic; CI runs on STAGING)
set -euo pipefail
cd "$(dirname "$0")/.."
REGION="${AWS_REGION:-us-east-1}"
PREFIX="${NAME_PREFIX:-evaldeck}"
ENDPOINT="${ENDPOINT:-PROD}"
RATE="${SAMPLING_RATE:-10}"               # percent: 10 = 10 % of sessions (1.0 means 1 %)
NAME="${PREFIX}_online_${ENDPOINT}"
PY="${PYTHON:-python3}"

RUNTIME_ID=$("$PY" -c 'from common import kit; print(kit.runtime_id())')
read -r SCOPE REDLINE < <("$PY" -c "from common import kit; ids = kit.evaluator_ids(['${PREFIX}_ScopeJudge', '${PREFIX}_RedlineCode']); print(*ids.values())")

if agentcore eval online-eval list --region "$REGION" --json | grep -q "\"${NAME}\""; then
  echo "online evaluation config ${NAME} already exists"
else
  agentcore eval online-eval create --name "$NAME" --agent "$RUNTIME_ID" --endpoint "$ENDPOINT" \
    --evaluators "$SCOPE" "$REDLINE" Builtin.GoalSuccessRate Builtin.Helpfulness \
    --sampling-rate "$RATE" --session-timeout-minutes "${SESSION_TIMEOUT_MINUTES:-15}" \
    --description "starter-kit online evaluation (${ENDPOINT})" --tags project="$PREFIX" \
    --region "$REGION" --json
fi
agentcore eval online-eval list --region "$REGION" --json | "$PY" -c "
import json, sys
for c in json.load(sys.stdin).get('onlineEvaluationConfigs', []):
    if c.get('onlineEvaluationConfigName') == '${NAME}':
        print(json.dumps({k: c.get(k) for k in ('onlineEvaluationConfigId', 'status', 'executionStatus')}))
"
