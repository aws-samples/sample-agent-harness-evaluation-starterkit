#!/usr/bin/env bash
# Step 4 Release - ship (or not) after an A/B test, and pin the Config bundle production traffic gets.
#
# A/B testing has no "promote" API: `agentcore eval ab-test stop` returns all traffic to control. If T1 won, a
# Gateway rule with configurationBundle.staticOverride pins the winning bundle version for every request through
# the front door (rule = catch-all, priority SHIP_PRIORITY, one per gateway, found by its description). The same
# rule is what release.sh moves to the gated bundle and what config rollback moves back (update-gateway-rule).
# Without any rule the agent falls back to the prompt in its code.
# Rules resolve each action type independently (first match by ascending priority): a canary routeToTarget rule
# (canary_rule.yaml, priority 200) and this bundle rule (100) apply together.
#
#   decide   winner from GetABTest: T1 only if it is significantly better on PRIMARY_EVALUATOR, no evaluator is
#            significantly worse, and each arm has >= MIN_SAMPLES; otherwise C (control kept)
#   ship     requires executionStatus STOPPED; C -> nothing to do; T1 (or VARIANT=T1) -> pin its bundle
#   pin <bundle-arn> <version>   create or update the ship rule (state before -> out/gateway-rule-before.json)
#   restore  pin the bundle of the current release record (/<prefix>/release/current) again
#   show     list the gateway rules
# Env: AB_TEST, GATEWAY_ID (else agentcore status / ListGateways), SHIP_PRIORITY (100), PRIMARY_EVALUATOR
#      (Builtin.GoalSuccessRate), MIN_SAMPLES (30), VARIANT (override the decision - drills only).
# IAM: bedrock-agentcore:GetABTest, Create/Update/ListGatewayRules on the gateway, ssm:GetParameter (restore).
set -euo pipefail
cd "$(dirname "$0")/.."
REGION="${AWS_REGION:-us-east-1}"
PREFIX="${NAME_PREFIX:-evaldeck}"
PY="${PYTHON:-python3}"
PRIO="${SHIP_PRIORITY:-100}"
TAG="${PREFIX}-ship"
mkdir -p out
GW="${GATEWAY_ID:-$("$PY" -c 'from common import kit; print(kit.gateway_id())')}"

ship_rule_id() {
  aws bedrock-agentcore-control list-gateway-rules --gateway-identifier "$GW" --region "$REGION" \
    --query "gatewayRules[?starts_with(description, '${TAG}')].ruleId | [0]" --output text 2>/dev/null | grep -v None || true
}

pin() {   # pin <bundle-arn> <version>
  local arn="$1" ver="$2" rule actions
  actions="[{\"configurationBundle\":{\"staticOverride\":{\"bundleArn\":\"$arn\",\"bundleVersion\":\"$ver\"}}}]"
  aws bedrock-agentcore-control list-gateway-rules --gateway-identifier "$GW" --region "$REGION" --output json \
    > out/gateway-rule-before.json
  rule=$(ship_rule_id)
  if [ -z "$rule" ]; then
    aws bedrock-agentcore-control create-gateway-rule --gateway-identifier "$GW" --priority "$PRIO" \
      --actions "$actions" --description "${TAG}: production Config bundle (catch-all)" --region "$REGION" \
      --query '{ruleId:ruleId,priority:priority,status:status}' --output json
  else
    aws bedrock-agentcore-control update-gateway-rule --gateway-identifier "$GW" --rule-id "$rule" --priority "$PRIO" \
      --actions "$actions" --description "${TAG}: production Config bundle (catch-all)" --region "$REGION" \
      --query '{ruleId:ruleId,priority:priority,status:status}' --output json
  fi
}

decide() {
  agentcore eval ab-test get --id "${AB_TEST:?set AB_TEST}" --region "$REGION" --json > out/ab_results.json
  "$PY" - <<'PY'
import json, os
r = json.load(open("out/ab_results.json"))
primary, min_n = os.getenv("PRIMARY_EVALUATOR", "Builtin.GoalSuccessRate"), int(os.getenv("MIN_SAMPLES", "30"))
lower_is_better = {"Builtin.FailureAnalysis"}
why, better, worse, small = [], False, [], []
for m in (r.get("results") or {}).get("evaluatorMetrics", []):
    name = m["evaluatorArn"].rsplit("/", 1)[1]
    c = m["controlStats"]
    for v in m["variantResults"]:
        sign = -1 if name.split("-")[0] in lower_is_better else 1
        delta = sign * (v.get("absoluteChange") or 0)
        if min(c.get("sampleSize") or 0, v.get("sampleSize") or 0) < min_n:
            small.append(name)
        if v.get("isSignificant") and delta < 0:
            worse.append(name)
        if name.split("-")[0] == primary and v.get("isSignificant") and delta > 0:
            better = True
if small: why.append(f"arms below MIN_SAMPLES={min_n}: {sorted(set(small))}")
if worse: why.append(f"T1 significantly worse on {worse}")
if not better: why.append(f"T1 not significantly better on {primary}")
winner = "T1" if better and not worse and not small else "C"
variants = {v["name"]: v["variantConfiguration"].get("configurationBundle") for v in r.get("variants", [])}
json.dump({"winner": winner, "why": why, "executionStatus": r.get("executionStatus"), "variants": variants},
          open("out/ab_decision.json", "w"), indent=2)
print(json.dumps({"winner": winner, "why": why, "executionStatus": r.get("executionStatus")}))
PY
}

case "${1:-}" in
  decide) decide ;;
  ship)
    decide
    read -r WIN STATUS ARN VER < <("$PY" -c "
import json, os
d = json.load(open('out/ab_decision.json')); w = os.getenv('VARIANT') or d['winner']
b = d['variants'].get(w) or {}
print(w, d['executionStatus'], b.get('bundleArn', '-'), b.get('bundleVersion', '-'))")
    [ "$STATUS" = STOPPED ] || { echo "A/B test is $STATUS: run 'AB_TEST=$AB_TEST bash release/ab_test.sh stop' first" >&2; exit 1; }
    if [ "$WIN" = C ]; then echo "control kept: nothing to ship (traffic is on control since stop)"; exit 0; fi
    [ -n "${VARIANT:-}" ] && echo "VARIANT=$VARIANT overrides the decision (drill)"
    pin "$ARN" "$VER"
    ;;
  pin) pin "${2:?bundle arn}" "${3:?bundle version}" ;;
  restore)
    read -r ARN VER < <(aws ssm get-parameter --name "/${PREFIX}/release/current" --region "$REGION" \
      --query Parameter.Value --output text | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("bundleArn") or "-", d.get("bundleVersion") or "-")')
    if [ "$ARN" = "-" ]; then
      echo "current release has no bundle: delete the ship rule to fall back to the code prompt:"
      echo "  aws bedrock-agentcore-control delete-gateway-rule --gateway-identifier $GW --rule-id $(ship_rule_id)"
      exit 0
    fi
    pin "$ARN" "$VER"
    ;;
  show)
    aws bedrock-agentcore-control list-gateway-rules --gateway-identifier "$GW" --region "$REGION" \
      --query 'gatewayRules[].{priority:priority,description:description,status:status,actions:actions}' --output json ;;
  *) echo "usage: $0 decide|ship|pin <arn> <ver>|restore|show" >&2; exit 2 ;;
esac
