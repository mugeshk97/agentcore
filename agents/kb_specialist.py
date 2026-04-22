"""kb_specialist — A2A runtime. Stateless. Answers from Bedrock KB."""

import logging
import os

from bedrock_agentcore.runtime.a2a import serve_a2a
from strands import Agent
from strands.models import BedrockModel
from strands.multiagent.a2a.executor import StrandsA2AExecutor
from strands_tools import retrieve

os.environ.setdefault("KNOWLEDGE_BASE_ID", "GLSSIBXSBD")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("MIN_SCORE", "0.3")

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(name)s | %(message)s",
)

REGION = os.environ.get("AWS_REGION", "us-east-1")

agent = Agent(
    name="kb_specialist",
    description=(
        "Answers factual questions grounded in the Bedrock Knowledge "
        "Base. Use for lookups where authoritative source material exists."
    ),
    model=BedrockModel(
        model_id="us.amazon.nova-2-lite-v1:0",
        region_name=REGION,
        temperature=0.3,
        max_tokens=1000,
    ),
    system_prompt=(
        "Answer strictly from the knowledge base retrieved via the "
        "retrieve tool. If the answer is not retrievable, say so "
        "explicitly — do not speculate."
    ),
    tools=[retrieve],
)

executor = StrandsA2AExecutor(agent)


if __name__ == "__main__":
    serve_a2a(executor, port=int(os.environ.get("PORT", "9000")))
