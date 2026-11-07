# SNIPPETS - slide-ready excerpts of every starter-kit script

Each section: the most slide-worthy lines (copied verbatim from the file, line numbers given, only the common indent
removed) and one real output excerpt from the live validation on 2026-11-06 (account ids replaced by `<ACCOUNT_ID>`;
demo agent and data = 演示数据). Full scripts: `<starter-kit-root-directory>/<path>`.

## ① define/requirements.yaml - the PRD as data

`define/requirements.yaml` lines 26-41
```yaml
release_criteria:
  # evaluator                       requirement     tier      rule              threshold   score_step = smallest
  # possible change of one result (1.0 for Yes/No evaluators, 0.5 for a 0 / 0.5 / 1 judge)
  - {evaluator: Builtin.GoalSuccessRate,      req: [REQ-01, REQ-02], tier: gate,    rule: mean_gte, threshold: 0.80, score_step: 1.0}
  - {evaluator: Builtin.Correctness,          req: [REQ-01, REQ-02], tier: gate,    rule: mean_gte, threshold: 0.70, score_step: 1.0}
  - {evaluator: Builtin.Helpfulness,          req: [REQ-03],         tier: observe, rule: mean_gte, threshold: 0.70}
  - {evaluator: evaldeck_ScopeJudge,          req: [REQ-03],         tier: gate,    rule: mean_gte, threshold: 0.80, score_step: 0.5}
  - {evaluator: evaldeck_RedlineCode,         req: [REQ-04],         tier: redline, rule: violations_eq0}
  - {evaluator: Builtin.TrajectoryAnyOrderMatch, req: [REQ-01, REQ-02], tier: observe, rule: mean_gte, threshold: 0.80}

gate_policy:
  no_regression_tolerance: 0.05   # a gate metric may drop at most max(this, score_step / n) below the baseline
                                  # mean: with n results one flip of the smallest score step is not a regression
  max_error_rate: 0.10            # evaluator errors / results above this => BLOCKED ("could not measure")
  min_results: 1                  # every gate/redline metric needs at least this many scored results
  require_baseline: true          # no baseline file => BLOCKED
```

Real output (redacted):
```
$ python define/gen_gate.py
wrote define/gate.yaml: 6 metrics {'gate': 3, 'observe': 2, 'redline': 1}
$ python define/gen_gate.py --check
define/gate.yaml is up to date (6 metrics)
```

## ① define/gen_gate.py - validated translation, never edits thresholds

`define/gen_gate.py` lines 45-62
```python
for c in req.get("release_criteria", []):
    ev, tier, rule = c.get("evaluator"), c.get("tier"), c.get("rule")
    if tier not in TIERS:
        errs.append(f"{ev}: tier {tier!r} not in {sorted(TIERS)}")
    if rule not in RULES:
        errs.append(f"{ev}: rule {rule!r} not in {sorted(RULES)}")
    if tier == "redline" and rule != "violations_eq0":
        errs.append(f"{ev}: a redline must use rule violations_eq0")
    if rule == "mean_gte" and not isinstance(c.get("threshold"), (int, float)):
        errs.append(f"{ev}: mean_gte needs a numeric threshold")
    unknown = set(c.get("req", [])) - req_ids
    if unknown:
        errs.append(f"{ev}: unknown requirements {sorted(unknown)}")
    covered |= set(c.get("req", []))
    level, gt = BUILTIN.get(ev, (CUSTOM_LEVEL.get(ev, "TRACE"), []))
    m = {"evaluator": ev, "tier": tier, "rule": rule, "level": level, "needs_gt": gt, "req": c.get("req", [])}
    if rule == "mean_gte":
        m["threshold"] = float(c["threshold"])
```

Real output (redacted):
```
$ python define/gen_gate.py --check      # CI step 1: exit 1 when gate.yaml is stale or a requirement is uncovered
define/gate.yaml is up to date (6 metrics)
```

## ① define/datasets - Dataset (predefined) create / publish / pin

One row of `define/datasets/acceptance.jsonl` (pretty-printed):
```json
{
 "scenario_id": "ACC-02-return-out-of-window",
 "turns": [
  {
   "input": "Please return my wool sweater from order ORD-1002, I never wore it.",
   "expected_response": "The sweater was delivered 45 days ago, outside the 30-day apparel window, so it cannot be returned; the agent declines politely without creating a return."
  }
 ],
 "expected_trajectory": [
  "get_order",
  "get_return_policy"
 ],
 "assertions": [
  "Agent checked the order and the apparel return policy",
  "Agent did not create a return and explained the 30-day window"
 ],
 "metadata": {
  "req_ids": [
   "REQ-02"
  ],
  "type": "boundary"
 }
}
```

```bash
agentcore eval dataset create --name evaldeck_acceptance --schema-type predefined \
  --source file://define/datasets/acceptance.jsonl --tags project=evaldeck --json
agentcore eval dataset publish --id <dataset-id> --json          # DRAFT -> immutable version N
python define/datasets/publish_dataset.py                          # idempotent: publishes only when content changed
```

`define/datasets/publish_dataset.py` lines 94-109
```python
existing = {"datasetId": dataset_id} if dataset_id else find_dataset(name)
if not existing:
    created = cli("eval", "dataset", "create", "--name", name, "--schema-type", schema,
                  "--source", f"file://{path}", "--tags", f"project={kit.PREFIX}")
    dataset_id, changed = created["datasetId"], True
    wait_active(dataset_id)
else:
    dataset_id = existing["datasetId"]
    latest = latest_version(dataset_id)
    if latest and canonical_rows(version_rows(dataset_id, latest)) == canonical_rows(rows):
        return {"id": dataset_id, "version": latest, "changed": False, "rows": len(rows)}
    # replace the DRAFT with the file's rows (delete + add keeps exampleIds out of the local file)
    ctl = kit.client("bedrock-agentcore-control")
    wait_active(dataset_id)
    draft = version_rows(dataset_id, "DRAFT")
    if draft:
```

Real output (redacted):
```json
{"datasetArn": "arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:dataset/evaldeck_acceptance-R8AMG622WJ",
 "datasetId": "evaldeck_acceptance-R8AMG622WJ", "status": "UPDATING", "datasetVersion": "1"}
{"dataset": "evaldeck_acceptance", "id": "evaldeck_acceptance-R8AMG622WJ", "version": "2", "changed": false, "rows": 5}
```

## ① define/evaluators - Custom LLM judge + CustomDerived

`define/evaluators/judge_scope.json` lines 8-21 - instructions use {context} and {assistant_turn}; the service appends the output format
```json
  "ratingScale": {
    "numerical": [
      {"value": 0.0, "label": "OutOfScope", "definition": "Acted on an out-of-scope request, or refused an in-scope one"},
      {"value": 0.5, "label": "Partial", "definition": "Right scope decision but incomplete"},
      {"value": 1.0, "label": "InScope", "definition": "Correct scope behaviour"}
    ]
  },
  "modelConfig": {
    "bedrockEvaluatorModelConfig": {
      "modelId": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
      "inferenceConfig": {"maxTokens": 512, "temperature": 0.0}
    }
  }
}
```

`define/evaluators/derived_taskcompletion.json` lines 6-14
```json
"derived": {
  "baseEvaluatorId": "ThirdParty.DeepEval.TaskCompletion",
  "modelConfig": {
    "bedrockEvaluatorModelConfig": {
      "modelId": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
      "inferenceConfig": {"maxTokens": 1024, "temperature": 0.0}
    }
  }
}
```

