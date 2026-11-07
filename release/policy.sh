#!/usr/bin/env bash
# Step 4 Release - AgentCore Policy on the front-door Gateway: observe first (engine LOG_ONLY), then enforce.
#
# Two different LOG_ONLY switches exist; this script uses the ENGINE one:
#   engine mode  policyEngineConfiguration.mode on the Gateway (UpdateGateway): LOG_ONLY = nothing is enforced,
#                every decision is only logged / counted; ENFORCE (API default) = the engine's decision is applied
#   policy mode  enforcementMode on one policy (CreatePolicy / UpdatePolicy): LOG_ONLY shadows ONE new policy
#                inside an ENFORCE engine; promote with update-policy --enforcement-mode ACTIVE
# Cedar is default-deny: an engine with only forbid rules denies everything once ENFORCE - start with the permit.
#
#   setup    create engine <prefix>_frontdoor + an ACTIVE permit for this gateway + a LOG_ONLY forbid that only
#            matches a header nobody sends (harmless rule to watch LogOnly* metrics); grant the Gateway role the
#            authorize actions (inline policy <prefix>-policy-engine-authorize)
#   observe  attach the engine to the Gateway in LOG_ONLY mode (UpdateGateway, other gateway settings copied)
#   audit    policy metrics of the last HOURS (dimensions PolicyEngine + OperationName [+ Policy])
#   enforce  set the engine mode to ENFORCE (refuses while audit shows denies, unless FORCE=1)
# Metrics (AWS/Bedrock-AgentCore): every policy metric has PolicyEngine + OperationName (AuthorizeAction); per-policy
# ones add Policy and TargetResource, and Mode=LOG_ONLY while the engine observes. Give the exact set or get nothing:
#   aws cloudwatch get-metric-statistics --namespace AWS/Bedrock-AgentCore --metric-name AllowDecisions \
#     --dimensions Name=OperationName,Value=AuthorizeAction Name=PolicyEngine,Value=<engine-id> \
#     --start-time <iso> --end-time <iso> --period 3600 --statistics Sum      # also DenyDecisions, LogOnly*
# The Gateway here is managed by the agentcore project (CDK); UpdateGateway from this script is drift the next
# deploy keeps only while the gateway resource itself is unchanged. In a project, the same flow is
# `agentcore add policy-engine --attach-to-gateways evaldeckFront --attach-mode log-only` + deploy, then mode ENFORCE
# in agentcore.json + deploy.
# IAM (caller): bedrock-agentcore:CreatePolicyEngine/GetPolicyEngine/CreatePolicy/ListPolicies/GetGateway/UpdateGateway,
#      iam:PassRole + iam:PutRolePolicy on the Gateway role, cloudwatch:GetMetricData/ListMetrics.
# Usage: bash release/policy.sh setup|observe|audit|enforce      (HOURS=3 for audit)
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
mkdir -p out

"$PY" - "${1:-}" <<'PY'
import json, os, sys, time
from datetime import UTC, datetime, timedelta
from common import kit
cmd = sys.argv[1]
ctl, gw_id = kit.client("bedrock-agentcore-control"), kit.gateway_id()
gw = ctl.get_gateway(gatewayIdentifier=gw_id)
engine_name = f"{kit.PREFIX}_frontdoor"

def engine():
    for e in ctl.list_policy_engines().get("policyEngines", []):
        if e["name"] == engine_name:
            return e
    return None

def set_mode(mode):
    e = engine() or sys.exit("run setup first")
    keep = ("name", "description", "roleArn", "protocolType", "protocolConfiguration", "authorizerType",
            "authorizerConfiguration", "kmsKeyArn", "customTransformConfiguration", "interceptorConfigurations",
            "exceptionLevel", "wafConfiguration")
    args = {k: gw[k] for k in keep if gw.get(k) not in (None, {}, [])}
    # the Gateway role's grant from `setup` (IAM) takes a while to propagate: UpdateGateway then fails with
    # "Access denied while calling GetPolicyEngine ... with Gateway role" - retry for up to ~3 minutes
    for attempt in range(12):
        try:
            ctl.update_gateway(gatewayIdentifier=gw_id,
                               policyEngineConfiguration={"arn": e["policyEngineArn"], "mode": mode}, **args)
            break
        except ctl.exceptions.ValidationException as err:
            if "Access denied while calling GetPolicyEngine" not in str(err) or attempt == 11:
                raise
            print(f"Gateway role cannot read the engine yet (IAM propagation), retry {attempt + 1}/11 in 15 s",
                  file=sys.stderr)
            time.sleep(15)
    while (g := ctl.get_gateway(gatewayIdentifier=gw_id))["status"] in ("UPDATING", "CREATING"):
        time.sleep(3)
    print(json.dumps({"gateway": g["name"], "status": g["status"], "policyEngineConfiguration": {
        "engine": e["policyEngineId"], "mode": g.get("policyEngineConfiguration", {}).get("mode")}}))

