#!/usr/bin/env bash
# Step 4 Release - online A/B test of two system prompts via AgentCore Gateway + Configuration bundles.
#
#   control   = the prompt running today (DEFAULT_SYSTEM_PROMPT in agent/evaldeck/app/ShopAgent/main.py)
#   treatment = a candidate prompt (e.g. the Recommendations output, after it passed release/gate.py offline)
# The Gateway assigns each runtime session to C or T1 (sticky by X-Amzn-Bedrock-AgentCore-Runtime-Session-Id),
# injects the bundle reference as W3C baggage, and the shared online evaluation config scores both arms.
# Results: per evaluator control/treatment mean, sampleSize, absoluteChange, percentChange, pValue,
# confidenceInterval (95 %, absolute change), isSignificant. Decide on ALL evaluators (red line must not move).
# Ship the winner: `agentcore eval ab-test stop` (irreversible; traffic returns to control), then a Gateway rule
# that pins the winning bundle version (configurationBundle.staticOverride). Control wins -> stop is enough.
#
# Prerequisites: AgentCore CLI 1.0.x; the project's Gateway `evaldeckFront` with target `shop` (httpRuntime ->
# ShopAgent) deployed; online evaluation config (observe/online_eval.sh) on the runtime, sampling 100 % for the test.
# Usage:
#   TREATMENT_PROMPT_FILE=out/treatment_prompt.txt ONLINE_EVAL=<config-id> SESSIONS=20 bash release/ab_test.sh start
#   AB_TEST=<ab-test-id> bash release/ab_test.sh results
#   AB_TEST=<ab-test-id> bash release/ab_test.sh stop
set -euo pipefail
cd "$(dirname "$0")/.."
REGION="${AWS_REGION:-us-east-1}"
PREFIX="${NAME_PREFIX:-evaldeck}"
PY="${PYTHON:-python3}"
PROJECT=agent/evaldeck
GATEWAY_NAME="${GATEWAY_NAME:-evaldeckFront}"
TARGET="${GATEWAY_TARGET:-shop}"
mkdir -p out

gateway_id() {
  (cd "$PROJECT" && agentcore status --json) | "$PY" -c "
import json, sys
for r in json.load(sys.stdin)['resources']:
    if r['resourceType'] == 'gateway' and r['name'] == '$GATEWAY_NAME':
        print(r['arn'].rsplit('/', 1)[1])"
}

bundle() {   # bundle <name> <prompt-file> -> prints "<bundleId> <versionId>" (bundle names are unique: delete or rename to re-run)
  local name="$1" file="$2" comps
  comps=$("$PY" -c "
import json, sys
from common import kit
print(json.dumps({kit.runtime_arn(): {'configuration': {'system_prompt': open('$file').read()}}}))")
  agentcore eval config-bundle create --name "$name" --components "$comps" \
      --commit-message "starter-kit A/B arm $name" --region "$REGION" --json \
    | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d["bundleId"], d["versionId"])'
}

case "${1:-}" in
  start)
    : "${ONLINE_EVAL:?set ONLINE_EVAL=<online evaluation config id>}"
    : "${TREATMENT_PROMPT_FILE:?set TREATMENT_PROMPT_FILE=<file with the candidate system prompt>}"
    "$PY" -c "
import re
src = open('$PROJECT/app/ShopAgent/main.py').read()
open('out/control_prompt.txt', 'w').write(re.search(r'DEFAULT_SYSTEM_PROMPT = \"\"\"(.*?)\"\"\"', src, re.S).group(1))"
    read -r C_ID C_VER < <(bundle "${PREFIX}_ShopAgent_control" out/control_prompt.txt)
    read -r T_ID T_VER < <(bundle "${PREFIX}_ShopAgent_treatment" "$TREATMENT_PROMPT_FILE")
    GW=$(gateway_id)
    agentcore eval ab-test config-based run --name "${PREFIX}_prompt_ab_$(date -u +%Y%m%dT%H%M%S)" --gateway "$GW" \
      --control "{\"config-bundle\":\"$C_ID\",\"bundle-version\":\"$C_VER\"}" \
      --treatment "{\"config-bundle\":\"$T_ID\",\"bundle-version\":\"$T_VER\"}" \
      --online-eval "$ONLINE_EVAL" --treatment-weight "${TREATMENT_WEIGHT:-50}" \
      --gateway-filter "{\"targetPaths\":[\"/${TARGET}/*\"]}" --region "$REGION" --json | tee out/ab_test.json
    # traffic through the Gateway: one new session per request, prompts from the acceptance dataset
    "$PY" -c "
import json
rows = [json.loads(l) for l in open('define/datasets/acceptance.jsonl') if l.strip()]
for i in range(int('${SESSIONS:-20}')):
    print(json.dumps({'prompt': rows[i % len(rows)]['turns'][0]['input']}))" > out/ab_payloads.jsonl
    while read -r payload; do
      (cd "$PROJECT" && agentcore invoke --gateway "$GATEWAY_NAME" --path "/${TARGET}/invocations" \
         --session-id "${PREFIX}-ab-$(uuidgen)-$(uuidgen)" --payload "$payload" >/dev/null) || echo "invoke failed"
    done < out/ab_payloads.jsonl
    echo "sent ${SESSIONS:-20} sessions; results appear ~session timeout + 5-15 min later"
    ;;
  results)
    : "${AB_TEST:?set AB_TEST=<ab-test-id>}"
    agentcore eval ab-test get --id "$AB_TEST" --region "$REGION" --json | tee out/ab_results.json | "$PY" -c "
import json, sys
r = json.load(sys.stdin)
print(json.dumps({k: r.get(k) for k in ('name', 'status', 'executionStatus')}))
for m in (r.get('results') or {}).get('evaluatorMetrics', []):
    c = m['controlStats']
    for v in m['variantResults']:
        print(f\"{m['evaluatorArn'].rsplit('/', 1)[1]:30s} C={c.get('mean')} (n={c.get('sampleSize')}) \"
              f\"{v['variantName']}={v.get('mean')} (n={v.get('sampleSize')}) delta={v.get('absoluteChange')} \"
              f\"p={v.get('pValue')} CI={v.get('confidenceInterval')} significant={v.get('isSignificant')}\")"
    ;;
  stop)
    : "${AB_TEST:?set AB_TEST=<ab-test-id>}"
    agentcore eval ab-test stop --id "$AB_TEST" --region "$REGION" --json
    echo "stopped; traffic is back on control. If T1 won, pin it with a Gateway rule (configurationBundle.staticOverride)."
    ;;
  *) echo "usage: $0 start|results|stop" >&2; exit 2 ;;
esac