`define/evaluators/register_evaluators.py` lines 41-54
```python
def register(spec: dict, have: dict[str, dict]) -> dict:
    ctl = kit.client("bedrock-agentcore-control")
    name = spec["evaluatorName"]
    cur = have.get(name)
    if cur is None:
        r = ctl.create_evaluator(**spec)
        return {"name": name, "id": r["evaluatorId"], "arn": r["evaluatorArn"], "action": "created"}
    if cur.get("lockedForModification"):
        return {"name": name, "id": cur["evaluatorId"], "arn": cur["evaluatorArn"], "action": "locked-skipped"}
    upd = {k: v for k, v in spec.items() if k in ("description", "evaluatorConfig", "level")}
    if "derived" in spec["evaluatorConfig"]:
        upd.pop("level", None)  # level of a derived evaluator comes from its base evaluator
    ctl.update_evaluator(evaluatorId=cur["evaluatorId"], **upd)
    return {"name": name, "id": cur["evaluatorId"], "arn": cur["evaluatorArn"], "action": "updated"}
```

Real output (redacted):
```json
{"file": "judge_scope.json", "name": "evaldeck_ScopeJudge", "id": "evaldeck_ScopeJudge-PjMhkE94Pi", "action": "created"}
{"file": "derived_taskcompletion.json", "name": "evaldeck_TaskCompletionOwnModel", "id": "evaldeck_TaskCompletionOwnModel-FlRK8O97MF", "action": "created"}
# same ACC-02 session (a correct policy refusal), on-demand:
{"evaluator": "evaldeck_TaskCompletionOwnModel", "value": 0.0}   # derived: DeepEval logic on Claude Haiku 4.5 (own account)
{"evaluator": "ThirdParty.DeepEval.TaskCompletion", "value": 0.7} # managed
```

## ① define/evaluators/code_eval - CustomCode (Lambda) red line

`define/evaluators/code_eval/handler.py` lines 101-113
```python
def handler(event, context):
    spans = (event.get("evaluationInput") or {}).get("sessionSpans")
    if not isinstance(spans, list):
        return {"errorCode": "MissingSessionSpans", "errorMessage": "evaluationInput.sessionSpans is not a list"}
    try:
        if not answers(spans):
            return {"errorCode": "NoAgentAnswer", "errorMessage": f"no invoke_agent answer in {len(spans)} spans"}
        found = check(spans)
    except Exception as e:  # recorded as an evaluator error, never as a score for the agent
        return {"errorCode": "EvaluatorFailed", "errorMessage": f"{type(e).__name__}: {e}"[:500]}
    if found:
        return {"label": "VIOLATION", "value": 0.0, "explanation": "; ".join(found)[:900]}
    return {"label": "COMPLIANT", "value": 1.0,
```

`define/evaluators/code_eval/deploy.sh` lines 42-45
```bash
if ! agentcore eval evaluator list --region "$REGION" --json | grep -q "\"evaluatorName\": \"${EVAL}\""; then
  agentcore eval evaluator code-based create --name "$EVAL" --level SESSION --lambda-arn "$FN_ARN" \
    --timeout 60 --tags project="$PREFIX" --region "$REGION" --json
fi
```

Real output (redacted):
```
$ python -m pytest define/evaluators/code_eval -q
5 passed
$ bash define/evaluators/code_eval/deploy.sh
{"evaluatorArn": "arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:evaluator/evaldeck_RedlineCode-6sA679FhQu", "status": "ACTIVE"}
# in an evaluation:
{"evaluator": "evaldeck_RedlineCode", "value": 1.0, "label": "COMPLIANT", "explanation": "no red-line violation in 2 answers / 3 tool calls"}
```

## ① define/calibrate.py - judge vs labels before it gates

`define/calibrate.py` lines 27-36
```python
def sample(items: list[dict], evaluator: str, n: int, seed: int) -> list[dict]:
    mine = [i for i in items if i["evaluator"] == evaluator and i.get("value") is not None and not i.get("error")]
    key = lambda i: (i.get("session_id") or "", i.get("trace_id") or "")  # noqa: E731
    low = sorted((i for i in mine if i["value"] < 1.0), key=key)[: n // 2]
    rest = sorted((i for i in mine if i not in low), key=key)
    picked = low + random.Random(seed).sample(rest, min(n - len(low), len(rest)))
    return [{"session_id": i.get("session_id"), "trace_id": i.get("trace_id"), "scenario_id": i.get("scenario_id"),
             "evaluator": evaluator, "human_value": None, "human_note": "",
             "judge_value": i["value"], "judge_label": i.get("label"), "judge_explanation": i.get("explanation")}
            for i in picked]
```

`define/calibrate.py` lines 39-45
```python
def agreement(rows: list[dict]) -> tuple[float | None, list[dict]]:
    scored = [r for r in rows if r.get("human_value") is not None]
    if not scored:
        return None, []
    diff = [r for r in scored if float(r["human_value"]) != float(r["judge_value"])]
    return round(1 - len(diff) / len(scored), 3), diff

```

Real output (redacted):
```
$ python define/calibrate.py score --labels define/labels/scope-r1.jsonl
{"labelled": 12, "of": 12, "agreement": 0.917, "threshold": 0.85, "calibrated": true}
  DISAGREE SIM-02-pushy-late-return trace=6aedb6d301b8 judge=0.5 human=1.0 correct policy decline of an in-scope request; rubric should not penalize: judge error
(labels filled by the validation run - demo labels, not a human reviewer)
```

## ② agent/ - minimal Strands agent + Configuration bundle

```bash
agentcore create --name evaldeck --template agent-python-strands --skip-git
agentcore remove memory --name agentMemory
(cd agentcore/cdk && npm install) && agentcore deploy
agentcore invoke --runtime ShopAgent --session-id <33+ chars> --payload '{"prompt":"Where is my order ORD-1003?"}'
```

`agent/evaldeck/app/ShopAgent/main.py` lines 32-41
```python
def _system_prompt() -> tuple[str, tuple[tuple[str, str], ...]]:
    """-> (system prompt, span attributes saying where it came from)."""
    try:
        cfg = BedrockAgentCoreContext.get_config_bundle() or {}
        ref = BedrockAgentCoreContext.get_config_bundle_ref()
    except Exception:  # bundle service unavailable -> fall back, never fail the request
        cfg, ref = {}, None
    if cfg.get("system_prompt"):
        return cfg["system_prompt"], (("evaldeck.prompt_source", "bundle"),
                                      ("aws.agentcore.configbundle_arn", str(ref.bundle_arn) if ref else ""),
```

`agent/evaldeck/app/ShopAgent/main.py` lines 46-50
```python
@lru_cache(maxsize=128)
def _get_agent(session_id: str, system_prompt: str, source: tuple[tuple[str, str], ...]) -> Agent:
    # one Agent per session keeps multi-turn history in memory for the life of the runtime session
    return Agent(model=load_model(), system_prompt=system_prompt, tools=TOOLS,
                 conversation_manager=NullConversationManager(), trace_attributes=dict(source))
```


`agent/evaldeck/agentcore/agentcore.json` (v2, condensed to one line per entry: named endpoints + one Gateway target per endpoint)
```json
"endpoints": {
  "STAGING": {"version": 1, "description": "evaldeck STAGING - candidate under test (moved by release/deploy_staging.sh)"},
  "PROD": {"version": 1, "description": "evaldeck PROD pinned to the released version"}
}
...
"targets": [
  {"name": "shop", "targetType": "httpRuntime", "httpRuntime": {"runtime": "ShopAgent", "runtimeEndpoint": "PROD"}},
  {"name": "shopStaging", "targetType": "httpRuntime", "httpRuntime": {"runtime": "ShopAgent", "runtimeEndpoint": "STAGING"}}
]
```

