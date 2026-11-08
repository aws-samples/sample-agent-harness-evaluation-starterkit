"""Unit tests for release/gate.py decisions (no AWS): python -m pytest release -q"""

import gate as g

GATE = {"policy": {"no_regression_tolerance": 0.05, "max_error_rate": 0.10, "min_results": 1, "require_baseline": True},
        "metrics": [
            {"evaluator": "Builtin.GoalSuccessRate", "tier": "gate", "rule": "mean_gte", "threshold": 0.8},
            {"evaluator": "evaldeck_RedlineCode", "tier": "redline", "rule": "violations_eq0"},
            {"evaluator": "Builtin.Helpfulness", "tier": "observe", "rule": "mean_gte", "threshold": 0.7}]}
DS = {"id": "ds-1", "version": "2"}
BASE = {"dataset": DS, "metrics": {"Builtin.GoalSuccessRate": {"mean": 0.9}, "evaldeck_RedlineCode": {"mean": 1.0}}}


def m(gsr=(1, 1, 1, 0.8), red=(1, 1, 1), helpful=(0.5,), gsr_err=0):
    return {"Builtin.GoalSuccessRate": {"values": list(gsr), "errors": gsr_err},
            "evaldeck_RedlineCode": {"values": list(red), "errors": 0},
            "Builtin.Helpfulness": {"values": list(helpful), "errors": 0}}


def test_pass_and_observe_only_warns():
    r = g.decide(GATE, m(), DS, BASE, [])
    assert r["decision"] == "PASS", r["reasons"]
    assert [x["status"] for x in r["metrics"]] == ["PASS", "PASS", "WARN"]


def test_threshold_blocks():
    r = g.decide(GATE, m(gsr=(1, 0, 1, 0)), DS, BASE, [])
    assert r["decision"] == "BLOCKED" and any("threshold" in x for x in r["reasons"])


def test_redline_violation_blocks_even_if_mean_high():
    r = g.decide(GATE, m(red=(1,) * 99 + (0,)), DS, BASE, [])
    assert r["decision"] == "BLOCKED" and any("red-line violation" in x for x in r["reasons"])


def test_regression_blocks():
    base = {**BASE, "metrics": {**BASE["metrics"], "Builtin.GoalSuccessRate": {"mean": 1.0}}}
    r = g.decide(GATE, m(gsr=(1, 1, 0.8, 0.8)), DS, base, [])          # 0.9 >= 0.8 but dropped 0.1 > 0.05
    assert r["decision"] == "BLOCKED" and any("regressed" in x for x in r["reasons"])


def test_missing_baseline_blocks():
    r = g.decide(GATE, m(), DS, None, [])
    assert r["decision"] == "BLOCKED" and any("baseline missing" in x for x in r["reasons"])


def test_baseline_on_other_dataset_version_blocks():
    r = g.decide(GATE, m(), {"id": "ds-1", "version": "3"}, BASE, [])
    assert r["decision"] == "BLOCKED" and any("re-baseline" in x for x in r["reasons"])


def test_missing_results_block():
    metrics, ds, problems = g.merge([])
    r = g.decide(GATE, metrics, ds, BASE, problems)
    assert r["decision"] == "BLOCKED" and "no results files given" in r["reasons"]
    assert any("not measured" in x for x in r["reasons"])


def test_unmeasured_redline_blocks():
    metrics = m()
    metrics.pop("evaldeck_RedlineCode")
    r = g.decide(GATE, metrics, DS, BASE, [])
    assert r["decision"] == "BLOCKED" and any("RedlineCode: not measured" in x for x in r["reasons"])


def test_error_rate_blocks():
    r = g.decide(GATE, m(gsr_err=2), DS, BASE, [])                    # 2 errors / 6 results = 33 %
    assert r["decision"] == "BLOCKED" and any("error rate" in x for x in r["reasons"])


def test_one_step_flip_is_tolerated_two_are_not():
    gate = {**GATE, "metrics": [{**GATE["metrics"][0], "score_step": 1.0}] + GATE["metrics"][1:]}
    base = {**BASE, "metrics": {**BASE["metrics"], "Builtin.GoalSuccessRate": {"mean": 1.0}}}
    ten = (1.0,) * 10
    assert g.decide(gate, m(gsr=ten[:9] + (0.0,)), DS, base, [])["decision"] == "PASS"            # 1 of 10 flipped
    assert g.decide(gate, m(gsr=ten[:8] + (0.0, 0.0)), DS, base, [])["decision"] == "BLOCKED"     # 2 of 10


