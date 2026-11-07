"""Shared helpers for the starter-kit scripts (all steps).

Configuration comes from the environment only - no account ids or names are hard-coded:
  AWS_REGION / AWS_PROFILE   standard boto3 resolution (default region us-east-1)
  AGENT_RUNTIME_ARN          deployed runtime ARN; if unset we ask `agentcore status --json` in AGENT_PROJECT_DIR
  AGENT_PROJECT_DIR          agentcore project directory (default: starter-kit/agent/evaldeck)
  AGENT_ENDPOINT             runtime endpoint qualifier: DEFAULT | STAGING | PROD (default: STAGING; DEFAULT is
                             denied by release/harden_default.sh once STAGING / PROD exist)
  BUNDLE_ID, BUNDLE_VERSION  Config bundle (id or ARN) + version id sent as W3C baggage on every invoke, so a
                             run scores the exact bundle version that ships (scripts: --bundle / --bundle-version)
  EVAL_OUT                   output directory for run results (default: starter-kit/out)
  NAME_PREFIX                prefix for every resource the kit creates (default: evaldeck)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.config import Config

KIT = Path(__file__).resolve().parents[1]
REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
ENDPOINT = os.getenv("AGENT_ENDPOINT", "STAGING")
ENDPOINTS = ("DEFAULT", "STAGING", "PROD")
BUNDLE: dict | None = None          # {"arn", "id", "version"} once set_target() resolved a bundle
PREFIX = os.getenv("NAME_PREFIX", "evaldeck")
OUT = Path(os.getenv("EVAL_OUT", KIT / "out"))
PROJECT_DIR = Path(os.getenv("AGENT_PROJECT_DIR", KIT / "agent" / "evaldeck"))
RESULTS_SCHEMA = "evaldeck.results/v1"
_CFG = Config(read_timeout=300, retries={"max_attempts": 5, "mode": "adaptive"})


_LOCAL = threading.local()
_SESSION_LOCK = threading.Lock()


def client(name: str):
    """One boto3 client per service per thread (the SDK runners call the invoker from a thread pool; creating
    clients concurrently from the shared default session is not thread-safe)."""
    cache = _LOCAL.__dict__.setdefault("clients", {})
    if name not in cache:
        with _SESSION_LOCK:
            cache[name] = boto3.session.Session().client(name, region_name=REGION, config=_CFG)
    return cache[name]


def now_utc() -> datetime:
    return datetime.now(UTC)


def stamp() -> str:
    return now_utc().strftime("%Y%m%dT%H%M%SZ")


def new_session_id(tag: str) -> str:
    """Runtime session ids must be >= 33 chars; keep a readable tag in front."""
    tag = re.sub(r"[^A-Za-z0-9-]", "-", tag)[:40]
    return f"{PREFIX}-{tag}-{uuid.uuid4().hex}"


# ---------------------------------------------------------------- runtime discovery
def runtime_arn() -> str:
    arn = os.getenv("AGENT_RUNTIME_ARN")
    if arn:
        return arn
    out = subprocess.run(["agentcore", "status", "--json"], cwd=PROJECT_DIR, capture_output=True, text=True,
                         check=True).stdout
    for r in json.loads(out).get("resources", []):
        if r.get("resourceType") == "runtime" and r.get("arn"):
            return r["arn"]
    raise SystemExit("no deployed runtime found: set AGENT_RUNTIME_ARN or deploy agent/evaldeck first")


def runtime_id(arn: str | None = None) -> str:
    return (arn or runtime_arn()).split("runtime/")[1].split("/")[0]


def runtime_name(arn: str | None = None) -> str:
    """evaldeck_default_ShopAgent-AbCd123456 -> evaldeck_default_ShopAgent (the OTel service name stem)."""
    return runtime_id(arn).rsplit("-", 1)[0]


def service_name(arn: str | None = None, endpoint: str | None = None) -> str:
    return f"{runtime_name(arn)}.{endpoint or ENDPOINT}"


def runtime_log_group(arn: str | None = None, endpoint: str | None = None) -> str:
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id(arn)}-{endpoint or ENDPOINT}"


# ---------------------------------------------------------------- endpoint + Config bundle selection
def add_target_args(ap) -> None:
    """--endpoint / --bundle / --bundle-version on every invoke / eval script."""
    ap.add_argument("--endpoint", choices=ENDPOINTS, default=ENDPOINT, help="runtime endpoint (env AGENT_ENDPOINT)")
    ap.add_argument("--bundle", default=os.getenv("BUNDLE_ID"), help="Config bundle id or ARN (env BUNDLE_ID)")
    ap.add_argument("--bundle-version", default=os.getenv("BUNDLE_VERSION"),
                    help="Config bundle version id sent as baggage (env BUNDLE_VERSION); omit = code default prompt")


def set_target(a) -> dict:
    """Apply --endpoint / --bundle / --bundle-version for this process; returns the target for the results doc.
    The bundle version is checked to exist before any traffic is sent."""
    global ENDPOINT, BUNDLE
    ENDPOINT = a.endpoint
    BUNDLE = None
    if a.bundle_version:
        if not a.bundle:
            raise SystemExit("--bundle-version needs --bundle (or BUNDLE_ID)")
        bid = a.bundle.rsplit("/", 1)[-1]
        ctl = client("bedrock-agentcore-control")
        v = ctl.get_configuration_bundle_version(bundleId=bid, versionId=a.bundle_version)
        BUNDLE = {"arn": v["bundleArn"], "id": bid, "version": v["versionId"]}
    return {"endpoint": ENDPOINT, "bundle": BUNDLE}


def bundle_baggage(bundle: dict | None = None) -> str | None:
    b = bundle or BUNDLE
    if not b:
        return None
    return f"aws.agentcore.configbundle_arn={b['arn']},aws.agentcore.configbundle_version={b['version']}"


def bundle_check(session_ids: list[str], arn: str | None = None, hours: int = 3) -> dict:
    """Prove which prompt the agent used: count invoke_agent spans per session by the span attributes the agent
    writes (evaldeck.prompt_source, aws.agentcore.configbundle_version) in the endpoint's runtime log group."""
    sids = sorted({s for s in session_ids if s})
    expected = (BUNDLE or {}).get("version")
    out = {"expected_version": expected, "sessions": len(sids), "spans": 0, "by_source": {}, "matched_sessions": 0,
           "unmatched_sessions": []}
    if not sids:
        return out
    logs = client("logs")
    end = int(time.time())
    seen: dict[str, set] = {}
    for i in range(0, len(sids), 20):
        quoted = ", ".join(f"'{s}'" for s in sids[i:i + 20])
        q = logs.start_query(logGroupName=runtime_log_group(arn), startTime=end - hours * 3600, endTime=end + 60,
                             queryString=("filter name like /^invoke_agent/ and attributes.session.id in [" + quoted + "]"
                                          " | stats count(*) as n by attributes.session.id as sid,"
                                          " attributes.evaldeck.prompt_source as src,"
                                          " attributes.aws.agentcore.configbundle_version as ver"))["queryId"]
        while (r := logs.get_query_results(queryId=q))["status"] in ("Scheduled", "Running"):
            time.sleep(2)
        for row in r.get("results", []):
            f = {c["field"]: c["value"] for c in row}
            n = int(f.get("n", 0))
            key = f"{f.get('src', 'none')}:{f.get('ver', '-')}"
            out["spans"] += n
            out["by_source"][key] = out["by_source"].get(key, 0) + n
            seen.setdefault(f["sid"], set()).add((f.get("src"), f.get("ver")))
    for s in sids:
        got = seen.get(s, set())
        ok = bool(got) and all((src == "bundle" and ver == expected) if expected else src == "code_default"
                               for src, ver in got)
        out["matched_sessions"] += ok
        if not ok:
            out["unmatched_sessions"].append(s)
    out["verified"] = out["matched_sessions"] == len(sids)
    return out