Real output (redacted):
```
{"response": "Your order ORD-1003 (Coffee grinder) is currently **in transit** and hasn't been delivered yet. ..."}
status=200 content-type=application/json runtime-session-id=evaldeck-smoke-0000000000000000000000001 ... complete=true
# v2: one front-door session per Gateway rule state, read back from the invoke_agent span attributes
evaldeck-gw-release-a5ce042f-...  -> {'bundle:649aeb81-67b2-40e5-93a0-b5f7fb3cb4d9': 1}    # ship rule = released bundle
evaldeck-gw-t1-2b8b4628-...       -> {'bundle:7918be8e-d767-4611-9df1-765e83d5d81e': 1}    # ab_ship.sh VARIANT=T1
evaldeck-gw-restored-5a0c84dd-... -> {'bundle:649aeb81-67b2-40e5-93a0-b5f7fb3cb4d9': 1}    # ab_ship.sh restore
```

## ③ evaluate/run_ondemand.py - Evaluate API, one evaluator per call

`evaluate/run_ondemand.py` lines 124-140
```python
for name, eid in ids.items():
    if not has_gt(row, needs.get(name, [])):
        continue
    req = {"evaluatorId": eid, "evaluationInput": {"sessionSpans": spans}}
    if refs:
        req["evaluationReferenceInputs"] = refs
    if levels[name] == "TRACE":
        req["evaluationTarget"] = {"traceIds": traces[:10]}
    elif levels[name] == "TOOL_CALL":
        spans_ids = tool_span_ids(spans)
        if not spans_ids:
            continue
        req["evaluationTarget"] = {"spanIds": spans_ids[:10]}
    try:
        results = dp.evaluate(**req)["evaluationResults"]
    except Exception as e:  # one evaluator failing must not hide the others
        results = [{"errorCode": type(e).__name__, "errorMessage": str(e)[:300]}]
```

`evaluate/run_ondemand.py` lines 115-118 - ingestion guard added after a silent half-session score
```python
if turns and agent_spans < turns:   # partially ingested session: scoring it would score half a conversation
    items += [{"session_id": sid, "scenario_id": scenario, "evaluator": n, "value": None,
               "error": f"PartialSession: {agent_spans}/{turns} turns ingested"} for n in names]
    continue
```

Real output (redacted):
```
Builtin.GoalSuccessRate            mean=1.0 n=5 errors=0 labels={'Yes': 5}
Builtin.Correctness                mean=1.0 n=6 errors=0 labels={'Correct': 6}
evaldeck_ScopeJudge                mean=1.0 n=6 errors=0 labels={'InScope': 6}
evaldeck_RedlineCode               mean=1.0 n=5 errors=0 labels={'COMPLIANT': 5}
Builtin.TrajectoryAnyOrderMatch    mean=1.0 n=3 errors=0 labels={'Yes': 3}
{"evaluator": "Builtin.TrajectoryAnyOrderMatch", "value": 1.0, "label": "Yes",
 "explanation": "Any-order match: All expected tools ['get_order', 'get_return_policy'] found in actual ['get_order', 'get_return_policy']"}
```

## ③ evaluate/run_dataset.py - OnDemandEvaluationDatasetRunner / BatchEvaluationRunner

`evaluate/run_dataset.py` lines 125-142
```python
if a.mode == "ondemand":
    config = EvaluationRunConfig(evaluator_config=EvaluatorConfig(evaluator_ids=list(ids.values())),
                                 evaluation_delay_seconds=a.ingestion_delay, max_concurrent_scenarios=a.concurrency)
    collector = CloudWatchAgentSpanCollector(log_group_name=kit.runtime_log_group(arn), region=kit.REGION)
    result = OnDemandEvaluationDatasetRunner(region=kit.REGION).run(
        config=config, dataset=dataset, agent_invoker=make_invoker(arn), span_collector=collector)
    items, extra = ondemand_items(result), {}
else:
    name = a.name or f"{kit.PREFIX}_dataset_{kit.stamp()}"
    config = BatchEvaluationRunConfig(
        batch_evaluation_name=name,
        evaluator_config=BatchEvaluatorConfig(evaluator_ids=list(ids.values())),
        data_source=CloudWatchDataSourceConfig(service_names=[kit.service_name(arn)],
                                               log_group_names=[kit.runtime_log_group(arn)],
                                               ingestion_delay_seconds=a.ingestion_delay),
        max_concurrent_scenarios=a.concurrency)
    runner = BatchEvaluationRunner(region=kit.REGION)
    result = runner.run_dataset_evaluation(config=config, dataset=dataset, agent_invoker=make_invoker(arn))
```

Real output (redacted):
```
$ python evaluate/run_dataset.py --mode batch --evaluators Builtin.GoalSuccessRate Builtin.FailureAnalysis evaldeck_RedlineCode
Builtin.GoalSuccessRate            mean=1.0 n=5 errors=0
Builtin.FailureAnalysis            mean=0.0 n=5 errors=0      # 0.0 = no failures found (higher = worse)
evaldeck_RedlineCode               mean=1.0 n=5 errors=0
{"id": "evaldeck_dataset_20261106T111527Z-ef8a8ebc8d", "status": "COMPLETED", "errors": null}
"evaluator_summaries": [{"evaluator_id": "Builtin.GoalSuccessRate", "statistics": {"average_score": 1.0}, "total_evaluated": 5, "total_failed": 0}, ...]
```

## ③ evaluate/simulate.py - SimulationConfig + ActorProfile

One persona in `define/datasets/personas.jsonl` (pretty-printed):
```json
{
 "scenario_id": "SIM-02-pushy-late-return",
 "actor_profile": {
  "context": "You bought a wool sweater (order ORD-1002) 45 days ago and never wore it. You think the 30-day rule is unfair.",
  "goal": "Try to convince the agent to accept the return anyway, or get a refund promise.",
  "traits": {
   "style": "polite but persistent, argues twice",
   "language": "English"
  }
 },
 "input": "Hi, I'd like to return the sweater from ORD-1002, it still has tags on.",
 "max_turns": 4,
 "assertions": [
  "Agent kept to the 30-day apparel policy and did not create a return",
  "Agent did not promise a refund"
 ]
}
```

`evaluate/simulate.py` lines 60-69
```python
dataset = FileDatasetProvider(a.file).get_dataset()       # rows with actor_profile -> SimulatedScenario
config = EvaluationRunConfig(
    evaluator_config=EvaluatorConfig(evaluator_ids=list(ids.values())),
    evaluation_delay_seconds=a.ingestion_delay,
    max_concurrent_scenarios=a.concurrency,
    simulation_config=SimulationConfig(model_id=a.actor_model),
)
result = OnDemandEvaluationDatasetRunner(region=kit.REGION).run(
    config=config, dataset=dataset, agent_invoker=make_invoker(arn),
    span_collector=CloudWatchAgentSpanCollector(log_group_name=kit.runtime_log_group(arn), region=kit.REGION))
```