def test_one_step_flip_survives_4_decimal_rounding():
    # clone run 2026-11-07: baseline 6.5 / 7 stored as 0.9286, candidate 6 / 7 = 0.8571, step 0.5 -> tolerance 0.5 / 7
    gate = {**GATE, "metrics": [{**GATE["metrics"][0], "score_step": 0.5}] + GATE["metrics"][1:]}
    base = {**BASE, "metrics": {**BASE["metrics"], "Builtin.GoalSuccessRate": {"mean": 0.9286}}}
    one_flip = (1.0,) * 5 + (0.5, 0.5)                     # 6 / 7 = 0.8571: one more 0.5 than the baseline
    two_flips = (1.0,) * 4 + (0.5, 0.5, 0.5)               # 5.5 / 7 = 0.7857
    assert g.decide(gate, m(gsr=one_flip), DS, base, [])["decision"] == "PASS"
    assert g.decide(gate, m(gsr=two_flips), DS, base, [])["decision"] == "BLOCKED"


# ---------------------------------------------------------------- segments, bundle check, waivers
import json  # noqa: E402
from datetime import date  # noqa: E402

TODAY = date(2026, 11, 6)


def waivers_file(tmp_path, entries):
    import yaml
    p = tmp_path / "waivers.yaml"
    p.write_text(yaml.safe_dump({"waivers": entries}))
    return p


W_OK = {"metric": "Builtin.GoalSuccessRate", "floor": 0.6, "expires": "2026-11-30", "approver": "a@example.com",
        "reason": "known gap"}


def test_waiver_lets_threshold_pass_above_floor(tmp_path):
    waivers, report = g.load_waivers(waivers_file(tmp_path, [W_OK]), TODAY)
    r = g.decide(GATE, m(gsr=(1, 0, 1, 1, 0.5)), DS, {**BASE, "metrics": {**BASE["metrics"],
                 "Builtin.GoalSuccessRate": {"mean": 0.7}}}, [], waivers)                       # 0.7 < 0.8, >= 0.6
    assert r["decision"] == "PASS", r["reasons"]
    assert r["metrics"][0]["status"] == "WAIVED" and report[0]["status"] == "applied"


def test_waiver_below_floor_still_blocks(tmp_path):
    waivers, report = g.load_waivers(waivers_file(tmp_path, [W_OK]), TODAY)
    r = g.decide(GATE, m(gsr=(1, 0, 0, 0)), DS, {**BASE, "metrics": {**BASE["metrics"],
                 "Builtin.GoalSuccessRate": {"mean": 0.25}}}, [], waivers)
    assert r["decision"] == "BLOCKED" and report[0]["status"] == "insufficient"


def test_waiver_never_covers_regression(tmp_path):
    waivers, _ = g.load_waivers(waivers_file(tmp_path, [W_OK]), TODAY)
    r = g.decide(GATE, m(gsr=(1, 0, 1, 1, 0.5)), DS, BASE, [], waivers)                          # base 0.9 -> 0.7
    assert r["decision"] == "BLOCKED" and any("regressed" in x for x in r["reasons"])


def test_redline_waiver_is_refused(tmp_path):
    waivers, report = g.load_waivers(waivers_file(tmp_path, [{**W_OK, "metric": "evaldeck_RedlineCode"}]), TODAY)
    r = g.decide(GATE, m(red=(1, 1, 0)), DS, BASE, [], waivers)
    assert r["decision"] == "BLOCKED" and any("red-line violation" in x for x in r["reasons"])
    assert report[0]["status"] == "refused" and "never waivable" in report[0]["why"]


def test_expired_and_incomplete_waivers_are_ignored(tmp_path):
    waivers, report = g.load_waivers(waivers_file(tmp_path, [{**W_OK, "expires": "2026-11-01"},
                                                             {**W_OK, "approver": None}]), TODAY)
    assert waivers == {} and [x["status"] for x in report] == ["expired", "refused"]
    r = g.decide(GATE, m(gsr=(1, 0, 1, 1, 0.5)), DS, BASE, [], waivers)
    assert r["decision"] == "BLOCKED"


def test_segment_tier_override_observes_in_that_segment_only():
    spec = {**GATE, "metrics": [dict(GATE["metrics"][0], segment_tiers={"sim": "observe"}), *GATE["metrics"][1:]]}
    low = {"Builtin.GoalSuccessRate": {"values": [0.5, 0.5], "errors": 0},
           "evaldeck_RedlineCode": {"values": [1.0, 1.0], "errors": 0}}
    sim = g.decide(spec, low, DS, BASE, [], segment="sim", only={"Builtin.GoalSuccessRate"})
    acc = g.decide(spec, low, DS, BASE, [], segment="acceptance", only={"Builtin.GoalSuccessRate"})
    assert sim["decision"] == "PASS" and sim["metrics"][0]["tier"] == "observe" and sim["metrics"][0]["status"] == "WARN"
    assert acc["decision"] == "BLOCKED"


