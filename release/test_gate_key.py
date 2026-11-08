"""Unit tests for release/gate_key.py (no AWS): any input change changes the key; --check catches drift."""

import json

import gate_key as gk

DOC = {"gitCommit": "abc123", "runtimeId": "rt-a", "runtimeVersion": "4", "bundleArn": "arn:b/1",
       "bundleVersion": "649aeb81", "datasetId": "ds-1", "datasetVersion": "4"}


def test_key_changes_with_every_input():
    base = gk.from_staging(DOC)
    assert base == gk.from_staging(dict(DOC)) and len(base) == 16
    for k, v in (("gitCommit", "abc124"), ("runtimeVersion", "5"), ("bundleVersion", None), ("datasetVersion", "3"),
                 ("runtimeId", "rt-b"), ("bundleArn", "arn:b/2"), ("datasetId", "ds-2")):
        assert gk.from_staging({**DOC, k: v}) != base, k


def test_gate_yaml_is_part_of_the_key(tmp_path):
    g1, g2 = tmp_path / "a.yaml", tmp_path / "b.yaml"
    g1.write_text("metrics: []\n")
    g2.write_text("metrics: [x]\n")
    assert gk.compute("c", "4", None, "4", g1) != gk.compute("c", "4", None, "4", g2)


def test_check_detects_a_tampered_staging_record(tmp_path, monkeypatch, capsys):
    p = tmp_path / "staging.json"
    p.write_text(json.dumps({**DOC, "gate_key": gk.from_staging(DOC)}))
    monkeypatch.setattr("sys.argv", ["gate_key.py", "--staging", str(p), "--check"])
    assert gk.main() == 0
    p.write_text(json.dumps({**DOC, "runtimeVersion": "5", "gate_key": gk.from_staging(DOC)}))
    assert gk.main() == 1
