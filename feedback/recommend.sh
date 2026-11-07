#!/usr/bin/env bash
# Step 6 Feedback - AgentCore Recommendations: a rewritten system prompt from real traces, optimised for ONE
# reward evaluator (built-in, numerical custom judge, or code evaluator returning a value; multi-dimension
# judges are not accepted). Output is a candidate: diff it against the current prompt, then it goes through the
# same gate (release/gate.py) and, in production, an A/B test - never straight to PROD.
# Note: traces come from CloudWatch Logs by log group ARN (logGroupArns), not by name as in batch evaluation.
#
# Prerequisites: AgentCore CLI 1.0.x; Transaction Search; traces in the window; the reward evaluator registered.
# Usage: HOURS=6 REWARD=evaldeck_ScopeJudge bash feedback/recommend.sh
set -euo pipefail
cd "$(dirname "$0")/.."
REGION="${AWS_REGION:-us-east-1}"
PREFIX="${NAME_PREFIX:-evaldeck}"
PY="${PYTHON:-python3}"
NAME="${PREFIX}_rec_$(date -u +%Y%m%dT%H%M%S)"
CFG="out/${NAME}.config.json"
mkdir -p out

REWARD="${REWARD:-${PREFIX}_ScopeJudge}" HOURS="${HOURS:-6}" "$PY" - "$CFG" <<'PY'
import json, os, re, sys
from datetime import timedelta
from common import kit
arn = kit.runtime_arn()
acct = arn.split(":")[4]
reward = kit.evaluator_ids([os.environ["REWARD"]])[os.environ["REWARD"]]
reward_arn = (f"arn:aws:bedrock-agentcore:::evaluator/{reward}" if reward.startswith("Builtin.")
              else f"arn:aws:bedrock-agentcore:{kit.REGION}:{acct}:evaluator/{reward}")
src = (kit.KIT / "agent/evaldeck/app/ShopAgent/main.py").read_text()
prompt = re.search(r'DEFAULT_SYSTEM_PROMPT = """(.*?)"""', src, re.S).group(1)   # the prompt the agent runs today
end = kit.now_utc()
cfg = {"systemPromptRecommendationConfig": {
    "systemPrompt": {"text": prompt},
    "agentTraces": {"cloudwatchLogs": {
        "logGroupArns": [f"arn:aws:logs:{kit.REGION}:{acct}:log-group:{kit.runtime_log_group(arn)}"],
        "serviceNames": [kit.service_name(arn)],
        "startTime": (end - timedelta(hours=int(os.environ["HOURS"]))).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ")}},
    "evaluationConfig": {"evaluators": [{"evaluatorArn": reward_arn}]}}}
json.dump(cfg, open(sys.argv[1], "w"), indent=2)
PY

ID=$(agentcore eval recommendation start --name "$NAME" --type SYSTEM_PROMPT_RECOMMENDATION \
       --recommendation-config "file://$CFG" --tags project="$PREFIX" --region "$REGION" --json \
     | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("recommendationId") or d["id"])')
echo "started recommendation $ID"
while :; do
  agentcore eval recommendation get --id "$ID" --region "$REGION" --json > "out/${NAME}.json"
  STATUS=$("$PY" -c "import json; print(json.load(open('out/${NAME}.json'))['status'])")
  case "$STATUS" in COMPLETED|FAILED) break ;; esac
  sleep 30
done
"$PY" - "out/${NAME}.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
res = (r.get("recommendationResult") or {}).get("systemPromptRecommendationResult") or {}
print(json.dumps({"status": r["status"], "errorCode": res.get("errorCode"), "errorMessage": res.get("errorMessage")}))
if res.get("recommendedSystemPrompt"):
    print("\n--- explanation\n" + (res.get("explanation") or ""))
    print("\n--- recommended system prompt\n" + res["recommendedSystemPrompt"])
PY
echo "full result: out/${NAME}.json"
