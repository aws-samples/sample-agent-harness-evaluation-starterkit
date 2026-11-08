"""Request parsing for ShopAgent: one user turn per request; conversation history stays server-side.

The caller sends {"prompt": "<user text>"}. Assistant turns, tool calls and tool results are never accepted from the
caller: the agent's own history (kept per runtime session in main.py) is the only source of prior turns, so a caller
cannot fabricate what the agent said or what a tool returned (the prompt-level rules depend on that state).
"""

from typing import Any

MAX_PROMPT_CHARS = 4000


def parse_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if "messages" in payload:
        raise ValueError("caller-supplied message history is not accepted: send {\"prompt\": ...}; "
                         "the conversation is kept per runtime session")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt longer than {MAX_PROMPT_CHARS} characters")
    return prompt
