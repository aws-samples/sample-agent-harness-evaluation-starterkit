# agent/ - step ② Build: the minimal agent every other script evaluates

`agent/evaldeck` is an AgentCore CLI 1.0.x project (`agentcore create --template agent-python-strands`) with one
Strands runtime, `ShopAgent`: an online-shop support agent with three deterministic mock tools
(`get_order`, `get_return_policy`, `create_return`, data in `app/ShopAgent/tools.py`, 演示数据) so that scores are
reproducible. Model: `us.anthropic.claude-haiku-4-5-20251001-v1:0` (override with the `MODEL_ID` env var).

## Recreate it from scratch (what was run on 2026-11-06)

```bash
npm i -g @aws/agentcore                     # CLI 1.0.x (Node >= 20.12); the Python starter toolkit is deprecated
agentcore create --name evaldeck --template agent-python-strands --skip-git
cd evaldeck
agentcore remove memory --name agentMemory  # template ships a 4-strategy Memory; not needed here (extraction cost)
# rename runtime "agent" -> "ShopAgent" in agentcore/agentcore.json and app/, replace main.py / tools.py
(cd agentcore/cdk && npm install)           # needed when the project was created with --skip-install
agentcore deploy                            # CDK stack AgentCore-evaldeck-<target>; enables Transaction Search
agentcore invoke --runtime ShopAgent --session-id "$(uuidgen)-$(uuidgen)" \
  --payload '{"prompt":"Where is my order ORD-1003?"}'
```

The first deploy also creates the named endpoints `STAGING` and `PROD` declared in `agentcore/agentcore.json`
(`runtimes[].endpoints`, both at version 1) and two Gateway targets on the front door `evaldeckFront`:
`shop` -> PROD and `shopStaging` -> STAGING (`httpRuntime.runtimeEndpoint`). DEFAULT keeps following the newest
version; `release/deploy_staging.sh` moves STAGING, `release/release.sh` moves PROD (UpdateAgentRuntimeEndpoint),
and `release/harden_default.sh` then denies direct invokes of DEFAULT. The endpoint versions in agentcore.json are
creation pins: `deploy_staging.sh` writes the live versions back before every deploy, because CloudFormation
re-applies the declared version whenever the endpoint resource's properties change.

Runtime session ids must be at least 33 characters. Spans land in
`/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT` (stream `spans`, unified mode); the OTel service name is
`<runtime-name>.DEFAULT`, e.g. `evaldeck_default_ShopAgent.DEFAULT`. Wait 2-5 minutes after an invocation
before evaluating it.

## Configuration bundle usage

`main.py` reads the system prompt from a Configuration bundle when the request carries one (the Gateway injects
`aws.agentcore.configbundle_arn` / `_version` as W3C baggage during an A/B test or a bundle override rule) and
falls back to `DEFAULT_SYSTEM_PROMPT` otherwise:

```python
cfg = BedrockAgentCoreContext.get_config_bundle() or {}   # {} when the request has no bundle
return cfg.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
```

Every `invoke_agent` span carries where the prompt came from (Strands `trace_attributes`):
`evaldeck.prompt_source` = `bundle` | `code_default`, plus `aws.agentcore.configbundle_arn` / `_version`.
`evaluate/run_dataset.py` and `simulate.py` read them back (`bundle_check`) so the gate proves it scored the bundle
version that ships; scripts send the baggage with `--bundle <id> --bundle-version <version-id>`.

A bundle's components are keyed by the runtime ARN:
`agentcore eval config-bundle create --name evaldeck_ShopAgentConfig --components '{"<runtime-arn>": {"configuration": {"system_prompt": "..."}}}'`
(see `release/ab_test.sh`).

Files that are not committed (account-specific, re-created by `agentcore deploy`): `agentcore/aws-targets.json`,
`agentcore/.cli/`, `agentcore/cdk/node_modules`, `app/ShopAgent/.venv`.
