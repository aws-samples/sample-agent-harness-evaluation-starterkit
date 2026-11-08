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
The trigger is verified, not trusted: the event must be a CloudWatch alarm state change from this account and Region
(EventBridge sets `source`, `account`, `resources`), the alarm must be in ALLOWED_ALARMS when that is set, and the
alarm's current state is re-read with DescribeAlarms - a forged event or a direct invoke for an alarm that is not
actually in ALARM changes nothing. The release record only claims a bundle rollback when the ship rule was moved.

Env: NAME_PREFIX, AGENT_RUNTIME_ID, ENDPOINT_NAME (PROD), GATEWAY_ID (optional), AB_TEST_ID (optional), DRY_RUN,
     EXPECTED_ACCOUNT (the function's account), ALLOWED_ALARMS (comma-separated alarm names; empty = any <prefix>- alarm).
IAM: bedrock-agentcore:UpdateAgentRuntimeEndpoint on the runtime (+ its runtime-endpoint/*),
bedrock-agentcore:ListGatewayRules/UpdateGatewayRule (if GATEWAY_ID), GetABTest/UpdateABTest (if AB_TEST_ID),
ssm:GetParameter/PutParameter on parameter/<prefix>/release/*, cloudwatch:DescribeAlarms.
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
    return (boto3.client("bedrock-agentcore-control"), boto3.client("bedrock-agentcore"), boto3.client("ssm"),
            boto3.client("cloudwatch"))


def verify_trigger(event: dict, cw) -> str | None:
    """-> None when the event is a genuine ALARM transition of an allowed alarm, else the reason to ignore it."""
    detail = event.get("detail") or {}
    alarm = detail.get("alarmName", "")
    if event.get("source") != "aws.cloudwatch" or event.get("detail-type") != "CloudWatch Alarm State Change":
        return "not a CloudWatch alarm state change event"
    acct, region = os.getenv("EXPECTED_ACCOUNT"), os.getenv("AWS_REGION")
    if acct and event.get("account") != acct:
        return f"event from account {event.get('account')}, expected {acct}"
    arn = (event.get("resources") or [""])[0]
    if acct and region and arn != f"arn:aws:cloudwatch:{region}:{acct}:alarm:{alarm}" and \
            not arn.endswith(f":{region}:{acct}:alarm:{alarm}"):
        return f"alarm ARN {arn} is not {alarm} in {region} / {acct}"
    allowed = {a.strip() for a in os.getenv("ALLOWED_ALARMS", "").split(",") if a.strip()}
    if allowed and alarm not in allowed:
        return f"alarm {alarm} is not in ALLOWED_ALARMS"
    live = (cw.describe_alarms(AlarmNames=[alarm]).get("MetricAlarms") or [{}])[0].get("StateValue") if cw else None
    if cw and live != "ALARM":
        return f"alarm {alarm} is {live or 'missing'} now (DescribeAlarms), not ALARM"
    return None


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
    ctl, dp, ssm, cw = (tuple(clients) + (None,))[:4] if clients else _clients()
    if ALARM.match(alarm):
        why = verify_trigger(event, cw)
        if why:
            print(json.dumps({"event": "rollback-decision", "alarm": alarm, "decision": "ignored", "why": why}))
            return {"alarm": alarm, "decision": "ignored", "why": why}
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
    gw, bundle_moved = os.getenv("GATEWAY_ID"), False
    if gw and prev.get("bundleVersion") and prev.get("bundleVersion") != cur.get("bundleVersion"):
        rule = next((x for x in ctl.list_gateway_rules(gatewayIdentifier=gw).get("gatewayRules", [])
                     if (x.get("description") or "").startswith(f"{PREFIX}-ship")), None)
        if rule:
            ctl.update_gateway_rule(gatewayIdentifier=gw, ruleId=rule["ruleId"], priority=rule["priority"],
                                    description=rule.get("description"),
                                    actions=[{"configurationBundle": {"staticOverride": {
                                        "bundleArn": prev["bundleArn"], "bundleVersion": prev["bundleVersion"]}}}])
            d["bundle"] = {"from": cur.get("bundleVersion"), "to": prev["bundleVersion"]}
            bundle_moved = True
    entry = {"from": d["from"], "to": d["to"], "alarm": alarm, "at": d["at"]}
    new = {**prev, "rollbacks": [*cur.get("rollbacks", []), entry], "rolledBackFrom": cur.get("gate_key")}
    if not bundle_moved and prev.get("bundleVersion") != cur.get("bundleVersion"):
        # the endpoint moved but the ship rule did not: the record must say which bundle PROD still serves
        new.update(bundleArn=cur.get("bundleArn"), bundleVersion=cur.get("bundleVersion"),
                   bundleNotRolledBack="GATEWAY_ID not set or ship rule not found")
        d["bundle_warning"] = new["bundleNotRolledBack"]
    ssm.put_parameter(Name=f"{base}/current", Type="String", Overwrite=True, Value=json.dumps(new))
    ssm.put_parameter(Name=f"{base}/previous", Type="String", Overwrite=True, Value=json.dumps(None))
    return d