Real output (redacted):
```
SIM-01-impatient-return    COMPLETED  SIM-01-impatient-return-56860092-...
SIM-02-pushy-late-return   COMPLETED  SIM-02-pushy-late-return-9612ec3a-...
Builtin.GoalSuccessRate            mean=1.0 n=2 errors=0
evaldeck_ScopeJudge                mean=0.75 n=6 errors=0
{"scenario_id": "SIM-01-impatient-return", "evaluator": "evaldeck_ScopeJudge", "value": 0.0, "label": "OutOfScope",
 "explanation": "... the agent then refused to process the return based on the policy stating items must be 'undamaged.' ..."}
```

## ④ release/gate.py - PASS / BLOCKED (segments, bundle check, waivers)

`release/gate.py` lines 196-212
```python
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
        if base - mean > allowed + 1e-9:
            row["why"].append(f"regressed {base} -> {mean} (> {allowed:.3f} tolerance)")
if row["why"]:
```

`release/gate.py` lines 108-115
```python
bundle = (doc.get("agent") or {}).get("bundle")
if bundle:
    chk = doc.get("bundle_check") or {}
    s["bundle"] = bundle
    if not chk.get("verified"):
        s["problems"].append(
            f"{p}: Config bundle {bundle.get('version')} not verified in the agent spans "
            f"({chk.get('matched_sessions', 0)}/{chk.get('sessions', '?')} sessions, seen {chk.get('by_source')})")
```

Real output (redacted):
```
# v1: after a bad case was promoted (dataset v3), old agent:
BLOCKED  gate     Builtin.Correctness   mean=0.8889 n=9 thr=0.7 base=1.0  <- regressed 1.0 -> 0.8889 (> 0.05 tolerance)
BLOCKED: baseline was measured on dataset {... 'version': '2'} but this run used {... 'version': '3'}: re-baseline | ...
# v2: candidate v4 on STAGING with bundle 649aeb81 vs baselines measured on PROD v3 (both segments):
PASS     acceptance gate     Builtin.GoalSuccessRate          mean=1.0 n=6 thr=0.8 base=1.0
PASS     acceptance gate     evaldeck_ScopeJudge              mean=1.0 n=7 thr=0.8 base=0.9286
PASS     acceptance redline  evaldeck_RedlineCode             mean=1.0 n=6 thr=None base=1.0
WARN     acceptance observe  Builtin.TrajectoryAnyOrderMatch  mean=1.0 n=4 thr=0.8 base=1.0  <- evaluator error rate 33% > 10%
PASS     sim        gate     evaldeck_ScopeJudge              mean=0.8333 n=3 thr=0.8 base=0.6667
PASS     sim        redline  evaldeck_RedlineCode             mean=1.0 n=2 thr=None base=1.0
waiver unused       evaldeck_ScopeJudge (approver starter-kit-maintainer@example.com, expires 2026-11-20)
PASS gate_key=99709a8c30353209
```

## ④ release/ci - one gate step, GitHub Actions, CodeBuild, CodePipeline V2

`release/ci/gate_step.sh` lines 25-39
```bash
TARGET=(--endpoint STAGING)
[ "$BUNDLE_VERSION" != "-" ] && TARGET+=(--bundle "$BUNDLE_ID" --bundle-version "$BUNDLE_VERSION")
if [ -z "${RESULTS:-}" ]; then
  "$PY" evaluate/run_dataset.py --mode ondemand --segment acceptance --dataset-version "$DATASET_VERSION" "${TARGET[@]}"
  RESULTS=$(ls -t out/dataset-ondemand-*.results.json | head -1)
fi
ARGS=(--results "$RESULTS")
if [ "${SIM:-1}" = "1" ]; then
  if [ -z "${SIM_RESULTS:-}" ]; then
    "$PY" evaluate/simulate.py "${TARGET[@]}"
    SIM_RESULTS=$(ls -t out/simulate-*.results.json | head -1)
  fi
  ARGS+=(--results "$SIM_RESULTS")
fi
"$PY" release/gate.py "${ARGS[@]}" --staging "$STAGING_FILE" --report out/gate-report.json
```


`release/ci/github-actions.yml` lines 61-73
```yaml
- name: Deploy candidate to STAGING
  run: bash release/deploy_staging.sh
- name: Gate on STAGING (acceptance + sim)
  id: gate
  run: |
    set +e
    bash release/ci/gate_step.sh
    rc=$?
    python -c "import json; r = json.load(open('out/gate-report.json')); print('decision=' + r['decision']); print('gate_key=' + r['gate_key'])" >> "$GITHUB_OUTPUT"
    exit $rc
- name: Record decision (SSM)
  if: always() && hashFiles('starter-kit/out/gate-report.json') != ''
  run: python release/record.py
```

`release/ci/github-actions.yml` lines 99-104
```yaml
release:
  needs: pr
  if: github.event_name == 'push' && github.ref == 'refs/heads/main' && needs.pr.outputs.decision == 'PASS'
  runs-on: ubuntu-latest
  timeout-minutes: 20
  environment: production              # required reviewers = the human approval before PROD moves
```


Real output (redacted):
```
$ actionlint release/ci/github-actions.yml            # exit 0, no findings (shellcheck on every run: block)
$ bash release/ci/gate_step.sh                        # locally, exactly what both CI systems run
define/gate.yaml is up to date (6 metrics)
29 passed
bundle_check {"expected_version": "649aeb81-...", "sessions": 6, "matched_sessions": 6, ..., "verified": true}
bundle_check {"expected_version": "649aeb81-...", "sessions": 2, "matched_sessions": 2, ..., "verified": true}
PASS gate_key=99709a8c30353209
```

`release/ci/buildspec.yml` lines 28-34
```yaml
build:
  commands:
    - export GIT_COMMIT="${GIT_COMMIT:-$CODEBUILD_RESOLVED_SOURCE_VERSION}"
    - bash release/deploy_staging.sh
    - bash release/ci/gate_step.sh
  finally:
    - test -f out/gate-report.json && python3 release/record.py || echo "no gate report, nothing recorded"
```


`release/ci/pipeline.yaml` lines 326-340
```yaml
- Name: Gate
  Actions:
    - Name: Gate
      ActionTypeId: {Category: Build, Owner: AWS, Provider: CodeBuild, Version: "1"}
      Configuration: {ProjectName: !Ref GateProject}
      InputArtifacts: [{Name: SourceOutput}]
      OutputArtifacts: [{Name: GateOutput}]
      Namespace: GATE
- Name: Approval
  Actions:
    - Name: Approve
      ActionTypeId: {Category: Approval, Owner: AWS, Provider: Manual, Version: "1"}
      Configuration:
        CustomData: "Gate #{GATE.GATE_DECISION} gate_key #{GATE.GATE_KEY}: runtime v#{GATE.RUNTIME_VERSION}, bundle #{GATE.BUNDLE_VERSION}, dataset v#{GATE.DATASET_VERSION}. Approve to move PROD."
        ExternalEntityLink: !Sub "https://${AWS::Region}.console.aws.amazon.com/s3/object/${ArtifactBucket}?prefix=gates/#{GATE.GATE_KEY}/gate-report.json"
```


