import os

from strands.models.bedrock import BedrockModel

# Cheap model by default; override with the MODEL_ID environment variable (agentcore.json -> envVars).
MODEL_ID = os.getenv("MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")


def load_model(model_id: str | None = None) -> BedrockModel:
    """Bedrock model client using the runtime's IAM role."""
    return BedrockModel(model_id=model_id or MODEL_ID, temperature=0.0, max_tokens=800)
