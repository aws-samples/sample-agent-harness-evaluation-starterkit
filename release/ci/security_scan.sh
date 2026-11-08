#!/usr/bin/env bash
# Step 4 Release - CI security scan before the gate: Amazon Inspector SBOM Generator + Amazon Inspector Scan API (SCA).
# Both CI systems run it first in the deploy step (buildspec-deploy.yml, github-actions.yml), so a vulnerable
# dependency stops the release before anything is deployed or evaluated.
#   1. inspector-sbomgen (Linux only; downloaded, checksum-verified) inventories the repository: requirements.txt,
#      agent/evaldeck/app/ShopAgent/uv.lock (the runtime's packages), agent/evaldeck/agentcore/cdk/package-lock.json
#   2. inspector-scan ScanSbom returns the CycloneDX SBOM with vulnerabilities  -> out/inspector-scan.json
#   3. summary by severity -> out/security-report.json; exit 1 when a finding is at or above SECURITY_FAIL_ON
# No Inspector activation is needed for ScanSbom (billed per call, see the Inspector pricing page).
# Code-level findings (exploitability, patches) come from AWS Continuum: release/security/continuum_scan.sh.
# Env: AWS_REGION, SECURITY_FAIL_ON (CRITICAL | HIGH (default) | MEDIUM | LOW | NONE = report only),
#      SBOMGEN (path to an installed inspector-sbomgen; else the latest is downloaded), SECURITY_ALLOW (comma-separated
#      vulnerability ids accepted for now, e.g. CVE-2026-1234; reported, not failed).
# IAM: inspector-scan:ScanSbom.
# Usage: bash release/ci/security_scan.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
REGION="${AWS_REGION:-us-east-1}"
PY="${PYTHON:-python3}"
mkdir -p out

SBOMGEN="${SBOMGEN:-}"
if [ -z "$SBOMGEN" ]; then
  [ "$(uname -s)" = Linux ] || { echo "inspector-sbomgen runs on Linux only: run this step in CI (CodeBuild, GitHub Actions) or a Linux container, or set SBOMGEN" >&2; exit 2; }
  case "$(uname -m)" in x86_64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; *) echo "unsupported arch $(uname -m)" >&2; exit 2 ;; esac
  TMP=$(mktemp -d)
  curl -sSfL -o "$TMP/sbomgen.zip" "https://amazon-inspector-sbomgen.s3.amazonaws.com/latest/linux/${ARCH}/inspector-sbomgen.zip"
  unzip -q -o "$TMP/sbomgen.zip" -d "$TMP"
  SBOMGEN=$(find "$TMP" -name inspector-sbomgen -type f | head -1)
  SUMS=$(find "$TMP" -name 'checksum*.txt' -type f | head -1)
  SHA=$(sha256sum "$SBOMGEN" | cut -d' ' -f1)
  grep -q "$SHA" "$SUMS" || { echo "inspector-sbomgen checksum mismatch ($SHA not in $(basename "$SUMS"))" >&2; exit 2; }
  chmod +x "$SBOMGEN"
fi
echo "inspector-sbomgen $("$SBOMGEN" --version)"

# 1. SBOM of the repository (virtualenvs, node_modules, run outputs and the reference-run copies are not shipped code)
"$SBOMGEN" directory --path . --outfile out/sbom.json --skip-cloud-metadata --quiet \
  --skip-files "./.venv,./out,./agent/evaldeck/app/ShopAgent/.venv,./agent/evaldeck/agentcore/cdk/node_modules,./agent/evaldeck/agentcore/cdk/cdk.out,./agent/evaldeck/agentcore/.cache"

# 2. vulnerabilities for every component in the SBOM
aws inspector-scan scan-sbom --sbom file://out/sbom.json --output-format CYCLONE_DX_1_5 --region "$REGION" \
  --output json > out/inspector-scan.json

# 3. summary + decision
"$PY" - <<'PY'
import json, os, sys
order = ["NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
fail_on = os.getenv("SECURITY_FAIL_ON", "HIGH").upper()
allow = {x.strip() for x in os.getenv("SECURITY_ALLOW", "").split(",") if x.strip()}
sbom = json.load(open("out/inspector-scan.json"))["sbom"]
inv = json.load(open("out/sbom.json"))
names = {c.get("bom-ref"): f"{c.get('name')}@{c.get('version')}" for c in sbom.get("components", [])}
rows = []
for v in sbom.get("vulnerabilities", []):
    sev = "NONE"
    for r in v.get("ratings", []):
        s = str(r.get("severity", "")).upper()
        if s in order and order.index(s) > order.index(sev):
            sev = s
    affects = sorted({names.get(a.get("ref"), a.get("ref")) for a in v.get("affects", [])})
    fixed = sorted({p.get("value") for p in v.get("properties", []) if ":fixed_version" in p.get("name", "")} - {None})
    rows.append({"id": v.get("id"), "severity": sev, "affects": affects, "fixed_in": fixed, "allowed": v.get("id") in allow})
counts = {s: sum(1 for r in rows if r["severity"] == s) for s in order[1:]}
blocking = [] if fail_on == "NONE" else [r for r in rows if not r["allowed"] and order.index(r["severity"]) >= order.index(fail_on)]
report = {"scanner": "amazon-inspector-sbom", "components": len(inv.get("components", [])), "vulnerabilities": counts,
          "fail_on": fail_on, "allowed": sorted(allow), "decision": "BLOCKED" if blocking else "PASS",
          "findings": sorted(rows, key=lambda r: -order.index(r["severity"]))}
json.dump(report, open("out/security-report.json", "w"), indent=2)
print(json.dumps({k: report[k] for k in ("components", "vulnerabilities", "fail_on", "decision")}))
for r in report["findings"][:30]:
    print(f"  {r['severity']:8s} {r['id']:22s} {', '.join(r['affects'])[:80]}"
          + (f"  fixed in {', '.join(r['fixed_in'])}" if r["fixed_in"] else "") + ("  (allowed)" if r["allowed"] else ""))
sys.exit(1 if blocking else 0)
PY