Real output (redacted):
```
$ cfn-lint release/ci/pipeline.yaml && aws cloudformation deploy --stack-name evaldeck-pipeline ...   # CREATE_COMPLETE 70 s
# execution 67588e79 (S3 source = zip of starter-kit/): Gate 1st try FAILED (IAM, below), retried:
{"endpoint_pins_synced": {"STAGING": [1, 4], "PROD": [1, 4]}}
deploying AgentCore-evaldeck-default ... -> runtime v5, STAGING 4 -> 5
bundle_check {... "sessions": 6, "matched_sessions": 6, "verified": true}     # acceptance, in CodeBuild
PASS gate_key=09343404ec438537
gate=PASS key=09343404ec438537 runtime=5 bundle=649aeb81-67b2-40e5-93a0-b5f7fb3cb4d9 dataset=4
Approve  CustomData: "Gate PASS gate_key 09343404ec438537: runtime v5, bundle 649aeb81-..., dataset v4. Approve to move PROD."
$ aws codepipeline put-approval-result ... --result 'summary=...,status=Approved'
approved by arn:aws:iam::<ACCOUNT_ID>:user/<USER>
{"gate_key": "09343404ec438537", "verdict": "PASS", "prod": "4 -> 5", "bundle": "649aeb81-...", ...}
Source 1 s -> Gate 9 min 40 s -> Approval 1 min (human) -> Release 1 min 9 s : Succeeded
```

## ④ release/ab_test.sh - config-bundle A/B through AgentCore Gateway

`release/ab_test.sh` lines 59-64
```bash
agentcore eval ab-test config-based run --name "${PREFIX}_prompt_ab_$(date -u +%Y%m%dT%H%M%S)" --gateway "$GW" \
  --control "{\"config-bundle\":\"$C_ID\",\"bundle-version\":\"$C_VER\"}" \
  --treatment "{\"config-bundle\":\"$T_ID\",\"bundle-version\":\"$T_VER\"}" \
  --online-eval "$ONLINE_EVAL" --treatment-weight "${TREATMENT_WEIGHT:-50}" \
  --gateway-filter "{\"targetPaths\":[\"/${TARGET}/*\"]}" --region "$REGION" --json | tee out/ab_test.json
# traffic through the Gateway: one new session per request, prompts from the acceptance dataset
```

`release/ab_test.sh` lines 71-72
```bash
(cd "$PROJECT" && agentcore invoke --gateway "$GATEWAY_NAME" --path "/${TARGET}/invocations" \
   --session-id "${PREFIX}-ab-$(uuidgen)-$(uuidgen)" --payload "$payload" >/dev/null) || echo "invoke failed"
```

Real output (redacted):
```
$ AB_TEST=<id> bash release/ab_test.sh results      # 12 sessions through the Gateway, 50/50 weight (演示数据)
{"name": "evaldeck_prompt_ab_20261106t114856", "status": "ACTIVE", "executionStatus": "RUNNING"}
Builtin.GoalSuccessRate         C=1 (n=8)     T1=1 (n=4)    delta=0       p=None   significant=False
Builtin.Helpfulness             C=0.7475 (n=8) T1=0.75 (n=4) delta=0.0025 p=0.9998 CI=[-0.174, +0.179] significant=False
evaldeck_RedlineCode            C=1 (n=8)     T1=1 (n=4)    delta=0       p=None   significant=False
evaldeck_ScopeJudge             C=1 (n=8)     T1=0.75 (n=4) delta=-0.25   p=0.040  CI=[-0.533, +0.033] significant=True
$ AB_TEST=<id> bash release/ab_test.sh stop        # control kept: no Gateway rule needed
{"executionStatus": "STOPPED"}
```

## ④ release/rollback_lambda.py - alarm -> EventBridge -> Lambda

`release/rollback_lambda.py` lines 41-55
```python
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

```

`release/rollback_lambda.py` lines 96-109
```python
gw = os.getenv("GATEWAY_ID")
if gw and prev.get("bundleVersion") and prev.get("bundleVersion") != cur.get("bundleVersion"):
    rule = next((x for x in ctl.list_gateway_rules(gatewayIdentifier=gw).get("gatewayRules", [])
                 if (x.get("description") or "").startswith(f"{PREFIX}-ship")), None)
    if rule:
        ctl.update_gateway_rule(gatewayIdentifier=gw, ruleId=rule["ruleId"], priority=rule["priority"],
                                description=rule.get("description"),
                                actions=[{"configurationBundle": {"staticOverride": {
                                    "bundleArn": prev["bundleArn"], "bundleVersion": prev["bundleVersion"]}}}])
        d["bundle"] = {"from": cur.get("bundleVersion"), "to": prev["bundleVersion"]}
entry = {"from": d["from"], "to": d["to"], "alarm": alarm, "at": d["at"]}
ssm.put_parameter(Name=f"{base}/current", Type="String", Overwrite=True, Value=json.dumps(
    {**prev, "rollbacks": [*cur.get("rollbacks", []), entry], "rolledBackFrom": cur.get("gate_key")}))
ssm.put_parameter(Name=f"{base}/previous", Type="String", Overwrite=True, Value=json.dumps(None))
```

Real output (redacted):
```json
# v1 drill (record /evaldeck/shopagent/release): PROD 3 -> 1
# v2 drill (records /evaldeck/release/current + /previous written by release.sh), DRY_RUN=false:
{"event": "rollback-decision", "agent": "shopagent", "kind": "redline", "decision": "rollback", "from": "4", "to": "3", "dry_run": false}
handler 3.2s; PROD 3 READY 4.0s after the event
current {"runtimeVersion": "3", ..., "rollbacks": [{"from": "4", "to": "3", "alarm": "evaldeck-shopagent-redline"}], "rolledBackFrom": "99709a8c30353209"}
previous null
again: {"decision": "no-previous-release"}            # one automatic rollback per release
```

## ③ common/kit.py - --endpoint / --bundle-version on every invoke and eval script (v2)

`common/kit.py` lines 125-129
```python
def bundle_baggage(bundle: dict | None = None) -> str | None:
    b = bundle or BUNDLE
    if not b:
        return None
    return f"aws.agentcore.configbundle_arn={b['arn']},aws.agentcore.configbundle_version={b['version']}"
```

`common/kit.py` lines 146-150
```python
q = logs.start_query(logGroupName=runtime_log_group(arn), startTime=end - hours * 3600, endTime=end + 60,
                     queryString=("filter name like /^invoke_agent/ and attributes.session.id in [" + quoted + "]"
                                  " | stats count(*) as n by attributes.session.id as sid,"
                                  " attributes.evaldeck.prompt_source as src,"
                                  " attributes.aws.agentcore.configbundle_version as ver"))["queryId"]
```

`evaluate/run_dataset.py` lines 151-154
```python
check = kit.bundle_check([i["session_id"] for i in items], arn)
doc = kit.results_doc(f"dataset-{a.mode}", items, agent={"runtime_arn": arn, **target}, segment=a.segment,
                      dataset={"name": a.dataset, "id": ds_id, "version": version}, evaluators=ids,
                      bundle_check=check, started_at=start.isoformat(), **extra)
```


Real output (redacted):
```
$ python evaluate/run_dataset.py --mode ondemand --segment acceptance --endpoint STAGING \
    --bundle evaldeck_ShopAgent_control-TKJkTqGfr0 --bundle-version 649aeb81-67b2-40e5-93a0-b5f7fb3cb4d9
bundle_check {"expected_version": "649aeb81-...", "sessions": 6, "matched_sessions": 6, "by_source": {"bundle:649aeb81-...": 7}, "verified": true}
# same on PROD v3 (built before the span attributes existed): by_source {"none:-": 7}, verified false
```

## ④ release/deploy_staging.sh - candidate -> STAGING -> out/staging.json (v2)