def gateway_id(name: str | None = None) -> str:
    """Deployed Gateway id: GATEWAY_ID, else `agentcore status`, else ListGateways by the CLI's naming
    (<project>-<target>-<gatewayname lowercased>)."""
    if os.getenv("GATEWAY_ID"):
        return os.environ["GATEWAY_ID"]
    name = name or os.getenv("GATEWAY_NAME", "evaldeckFront")
    try:
        out = subprocess.run(["agentcore", "status", "--json"], cwd=PROJECT_DIR, capture_output=True, text=True,
                             check=True).stdout
        for r in json.loads(out).get("resources", []):
            if r.get("resourceType") == "gateway" and r.get("name") == name and r.get("arn"):
                return r["arn"].rsplit("/", 1)[1]
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
        pass
    for g in client("bedrock-agentcore-control").list_gateways().get("items", []):
        if g["name"].endswith(f"-{name.lower()}") and g["name"].startswith(PREFIX):
            return g["gatewayId"]
    raise SystemExit(f"gateway {name} not found: set GATEWAY_ID")


def account_id() -> str:
    return client("sts").get_caller_identity()["Account"]


# ---------------------------------------------------------------- invoking the agent
def invoke(prompt: str, session_id: str, arn: str | None = None, endpoint: str | None = None,
           baggage: str | None = None) -> str:
    """Invoke the runtime once and return the agent's text answer (baggage defaults to the selected bundle)."""
    kw = {"agentRuntimeArn": arn or runtime_arn(), "qualifier": endpoint or ENDPOINT, "runtimeSessionId": session_id,
          "payload": json.dumps({"prompt": prompt}).encode()}
    baggage = baggage or bundle_baggage()
    if baggage:
        kw["baggage"] = baggage
    dp = client("bedrock-agentcore")
    for attempt in range(3):
        try:
            r = dp.invoke_agent_runtime(**kw)
            body = r["response"].read().decode()
            try:
                return json.loads(body).get("response", body)
            except json.JSONDecodeError:
                return body
        except dp.exceptions.ThrottlingException:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("invoke_agent_runtime throttled 3 times")