def test_segment_judges_requested_metrics_plus_redlines():
    r = g.decide(GATE, {"Builtin.GoalSuccessRate": {"values": [1, 1], "errors": 0}}, None,
                 {"metrics": {"Builtin.GoalSuccessRate": {"mean": 1.0}}}, [], segment="sim",
                 only={"Builtin.GoalSuccessRate"})
    assert [x["metric"] for x in r["metrics"]] == ["Builtin.GoalSuccessRate", "evaldeck_RedlineCode"]
    assert r["decision"] == "BLOCKED" and any("sim/evaldeck_RedlineCode: not measured" in x for x in r["reasons"])


def _doc(tmp_path, name, segment, bundle=None, check=None):
    p = tmp_path / name
    p.write_text(json.dumps({"schema": "evaldeck.results/v1", "segment": segment, "dataset": DS,
                             "agent": {"endpoint": "STAGING", "bundle": bundle}, "bundle_check": check,
                             "evaluators": {"Builtin.GoalSuccessRate": "Builtin.GoalSuccessRate"},
                             "metrics": {"Builtin.GoalSuccessRate": {"values": [1.0], "errors": 0}}, "items": []}))
    return p


def test_unverified_bundle_blocks(tmp_path):
    b = {"arn": "arn:x", "id": "b", "version": "v2"}
    segs = g.merge_segments([_doc(tmp_path, "a.json", "acceptance", b, {"verified": False, "sessions": 2,
                                                                         "matched_sessions": 0})])
    assert any("not verified" in x for x in segs["acceptance"]["problems"])
    segs = g.merge_segments([_doc(tmp_path, "b.json", "acceptance", b, {"verified": True})])
    assert segs["acceptance"]["problems"] == []


def test_segment_prefix_on_command_line(tmp_path):
    p = _doc(tmp_path, "s.json", "acceptance")
    segs = g.merge_segments([__import__("pathlib").Path(f"sim={p}")])
    assert list(segs) == ["sim"]


def test_unknown_rule_and_gate_tier_violations_fail_closed():
    spec = {**GATE, "metrics": [{"evaluator": "Builtin.GoalSuccessRate", "tier": "gate", "rule": "median_gte", "threshold": 0.8},
                                {"evaluator": "evaldeck_RedlineCode", "tier": "gate", "rule": "violations_eq0"}]}
    r = g.decide(spec, m(), DS, BASE, [])
    assert r["decision"] == "BLOCKED" and any("unknown rule" in x for x in r["reasons"]), r["reasons"]
    bad = {"Builtin.GoalSuccessRate": {"values": [1.0], "errors": 0}, "evaldeck_RedlineCode": {"values": [1.0, 0.0], "errors": 0}}
    spec2 = {**GATE, "metrics": [spec["metrics"][1]]}
    r = g.decide(spec2, bad, DS, BASE, [])
    assert r["decision"] == "BLOCKED" and any("1 violation" in x for x in r["reasons"]), r["reasons"]


def test_binding_rejects_evidence_from_another_candidate(tmp_path):
    staging = {"endpoint": "STAGING", "runtimeArn": "arn:rt/a", "runtimeId": "a", "bundleVersion": None,
               "datasetId": "ds-1", "datasetVersion": "2", "at": "2026-11-08T10:00:00+00:00"}
    good = {"agent": {"endpoint": "STAGING", "runtime_arn": "arn:rt/a", "bundle": None},
            "dataset": {"id": "ds-1", "version": "2"}, "created_at": "2026-11-08T10:30:00+00:00"}
    segs = {"acceptance": {**g._empty(), "docs": [(tmp_path / "ok.json", good)]}}
    assert g.binding_problems(staging, segs) == []
    for change in ({"agent": {**good["agent"], "endpoint": "PROD"}}, {"agent": {**good["agent"], "runtime_arn": "arn:rt/b"}},
                   {"agent": {**good["agent"], "bundle": {"version": "v9"}}}, {"dataset": {"id": "ds-1", "version": "1"}},
                   {"created_at": "2026-11-08T09:00:00+00:00"}):
        segs = {"acceptance": {**g._empty(), "docs": [(tmp_path / "bad.json", {**good, **change})]}}
        assert g.binding_problems(staging, segs), change