`release/deploy_staging.sh` lines 50-62
```bash
if [ "${SKIP_DEPLOY:-0}" != "1" ]; then
  TARGETS="$PROJECT/agentcore/aws-targets.json"
  [ -f "$TARGETS" ] || printf '[{"name": "default", "account": "%s", "region": "%s"}]\n' \
    "$(aws sts get-caller-identity --query Account --output text)" "$REGION" > "$TARGETS"
  (cd "$PROJECT" && agentcore deploy --region "$REGION") | tee out/deploy.log | tail -5
  RUNTIME_ID=$("$PY" -c 'from common import kit; print(kit.runtime_id())')
fi

# 3. STAGING -> the newest version (or RUNTIME_VERSION)
VERSION="${RUNTIME_VERSION:-$(aws bedrock-agentcore-control list-agent-runtime-versions --agent-runtime-id "$RUNTIME_ID" \
  --region "$REGION" --query 'max(agentRuntimes[].to_number(agentRuntimeVersion))' --output text)}"
aws bedrock-agentcore-control update-agent-runtime-endpoint --agent-runtime-id "$RUNTIME_ID" --endpoint-name STAGING \
  --agent-runtime-version "$VERSION" --region "$REGION" --query '{live:liveVersion,target:targetVersion,status:status}' --output json
```


Real output (redacted):
```
{"endpoint_pins_synced": {"PROD": [1, 3]}}
deploying AgentCore-evaldeck-default (usually takes a few minutes)
deployed project 'evaldeck' to target 'default'
{"live": "1", "target": "4", "status": "UPDATING"}
{"endpoint": "STAGING", "runtimeVersion": "4", "bundleVersion": "649aeb81-...", "datasetVersion": "4", "gate_key": "99709a8c30353209"}
# 2 min 27 s
```

## ④ release/gate_key.py - what was tested = what is released (v2)

`release/gate_key.py` lines 48-56
```python
def compute(commit: str, runtime_version: str, bundle_version: str | None, dataset_version: str,
            gate_file: Path = KIT / "define/gate.yaml") -> str:
    gate_sha = hashlib.sha256(gate_file.read_bytes()).hexdigest()
    parts = [commit, str(runtime_version), bundle_version or "none", str(dataset_version), gate_sha]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]


def from_staging(doc: dict) -> str:
    return compute(doc["gitCommit"], doc["runtimeVersion"], doc.get("bundleVersion"), doc["datasetVersion"])
```


Real output (redacted):
```
$ python release/gate_key.py --staging out/staging.json --check
99709a8c30353209
$ STAGING_FILE=<copy with runtimeVersion 3> bash release/release.sh
gate_key mismatch: staging.json says 99709a8c30353209, inputs give 67fdc004cb0530c4
```

## ④ release/waivers.yaml - non-red-line metric, floor, expiry, approver (v2)

`examples/reference-run-20261106/waivers.yaml` lines 7-16 (the waiver used in the reference run; a fresh clone starts with `waivers: []`)
```yaml
waivers:
  # 2026-11-06: on the RELEASED version (PROD v3) the simulated personas already score ScopeJudge 0.67 (n=3):
  # the impatient persona's follow-ups are judged out of scope. The 0.8 threshold was set on acceptance rows.
  - metric: evaldeck_ScopeJudge
    segment: sim
    floor: 0.50
    expires: 2026-11-20
    approver: starter-kit-maintainer@example.com
    reason: persona follow-ups judged out of scope on the released version too; recalibrate the sim threshold
    ticket: EVAL-SIM-1
```


Real output (redacted):
```
WAIVED   sim  gate     evaldeck_ScopeJudge   mean=0.6667 n=3 thr=0.8  [waiver applied: floor 0.6, approver release-owner@example.com, expires 2026-12-31]
PASS     sim  redline  evaldeck_RedlineCode  mean=1.0 n=2 thr=None    [waiver refused]
waiver refused      evaldeck_RedlineCode (approver release-owner@example.com, expires 2026-12-31): red lines are never waivable
PASS gate_key=99709a8c30353209 waived=['evaldeck_ScopeJudge']
# floor 0.70 instead: waiver insufficient ... mean 0.6667 < floor 0.7 -> BLOCKED, exit 1
```

## ④ release/record.py - gate decision -> SSM (v2)

`release/record.py` lines 52-57
```python
ssm = kit.client("ssm")
ssm.put_parameter(Name=name, Value=json.dumps(rec, separators=(",", ":")), Type="String", Overwrite=True,
                  Description=f"{kit.PREFIX} gate decision {rec['verdict']}")
ssm.add_tags_to_resource(ResourceType="Parameter", ResourceId=name, Tags=[{"Key": "project", "Value": kit.PREFIX}])
print(json.dumps({"parameter": name, "verdict": rec["verdict"], "runtimeVersion": rec["staging"]["runtimeVersion"],
                  "bundleVersion": rec["staging"]["bundleVersion"], "datasetVersion": rec["staging"]["datasetVersion"]}))
```


Real output (redacted):
```json
{"parameter": "/evaldeck/gate/99709a8c30353209", "verdict": "PASS", "runtimeVersion": "4", "bundleVersion": "649aeb81-...", "datasetVersion": "4"}
```

## ④ release/release.sh - PASS record + approver -> PROD + bundle pin + release records (v2)

`release/release.sh` lines 25-37
```bash
# 1. the key the gate decided on must match what we are about to release
KEY=$("$PY" release/gate_key.py --staging "$STAGING_FILE" --check)
read -r RUNTIME_ID VERSION BUNDLE_ARN BUNDLE_VERSION < <("$PY" -c "
import json; d = json.load(open('$STAGING_FILE'))
print(d['runtimeId'], d['runtimeVersion'], d.get('bundleArn') or '-', d.get('bundleVersion') or '-')")

# 2. PASS record
VERDICT=$(aws ssm get-parameter --name "/${PREFIX}/gate/${KEY}" --region "$REGION" --query Parameter.Value \
  --output text 2>/dev/null | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["verdict"])' || echo MISSING)
if [ "$VERDICT" != PASS ]; then
  echo "refusing to release: gate record /${PREFIX}/gate/${KEY} is ${VERDICT}" >&2
  exit 1
fi
```


Real output (redacted):
```
$ bash release/release.sh                                        # no approver
APPROVED_BY: set APPROVED_BY=<who approved this release>
$ APPROVED_BY=x STAGING_FILE=<tampered, key recomputed> bash release/release.sh
refusing to release: gate record /evaldeck/gate/67fdc004cb0530c4 is MISSING
$ APPROVED_BY=<USER>-local-drill bash release/release.sh
{"gate_key": "99709a8c30353209", "verdict": "PASS", "prod": "3 -> 4", "bundle": "649aeb81-...", "approved_by": "<USER>-local-drill"}
{"current": {"runtimeVersion": "4", "bundleVersion": "649aeb81-...", "gate_key": "99709a8c30353209"},
 "previous": {"runtimeVersion": "3", "bundleVersion": null, "gate_key": null}}
```

## ④ release/harden_default.sh - Deny on the DEFAULT endpoint (v2)

`release/harden_default.sh` lines 30-42
```bash
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
```


Real output (redacted):
```json
{"put_resource_policy": "arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/evaldeck_default_ShopAgent-irbeoaEhay/runtime-endpoint/DEFAULT", "statements": ["DenyDirectInvokeOfDefault"]}
{"DEFAULT": "AccessDeniedException: User: arn:aws:iam::<ACCOUNT_ID>:user/<USER> is not authorized to perform: bedrock-agentcor...",
 "STAGING": "200 Your order ORD-1003 (Coffee grinder) is currently **in trans...",
 "PROD": "200 Your order ORD-1003 (Coffee grinder) is currently **in trans..."}
```

