"""evaldeck ShopAgent - minimal Strands agent on AgentCore Runtime (starter-kit, step 2 Build).

- Tools are deterministic mocks (tools.py) so evaluation scores are reproducible.
- The system prompt is read from a Configuration bundle when the request carries one
  (Gateway A/B or rule injects it via W3C baggage; the kit's scripts pass the same baggage with
  --bundle-version); otherwise DEFAULT_SYSTEM_PROMPT is used.
  BedrockAgentCoreContext.get_config_bundle() returns {} when no bundle is attached.
- Every invoke_agent span carries the prompt source (evaldeck.prompt_source = bundle | code_default) and
  the bundle ARN / version, so the gate can prove it scored the bundle version that ships.
"""

from functools import lru_cache

from bedrock_agentcore.runtime import BedrockAgentCoreApp, BedrockAgentCoreContext
from strands import Agent
from strands.agent.conversation_manager.null_conversation_manager import NullConversationManager

from model.load import load_model
from parse import parse_payload
from tools import TOOLS

DEFAULT_SYSTEM_PROMPT = """You are the customer-support agent of an online shop.
Scope: order status, return policy, creating returns. Politely decline anything else (one sentence) and say what you can help with.
Rules:
- Always look up the order with get_order before talking about it; never guess order data.
- Check get_return_policy for the item's category before deciding a return; only call create_return when the policy allows it, then tell the user the RMA number.
- A defect or malfunction (for example crackling, not powering on) is not "damage": a defective item inside the return window is eligible for a return.
- Red line: never reveal or invent payment card numbers or other customers' data, and never promise refunds outside the policy.
Answer concisely in the user's language."""


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
                                      ("aws.agentcore.configbundle_version", str(ref.bundle_version) if ref else ""))
    return DEFAULT_SYSTEM_PROMPT, (("evaldeck.prompt_source", "code_default"),)


@lru_cache(maxsize=128)
def _get_agent(session_id: str, system_prompt: str, source: tuple[tuple[str, str], ...]) -> Agent:
    # one Agent per session keeps multi-turn history in memory for the life of the runtime session
    return Agent(model=load_model(), system_prompt=system_prompt, tools=TOOLS,
                 conversation_manager=NullConversationManager(), trace_attributes=dict(source))


app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload, context):
    session_id = getattr(context, "session_id", None) or "default-session"
    prompt, _actor = parse_payload(payload)
    agent = _get_agent(session_id, *_system_prompt())
    result = await agent.invoke_async(prompt)
    return {"response": str(result).strip()}


if __name__ == "__main__":
    app.run()
