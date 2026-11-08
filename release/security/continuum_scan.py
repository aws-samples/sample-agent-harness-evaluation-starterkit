#!/usr/bin/env python3
"""Step 4 Release - AWS Continuum for code vulnerabilities on this repository (Amazon S3 code source).

Amazon Inspector (release/ci/security_scan.sh) blocks known-vulnerable dependencies in CI. AWS Continuum looks at the
code itself: it scans, validates which findings are exploitable in a sandbox, promotes confirmed findings to issues
with severity and evidence, and writes patches. It runs outside the PR gate (minutes to hours, billed per inference
unit): run it on a schedule or before a release, and review its patches like any other change.

  setup    deploy release/security/continuum.yaml (code bucket + integration role, external ID), create the S3
           integration and the environment (manual scans only, patches enabled). Creating the environment starts the
           first scan, so `setup` uploads the code first.
  upload   git archive HEAD -> s3://<bucket>/code/<prefix>-kit.zip; README / requirements.yaml / agent README ->
           context/ (what the service is and what must never happen, for prioritization)
  scan     CreateCodeScan (one manual scan) and wait for it; prints the scan status
  results  findings (status, discard reason), issues (severity, confidence, phase), PATCH artifacts of each issue
           -> out/continuum/<issue-id>.patch, summary -> out/continuum/report.json. Exit 1 when a CONFIRMED issue is at
           or above --fail-on (default: report only).
Cost: a monthly context fee by protected lines of code plus inference units for the security work (see the AWS
Continuum pricing page). The environment has no schedule; delete it when you stop using it.

Prerequisites: boto3 with the `continuum` client, AWS CLI v2 (CloudFormation deploy), git.
IAM (caller): continuum:CreateIntegration/GetIntegration/ListIntegrations/CreateEnvironment/GetEnvironment/
ListEnvironments/CreateCodeScan/GetCodeScan/ListCodeScans/ListCodeFindings/ListCodeIssues/GetCodeIssue/
ListCodeIssueArtifacts/GetCodeIssueArtifact, iam:PassRole on the integration role (iam:PassedToService =
continuum.amazonaws.com), s3:PutObject on the code bucket, CloudFormation deploy of the stack.
Usage:
  python release/security/continuum_scan.py setup
  python release/security/continuum_scan.py upload && python release/security/continuum_scan.py scan
  python release/security/continuum_scan.py results [--fail-on HIGH]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import kit  # noqa: E402

STACK = f"{kit.PREFIX}-continuum"
NAME = f"{kit.PREFIX}-kit"
OUT = kit.OUT / "continuum"
CONTEXT_FILES = ["README.md", "define/requirements.yaml", "agent/README.md"]
ALLOWED_DOMAINS = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]   # to build the code in the sandbox
SEVERITY = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


def ctm():
    return kit.client("continuum")


def stack_outputs() -> dict | None:
    cfn = kit.client("cloudformation")
    try:
        s = cfn.describe_stacks(StackName=STACK)["Stacks"][0]
    except cfn.exceptions.ClientError:
        return None
    out = {o["OutputKey"]: o["OutputValue"] for o in s.get("Outputs", [])}
    out["ExternalId"] = next(p["ParameterValue"] for p in s["Parameters"] if p["ParameterKey"] == "ExternalId")
    return out


def deploy_stack() -> dict:
    cur = stack_outputs()
    ext = cur["ExternalId"] if cur else f"{kit.PREFIX}-{uuid.uuid4().hex}"
    subprocess.run(["aws", "cloudformation", "deploy", "--stack-name", STACK, "--region", kit.REGION,
                    "--template-file", str(kit.KIT / "release/security/continuum.yaml"), "--capabilities", "CAPABILITY_IAM",
                    "--no-fail-on-empty-changeset", "--tags", f"project={kit.PREFIX}",
                    "--parameter-overrides", f"NamePrefix={kit.PREFIX}", f"ExternalId={ext}"], check=True)
    return stack_outputs()


def find(items_call, key: str, name: str):
    token = None
    while True:
        r = items_call(**({"nextToken": token} if token else {}))
        for it in r.get("items", []):
            if it.get("name") == name:
                return it[key]
        token = r.get("nextToken")
        if not token:
            return None


def upload(st: dict) -> None:
    s3 = kit.client("s3")
    zip_path = kit.OUT / f"{NAME}.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "archive", "--format=zip", "-o", str(zip_path), "HEAD"], cwd=kit.KIT, check=True)
    s3.upload_file(str(zip_path), st["CodeBucket"], f"code/{NAME}.zip")
    for f in CONTEXT_FILES:
        s3.upload_file(str(kit.KIT / f), st["CodeBucket"], "context/" + f.replace("/", "__"))
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=kit.KIT, capture_output=True, text=True).stdout.strip()
    print(json.dumps({"code": f"s3://{st['CodeBucket']}/code/{NAME}.zip", "commit": head or "nogit",
                      "context": [f"context/{f.replace('/', '__')}" for f in CONTEXT_FILES]}))


def setup() -> dict:
    st = deploy_stack()
    upload(st)
    integ = find(ctm().list_integrations, "integrationId", NAME)
    if not integ:
        for attempt in range(8):        # the new role needs a moment before Continuum can assume it
            try:
                integ = ctm().create_integration(name=NAME, providerConfiguration={"s3": {
                    "bucket": st["CodeBucket"], "roleArn": st["IntegrationRoleArn"], "externalId": st["ExternalId"]}},
                    clientToken=f"{NAME}-{st['ExternalId'][-12:]}-{attempt}")["integrationId"]
                break
            except ctm().exceptions.ValidationException as e:
                if attempt == 7 or "assume" not in str(e).lower() and "access" not in str(e).lower():
                    raise
                print(f"integration role not usable yet ({e}); retry in 15 s", file=sys.stderr)
                time.sleep(15)
    env = find(ctm().list_environments, "environmentId", NAME)
    if not env:
        env = ctm().create_environment(name=NAME, configuration={"codeConfiguration": {
            "sourceConfiguration": {"s3Sources": [{"integrationId": integ, "codePrefix": "code/", "contextPrefix": "context/"}]},
            "environmentConfiguration": {"patchCreationPolicy": "ENABLED", "network": {"allowedDomains": ALLOWED_DOMAINS}}}}
        )["environmentId"]                                   # no scanFrequency: manual scans only
    e = ctm().get_environment(environmentId=env)
    info = {"integrationId": integ, "environmentId": env, "status": e.get("status"),
            "codeConfiguration": (e.get("configuration") or {}).get("codeConfiguration")}
    print(json.dumps(info, default=str, indent=2))
    return info


def env_id() -> str:
    env = find(ctm().list_environments, "environmentId", NAME)
    if not env:
        raise SystemExit(f"no Continuum environment {NAME}: run `setup` first")
    return env


def latest_scan(env: str) -> dict | None:
    scans = ctm().list_code_scans(environmentId=env).get("scans", [])
    return max(scans, key=lambda s: str(s["createdAt"])) if scans else None


def scan(wait_only: bool = False, timeout_min: int = 120) -> dict:
    env = env_id()
    cur = latest_scan(env)
    if not wait_only and not (cur and cur["status"] not in ("SUCCEEDED", "FAILED")):
        cur = ctm().create_code_scan(environmentId=env)["scan"]        # one scan in progress at a time
    if not cur:
        raise SystemExit("no scan to wait for")
    sid, last = cur["scanRunId"], None
    for _ in range(timeout_min * 2):
        s = ctm().get_code_scan(environmentId=env, scanRunId=sid)["scan"]
        if s["status"] != last:
            print(f"{time.strftime('%H:%M:%S')} scan {sid} {s['status']}", flush=True)
            last = s["status"]
        if s["status"] in ("SUCCEEDED", "FAILED"):
            print(json.dumps(s, default=str))
            return s
        time.sleep(30)
    raise SystemExit(f"scan {sid} still {last} after {timeout_min} min")


def paged(call, **kw) -> list[dict]:
    items, token = [], None
    while True:
        r = call(**kw, **({"nextToken": token} if token else {}))
        items += r.get("items", [])
        token = r.get("nextToken")
        if not token:
            return items


def results(fail_on: str | None) -> int:
    env = env_id()
    OUT.mkdir(parents=True, exist_ok=True)
    findings = paged(ctm().list_code_findings, environmentId=env)
    issues = paged(ctm().list_code_issues, environmentId=env)
    rows = []
    for i in issues:
        d = ctm().get_code_issue(environmentId=env, issueId=i["issueId"])
        patches = []
        for a in paged(ctm().list_code_issue_artifacts, environmentId=env, issueId=i["issueId"], artifactType="PATCH"):
            body = ctm().get_code_issue_artifact(environmentId=env, issueId=i["issueId"], artifactId=a["artifactId"])["body"]
            patch = (body.get("patch") or {}).get("patch") or body.get("raw") or ""
            path = OUT / f"{i['issueId']}-{a['artifactId']}.patch"
            path.write_text(patch)
            patches.append(str(path.relative_to(kit.KIT)) if path.is_relative_to(kit.KIT) else str(path))
        rows.append({"issueId": i["issueId"], "name": i["name"], "severity": i.get("severity"), "confidence": i.get("confidence"),
                     "status": i["status"], "phase": i.get("phase"), "summary": d.get("summary"),
                     "severityReasoning": d.get("severityReasoning"), "evidence": d.get("evidenceAvailability"),
                     "patchDelivery": d.get("patchDelivery"), "patches": patches})
    by_status: dict[str, int] = {}
    for f in findings:
        k = f["status"] + (f":{f['discardReason']}" if f.get("discardReason") else "")
        by_status[k] = by_status.get(k, 0) + 1
    blocking = [r for r in rows if fail_on and r["severity"] in SEVERITY
                and SEVERITY.index(r["severity"]) >= SEVERITY.index(fail_on)]
    report = {"environmentId": env, "scan": latest_scan(env), "findings": len(findings), "findings_by_status": by_status,
              "issues": rows, "fail_on": fail_on, "decision": "BLOCKED" if blocking else "PASS"}
    (OUT / "report.json").write_text(json.dumps(report, default=str, indent=2))
    print(json.dumps({k: report[k] for k in ("findings", "findings_by_status", "fail_on", "decision")}, default=str))
    for f in findings[:30]:
        print(f"  finding {f['severity']:8s} {f['status']:10s} {f.get('discardReason') or '':38s} {f['title'][:70]}"
              f"  {f.get('filePath') or ''}")
    for r in rows:
        print(f"  issue   {r['severity'] or '-':8s} conf={r['confidence']} {r['status']}/{r['phase']}  {r['name'][:70]}"
              f"  patches={len(r['patches'])}")
    print(f"report: {OUT / 'report.json'}")
    return 1 if blocking else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["setup", "upload", "scan", "wait", "results"])
    ap.add_argument("--fail-on", choices=SEVERITY)
    ap.add_argument("--timeout-min", type=int, default=120)
    a = ap.parse_args()
    if a.cmd == "setup":
        setup()
    elif a.cmd == "upload":
        st = stack_outputs() or sys.exit("run `setup` first")
        upload(st)
    elif a.cmd in ("scan", "wait"):
        s = scan(wait_only=a.cmd == "wait", timeout_min=a.timeout_min)
        return 0 if s["status"] == "SUCCEEDED" else 1
    else:
        return results(a.fail_on)
    return 0


if __name__ == "__main__":
    sys.exit(main())
