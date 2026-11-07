# Reference run state (2026-11-06 / 07, 演示数据)

The account-specific state of the run behind the README tables and `SNIPPETS.md`, kept for reading only:

| File | Was | Fresh clone starts with |
|---|---|---|
| `registry.json` | `define/datasets/registry.json`: dataset id + published versions v2-v4 in the reference account | `{}`; `define/datasets/publish_dataset.py` writes your own ids |
| `baseline.json` | `release/baseline.json`: per-segment baseline measured on PROD | no file; the first gate run with `--write-baseline` creates it |
| `waivers.yaml` | `release/waivers.yaml`: one sim-segment waiver (expired 2026-11-20) | `waivers: []` |

The ids in these files belong to the reference account and do not resolve anywhere else; do not copy them back.