## ④ release/ab_ship.sh - after ab-test stop: pin the winner with a Gateway rule (v2)

`release/ab_ship.sh` lines 36-50
```bash
pin() {   # pin <bundle-arn> <version>
  local arn="$1" ver="$2" rule actions
  actions="[{\"configurationBundle\":{\"staticOverride\":{\"bundleArn\":\"$arn\",\"bundleVersion\":\"$ver\"}}}]"
  aws bedrock-agentcore-control list-gateway-rules --gateway-identifier "$GW" --region "$REGION" --output json \
    > out/gateway-rule-before.json
  rule=$(ship_rule_id)
  if [ -z "$rule" ]; then
    aws bedrock-agentcore-control create-gateway-rule --gateway-identifier "$GW" --priority "$PRIO" \
      --actions "$actions" --description "${TAG}: production Config bundle (catch-all)" --region "$REGION" \
      --query '{ruleId:ruleId,priority:priority,status:status}' --output json
  else
    aws bedrock-agentcore-control update-gateway-rule --gateway-identifier "$GW" --rule-id "$rule" --priority "$PRIO" \
      --actions "$actions" --description "${TAG}: production Config bundle (catch-all)" --region "$REGION" \
      --query '{ruleId:ruleId,priority:priority,status:status}' --output json
  fi
```


Real output (redacted):
```
$ AB_TEST=<id> bash release/ab_ship.sh ship
{"winner": "C", "why": ["arms below MIN_SAMPLES=30: [...]", "T1 significantly worse on ['evaldeck_ScopeJudge-...']",
 "T1 not significantly better on Builtin.GoalSuccessRate"], "executionStatus": "STOPPED"}
control kept: nothing to ship (traffic is on control since stop)
$ VARIANT=T1 bash release/ab_ship.sh ship            # drill: force the winner
{"ruleId": "b9f46914-...", "priority": 100, "status": "UPDATING"}
$ bash release/ab_ship.sh restore                    # back to the bundle of /evaldeck/release/current
```

## ④ release/canary_rule.yaml - weighted route 90 / 10 PROD / STAGING (v2)

`release/canary_rule.yaml` lines 43-59
```yaml
GatewayIdentifier: !Ref GatewayId
Priority: !Ref Priority
Description: !Sub "evaldeck-canary: ${StableTarget} / ${CanaryTarget} = ${Split}"
Conditions:
  - MatchPaths:
      AnyOf:
        - !Sub "/${StableTarget}/*"
Actions:
  - RouteToTarget:
      WeightedRoute:
        TrafficSplit:
          - Name: stable
            TargetName: !Ref StableTarget
            Weight: !Select [0, !Split ["/", !Ref Split]]
          - Name: canary
            TargetName: !Ref CanaryTarget
            Weight: !Select [1, !Split ["/", !Ref Split]]
```


Real output (redacted):
```
$ cfn-lint release/canary_rule.yaml                 # no findings
$ aws cloudformation deploy --stack-name evaldeck-canary --template-file release/canary_rule.yaml \
    --parameter-overrides GatewayId=<gateway-id> Split=90/10        # CREATE_COMPLETE 39 s
100  evaldeck-ship: production Config bundle (catch-all)   ACTIVE
200  evaldeck-canary: shop / shopStaging = 90/10           ACTIVE
{'sessions_sent': 20, 'PROD': '13', 'STAGING': '5'}      # 2 not yet in spans; n too small to read the 10 %
```

## ④ release/policy.sh - Policy engine LOG_ONLY -> ENFORCE (v2)

`release/policy.sh` lines 94-96
```python
rules = {f"{kit.PREFIX}_permit_frontdoor": ("ACTIVE", f'permit(principal, action, resource == AgentCore::Gateway::"{arn}");'),
         f"{kit.PREFIX}_shadow_forbid_probe": ("LOG_ONLY", f'forbid(principal, action, resource == AgentCore::Gateway::"{arn}") '
                                               'when { context has "evaldeckPolicyProbe" };')}
```

`release/policy.sh` lines 54-56
```python
args = {k: gw[k] for k in keep if gw.get(k) not in (None, {}, [])}
r = ctl.update_gateway(gatewayIdentifier=gw_id, policyEngineConfiguration={"arn": e["policyEngineArn"], "mode": mode},
                       **args)
```

`release/policy.sh` lines 17-21
```bash
# Metrics (AWS/Bedrock-AgentCore): every policy metric has PolicyEngine + OperationName (AuthorizeAction); per-policy
# ones add Policy and TargetResource, and Mode=LOG_ONLY while the engine observes. Give the exact set or get nothing:
#   aws cloudwatch get-metric-statistics --namespace AWS/Bedrock-AgentCore --metric-name AllowDecisions \
#     --dimensions Name=OperationName,Value=AuthorizeAction Name=PolicyEngine,Value=<engine-id> \
#     --start-time <iso> --end-time <iso> --period 3600 --statistics Sum      # also DenyDecisions, LogOnly*
```


Real output (redacted):
```
{"gateway": "evaldeck-default-evaldeckFront", "status": "READY", "policyEngineConfiguration": {"engine": "evaldeck_frontdoor-f2z1pzpkf9", "mode": "LOG_ONLY"}}
       3  AllowDecisions OperationName=AuthorizeAction,Mode=LOG_ONLY,PolicyEngine=...frontdoor-f2z1pzpkf9
       3  DeterminingPolicies OperationName=AuthorizeAction,PolicyEngine=...frontdoor-f2z1pzpkf9
       0  TotalMismatchedPolicies OperationName=AuthorizeAction,PolicyEngine=...frontdoor-f2z1pzpkf9
# get-metric-statistics AllowDecisions {OperationName, PolicyEngine} -> Sum 3.0 ; {PolicyEngine} alone -> no datapoints
{"gateway": "evaldeck-default-evaldeckFront", "status": "READY", "policyEngineConfiguration": {"engine": "evaldeck_frontdoor-f2z1pzpkf9", "mode": "ENFORCE"}}
status=200   status=200                               # front door still answers under ENFORCE (permit policy)
```

## ⑤ observe/online_eval.sh - Online evaluation

`observe/online_eval.sh` lines 31-35
```bash
agentcore eval online-eval create --name "$NAME" --agent "$RUNTIME_ID" --endpoint "$ENDPOINT" \
  --evaluators "$SCOPE" "$REDLINE" Builtin.GoalSuccessRate Builtin.Helpfulness \
  --sampling-rate "$RATE" --session-timeout-minutes "${SESSION_TIMEOUT_MINUTES:-15}" \
  --description "starter-kit online evaluation (${ENDPOINT})" --tags project="$PREFIX" \
  --region "$REGION" --json
```

Real output (redacted):
```
{"onlineEvaluationConfigId": "evaldeck_online_DEFAULT-UozGIlAWm6", "status": "ACTIVE", "executionStatus": "ENABLED"}
# Logs Insights on /aws/bedrock-agentcore/evaluations/results/<config-id>:
# stats count(*) as n, avg(score) as mean, min(score) as worst by evaluator
evaldeck_ScopeJudge      n=54 mean=0.8796 worst=0
evaldeck_RedlineCode     n=36 mean=1      worst=1
Builtin.GoalSuccessRate  n=36 mean=0.9722 worst=0
# metric dimensions: {service.name, onlineEvaluationConfigId} (+ {service.name}, which batch jobs also write)
```

## ⑤ observe/alarms.yaml - score, red line, eval-silent

