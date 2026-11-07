"""Step 4 Release / 5 Observe - automatic rollback: CloudWatch alarm -> EventBridge -> this Lambda.

Wiring (observe/alarms.yaml creates the alarms; add an EventBridge rule on
  {"source": ["aws.cloudwatch"], "detail-type": ["CloudWatch Alarm State Change"],
   "detail": {"alarmName": [{"prefix": "<prefix>-"}], "state": {"value": ["ALARM"]}}}
with this function as target).

Decision (pure function `decide`, unit-tested in test_rollback_lambda.py):
  - only alarms named <prefix>-<agent>-<kind> with kind in ROLLBACK_KINDS move PROD
    (redline, goal-score); eval-silent / traffic alarms only notify: rolling back without evidence is the
    same mistake as releasing without evidence
  - at most one automatic rollback per release (no flip-flopping); the re-release needs a human
  - a running A/B test is paused instead (all traffic back to control) - cheaper and reversible
  - first release (no previous version) -> record only
Release records (written by release/release.sh, SSM):
  /<prefix>/release/current   {"runtimeVersion": "4", "bundleArn", "bundleVersion", "gate_key", "rollbacks": [...]}
  /<prefix>/release/previous  the release before it (or null)
Action: UpdateAgentRuntimeEndpoint(PROD -> previous.runtimeVersion); if the previous release had a Config bundle
and GATEWAY_ID is set, the front-door ship rule (release/ab_ship.sh) goes back to that bundle version too
(UpdateGatewayRule). Then current := previous (+ the rollback entry), previous := null.
DRY_RUN=true (default) logs the decision without changing anything.

Env: NAME_PREFIX, AGENT_RUNTIME_ID, ENDPOINT_NAME (PROD), GATEWAY_ID (optional), AB_TEST_ID (optional), DRY_RUN.
IAM: bedrock-agentcore:UpdateAgentRuntimeEndpoint on the runtime (+ its runtime-endpoint/*),
bedrock-agentcore:ListGatewayRules/UpdateGatewayRule (if GATEWAY_ID), GetABTest/UpdateABTest (if AB_TEST_ID),
ssm:GetParameter/PutParameter on parameter/<prefix>/release/*.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime

PREFIX = os.getenv("NAME_PREFIX", "evaldeck")
ROLLBACK_KINDS = {"redline", "goal-score"}
ALARM = re.compile(rf"^{re.escape(PREFIX)}-([a-z0-9]+)-([a-z-]+)$")


def decide(alarm_name: str, release: dict | None, ab_running: bool = False) -> dict:
    m = ALARM.match(alarm_name or "")
    if not m:
        return {"alarm": alarm_name, "decision": "ignored", "why": "not a starter-kit alarm"}
    agent, kind = m.groups()
    if kind not in ROLLBACK_KINDS:
        return {"agent": agent, "kind": kind, "decision": "notify-only"}
    rel = release or {}
    if any(r.get("from") == rel.get("current") for r in rel.get("rollbacks", [])):
        return {"agent": agent, "kind": kind, "decision": "already-rolled-back"}
    if ab_running:
        return {"agent": agent, "kind": kind, "decision": "pause-ab-test"}
    if not rel.get("previous"):
        return {"agent": agent, "kind": kind, "decision": "no-previous-release"}
    return {"agent": agent, "kind": kind, "decision": "rollback", "from": rel.get("current"), "to": rel["previous"]}


def _clients():
    import boto3
    return boto3.client("bedrock-agentcore-control"), boto3.client("bedrock-agentcore"), boto3.client("ssm")


def _get(ssm, name):
    try:
        return json.loads(ssm.get_parameter(Name=name)["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        return None


def handler(event, context, clients=None):
    detail = event.get("detail") or {}
    alarm = detail.get("alarmName", "")
    if (detail.get("state") or {}).get("value") != "ALARM":
        return {"alarm": alarm, "decision": "ignored", "why": "state is not ALARM"}
    ctl, dp, ssm = clients or _clients()
    base = f"/{PREFIX}/release"
    cur, prev = (_get(ssm, f"{base}/current"), _get(ssm, f"{base}/previous")) if ALARM.match(alarm) else (None, None)
    release = cur and {"current": cur.get("runtimeVersion"), "previous": (prev or {}).get("runtimeVersion"),
                       "rollbacks": cur.get("rollbacks", [])}
    ab_id, ab_running = os.getenv("AB_TEST_ID"), False
    if ab_id:
        ab_running = dp.get_ab_test(abTestId=ab_id).get("executionStatus") == "RUNNING"
    d = decide(alarm, release, ab_running)
    d["dry_run"] = os.getenv("DRY_RUN", "true").lower() != "false"
    d["at"] = datetime.now(UTC).isoformat()
    print(json.dumps({"event": "rollback-decision", **d}))
    if d["dry_run"] or d["decision"] not in ("rollback", "pause-ab-test"):
        return d
    if d["decision"] == "pause-ab-test":
        dp.update_ab_test(abTestId=ab_id, executionStatus="PAUSED")
        return d
    r = ctl.update_agent_runtime_endpoint(agentRuntimeId=os.environ["AGENT_RUNTIME_ID"],
                                          endpointName=os.getenv("ENDPOINT_NAME", "PROD"),
                                          agentRuntimeVersion=str(d["to"]))
    d["endpoint"] = {"liveVersion": r.get("liveVersion"), "targetVersion": r.get("targetVersion")}
    gw = os.getenv("GATEWAY_ID")
    if gw and prev.get("bundleVersion") and prev.get("bundleVersion") != cur.get("bundleVersion"):
        rule = next((x for x in ctl.list_gateway_rules(gatewayIdentifier=gw).get("gatewayRules", [])
                     if (x.get("description") or "").startswith(f"{PREFIX}-ship")), None)
        if rule:
            ctl.update_gateway_rule(gatewayIdentifier=gw, ruleId=rule["ruleId"], priority=rule["priority"],
                                    description=rule.get("description"),
                                    actions=[{"configurationBundle": {"staticOverride": {
                                        "bundleArn": prev["bundleArn"], "bundleVersion": prev["bundleVersion"]}}}])
            d["bundle"] = {"from": cur.get("bundleVersion"), "to": prev["bundleVersion"]}
    entry = {"from": d["from"], "to": d["to"], "alarm": alarm, "at": d["at"]}
    ssm.put_parameter(Name=f"{base}/current", Type="String", Overwrite=True, Value=json.dumps(
        {**prev, "rollbacks": [*cur.get("rollbacks", []), entry], "rolledBackFrom": cur.get("gate_key")}))
    ssm.put_parameter(Name=f"{base}/previous", Type="String", Overwrite=True, Value=json.dumps(None))
    return d