def audit():
    e = engine() or sys.exit("run setup first")
    cw = kit.client("cloudwatch")
    end = datetime.now(UTC); start = end - timedelta(hours=int(os.getenv("HOURS", "3")))
    metrics = [m for m in cw.list_metrics(Namespace="AWS/Bedrock-AgentCore",
                                          Dimensions=[{"Name": "PolicyEngine", "Value": e["policyEngineId"]}])["Metrics"]]
    if not metrics:   # some deployments publish the engine ARN / name as the dimension value
        metrics = [m for m in cw.list_metrics(Namespace="AWS/Bedrock-AgentCore")["Metrics"]
                   if any(d["Name"] == "PolicyEngine" for d in m["Dimensions"])]
    queries = [{"Id": f"m{i}", "Label": m["MetricName"] + " " + ",".join(f"{d['Name']}={d['Value'][-24:]}" for d in m["Dimensions"]),
                "MetricStat": {"Metric": m, "Period": 3600, "Stat": "Sum"}} for i, m in enumerate(metrics[:100])]
    out = []
    if queries:
        for r in cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)["MetricDataResults"]:
            out.append({"metric": r["Label"], "sum": sum(r["Values"])})
    kit.write_json(kit.OUT / "policy-audit.json", out)
    for o in sorted(out, key=lambda x: x["metric"]):
        print(f"{o['sum']:8.0f}  {o['metric']}")
    if not out:
        print("no policy metrics yet (send traffic through the gateway; metrics lag a few minutes)")
    return out

if cmd == "setup":
    e = engine()
    if not e:
        e = ctl.create_policy_engine(name=engine_name, description="evaldeck front-door policy engine (starter-kit)",
                                     tags={"project": kit.PREFIX})
        while (e := ctl.get_policy_engine(policyEngineId=e["policyEngineId"]))["status"] == "CREATING":
            time.sleep(3)
    pid = e["policyEngineId"]
    have = {p["name"] for p in ctl.list_policies(policyEngineId=pid).get("policies", [])}
    arn = gw["gatewayArn"]
    rules = {f"{kit.PREFIX}_permit_frontdoor": ("ACTIVE", f'permit(principal, action, resource == AgentCore::Gateway::"{arn}");'),
             f"{kit.PREFIX}_shadow_forbid_probe": ("LOG_ONLY", f'forbid(principal, action, resource == AgentCore::Gateway::"{arn}") '
                                                   'when { context has "evaldeckPolicyProbe" };')}
    for name, (mode, stmt) in rules.items():
        if name not in have:
            p = ctl.create_policy(policyEngineId=pid, name=name, enforcementMode=mode, validationMode="IGNORE_ALL_FINDINGS",
                                  definition={"cedar": {"statement": stmt}}, description="evaldeck starter-kit (harmless)")
            print(json.dumps({"policy": name, "enforcementMode": p.get("enforcementMode"), "status": p.get("status")}))
    role = gw["roleArn"].rsplit("/", 1)[1]
    kit.client("iam").put_role_policy(RoleName=role, PolicyName=f"{kit.PREFIX}-policy-engine-authorize", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": [
            "bedrock-agentcore:GetPolicyEngine", "bedrock-agentcore:CheckAuthorizePermissions",
            "bedrock-agentcore:AuthorizeAction", "bedrock-agentcore:PartiallyAuthorizeActions"],
            "Resource": [e["policyEngineArn"], arn]}]}))
    print(json.dumps({"engine": pid, "status": e["status"], "gateway_role_policy": f"{kit.PREFIX}-policy-engine-authorize"}))
elif cmd == "observe":
    set_mode("LOG_ONLY")
elif cmd == "audit":
    audit()
elif cmd == "enforce":
    denies = [o for o in audit() if o["metric"].split()[0] in ("Denies", "DenyDecisions", "LogOnlyDecisionFlips") and o["sum"]]
    if denies and os.getenv("FORCE") != "1":
        sys.exit(f"refusing ENFORCE: {denies} (inspect the decisions, fix the policy or FORCE=1)")
    set_mode("ENFORCE")
else:
    sys.exit("usage: release/policy.sh setup|observe|audit|enforce")
PY
