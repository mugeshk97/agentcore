
"""math_specialist — A2A-protocol AgentCore runtime.

Wraps a Strands agent (Nova Lite + strands_tools.calculator) as an A2A
server via serve_a2a(). Stateless — no memory, no KB.
"""

import logging
import os

from bedrock_agentcore.runtime.a2a import serve_a2a
from strands import Agent
from strands.models import BedrockModel
from strands.multiagent.a2a.executor import StrandsA2AExecutor
from strands_tools import calculator

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(name)s | %(message)s",
)

REGION = os.environ.get("AWS_REGION", "us-east-1")

agent = Agent(
    name="math_specialist",
    description=(
        "Evaluates arithmetic and symbolic math expressions. "
        "Use for calculations, unit arithmetic, and closed-form math."
    ),
    model=BedrockModel(
        model_id="us.amazon.nova-2-lite-v1:0",
        region_name=REGION,
        temperature=0.0,
        max_tokens=500,
    ),
    system_prompt=(
        "You are a math specialist. Use the calculator tool for every "
        "non-trivial arithmetic step. Return only the result and a one-"
        "line explanation — no commentary."
    ),
    tools=[calculator],
)

executor = StrandsA2AExecutor(agent)


if __name__ == "__main__":
    serve_a2a(executor, port=int(os.environ.get("PORT", "9001")))
