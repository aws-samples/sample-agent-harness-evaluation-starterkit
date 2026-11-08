# Agent-DLC starter-kit: AgentCore Evaluations / Optimization in six steps

Runnable reference scripts for the self-improving loop ① Define ② Build ③ Evaluate ④ Release ⑤ Observe ⑥ Feedback on
Amazon Bedrock AgentCore. Every script below was run end to end against a real AgentCore Runtime agent in a test
account on **2026-11-06** (us-east-1, AgentCore CLI 1.0.1, bedrock-agentcore SDK 1.25.3), and the production release path
(STAGING / PROD endpoints, Config bundle per request, gate -> SSM record -> release -> rollback, DEFAULT Deny, waivers,
Gateway ship / canary rules, Policy LOG_ONLY -> ENFORCE, CodePipeline) was added and run the same day ("v2"); the lessons from those runs are in the script headers and in
"Things the run taught" below. The demo agent and its data are fictional (演示数据).

## 与 lifecycle 参考实现的关系

starter-kit 是在 [ac-evaluation-golden-dataset-demo / lifecycle](https://github.com/huanghang111/ac-evaluation-golden-dataset-demo/tree/main/lifecycle)
的基础上重新编写的模板工程: clone 之后按「从零运行」一节的顺序执行, 就能在自己的账号里跑通六步。
lifecycle 收尾的「从零搭建清单」把参考实现分成两部分: 平台通用的部分 (流水线、门禁、发布、回滚、回流脚本) 和每个 Agent
自己要写的部分 (PRD、数据集、判官、后端), 并把它作为 starter project 的需求。starter-kit 按这个要求实现: 平台部分做成可复用的脚本,
Agent 部分只留一个最小骨架。需要更多细节时, 到 lifecycle 中查看。

| | lifecycle (参考实现) | starter-kit (本目录) |
|---|---|---|
| 定位 | 完整的参考实现与讲解材料: 客户版 / 开发者版 deck、HTML 站点、field notes | 精简、可复用的模板工程, 配合技术实践版 PPT 讲解 |
| Agent | 3 个 (电商客服 / 证券投顾 / 智能家居), 真实后端 | 1 个 ShopAgent, mock 工具 |
| 轮次 | 0→1 首发 + R1 (提示词) / R2 (工具与后端) / R3 (经验库 + 策略), 有台账和每次门禁的记录 | 一轮完整闭环 + 生产发布路径 (v2) |
| 数据集 | acceptance、release_gate、redteam、simulated_users、dev_smoke、prod_badcase、judge_regression, 带血缘 | acceptance、personas、judge_regression |
| 版本 | AgentCore CLI 1.0.0-rc.5 (2026-11-04) | AgentCore CLI 1.0.1 / SDK 1.25.3, 2026-11-06 全部实测 |

只在 lifecycle 中有、需要时去那里看:

- 三段门禁 (gate / sim / redteam), 红队段红线 0 违规是硬门槛
- 每个 Agent 一条 CI + 一条 CD 流水线 (CDK), 部署时钉版本、加部署锁、写部署记录
- 告警 + 安全网回滚 Lambda + eval-silent 复合告警 (`ops-stack.ts`)、网关 TRACES 投递、前门网关栈
- Cedar 策略从 LOG_ONLY 到 ENFORCE 的完整过程 (两个 Agent 都切到了 ENFORCE)
- 经验库: Memory EPISODIC + 召回钩子, 用消融实验验证
- bad case 的 AI 预审 → 复核 → 发布 → 晋级, 行里带回线上会话的 id
- IDE 内环 (Dev Server、Trace Explorer、托管提示词 + Playground、Experiments)、Vibe Coding 实录、Omni Web 截图与录屏
- 多轮的五维趋势、成本与延迟、线上结果、时间线, 以及按 FN 编号整理的踩坑记录

starter-kit 相对 lifecycle 新增或改进的:

- 不写死账号、区域、profile 和 Agent 列表, 资源名前缀用 `NAME_PREFIX` 配置 (lifecycle 清单里列出的局限)
- CustomDerived 评估器 (`ThirdParty.DeepEval.TaskCompletion`)、`Builtin.FailureAnalysis` 根因分析、`run_ondemand.py` 的数据摄取检查
  (会话结束不到 5 分钟就拒绝打分)、GitHub Actions 版 CI、实际部署的 90/10 canary GatewayRule
- 两边都有的机制 (gate_key、bundle 版本校验、豁免、DEFAULT Deny、A/B 发布、Policy LOG_ONLY → ENFORCE) 各收成一个脚本,
  并给出每一步的最小 IAM; 两边写法不同时以 starter-kit 为准

## Setup

```bash
npm i -g @aws/agentcore                                  # AgentCore CLI 1.0.x, Node >= 20.12
uv venv --python 3.12 .venv && uv pip install -p .venv -r requirements.txt   # or: python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
(cd agent/evaldeck/agentcore/cdk && npm ci)              # CDK app dependencies (node_modules is not committed)
export AWS_PROFILE=<profile> AWS_REGION=us-east-1        # nothing in the kit hard-codes an account, profile or region
export PYTHON=.venv/bin/python                           # used by the .sh scripts
```
Tools: Node.js >= 20.12, [uv](https://docs.astral.sh/uv/) (local venv; `agentcore create` also checks for it), AWS CLI v2, `zip`.
Keep `NAME_PREFIX` at `evaldeck` unless you also rename the project in `agent/evaldeck` and the evaluator names in
`define/requirements.yaml` / `define/gen_gate.py`.
Prerequisites in the account: Bedrock model access (the kit uses Claude Haiku 4.5 for agent, judges and the simulated
user), CloudWatch Transaction Search (`agentcore deploy` enables it). Optional env: `AGENT_RUNTIME_ARN` (else read
from `agentcore status`), `AGENT_ENDPOINT` (`DEFAULT` | `STAGING` | `PROD`, default `STAGING`; also `--endpoint`),
`BUNDLE_ID` + `BUNDLE_VERSION` (Config bundle version sent as baggage; also `--bundle` / `--bundle-version`),
`GATEWAY_ID`, `NAME_PREFIX` (default `evaldeck`), `EVAL_OUT` (`out/`), `EVAL_CONCURRENCY` (macOS: `1`, see below).
Every script writes its results to `out/` in one normalized format (`evaldeck.results/v1`) that `release/gate.py` and
`feedback/mine_badcases.py` read.

## 从零运行 (clone 之后)

一个干净的 clone 不带任何账号状态: `define/datasets/registry.json` 是 `{}`, 没有 `release/baseline.json`,
`release/waivers.yaml` 为空; 这些都由下面的命令在你的账号里生成。参考运行留下的同名文件在
`examples/reference-run-20261106/`, 只供阅读。以下顺序在 2026-11-07 用一个全新的 clone 在另一个区域 (us-west-2)
从头跑过一遍 (先完成上面的 Setup):

```bash
# ① 定义: 门禁配置、数据集 v1、三个自定义评估器
$PYTHON define/gen_gate.py --check
$PYTHON define/datasets/publish_dataset.py                 # -> registry.json (dataset id + version 1)
$PYTHON define/evaluators/register_evaluators.py           # ScopeJudge (LLM judge) + TaskCompletionOwnModel (derived)
bash define/evaluators/code_eval/deploy.sh                 # RedlineCode (Lambda)
git add define/datasets/registry.json && git commit -m "dataset v1"

# ② 构建: 部署 Agent (首次部署建 STAGING / PROD 两个端点, 都在 version 1, 和前门 Gateway)
(cd agent/evaldeck && agentcore deploy)

# ③ 评估 (on-demand 需要会话结束 5 分钟后再评)
$PYTHON evaluate/send_traffic.py --tag acc --endpoint STAGING
$PYTHON evaluate/run_dataset.py --mode ondemand --segment acceptance --endpoint STAGING
$PYTHON evaluate/simulate.py --endpoint STAGING
$PYTHON evaluate/run_ondemand.py --sessions "$(ls -t out/sessions-acc-*.json | head -1)"
$PYTHON evaluate/run_dataset.py --mode batch --evaluators Builtin.GoalSuccessRate Builtin.FailureAnalysis

# ④ 发布: 先在 PROD (已发布版本) 上测基线并提交, 再部署候选、跑门禁、记录、发布
$PYTHON evaluate/run_dataset.py --mode ondemand --segment acceptance --endpoint PROD
$PYTHON evaluate/simulate.py --endpoint PROD
$PYTHON release/gate.py --results "$(ls -t out/dataset-ondemand-*.results.json | head -1)" \
    --results "$(ls -t out/simulate-*.results.json | head -1)" --write-baseline
git add release/baseline.json && git commit -m "baseline on PROD"
bash release/deploy_staging.sh                             # -> out/staging.json (gate_key)
bash release/ci/gate_step.sh                               # PASS / BLOCKED, 与 CI 跑的是同一个脚本
$PYTHON release/record.py                                  # SSM /evaldeck/gate/<gate_key>
APPROVED_BY=<you@example.com> PIN_BUNDLE=0 bash release/release.sh   # 有 Config bundle 时设 GATEWAY_ID 并去掉 PIN_BUNDLE=0
bash release/harden_default.sh                             # DEFAULT 端点拒绝直接调用
DRY_RUN=1 bash release/protect_prod.sh                     # 先看 stack policy, 确认后去掉 DRY_RUN 执行一次

# ⑤ 观测: PROD 上挂 Online 评估和告警
SAMPLING_RATE=100 ENDPOINT=PROD bash observe/online_eval.sh     # 演示用 100 %, 生产按流量调低
aws cloudformation deploy --stack-name evaldeck-alarms --template-file observe/alarms.yaml \
    --parameter-overrides ServiceName=<runtime-name>.PROD OnlineEvalConfigId=<config-id> \
    RuntimeArn=<runtime-arn> RuntimeMetricName=<runtime-name>::PROD
$PYTHON evaluate/send_traffic.py --tag prod --endpoint PROD      # Online 结果约 15 分钟后到

# ⑥ 回流: 低分 -> bad case 候选 -> 复核 -> 新数据集版本; 根因与提示词建议
$PYTHON feedback/mine_badcases.py --results "$(ls -t out/simulate-*.results.json | head -1)"
$PYTHON feedback/mine_badcases.py --online-config <config-id> --hours 3      # Online 结果 (PROD)
$PYTHON feedback/promote.py --reviewed feedback/reviewed/badcases-20261106-r1.jsonl   # 示例复核记录, 已在数据集里: 不发新版本
ENDPOINT=STAGING SESSION_IDS="<sid> ..." bash feedback/failure_analysis.sh   # ENDPOINT = 这些会话所在的端点 (默认 PROD)
HOURS=6 REWARD=evaldeck_ScopeJudge bash feedback/recommend.sh
```

要点:

- `gate_key` 绑定 git commit, 所以 `registry.json` 和 `baseline.json` 要在 `deploy_staging.sh` 之前提交;
  之后再提交会让 `release.sh` 的 `gate_key` 校验失败 (需要重跑 `deploy_staging.sh` + 门禁)。
- 数据集发了新版本 (例如 `promote.py` 晋级了新的 bad case) 之后, 门禁会因为基线测在旧版本上而 BLOCKED,
  按上面 ④ 的前三行在 PROD 上重测基线。
- clone 测试里还做了一次带代码改动的发布: 新 runtime version 2 -> STAGING -> 门禁 PASS -> PROD 1 -> 2, PROD 在部署过程中保持不变。
- 门禁的 sim 段里 `evaldeck_ScopeJudge` 只观测不拦截 (`requirements.yaml` 的 `segment_tiers: {sim: observe}`):
  模拟用户会故意反复追问, 判官把这些追问轮打 0.5, 4 轮的均值稳定在 0.75。GoalSuccessRate 和红线在 sim 段照常拦截。

### 可选: A/B、金丝雀、AgentCore Policy、流水线 (2026-11-07 在同一个 clone 里都跑过)

```bash
# A/B: 当前提示词 (control) vs Recommendations 的候选 (treatment), 走前门 Gateway, Online 评估打分
HOURS=6 REWARD=evaldeck_ScopeJudge bash feedback/recommend.sh            # -> out/recommended_prompt.txt
TREATMENT_PROMPT_FILE=out/recommended_prompt.txt ONLINE_EVAL=<config-id> SESSIONS=12 bash release/ab_test.sh start
AB_TEST=<ab-test-id> bash release/ab_test.sh results                     # 约 30 分钟后两组都有结果
AB_TEST=<ab-test-id> bash release/ab_test.sh stop
AB_TEST=<ab-test-id> bash release/ab_ship.sh ship                        # 样本不够 / 不显著 -> 保持 control, 不建规则
bash release/ab_ship.sh show                                             # 演练可用 VARIANT=T1 强制钉住 treatment

# 金丝雀: 前门 90 / 10 分给 PROD / STAGING 两个 target
aws cloudformation deploy --stack-name evaldeck-canary --template-file release/canary_rule.yaml \
    --parameter-overrides GatewayId=<gateway-id> Split=90/10              # 撤回: delete-stack

# 自动回滚: 告警 -> EventBridge -> Lambda -> PROD 回到 /<prefix>/release/previous (每次发布最多一次)
DRY_RUN=true bash release/deploy_rollback.sh                     # 先只记录判定
aws cloudwatch set-alarm-state --alarm-name evaldeck-shopagent-redline --state-value ALARM --state-reason drill
aws logs tail /aws/lambda/evaldeck-rollback --since 10m          # {"event": "rollback-decision", ...}
DRY_RUN=false bash release/deploy_rollback.sh                    # 确认判定正确后再打开

# AgentCore Policy: 引擎先 LOG_ONLY 观测, 审计无误伤再 ENFORCE
bash release/policy.sh setup && bash release/policy.sh observe
# ... 走前门发一些流量, 等 5-10 分钟指标
HOURS=1 bash release/policy.sh audit && bash release/policy.sh enforce

# CodePipeline (S3 源): Source -> DeployStaging -> Gate -> 人工审批 -> Release
aws cloudformation deploy --stack-name evaldeck-pipeline --template-file release/ci/pipeline.yaml \
    --capabilities CAPABILITY_IAM --parameter-overrides AgentRuntimeArn=<runtime-arn> GatewayId=<gateway-id> \
    CentralAccessLogBucket=<existing org access-log bucket>    # 可选: 让模板自己的访问日志桶也记日志
git archive -o /tmp/kit.zip HEAD && aws s3 cp /tmp/kit.zip s3://<SourceBucket>/evaldeck-kit.zip
aws codepipeline start-pipeline-execution --name evaldeck-release     # 审批: 控制台或 put-approval-result

# GitHub Actions: 三个 OIDC 角色, 仓库 secrets / vars, production environment (required reviewers)
aws cloudformation deploy --stack-name evaldeck-github-oidc --template-file release/ci/github-oidc-roles.yaml \
    --capabilities CAPABILITY_IAM --parameter-overrides GitHubRepository=<owner>/<repo> AgentRuntimeArn=<runtime-arn> \
    GitHubOwnerId=<id> GitHubRepositoryId=<id>    # ids: gh api repos/<owner>/<repo>/actions/oidc/customization/sub
# GitHub environments: staging (pr job; required reviewers recommended) and production (release job)
gh secret set AWS_DEPLOY_ROLE_ARN -b <DeployRoleArn>; gh secret set AWS_ROLE_ARN -b <GateRoleArn>
gh secret set AWS_RELEASE_ROLE_ARN -b <ReleaseRoleArn>
gh variable set AWS_REGION -b <region>; gh variable set AGENT_RUNTIME_ARN -b <runtime-arn>
# Settings -> Environments -> production -> Required reviewers (私有仓库需要 GitHub Team / Enterprise)
mkdir -p .github/workflows && cp release/ci/github-actions.yml .github/workflows/agent-eval.yml
```

### 安全扫描: Amazon Inspector (依赖, CI 里) 与 AWS Continuum (代码, 发布前)

```bash
# Amazon Inspector: SBOM (inspector-sbomgen, 仅 Linux) -> ScanSbom -> 有 HIGH 及以上即 exit 1
# CI 里是部署步骤的第一步 (buildspec-deploy.yml / github-actions.yml pr 作业), 有漏洞就什么都不部署
SECURITY_FAIL_ON=HIGH bash release/ci/security_scan.sh           # -> out/security-report.json; macOS 上请在 CI 或 Linux 容器里跑

# AWS Continuum (可选, 默认不启用; 会产生费用): 代码 zip + 业务上下文放进 S3, 验证漏洞是否可利用, 确认的漏洞变成 issue 并生成补丁
# setup / scan 需要显式打开: CONTINUUM_ENABLED=1 (或 --enable); CI 里不调用这个脚本
CONTINUUM_ENABLED=1 $PYTHON release/security/continuum_scan.py setup   # 栈 evaldeck-continuum + 集成 + 环境 (只手动扫描), 并开始第一次扫描
$PYTHON release/security/continuum_scan.py wait                  # 等这次扫描结束 (构建 + 扫描, 约 10-30 分钟)
$PYTHON release/security/continuum_scan.py results               # findings / issues / 补丁 -> out/continuum/
$PYTHON release/security/continuum_scan.py upload && CONTINUUM_ENABLED=1 $PYTHON release/security/continuum_scan.py scan   # 代码改了之后再扫
$PYTHON release/security/continuum_scan.py delete                # 删除环境, 停止 Continuum 的工作与计费
```
Continuum 按代码行数收月度 context 费, 按推理单元收每次安全工作的费用 (见 AWS Continuum 定价页; 这个 kit 一次扫描出了
20 条 findings, 验证按条计费), 所以默认不启用; 环境默认每天扫描一次
(`scanFrequency` P1D), 脚本建环境时设成 `PT0H` (只手动扫描)。不再使用时删除环境。

## Step -> files -> what it does -> verified

| Step | File | What it does | Verified |
|---|---|---|---|
| ① Define | `define/requirements.yaml` | PRD as data: requirement -> dimension -> evaluator -> threshold -> tier (redline / gate / observe), gate policy | ✅ ran on 2026-11-06 |
| ① | `define/gen_gate.py` | requirements.yaml -> `define/gate.yaml` (validated translation; `--check` for CI) | ✅ ran on 2026-11-06 |
| ① | `define/datasets/acceptance.jsonl`, `publish_dataset.py` | `AGENTCORE_EVALUATION_PREDEFINED_V1` scenarios (turns, expected_response, expected_trajectory, assertions, metadata); create + publish an immutable version, only when content changed; `registry.json` pins versions | ✅ ran on 2026-11-06 (v1-v4) |
| ① | `define/datasets/personas.jsonl` | `AGENTCORE_EVALUATION_SIMULATED_V1` personas (actor_profile, input, max_turns, assertions) | ✅ ran on 2026-11-06 |
| ① | `define/evaluators/judge_scope.json`, `derived_taskcompletion.json`, `register_evaluators.py` | Custom LLM judge (TRACE, numerical 0/0.5/1) and CustomDerived (ThirdParty.DeepEval.TaskCompletion on your model); create-or-update | ✅ ran on 2026-11-06 |
| ① | `define/evaluators/code_eval/` (`handler.py`, `deploy.sh`, `test_handler.py`) | CustomCode Lambda red line (card numbers, return outside window), deployed + registered with `agentcore eval evaluator code-based create` | ✅ ran on 2026-11-06 |
| ① | `define/calibrate.py`, `define/labels/` | judge vs labels: low-score-first sampling, agreement >= 0.85 or exit 1 | ✅ ran on 2026-11-06 (labels are demo labels) |
| ② Build | `agent/` (`agent/README.md`) | minimal Strands agent project (`agentcore create/deploy`), mock tools, system prompt from a Configuration bundle with a code default | ✅ ran on 2026-11-06 |
| ③ Evaluate | `evaluate/send_traffic.py` | replay dataset turns against the runtime, record session ids | ✅ ran on 2026-11-06 |
| ③ | `evaluate/run_ondemand.py` | `Evaluate` API over real session ids, one evaluator per call, level-aware targets, ground truth as reference inputs, ingestion guard | ✅ ran on 2026-11-06 |
| ③ | `evaluate/run_dataset.py` | `OnDemandEvaluationDatasetRunner` (`--mode ondemand`) or `BatchEvaluationRunner` / `StartBatchEvaluation` (`--mode batch`, incl. `Builtin.FailureAnalysis`) over a pinned Dataset version | ✅ ran on 2026-11-06 (both modes) |
| ③ | `evaluate/simulate.py` | user simulation: `SimulationConfig` + `ActorProfile` personas, scored like any session | ✅ ran on 2026-11-06 (2 personas) |
| ② | `agent/evaldeck/agentcore/agentcore.json` | named endpoints `STAGING` + `PROD` (DEFAULT follows latest); Gateway targets `shop` -> PROD, `shopStaging` -> STAGING; agent spans carry `evaldeck.prompt_source` + bundle version | ✅ v2: deployed (PROD imported into the stack, STAGING created, runtime v4) |
| ④ Release | `release/ci/security_scan.sh` | Amazon Inspector SBOM Generator (checksum-verified download) inventories requirements.txt, the runtime's uv.lock and the CDK package-lock.json -> `inspector-scan ScanSbom` -> `out/security-report.json`; finding >= `SECURITY_FAIL_ON` (HIGH) -> exit 1; first command of the deploy step in both CI systems | ✅ 2026-11-08: first run found 2 HIGH + 4 MEDIUM (brace-expansion 5.0.9 bundled in aws-cdk-lib 2.269, OpenTelemetry 1.44) -> BLOCKED; fixed by aws-cdk-lib ~2.275 + aws-cdk ~2.1148 + aws-opentelemetry-distro ~0.22 -> 133 components, 0 findings, PASS in CodeBuild DeployStaging; runtime v6 with the new packages passed the gate and was released |
| ④ | `release/security/continuum.yaml`, `continuum_scan.py` | Optional, off by default (`CONTINUUM_ENABLED=1` / `--enable` for the billed `setup` / `scan`; `delete` stops it). AWS Continuum for code vulnerabilities on an S3 code source: bucket (versioned, TLS-only) + integration role (`/continuum/integrations/`, external ID); integration + environment (manual scans, patches on, build domains pypi / npm); scan, findings, issues, PATCH artifacts -> `out/continuum/` | see "Things the run taught" |
| ④ | `release/deploy_staging.sh` | endpoint pins = the deployed template's pins -> `agentcore deploy` -> STAGING -> newest version -> PROD-unchanged check -> `out/staging.json` {runtimeVersion, bundleVersion, datasetVersion, gitCommit, gate_key} | ✅ v2: ran locally (STAGING 1 -> 4) and in CodeBuild (runtime v5); v3: own DeployStaging stage (runtime v8, STAGING 5 -> 8, PROD stayed 5) |
| ④ | `release/protect_prod.sh` | one-time: CloudFormation stack policy denying every update of the PROD `RuntimeEndpoint` resource + termination protection on the agent stack | ✅ v3: applied; a deploy that changed the PROD pin failed with "Action denied by stack policy" and rolled back, PROD stayed 5 |
| ④ | `release/gate_key.py`, `test_gate_key.py` | gate_key = sha256(commit, runtimeVersion, bundleVersion, datasetVersion, sha256(gate.yaml)); `--staging --check` recomputes | ✅ v2: unit tests; release refused a tampered staging.json |
| ④ | `release/gate.py`, `release/baseline.json`, `release/waivers.yaml`, `test_gate.py` | PASS / BLOCKED per segment (`acceptance` = run_dataset, `sim` = simulate): thresholds, red line = 0, no regression (`max(tol, step/n)`), baseline per segment + dataset version, missing results / unverified bundle = blocked; waivers (gate metric only, floor, expiry, approver; red line refused) | ✅ v2: STAGING gate PASS (bundle verified 8/8 sessions); waiver applied + red-line waiver refused; 27 release unit tests |
| ④ | `release/record.py` | gate decision -> SSM `/evaldeck/gate/<gate_key>` (latest decision wins) | ✅ v2: ran |
| ④ | `release/release.sh` | needs PASS record for the recomputed gate_key + approver -> PROD -> tested version, bundle pinned on the Gateway -> `/evaldeck/release/current` + `/previous` | ✅ v2: PROD 3 -> 4 (local) and 4 -> 5 (pipeline Release stage, approver read from the Manual action); refused: no approver / tampered key / no record |
| ④ | `release/rollback_lambda.py`, `test_rollback_lambda.py` | alarm -> EventBridge -> Lambda: PROD (and bundle rule) back to `/release/previous`, once per release; A/B paused instead; eval-silent notifies only | ✅ v2: unit tests + local drill; 2026-11-07 clone: deployed with `rollback.yaml` (Lambda python3.13, EventBridge rule, DLQ): DRY_RUN logged `rollback 5 -> 3` and changed nothing; eval-silent -> notify-only; armed -> PROD 5 -> 3, records current v3 / previous null; second alarm -> no-previous-release |
| ④ | `release/rollback.yaml`, `deploy_rollback.sh` | CloudFormation for the rollback Lambda: EventBridge rule on `<prefix>-*` alarms entering ALARM, least-privilege role (PROD endpoint, `/<prefix>/release/*`, optional ship rule / A/B test), SQS DLQ, reserved concurrency 1, X-Ray; `DRY_RUN=true` first | ✅ 2026-11-07: cfn-lint, checkov, deployed and drilled (row above) |
| ④ | `release/harden_default.sh` | Deny resource policy on the DEFAULT endpoint ARN (merged into an existing policy), then invokes DEFAULT / STAGING / PROD | ✅ v2: DEFAULT AccessDenied, STAGING / PROD 200; kit scripts re-run on STAGING / PROD afterwards |
| ④ | `release/ci/gate_step.sh` | the one gate step both CI systems call: gen_gate --check, pytest, acceptance + sim on STAGING with the bundle, gate.py | ✅ v2: ran locally (PASS) and in CodeBuild (PASS, gate_key `09343404ec438537`) |
| ④ | `release/ci/github-actions.yml` | `pr` job (checkout, setup-python/node, OIDC with aws-region, install, deploy_staging with the deploy role, then the gate role for gate + record, artifact, PR comment; STAGING concurrency lock) + `release` job on main with `environment: production` | ✅ 2026-11-07 clone: test repository, three OIDC roles from `github-oidc-roles.yaml`; PR -> gate BLOCKED + PR comment + SSM record; push to main -> gate PASS -> `production` approval -> PROD 2 -> 3 |
| ④ | `release/ci/github-oidc-roles.yaml` | CloudFormation: GitHub OIDC provider (or an existing one) + deploy / gate / release roles with the pipeline.yaml permissions; trust accepts both `repo:<owner>/<repo>` and the immutable `repo:<owner>@<id>/<repo>@<id>` subject; release role only from environment `production` | ✅ 2026-11-07: cfn-lint + deployed; used by the GitHub Actions run |
| ④ | `release/ci/buildspec-deploy.yml`, `buildspec.yml`, `buildspec-release.yml`, `pipeline.yaml` | CodePipeline V2 (QUEUED): Source (S3 or CodeConnections) -> DeployStaging (CodeBuild) -> Gate (CodeBuild) -> Manual approval (gate_key + versions + report link) -> Release (CodeBuild); one role per stage: only DeployRole deploys (STAGING only), GateRole cannot deploy or change any endpoint, only ReleaseRole moves PROD | ✅ v2: cfn-lint + validate-template + deployed (`evaldeck-pipeline`, S3 source = zip of the kit); one execution Source -> Gate (PASS) -> Approval (approved with `put-approval-result`) -> Release (PROD 4 -> 5). GitHub/CodeConnections source variant not run; 2026-11-07 clone (us-west-2, S3 source): Gate BLOCKED twice on the sim ScopeJudge -> `segment_tiers` -> Source -> DeployStaging -> Gate PASS -> Approval -> Release PROD 3 -> 5; 2026-11-08 CodeConnections source (GitHub connection AVAILABLE): push -> `WebhookV2` -> DeployStaging -> Gate PASS -> Approval -> Release PROD 3 -> 5; access-log bucket + versioning + TLS-only policies deployed (checkov clean) |
| ④ | `release/ab_test.sh` | config-bundle A/B through AgentCore Gateway (control = current prompt, treatment = candidate), results with p-value / CI, stop | ✅ ran on 2026-11-06 (12 sessions: mechanics only, no significance expected) |
| ④ | `release/ab_ship.sh` | after `ab-test stop`: decide (significant on the primary, nothing significantly worse, min samples) -> static-override Gateway rule for the winner; `pin` / `restore` / `show` | ✅ v2: decision C (kept, no rule); forced T1 drill: rule pinned T1, spans showed T1's version, `restore` pinned the released bundle again |
| ④ | `release/canary_rule.yaml` | CloudFormation `AWS::BedrockAgentCore::GatewayRule`: weighted route 90/10 between the PROD and STAGING targets | ✅ v2: cfn-lint + validate-template + deployed (`evaldeck-canary`); 20 sessions -> 13 PROD / 5 STAGING (2 not found in spans) |
| ④ | `release/policy.sh` | AgentCore Policy: engine + ACTIVE permit + LOG_ONLY shadow forbid -> engine LOG_ONLY (UpdateGateway) -> audit metrics (PolicyEngine + OperationName dims) -> ENFORCE | ✅ v2: ran on the evaldeck Gateway (harmless rules); 3 decisions logged, then ENFORCE, traffic still 200 |
| ⑤ Observe | `observe/online_eval.sh` | Online evaluation config on the runtime endpoint (sampling, session timeout, judges without ground truth) | ✅ ran on 2026-11-06 (100 % sampling for a tiny test) |
| ⑤ | `observe/alarms.yaml` | CloudFormation: red-line alarm, goal-score alarm with minimum samples, eval-silent alarm (traffic but no red-line results) | ✅ deployed on 2026-11-06 (`evaldeck-alarms`, all OK) |
| ⑥ Feedback | `feedback/mine_badcases.py` | low online / batch / simulation scores -> candidate rows with lineage (session, trace, evaluator, score, explanation), no evaluator cost | ✅ ran on 2026-11-06 |
| ⑥ | `feedback/promote.py`, `feedback/reviewed/` | reviewed rows -> acceptance dataset (accept) or judge-regression set (judge_error); publish a new version only if content changed | ✅ ran on 2026-11-06 (v3, idempotent re-run) |
| ⑥ | `feedback/failure_analysis.sh` | Batch evaluation with `Builtin.FailureAnalysis`: insights -> root causes -> suggested fix -> span-level findings | ✅ ran on 2026-11-06 |
| ⑥ | `feedback/recommend.sh` | AgentCore Recommendations (system prompt) from traces + one reward evaluator | ✅ ran on 2026-11-06 |

## The loop as it ran on 2026-11-06 (演示数据)

1. ① 5 acceptance scenarios -> Dataset v1; judge, derived and Lambda evaluators registered; gate.yaml generated.
2. ③ on-demand, dataset (on-demand + batch) and 2 simulated personas scored the agent; one persona exposed a real
   defect (a defective item refused as "damaged").
3. ⑥ the defect was mined from online / simulation scores, reviewed, promoted -> Dataset v3; FailureAnalysis
   independently named the same root cause with a span id.
4. ④ the CI gate on v3 BLOCKED (regression + baseline on the old dataset version). Prompt fixed, baseline re-measured on
   the PROD endpoint (v1) on dataset v4, candidate (v3) gate **PASS**; PROD moved to v3; rollback drill PROD 3 -> 1 -> 3.
5. ⑤ online evaluation + alarms on the endpoint; Recommendations produced a candidate prompt that was A/B-tested
   against the current prompt through the Gateway.
6. v2 ④ production path: STAGING + PROD declared in agentcore.json; `deploy_staging.sh` built runtime v4 and moved
   STAGING; baselines re-measured on PROD (v3) for both segments; the gate on STAGING scored Config bundle version
   `649aeb81` (sent as baggage, verified in every session's `invoke_agent` span) -> PASS -> SSM record -> `release.sh`
   PROD 3 -> 4 + bundle pinned on the Gateway; rollback drill 4 -> 3 from `/evaldeck/release/previous`; re-release;
   DEFAULT denied; A/B ship drill; 90/10 canary rule; Policy LOG_ONLY -> ENFORCE; the same path once more through
   CodePipeline (S3 source -> Gate in CodeBuild: deploy runtime v5 + gate PASS -> CLI approval -> Release PROD 4 -> 5; two IAM / YAML fixes on the way).

## Security model (what the gate proves, and what protects the gate)

The kit was scanned with Amazon Inspector (dependencies) and AWS Continuum (code) on 2026-11-08; the fixes are in the
code, and this is the trust model the scripts assume:

- **The gate's code ships in the commit it judges.** `gate_step.sh`, `gate.py`, `record.py` and the evaluators run
  from the candidate, so a change to them can change what PASS means. `gate_key` includes a hash of that code, the
  GitHub workflow writes gate records only from `main`, and the release reads only records for the key it recomputes.
  That is not enough on its own: protect `release/`, `define/`, `evaluate/`, `common/` and the workflow with
  CODEOWNERS + branch protection (`release/ci/CODEOWNERS.example`), and release only from the protected branch
  (CodePipeline: the CodeConnections source on `main`; S3 source = whoever can upload).
- **CI identities.** The pr job runs in the GitHub environment `staging`, the release job in `production`; each OIDC
  role trusts one environment, with the numeric owner / repository ids pinned (`github-oidc-roles.yaml`). The deploy
  role can use the CDK bootstrap roles, so give `staging` required reviewers if pull requests come from people who
  should not deploy. Fork pull requests get no OIDC token.
- **What a PASS is bound to.** `gate.py` blocks unless every results file comes from the staging run in
  `out/staging.json` (endpoint, runtime ARN, dataset id + version, bundle version, created after the deployment) and
  every scenario / persona was scored; `release.sh` releases only what the PASS record (written by the gate role) names.
- **Red line.** `evaldeck_RedlineCode` fails closed (a rule that cannot run is an evaluator error, never COMPLIANT) and
  normalizes Unicode digits and separators before the card-number check; keep an LLM judge next to it.
- **Agent.** One conversation per runtime session id (AgentCore Runtime runs each session in its own microVM); only
  the new user turn is accepted, history is a bounded sliding window. Callers are IAM-authorized backends; add the
  end-user identity to the session key before one session serves several users.
- **Rollback.** The Lambda acts only on a verified CloudWatch alarm event from this account / Region whose alarm is in
  ALARM when re-read (DescribeAlarms); it cannot touch STAGING or DEFAULT.
- **Supply chain.** CI installs Python packages from a hash-pinned lock (`release/ci/requirements.lock`, regenerate
  with the command in `requirements-ci.in`), actions are pinned to commit SHAs, Inspector scans every deploy.

## Things the run taught (also in the script headers)

- Continuum (2026-11-08) on this kit: 20 findings in one scan (2 CRITICAL, 9 HIGH), 16 confirmed exploitable within
  minutes of the scan; all are fixed or covered by the security model above (OIDC trust, session isolation, caller
  history, gate evidence binding, red-line evasion, rollback trigger verification, least privilege, pinned installs).
- Inspector (2026-11-08): the template's pinned CDK and OpenTelemetry versions carried 2 HIGH CVEs; upgrading aws-cdk-lib
  also needs a matching CDK CLI (`aws-cdk` ~2.1148 reads cloud assembly schema 55), otherwise synth fails in CI.
- Continuum (2026-11-08): CreateEnvironment without `scanFrequency` schedules a scan every day (P1D); `PT0H` = manual only.
- Security review (2026-11-08): checkov CKV_AWS_18 / 21 on the pipeline buckets. Artifact and source buckets log into a
  dedicated access-log bucket and are versioned; that bucket cannot log into itself (AWS: endless loop), so it logs to an
  optional `CentralAccessLogBucket` (an existing org log bucket) and has no access logging when the parameter is empty.
- Clone run (CodeConnections, 2026-11-08): a single 0.5 flip of ScopeJudge on n = 7 BLOCKED as a regression: means are
  rounded to 4 decimals (0.9286 -> 0.8571 = 0.0715 > 0.5 / 7). `gate.py` now allows for the rounding (unit test added).
- Clone run (2026-11-07): the sim segment BLOCKED in 3 of 3 runs (PR, CodePipeline, retry) on `evaldeck_ScopeJudge`
  0.75 < 0.8 over 4 turns: the pushy persona's follow-ups are scored 0.5 while the agent behaves as specified. The
  criterion now has `segment_tiers: {sim: observe}` (requirements.yaml -> gate.yaml): ScopeJudge gates the acceptance
  segment and is reported in sim; GoalSuccessRate and the red line still gate sim.
- Clone run (2026-11-07): a dataset created from a file stores `0.0` as `0`, so a plain string comparison saw a
  change and `promote.py` published an identical version 2 (the gate then blocks on the baseline's dataset version);
  `publish_dataset.py` now compares integral floats as ints.
- Clone run: `mine_badcases.py --online-config` looked for spans on STAGING while the online config scores PROD, so
  the flagged session was dropped silently; it now reads the endpoint of each result (`--endpoint`, default PROD,
  for online results) and lists sessions without spans.
- Clone run: `failure_analysis.sh` looks up `SESSION_IDS` on `ENDPOINT` (default PROD); STAGING sessions on PROD fail
  every session, and the script now exits 1 with the job's `errorDetails`.
- Wait for ingestion: scoring a session ~40 s after it ended scored half a conversation silently
  (`run_ondemand.py` now refuses sessions younger than 5 min and flags `PartialSession`).
- The dataset runner applies every evaluator to every row: rows without `expected_trajectory` return
  `EvaluationRequestValidationError` for trajectory evaluators - keep those metrics `observe` or run them on the subset.
- Batch job summaries can show `average_score: null` for a code evaluator while the per-session events carry values:
  gate on the per-session results.
- Alarm on `{service.name, onlineEvaluationConfigId}`; `{service.name}` alone also receives batch / CI scores.
- Online results arrived ~15 min after traffic; read them with GetLogEvents or Logs Insights.
- A replayed multi-turn bad case goes stale once the agent behaves differently; keep the failing turn or use a persona.
- Judge scores flip on identical answers: regression tolerance must be at least one score step / n.
- The CLI-provisioned online-eval role can invoke every Lambda in the account; use `--role-arn` in production.
- v2: endpoint versions in agentcore.json are creation pins; CloudFormation re-applies them whenever the endpoint
  resource changes. v3: `deploy_staging.sh` copies the DEPLOYED template's pins into agentcore.json, so the endpoint
  resources never get a change-set entry (writing the live PROD version would be a PROD change the stack policy denies).
- v2: the gate only proves it tested the shipped prompt if the agent says so: spans carry `evaldeck.prompt_source`
  and the bundle version, and `gate.py` blocks when any gate session did not use the expected version.
- v2: a Deny on the DEFAULT **endpoint** ARN blocks only DEFAULT; on the runtime ARN it would block every endpoint.
- v2: Gateway rules resolve bundle and route actions independently - the ship rule (bundle, 100) and the canary
  (route, 200) apply to the same request.
- v2: Policy metrics need the full dimension set: `{OperationName, PolicyEngine}` returns data, `{PolicyEngine}`
  alone returns none; the engine attached in LOG_ONLY still records `AllowDecisions` with `Mode=LOG_ONLY`.
- v2: macOS + Python 3.12: the SDK runner segfaulted again in concurrent TLS handshakes (span collection);
  `EVAL_CONCURRENCY=1` locally, the default 5 on Linux CI.

## Minimum IAM per step (what the v2 run actually needed)

Scope every `bedrock-agentcore` action to the runtime ARN **and** `<runtime-arn>/runtime-endpoint/*` (invokes and
endpoint updates are authorized on the endpoint child ARN as well). The CodeBuild roles in `release/ci/pipeline.yaml`
are these lists, validated by the pipeline run. v3 split: DeployRole = the deploy_staging.sh row with
`UpdateAgentRuntimeEndpoint` on `runtime-endpoint/STAGING` only and an explicit Deny on PROD / DEFAULT; GateRole =
the ③ row + record.py, with explicit Denies on every endpoint / runtime change and on `sts:AssumeRole` to `cdk-*`;
ReleaseRole = release.sh with `runtime-endpoint/PROD` only (checked with the IAM policy simulator).
The CDK deploy itself runs as the CDK bootstrap execution role (AdministratorAccess by default): protect the PROD
endpoint with `release/protect_prod.sh`; in production put STAGING and PROD in separate accounts.

| Step / script | Needs |
|---|---|
| ③ send_traffic / run_dataset / simulate (caller = CI gate role) | `bedrock-agentcore:InvokeAgentRuntime` (STAGING), `Evaluate`, `ListEvaluators`, `GetEvaluator`, `GetDataset`, `ListDatasetExamples`, `ListDatasetVersions`, `GetConfigurationBundleVersion`; **logs:StartQuery / GetQueryResults / FilterLogEvents** on `/aws/bedrock-agentcore/runtimes/*` and `aws/spans` (span collector, ingestion guard, bundle_check); `bedrock:InvokeModel` (custom / derived judges and the simulated user run on the caller's credentials); `lambda:InvokeFunction` + `GetFunction` on the CustomCode evaluator |
| ④ security_scan.sh (DeployRole) | `inspector-scan:ScanSbom` (no Inspector activation needed) |
| ④ continuum_scan.py (operator) | `continuum:*Integration*`, `*Environment*`, `CreateCodeScan` / `GetCodeScan` / `ListCodeScans`, `ListCodeFindings`, `ListCodeIssues` / `GetCodeIssue`, `ListCodeIssueArtifacts` / `GetCodeIssueArtifact`; `iam:PassRole` on the integration role with `iam:PassedToService` = `continuum.amazonaws.com`; `s3:PutObject` on the code bucket; CloudFormation for `continuum.yaml` |
| ④ deploy_staging.sh (DeployRole) | CDK deploy = `sts:AssumeRole` on `cdk-hnb659fds-*`; the CLI itself reads `cloudformation:DescribeStacks` on the project stack **and `CDKToolkit`** (+ `GetTemplate` for the pin sync), `ssm:GetParameter /cdk-bootstrap/*`, Transaction Search read (`xray:GetTraceSegmentDestination`, `GetIndexingRules`, `logs:DescribeResourcePolicies`); `ListAgentRuntimeVersions`, `ListAgentRuntimeEndpoints`, `GetAgentRuntimeEndpoint`, `UpdateAgentRuntimeEndpoint` (STAGING only; PROD / DEFAULT denied) |
| ④ protect_prod.sh (platform admin, once) | `cloudformation:GetTemplate`, `SetStackPolicy`, `UpdateTerminationProtection` on the agent stack |
| ④ record.py | `ssm:PutParameter`, `ssm:AddTagsToResource` on `parameter/<prefix>/gate/*` |
| ④ release.sh | **`ssm:GetParameter`** on `parameter/<prefix>/*`, **`ssm:PutParameter` / `AddTagsToResource`** on `parameter/<prefix>/release/*`; `GetAgentRuntimeEndpoint`, `UpdateAgentRuntimeEndpoint` (PROD); `ListGatewayRules`, `CreateGatewayRule`, `UpdateGatewayRule`; in CodePipeline `codepipeline:ListActionExecutions` (who approved) |
| ④ rollback_lambda.py | `UpdateAgentRuntimeEndpoint` (PROD; STAGING / DEFAULT denied), `ListGatewayRules` / `UpdateGatewayRule` (if GATEWAY_ID), `GetABTest` / `UpdateABTest` (if AB_TEST_ID), `ssm:GetParameter` / `PutParameter` on `parameter/<prefix>/release/*`, `cloudwatch:DescribeAlarms` on `alarm:<prefix>-*` |
| ④ harden_default.sh | `GetResourcePolicy`, `PutResourcePolicy` on `<runtime-arn>/runtime-endpoint/DEFAULT`, `ListAgentRuntimeEndpoints` |
| ④ ab_ship.sh | `GetABTest`, `ListGatewayRules`, `CreateGatewayRule`, `UpdateGatewayRule`; `ssm:GetParameter` (restore) |
| ④ policy.sh | `CreatePolicyEngine`, `GetPolicyEngine`, `ListPolicyEngines`, `CreatePolicy`, `ListPolicies`, `GetGateway`, `UpdateGateway`, `iam:PassRole` on the Gateway role, `iam:PutRolePolicy` (grants the Gateway role `GetPolicyEngine`, `CheckAuthorizePermissions`, `AuthorizeAction`, `PartiallyAuthorizeActions` on engine + gateway), `cloudwatch:ListMetrics` / `GetMetricData` |
| ⑤ online_eval.sh | own execution role (`--role-arn`): logs read on the runtime log group, logs write on `/aws/bedrock-agentcore/evaluations/*`, `bedrock:InvokeModel`, `lambda:InvokeFunction` + `GetFunction` on the code evaluator only |

Slide-ready excerpts of every script: [`SNIPPETS.md`](SNIPPETS.md).

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.

