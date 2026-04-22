"""math_specialist — A2A runtime. Stateless."""

import logging
import os

from strands import Agent
from strands.models import BedrockModel
from strands.multiagent.a2a import A2AServer
from strands_tools import calculator

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(name)s | %(message)s",
)

REGION = os.environ.get("AWS_REGION", "us-east-1")

strands_agent = Agent(
    name="math_specialist",
    description="Evaluates arithmetic and symbolic math expressions.",
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
    callback_handler=None,
)


if __name__ == "__main__":
    A2AServer(
        agent=strands_agent,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "9000")),
    ).serve()