# ---------------------------------------------------------------- evaluators
def evaluator_ids(names: list[str]) -> dict[str, str]:
    """Map names to evaluator ids. Builtin./ThirdParty. ids are used as-is; custom evaluators are
    looked up by evaluatorName (their ids carry a random suffix)."""
    need = [n for n in names if not n.startswith(("Builtin.", "ThirdParty."))]
    found = {}
    if need:
        paginator = client("bedrock-agentcore-control").get_paginator("list_evaluators")
        for page in paginator.paginate():
            for e in page.get("evaluators", []):
                if e.get("evaluatorName") in need:
                    found[e["evaluatorName"]] = e["evaluatorId"]
    missing = [n for n in need if n not in found]
    if missing:
        raise SystemExit(f"custom evaluators not found (register them in define/evaluators first): {missing}")
    return {n: found.get(n, n) for n in names}


def canonical(name: str | None) -> str:
    """Evaluator display name used as the gate metric key: Builtin ids stay, custom ids lose the suffix."""
    name = name or ""
    if name.startswith(("Builtin.", "ThirdParty.")):
        return name
    return re.sub(r"-[A-Za-z0-9]{10}$", "", name)


# ---------------------------------------------------------------- files
def load_jsonl(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def write_json(path: str | Path, data) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")
    return path


def results_doc(source: str, items: list[dict], **extra) -> dict:
    """Normalized run output read by release/gate.py and feedback/mine_badcases.py.
    items: one row per evaluator result {session_id, scenario_id, evaluator, value, label, explanation,
    trace_id, error}."""
    metrics: dict[str, dict] = {}
    for it in items:
        m = metrics.setdefault(canonical(it["evaluator"]), {"values": [], "errors": 0, "labels": {}})
        if it.get("error") or it.get("value") is None:
            m["errors"] += 1
        else:
            m["values"].append(float(it["value"]))
        if it.get("label"):
            m["labels"][it["label"]] = m["labels"].get(it["label"], 0) + 1
    for m in metrics.values():
        m["n"] = len(m["values"])
        m["mean"] = round(sum(m["values"]) / m["n"], 4) if m["n"] else None
    return {"schema": RESULTS_SCHEMA, "source": source, "created_at": now_utc().isoformat(),
            "region": REGION, "metrics": metrics, "items": items, **extra}
