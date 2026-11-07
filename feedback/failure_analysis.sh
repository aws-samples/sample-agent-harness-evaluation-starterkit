#!/usr/bin/env bash
# Step 6 Feedback (also 5 Observe audits) - root-cause clustering with Builtin.FailureAnalysis.
# FailureAnalysis runs only inside a Batch evaluation; its score is 1.0 = failures found (higher = worse), and
# the job's insightResults group failures -> rootCauses (explanation, suggestedFix) -> sample sessions ->
# findings pinned to span ids. Treat it as a lead, not a verdict: review fixes against the current prompt, and
# never use insight names as stable ids across runs (they are regenerated each time).
# There is no schedule: run it from cron / EventBridge Scheduler / CodeBuild if you want it daily.
#
# Prerequisites: AgentCore CLI 1.0.x; caller credentials need StartBatchEvaluation/GetBatchEvaluation and
# logs read on the runtime log group (no service role is used for batch).
# Usage:
#   SESSION_IDS="sid1 sid2" bash feedback/failure_analysis.sh          # specific sessions (e.g. mined bad cases)
#   HOURS=24 bash feedback/failure_analysis.sh                         # every session of the last N hours (<=500)
#   ONLINE_EVAL=<config-id> HOURS=24 bash feedback/failure_analysis.sh # sessions an online config sampled
set -euo pipefail
cd "$(dirname "$0")/.."
REGION="${AWS_REGION:-us-east-1}"
PREFIX="${NAME_PREFIX:-evaldeck}"
PY="${PYTHON:-python3}"
NAME="${PREFIX}_fa_$(date -u +%Y%m%dT%H%M%S)"
RUNTIME_ID=$("$PY" -c 'from common import kit; print(kit.runtime_id())')
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
START=$("$PY" -c "from datetime import datetime, timedelta, UTC; print((datetime.now(UTC)-timedelta(hours=${HOURS:-6})).strftime('%Y-%m-%dT%H:%M:%SZ'))")

if [ -n "${ONLINE_EVAL:-}" ]; then
  SRC=(--online-eval "$ONLINE_EVAL" --start-time "$START" --end-time "$END")
elif [ -n "${SESSION_IDS:-}" ]; then
  # shellcheck disable=SC2206
  SRC=(--agent "$RUNTIME_ID" --endpoint "${ENDPOINT:-PROD}" --session-ids $SESSION_IDS)
else
  SRC=(--agent "$RUNTIME_ID" --endpoint "${ENDPOINT:-PROD}" --start-time "$START" --end-time "$END")
fi
JOB=$(agentcore eval batch-evaluation evaluate --name "$NAME" "${SRC[@]}" \
        --evaluators Builtin.FailureAnalysis Builtin.GoalSuccessRate --region "$REGION" --json \
      | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["batchEvaluationId"])')
echo "started $JOB"
while :; do
  agentcore eval batch-evaluation get --id "$JOB" --region "$REGION" --json > "out/${NAME}.json"
  STATUS=$("$PY" -c "import json; print(json.load(open('out/${NAME}.json'))['status'])")
  case "$STATUS" in COMPLETED|COMPLETED_WITH_ERRORS|FAILED|STOPPED) break ;; esac
  sleep 30
done
"$PY" - "out/${NAME}.json" <<'PY'
import json, sys
job = json.load(open(sys.argv[1]))
res = job.get("evaluationResults") or {}
print(json.dumps({"status": job["status"], "sessions": res.get("totalNumberOfSessions"),
                  "summaries": [{"evaluator": s.get("evaluatorId"), "avg": (s.get("statistics") or {}).get("averageScore"),
                                 "evaluated": s.get("totalEvaluated")} for s in res.get("evaluatorSummaries", [])]}))
for ins in job.get("insightResults") or []:
    print(f"\n[{ins.get('type')}] {ins.get('name')} - sessions affected: {ins.get('affectedSessionCount')}\n  {ins.get('description')}")
    for rc in ins.get("rootCauses") or []:
        print(f"  root cause: {rc.get('name')}\n    why: {rc.get('explanation')}\n    fix: {rc.get('suggestedFix')}")
        for s in (rc.get("sampleSessions") or [])[:2]:
            for f in (s.get("findings") or [])[:2]:
                print(f"    - {s.get('sessionId')} {f.get('targetType')}={f.get('targetId')} [{f.get('category')}] {f.get('description')}")
PY
echo "full job: out/${NAME}.json"
