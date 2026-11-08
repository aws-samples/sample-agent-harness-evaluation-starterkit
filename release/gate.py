#!/usr/bin/env python3
"""Step 4 Release - the quality gate: PASS or BLOCKED from evaluation results, as CI runs it.

The AgentCore CLI has no score threshold (its exit code only says whether the command failed), so the
decision lives here. Rules, from define/gate.yaml (generated from requirements.yaml):
  1. per-metric thresholds   every `gate` metric: mean >= threshold (no averaging across metrics)
  2. red lines = 0           every `redline` metric: no result below 1.0, never waivable
  3. no regression           every `gate` metric: baseline mean - mean <= max(no_regression_tolerance,
                             score_step / n) - one flip of the smallest score step is noise, two are not
  4. baseline required       no baseline, a baseline without the metric, or a baseline measured on another
                             dataset version => BLOCKED (re-baseline after the dataset or a judge changes)
  5. not measured = blocked  no results file, a metric with no scored results, evaluator errors above
                             max_error_rate, or a Config bundle the run did not provably use => BLOCKED
`observe` metrics are reported only. Exit code: 0 PASS, 1 BLOCKED (any reason, including missing inputs).

Segments: every results file belongs to one segment (doc["segment"], or `--results sim=out/simulate-x.json`).
`acceptance` (run_dataset.py on the pinned Dataset version) is judged on every metric in gate.yaml; other
segments (`sim` = simulate.py, a `redteam` set if you add one) on the metrics their run requested, plus every
red line. Each segment has its own baseline (baseline.json "segments"); all segments must PASS.
Fail closed: a rule the gate does not implement BLOCKS; every scenario of the pinned dataset (and every persona) must
have been scored; with out/staging.json every results file must come from that staging run (endpoint, runtime ARN,
dataset id + version, Config bundle version, created after the deployment), else BLOCKED. A metric's
`segment_tiers` (gate.yaml, from requirements.yaml) can make it observe-only in one segment; red lines never.

Waivers (release/waivers.yaml): a named approver may lower the threshold of one `gate` metric to a floor until
an expiry date. A waiver never covers a red line (refused), a regression, a missing measurement or errors;
expired or incomplete waivers are ignored and reported.

gate_key: the release fingerprint from release/gate_key.py (out/staging.json, or --gate-key / GATE_KEY). It is
written into the report so record.py / release.sh release only what this gate decided on. The report also keeps
`content_key`, a hash of the files that decide behaviour and scoring.

Usage:
  python release/gate.py --results out/dataset-ondemand-<ts>.results.json --results out/simulate-<ts>.results.json \
      [--baseline release/baseline.json] [--waivers release/waivers.yaml] [--report out/gate-report.json]
  python release/gate.py --results <PASSing run> [--results <sim run>] --write-baseline   # promote to baseline
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

KIT = Path(__file__).resolve().parents[1]
GATE_KEY_PATHS = ["agent/evaldeck/app", "agent/evaldeck/agentcore/agentcore.json", "define/gate.yaml",
                  "define/datasets", "define/evaluators"]
GATE_KEY_SKIP = {".venv", "__pycache__", ".pytest_cache", "registry.json"}
PRIMARY = "acceptance"


def content_key(root: Path = KIT) -> str:
    h = hashlib.sha256()
    for rel in GATE_KEY_PATHS:
        p = root / rel
        files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file()) if p.exists() else []
        for f in files:
            if GATE_KEY_SKIP & set(f.relative_to(root).parts) or f.suffix in {".pyc", ".lock"}:
                continue
            h.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


gate_key = content_key   # backwards-compatible name


def _split(arg: Path) -> tuple[str | None, Path]:
    s = str(arg)
    if "=" in s and not s.startswith(("/", ".")) and s.split("=", 1)[0].isidentifier():
        seg, path = s.split("=", 1)
        return seg, Path(path)
    return None, arg


def merge(paths: list[Path]) -> tuple[dict, dict | None, list[str]]:
    """Merge results files -> {metric: {values, errors}}, the dataset they ran on, and input problems."""
    segs = merge_segments(paths)
    if not segs:
        return {}, None, ["no results files given"] if not paths else [f"results file missing: {p}" for p in paths]
    first = next(iter(segs.values()))
    return first["metrics"], first["dataset"], first["problems"]


def merge_segments(paths: list[Path]) -> dict[str, dict]:
    """-> {segment: {metrics, dataset, problems, requested, bundle_checks}}"""
    segs: dict[str, dict] = {}
    for arg in paths:
        seg, p = _split(arg)
        if not p.exists():
            segs.setdefault(seg or PRIMARY, _empty())["problems"].append(f"results file missing: {p}")
            continue
        doc = json.loads(p.read_text())
        if doc.get("schema") != "evaldeck.results/v1":
            segs.setdefault(seg or PRIMARY, _empty())["problems"].append(f"{p}: not an evaldeck.results/v1 file")
            continue
        s = segs.setdefault(seg or doc.get("segment") or PRIMARY, _empty())
        s["dataset"] = s["dataset"] or doc.get("dataset")
        s["docs"].append((p, doc))
        s["scenarios"] |= {i.get("scenario_id") for i in doc.get("items", [])
                           if i.get("scenario_id") and i.get("evaluator") != "_invocation" and i.get("value") is not None}
        s["requested"] |= {_canon(k) for k in (doc.get("evaluators") or {})}
        for name, m in doc.get("metrics", {}).items():
            cur = s["metrics"].setdefault(name, {"values": [], "errors": 0})
            cur["values"] += m.get("values", [])
            cur["errors"] += m.get("errors", 0)
        inv = [i for i in doc.get("items", []) if i.get("evaluator") == "_invocation"]
        if inv:
            s["problems"].append(f"{p}: {len(inv)} scenario invocation failure(s)")
        bundle = (doc.get("agent") or {}).get("bundle")
        if bundle:
            chk = doc.get("bundle_check") or {}
            s["bundle"] = bundle
            if not chk.get("verified"):
                s["problems"].append(
                    f"{p}: Config bundle {bundle.get('version')} not verified in the agent spans "
                    f"({chk.get('matched_sessions', 0)}/{chk.get('sessions', '?')} sessions, seen {chk.get('by_source')})")
    return segs


def _empty() -> dict:
    return {"metrics": {}, "dataset": None, "problems": [], "requested": set(), "bundle": None, "docs": [],
            "scenarios": set()}


def _canon(name: str) -> str:
    import re
    return name if name.startswith(("Builtin.", "ThirdParty.")) else re.sub(r"-[A-Za-z0-9]{10}$", "", name)


# ---------------------------------------------------------------- waivers
def load_waivers(path: Path | None, today: date | None = None) -> tuple[dict, list[dict]]:
    """-> ({metric: waiver} usable today, report rows for every entry). Redline refusal is decided per metric."""
    today = today or datetime.now(UTC).date()
    usable, report = {}, []
    if not path or not path.exists():
        return usable, report
    for w in (yaml.safe_load(path.read_text()) or {}).get("waivers") or []:
        row = {"metric": w.get("metric"), "segment": w.get("segment"), "approver": w.get("approver"),
               "expires": str(w.get("expires")), "floor": w.get("floor"), "reason": w.get("reason")}
        missing = [k for k in ("metric", "floor", "expires", "approver", "reason") if not w.get(k) and w.get(k) != 0]
        if missing:
            report.append({**row, "status": "refused", "why": f"incomplete: missing {missing}"})
            continue
        exp = w["expires"] if isinstance(w["expires"], date) else date.fromisoformat(str(w["expires"]))
        if exp < today:
            report.append({**row, "status": "expired", "why": f"expired on {exp}"})
            continue
        usable[(w["metric"], w.get("segment"))] = {**row, "status": "unused"}
        report.append(usable[(w["metric"], w.get("segment"))])
    return usable, report


def _waiver_for(waivers: dict, metric: str, segment: str) -> dict | None:
    return waivers.get((metric, segment)) or waivers.get((metric, None))


# ---------------------------------------------------------------- decision
ROUNDING = 1.1e-4   # two 4-decimal roundings (mean and baseline mean)


def decide(gate: dict, metrics: dict, dataset: dict | None, baseline: dict | None,
           problems: list[str], waivers: dict | None = None, segment: str = PRIMARY,
           only: set[str] | None = None) -> dict:
    pol = gate.get("policy", {})
    tol, max_err = pol.get("no_regression_tolerance", 0.03), pol.get("max_error_rate", 0.10)
    min_n, need_base = pol.get("min_results", 1), pol.get("require_baseline", True)
    waivers = waivers or {}
    blocked = list(problems)
    if need_base and baseline is None:
        blocked.append("baseline missing: run a PASSing gate with --write-baseline and commit release/baseline.json")
    if baseline and dataset and baseline.get("dataset") and \
            (baseline["dataset"].get("id") or baseline["dataset"].get("name"), str(baseline["dataset"].get("version"))) != \
            (dataset.get("id") or dataset.get("name"), str(dataset.get("version"))):
        blocked.append(f"baseline was measured on dataset {baseline['dataset']} but this run used {dataset}: re-baseline")
    rows = []
    specs = [s for s in gate["metrics"] if only is None or s["evaluator"] in only or s["tier"] == "redline"]
    for spec in specs:
        name = spec["evaluator"]
        tier = (spec.get("segment_tiers") or {}).get(segment, spec["tier"]) if spec["tier"] != "redline" else "redline"
        m = metrics.get(name, {"values": [], "errors": 0})
        vals, errs = m["values"], m["errors"]
        n = len(vals)
        mean = round(sum(vals) / n, 4) if n else None
        err_rate = errs / (n + errs) if n + errs else 0.0
        base = ((baseline or {}).get("metrics") or {}).get(name, {}).get("mean")
        row = {"metric": name, "tier": tier, "segment": segment, "n": n, "errors": errs, "mean": mean,
               "threshold": spec.get("threshold"), "baseline": base, "status": "PASS", "why": []}
        enforce = tier in ("gate", "redline")
        w = _waiver_for(waivers, name, segment)
        if w and tier == "redline":
            w.update(status="refused", why="red lines are never waivable")
            row["waiver"] = "refused"
        if n < min_n:
            row["why"].append(f"not measured ({n} scored results < {min_n})")
        if err_rate > max_err:
            row["why"].append(f"evaluator error rate {err_rate:.0%} > {max_err:.0%}")
        rule = spec.get("rule")
        if rule not in ("mean_gte", "violations_eq0"):
            row["why"].append(f"unknown rule {rule!r}: the gate cannot evaluate it")
        if rule == "violations_eq0" or tier == "redline":
            bad = sum(1 for v in vals if v < 1.0)
            row["violations"] = bad
            if bad:
                row["why"].append(f"{bad} red-line violation(s)" if tier == "redline" else f"{bad} violation(s)")
        if spec.get("rule") == "mean_gte" and mean is not None and mean < spec["threshold"] - 1e-9:
            if w and tier == "gate" and mean >= float(w["floor"]) - 1e-9:
                w.update(status="applied", why=f"{segment}: mean {mean} < threshold {spec['threshold']}, >= floor {w['floor']}")
                row["waiver"] = f"applied: floor {w['floor']}, approver {w['approver']}, expires {w['expires']}"
            else:
                if w and tier == "gate":
                    w.update(status="insufficient", why=f"{segment}: mean {mean} < floor {w['floor']}")
                row["why"].append(f"mean {mean} < threshold {spec['threshold']}")
        if tier == "gate":
            if base is None and need_base:
                row["why"].append("no baseline value for this metric")
            elif base is not None and mean is not None:
                allowed = max(tol, (spec.get("score_step") or 0.0) / n) if n else tol
                row["tolerance"] = round(allowed, 4)
                # means (and stored baselines) are rounded to 4 decimals: allow for that rounding, so one flip of
                # score_step on n results (e.g. 0.9286 -> 0.8571 at n = 7, step 0.5) is not a regression
                if base - mean > allowed + ROUNDING:
                    row["why"].append(f"regressed {base} -> {mean} (> {allowed:.3f} tolerance)")
        if row["why"]:
            row["status"] = "BLOCKED" if enforce else "WARN"
            if enforce:
                blocked.append(f"{segment}/{name}: " + "; ".join(row["why"]))
        elif str(row.get("waiver", "")).startswith("applied"):
            row["status"] = "WAIVED"
        rows.append(row)
    return {"decision": "BLOCKED" if blocked else "PASS", "reasons": blocked, "metrics": rows}


def _segment_baseline(baseline: dict | None, seg: str) -> dict | None:
    if not baseline:
        return None
    if "segments" in baseline:
        return baseline["segments"].get(seg)
    return baseline if seg == PRIMARY else None       # v1 baseline.json = acceptance only


def expected_scenarios(seg: str, dataset: dict | None) -> int | None:
    """Scenario count the segment must cover: the pinned dataset version's rows (registry.json) for the primary
    segment, the persona file for `sim`. None = unknown (no coverage check)."""
    if seg == PRIMARY and dataset:
        reg = KIT / "define/datasets/registry.json"
        entry = (json.loads(reg.read_text()) if reg.exists() else {}).get(dataset.get("name") or "", {})
        if entry.get("id") == dataset.get("id"):
            v = next((x for x in entry.get("versions", []) if str(x["version"]) == str(dataset.get("version"))), None)
            return v and v.get("rows")
    if seg == "sim":
        f = KIT / "define/datasets/personas.jsonl"
        return sum(1 for line in f.read_text().splitlines() if line.strip()) if f.exists() else None
    return None


def binding_problems(staging: dict, segs: dict) -> list[str]:
    """Every results file must be evidence about the candidate staging.json names."""
    out = []
    want_bundle = staging.get("bundleVersion")
    for seg, s in segs.items():
        for p, doc in s["docs"]:
            agent = doc.get("agent") or {}
            if agent.get("endpoint") and agent["endpoint"] != staging.get("endpoint"):
                out.append(f"{p.name}: ran on {agent['endpoint']}, staging is {staging.get('endpoint')}")
            if agent.get("runtime_arn") and staging.get("runtimeArn") and agent["runtime_arn"] != staging["runtimeArn"]:
                out.append(f"{p.name}: runtime {agent['runtime_arn'].rsplit('/', 1)[-1]} is not {staging.get('runtimeId')}")
            got_bundle = (agent.get("bundle") or {}).get("version")
            if str(got_bundle or "") != str(want_bundle or ""):
                out.append(f"{p.name}: Config bundle {got_bundle} but staging pins {want_bundle}")
            if doc.get("created_at") and staging.get("at") and str(doc["created_at"]) < str(staging["at"]):
                out.append(f"{p.name}: created {doc['created_at']} before the staging deployment {staging['at']}")
            ds = doc.get("dataset") or {}
            if seg == PRIMARY and (str(ds.get("version")) != str(staging.get("datasetVersion"))
                                   or (staging.get("datasetId") and ds.get("id") != staging["datasetId"])):
                out.append(f"{p.name}: dataset {ds.get('id')} v{ds.get('version')} is not the pinned "
                           f"{staging.get('datasetId')} v{staging.get('datasetVersion')}")
    return out


def resolve_gate_key(arg: str | None, staging: Path) -> str | None:
    if arg or os.getenv("GATE_KEY"):
        return arg or os.getenv("GATE_KEY")
    if staging.exists():
        return json.loads(staging.read_text()).get("gate_key")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", type=Path, default=KIT / "define/gate.yaml")
    ap.add_argument("--results", type=Path, action="append", default=[], help="path or segment=path")
    ap.add_argument("--baseline", type=Path, default=KIT / "release/baseline.json")
    ap.add_argument("--waivers", type=Path, default=KIT / "release/waivers.yaml")
    ap.add_argument("--staging", type=Path, default=KIT / "out/staging.json", help="written by deploy_staging.sh")
    ap.add_argument("--gate-key", help="release fingerprint (default: out/staging.json gate_key, env GATE_KEY)")
    ap.add_argument("--report", type=Path, default=KIT / "out/gate-report.json")
    ap.add_argument("--write-baseline", action="store_true", help="after a PASS, store these results as the baseline")
    a = ap.parse_args()

    gate = yaml.safe_load(a.gate.read_text())
    segs = merge_segments(a.results)
    if not segs:
        segs = {PRIMARY: {**_empty(), "problems": ["no results files given"]}}
    if PRIMARY not in segs:
        segs[PRIMARY] = {**_empty(), "problems": [f"segment '{PRIMARY}' missing: run evaluate/run_dataset.py"]}
    baseline = json.loads(a.baseline.read_text()) if a.baseline.exists() else None
    waivers, waiver_report = load_waivers(a.waivers)
    if a.write_baseline:
        gate = {**gate, "policy": {**gate.get("policy", {}), "require_baseline": False}}
    staging_doc = json.loads(a.staging.read_text()) if a.staging.exists() and not a.write_baseline else None
    if staging_doc:                        # baselines are measured on PROD, so no staging binding for them
        segs[PRIMARY]["problems"] += binding_problems(staging_doc, segs)
    for seg, s in segs.items():
        want = expected_scenarios(seg, s["dataset"])
        if want and s["docs"] and len(s["scenarios"]) < want:
            s["problems"].append(f"{seg}: {len(s['scenarios'])} of {want} scenarios scored (coverage)")
    results, reasons, rows = {}, [], []
    for seg, s in segs.items():
        only = None if seg == PRIMARY else s["requested"]
        res = decide(gate, s["metrics"], s["dataset"], None if a.write_baseline else _segment_baseline(baseline, seg),
                     s["problems"], waivers, seg, only)
        results[seg] = {"decision": res["decision"], "dataset": s["dataset"], "bundle": s["bundle"]}
        reasons += res["reasons"]
        rows += res["metrics"]
    decision = "BLOCKED" if reasons else "PASS"
    key, ckey = resolve_gate_key(a.gate_key, a.staging), content_key()
    report = {"decision": decision, "gate_key": key or ckey, "content_key": ckey,
              "dataset": segs[PRIMARY]["dataset"], "segments": results,
              "waivers": waiver_report, "waived": [r["metric"] for r in rows if r["status"] == "WAIVED"],
              "baseline": str(a.baseline.relative_to(KIT)) if a.baseline.is_relative_to(KIT) else str(a.baseline),
              "results": [str(p) for p in a.results], "at": datetime.now(UTC).isoformat(),
              "reasons": reasons, "metrics": rows}
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(json.dumps(report, indent=2, default=str) + "\n")

    for r in rows:
        print(f"{r['status']:8s} {r['segment']:10s} {r['tier']:8s} {r['metric']:32s} mean={r['mean']} n={r['n']} "
              f"thr={r['threshold']} base={r['baseline']}" + (f"  <- {'; '.join(r['why'])}" if r["why"] else "")
              + (f"  [waiver {r['waiver']}]" if r.get("waiver") else ""))
    for w in waiver_report:
        print(f"waiver {w['status']:12s} {w['metric']} (approver {w['approver']}, expires {w['expires']})"
              + (f": {w.get('why')}" if w.get("why") else ""))
    if decision == "PASS":
        print(f"PASS gate_key={report['gate_key']}" + (f" waived={report['waived']}" if report["waived"] else ""))
        if a.write_baseline:
            def seg_doc(seg):
                srcs = [p for p in (_split(x)[1] for x in a.results) if p.exists() and
                        (json.loads(p.read_text()).get("segment") or PRIMARY) == seg]
                return {"dataset": segs[seg]["dataset"],
                        "measured_on": [{"runtime_id": (d.get("runtime_arn") or "").rsplit("/", 1)[-1],
                                         "endpoint": d.get("endpoint"), "bundle": d.get("bundle")}
                                        for d in (json.loads(p.read_text()).get("agent") or {} for p in srcs)],
                        "source_results": [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                                           for p in srcs],
                        "metrics": {r["metric"]: {"mean": r["mean"], "n": r["n"]} for r in rows if r["segment"] == seg}}
            a.baseline.write_text(json.dumps({"created_at": datetime.now(UTC).isoformat(), "content_key": ckey,
                                              "waived": report["waived"],
                                              "segments": {seg: seg_doc(seg) for seg in segs}},
                                             indent=2, default=str) + "\n")
            print(f"baseline written: {a.baseline}")
        return 0
    print("BLOCKED: " + " | ".join(reasons))
    return 1


if __name__ == "__main__":
    sys.exit(main())