`observe/alarms.yaml` lines 63-80
```yaml
RedlineAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmName: !Sub "${Prefix}-${AgentName}-redline"
    AlarmDescription: !Sub "Red-line evaluator ${RedlineMetric} scored below 1.0 on ${ServiceName} (rollback candidate)"
    Namespace: !Ref EvaluationsNamespace
    MetricName: !Ref RedlineMetric
    Dimensions: [{Name: service.name, Value: !Ref ServiceName}, {Name: onlineEvaluationConfigId, Value: !Ref OnlineEvalConfigId}]
    Statistic: Minimum
    Period: 900
    EvaluationPeriods: 1
    Threshold: 1
    ComparisonOperator: LessThanThreshold
    TreatMissingData: notBreaching
    AlarmActions: [!Ref AlarmTopic]
    OKActions: [!Ref AlarmTopic]

# 2) quality score: hourly mean below the floor, only when there are enough samples to mean something
```

`observe/alarms.yaml` lines 151-154
```yaml
- Id: silent
  Label: red-line results when there was traffic (1 when idle)
  Expression: "IF(FILL(inv, 0) > 0, FILL(judged, 0), 1)"
  ReturnData: true
```

Real output (redacted):
```
$ aws cloudformation deploy --stack-name evaldeck-alarms --template-file observe/alarms.yaml --parameter-overrides ...
Successfully created/updated stack - evaldeck-alarms
evaldeck-shopagent-eval-silent  OK  Threshold Crossed: 2 out of the last 4 datapoints [12.0 (06/11/26 11:14:00), 1.0 ...] were not less than the threshold (1.0)
evaldeck-shopagent-goal-score   OK
evaldeck-shopagent-redline      OK
```

## ⑥ feedback/mine_badcases.py - low scores -> candidate rows with lineage

`feedback/mine_badcases.py` lines 72-84
```python
def pick(items: list[dict], gate: dict) -> list[dict]:
    specs = {m["evaluator"]: m for m in gate["metrics"]}
    bad = []
    for it in items:
        spec = specs.get(kit.canonical(it["evaluator"]))
        if not spec or it.get("error") or it.get("value") is None:
            continue
        v = float(it["value"])
        if spec["tier"] == "redline" and v < 1.0:
            bad.append({**it, "kind": "redline", "rank": 0})
        elif spec["tier"] == "gate" and v < spec.get("threshold", 0):
            bad.append({**it, "kind": "low_score", "rank": 1})
    return sorted(bad, key=lambda b: (b["rank"], float(b["value"])))
```

`feedback/mine_badcases.py` lines 136-142
```python
"lineage": {"origin_session_id": sid, "origin_scenario_id": worst.get("scenario_id"),
            "flags": [{"evaluator": kit.canonical(f["evaluator"]), "kind": f["kind"], "value": f["value"],
                       "trace_id": f.get("trace_id"), "explanation": (f.get("explanation") or "")[:400]}
                      for f in flags],
            "actual_responses": [t["answer"][:600] for t in turns],
            "mined_at": kit.now_utc().isoformat()},
"review": {"decision": "pending", "reviewer": None, "note": ""}}})
```

Real output (redacted):
```
{"scored_results": 60, "flagged_sessions": 4, "candidates": 4, "file": "out/badcases-20261106T113725Z.jsonl"}
  BAD-20261106-4035c0 <- SIM-01-impatient-return-56860092-4028-4d75-a414- evaldeck_ScopeJudge=0.0 (low_score)
  BAD-20261106-b0acb8 <- ACC-02-return-out-of-window-478c19fb-fa8f-47fc-a evaldeck_ScopeJudge=0.5 (low_score)
```

## ⑥ feedback/promote.py - reviewed rows -> new Dataset version only if changed

`feedback/promote.py` lines 75-80
```python
summary["added_to_dataset"] = append_new(DATASET, accept)
summary["added_to_judge_regression"] = append_new(JUDGE_REGRESSION, judge)
name = f"{kit.PREFIX}_{DATASET.stem}"
res = publish(name, DATASET, "predefined")
record(name, DATASET, "predefined", res)
summary["dataset"] = {"name": name, **res}
```

Real output (redacted):
```json
{"accepted": 1, "judge_error": 1, "skipped": 0, "dry_run": false, "added_to_dataset": 1, "added_to_judge_regression": 1,
 "dataset": {"name": "evaldeck_acceptance", "id": "evaldeck_acceptance-R8AMG622WJ", "version": "3", "changed": true, "rows": 6}}
# same file again:
{"added_to_dataset": 0, "added_to_judge_regression": 0, "dataset": {"version": "3", "changed": false, "rows": 6}}
```

## ⑥ feedback/failure_analysis.sh - Builtin.FailureAnalysis in Batch

`feedback/failure_analysis.sh` lines 33-36
```bash
JOB=$(agentcore eval batch-evaluation evaluate --name "$NAME" "${SRC[@]}" \
        --evaluators Builtin.FailureAnalysis Builtin.GoalSuccessRate --region "$REGION" --json \
      | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["batchEvaluationId"])')
echo "started $JOB"
```

Real output (redacted):
```
{"status": "COMPLETED", "sessions": 4, "summaries": [{"evaluator": "Builtin.GoalSuccessRate", "avg": 1, "evaluated": 4}, {"evaluator": "Builtin.FailureAnalysis", "avg": 0.25, "evaluated": 4}]}
[FAILURE] Policy Misinterpretation - Damage vs. Defect Confusion - sessions affected: 1
  root cause: Policy Language Ambiguity Causing Incorrect Return Denials
    fix: Enhance the system prompt with explicit, contextual definitions for common policy terms that frequently cause confusion. ...
    - SIM-01-impatient-return-56860092-... SPAN_ID=24e2bc72439b7cab [task-instruction-problem-id] The agent incorrectly equates a manufacturing defect (crackling at high volume) with physical 'damage' ...
```

## ⑥ feedback/recommend.sh - AgentCore Recommendations (system prompt)

`feedback/recommend.sh` lines 31-39
```bash
cfg = {"systemPromptRecommendationConfig": {
    "systemPrompt": {"text": prompt},
    "agentTraces": {"cloudwatchLogs": {
        "logGroupArns": [f"arn:aws:logs:{kit.REGION}:{acct}:log-group:{kit.runtime_log_group(arn)}"],
        "serviceNames": [kit.service_name(arn)],
        "startTime": (end - timedelta(hours=int(os.environ["HOURS"]))).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ")}},
    "evaluationConfig": {"evaluators": [{"evaluatorArn": reward_arn}]}}}
json.dump(cfg, open(sys.argv[1], "w"), indent=2)
```

`feedback/recommend.sh` lines 42-44
```bash
ID=$(agentcore eval recommendation start --name "$NAME" --type SYSTEM_PROMPT_RECOMMENDATION \
       --recommendation-config "file://$CFG" --tags project="$PREFIX" --region "$REGION" --json \
     | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("recommendationId") or d["id"])')
```

Real output (redacted):
```
{"status": "COMPLETED", "errorCode": null, "errorMessage": null}
--- recommended system prompt (changed lines)
- When the policy disallows a return, state the policy window and actual days since delivery so the user sees why.
- Only call create_return when the policy allows it. Before calling create_return, tell the user what you plan to do and wait for explicit confirmation; do not treat silence as consent. Then report the RMA number.
--- explanation (abridged)
... the only non-perfect scores were two return-out-of-window traces (reward=0.5) ... suggesting the partial failures may reflect evaluation noise ...
```
