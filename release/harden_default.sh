#!/usr/bin/env bash
# Step 4 Release (production hardening) - deny every direct invoke of the DEFAULT endpoint.
#
# DEFAULT always follows the newest runtime version, i.e. whatever the last `agentcore deploy` built, before any
# gate ran. Once STAGING (candidate under test) and PROD (released) exist, nobody should call DEFAULT: put a Deny
# resource policy on the DEFAULT endpoint ARN (<runtime-arn>/runtime-endpoint/DEFAULT, not the runtime ARN - a
# Deny on the runtime would block STAGING and PROD too). Explicit Deny wins over every Allow, for every principal.
# After this, kit scripts run with AGENT_ENDPOINT=STAGING (default) or PROD; Gateway targets point at PROD /
# STAGING (agentcore.json httpRuntime.runtimeEndpoint). An existing endpoint policy is merged, not replaced.
#
# Prerequisites: STAGING and PROD endpoints READY. IAM: bedrock-agentcore:GetResourcePolicy/PutResourcePolicy on
# the DEFAULT endpoint ARN, ListAgentRuntimeEndpoints. Undo: delete-resource-policy --resource-arn <same ARN>.
# Usage: bash release/harden_default.sh [--check]     (--check: only invoke DEFAULT / STAGING / PROD and report)
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
mkdir -p out

"$PY" - "${1:-apply}" <<'PY'
import json, sys
from botocore.exceptions import ClientError
from common import kit
mode = sys.argv[1]
arn, rid = kit.runtime_arn(), kit.runtime_id()
ctl = kit.client("bedrock-agentcore-control")
ready = {e["name"]: e["status"] for e in ctl.list_agent_runtime_endpoints(agentRuntimeId=rid)["runtimeEndpoints"]}
if mode != "--check":
    if ready.get("STAGING") != "READY" or ready.get("PROD") != "READY":
        raise SystemExit(f"refusing: STAGING and PROD must be READY first (have {ready})")
    default_arn = f"{arn}/runtime-endpoint/DEFAULT"
    deny = {"Sid": "DenyDirectInvokeOfDefault", "Effect": "Deny", "Principal": "*",
            "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeCommand"],
            "Resource": default_arn}
    try:
        cur = ctl.get_resource_policy(resourceArn=default_arn).get("policy")   # no policy yet: key absent
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("ResourceNotFoundException", "ValidationException"):
            raise
        cur = None
    pol = json.loads(cur) if cur else {"Version": "2012-10-17", "Statement": []}
    pol["Statement"] = [s for s in pol["Statement"] if s.get("Sid") != deny["Sid"]] + [deny]
    ctl.put_resource_policy(resourceArn=default_arn, policy=json.dumps(pol))
    print(json.dumps({"put_resource_policy": default_arn.replace(kit.account_id(), "<ACCOUNT_ID>"),
                      "statements": [s.get("Sid") for s in pol["Statement"]]}))
# verify: DEFAULT denied, STAGING / PROD answer
out = {}
for ep in ("DEFAULT", "STAGING", "PROD"):
    try:
        ans = kit.invoke("Where is my order ORD-1003?", kit.new_session_id(f"harden-{ep}"), arn, endpoint=ep)
        out[ep] = "200 " + ans[:60]
    except ClientError as e:
        out[ep] = f"{e.response['Error']['Code']}: {e.response['Error']['Message'][:90]}"
print(json.dumps(out, indent=1))
PY
