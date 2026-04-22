"""Coordinator runtime: HTTP entrypoint, memory (STM+LTM), A2A delegation."""

import os
import uuid
import logging
from typing import Generator

import boto3
import httpx
from a2a.client import ClientConfig
from bedrock_agentcore import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from strands_tools.agent_core_memory import AgentCoreMemoryToolProvider
from strands.agent.a2a_agent import A2AAgent
from strands.models import BedrockModel
from strands import Agent, tool


os.environ.setdefault("AWS_REGION", "us-east-1")

REGION    = os.environ.get("AWS_REGION", "us-east-1")
MEMORY_ID = os.environ.get("MEMORY_ID", "alpha_memory-cQMHNRHuNG")

KB_SPECIALIST_URL   = os.environ.get("KB_SPECIALIST_URL",   "http://127.0.0.1:9000/")
MATH_SPECIALIST_URL = os.environ.get("MATH_SPECIALIST_URL", "http://127.0.0.1:9001/")


class SigV4HttpxAuth(httpx.Auth):
    """Signs each outbound httpx request with AWS SigV4.

    Credentials resolve lazily per call via boto3.Session — picks up
    rotating task-role creds in AgentCore without caching or refresh.
    """

    requires_request_body = True

    def __init__(self, service: str, region: str) -> None:
        self._service = service
        self._region = region
        self._session = boto3.Session()

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        body = request.read()
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=body,
            headers={k: v for k, v in request.headers.items()},
        )
        credentials = self._session.get_credentials()
        if credentials is None:
            raise RuntimeError("No AWS credentials available for SigV4 signing")
        frozen = credentials.get_frozen_credentials()
        SigV4Auth(frozen, self._service, self._region).add_auth(aws_request)
        for header_name, header_value in aws_request.headers.items():
            request.headers[header_name] = header_value
        yield request

_kb_http_client = httpx.AsyncClient(
    auth=(
        SigV4HttpxAuth("bedrock-agentcore", REGION)
        if KB_SPECIALIST_URL.startswith("https://")
        else None
    ),
    timeout=300.0,
)
_kb_agent = A2AAgent(
    endpoint=KB_SPECIALIST_URL,
    name="kb_specialist",
    description="KB-grounded factual Q&A",
    client_config=ClientConfig(httpx_client=_kb_http_client, streaming=False),
)


@tool
def ask_kb_specialist(query: str) -> str:
    """Delegate factual questions to the KB specialist. Use whenever the user is asking for information that could be in a documented knowledge base."""
    result = _kb_agent(query)
    return str(result.message["content"][0]["text"])

_math_http_client = httpx.AsyncClient(
    auth=(
        SigV4HttpxAuth("bedrock-agentcore", REGION)
        if MATH_SPECIALIST_URL.startswith("https://")
        else None
    ),
    timeout=300.0,
)
_math_agent = A2AAgent(
    endpoint=MATH_SPECIALIST_URL,
    name="math_specialist",
    description="arithmetic & symbolic math",
    client_config=ClientConfig(httpx_client=_math_http_client, streaming=False),
)


@tool
def ask_math_specialist(query: str) -> str:
    """Delegate arithmetic or math expression evaluation to the math specialist. Use whenever a numeric calculation is required."""
    result = _math_agent(query)
    return str(result.message["content"][0]["text"])


def ltm_namespace(actor_id: str) -> str:
    return f"actor/{actor_id}/sessions"


logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler()],
)
logging.getLogger("strands").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)


memory_client = MemoryClient(region_name=REGION)

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke_agent(payload, context):
    prompt    = payload.get("prompt", "")
    actor_id  = payload.get("actor_id", "default-user")
    session_id = (
        (context.session_id if context and context.session_id else None)
        or payload.get("session_id")
        or str(uuid.uuid4())
    )
    namespace = ltm_namespace(actor_id)

    logger.info("invoke_agent | session=%s actor=%s", session_id, actor_id)

    ltm_context = ""
    try:
        memories = memory_client.retrieve_memories(
            memory_id=MEMORY_ID,
            namespace=namespace,
            query=prompt,
            actor_id=actor_id,
            top_k=5,
        )
        if memories:
            snippets = "\n".join(
                f"- {m.get('content', {}).get('text', '')}"
                for m in memories
                if m.get("content", {}).get("text")
            )
            ltm_context = f"\n\nRelevant long-term memories about this user:\n{snippets}"
            logger.debug("LTM: retrieved %d records", len(memories))
    except Exception as e:
        logger.warning("LTM retrieval failed (non-fatal): %s", e)

    stm_history = ""
    try:
        turns = memory_client.get_last_k_turns(
            memory_id=MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            k=10,
        )
        if turns:
            lines = []
            for turn in turns:
                for msg in turn:
                    role = msg.get("role", "")
                    text = msg.get("content", "")
                    if role and text:
                        lines.append(f"{role.upper()}: {text}")
            if lines:
                stm_history = "\n\nConversation history (this session):\n" + "\n".join(lines)
                logger.debug("STM: %d turns loaded", len(turns))
    except Exception as e:
        logger.warning("STM retrieval failed (non-fatal): %s", e)

    memory_provider = AgentCoreMemoryToolProvider(
        memory_id=MEMORY_ID,
        actor_id=actor_id,
        session_id=session_id,
        namespace=namespace,
        region=REGION,
    )

    model = BedrockModel(
        model_id="us.amazon.nova-2-lite-v1:0",
        region_name=REGION,
        temperature=0.7,
        max_tokens=1000,
    )

    system_prompt = (
        "You are a coordinator agent with persistent memory and two remote "
        "specialists you can delegate to.\n\n"
        "Delegation tools:\n"
        "  • ask_kb_specialist(query)   — factual, KB-grounded questions\n"
        "  • ask_math_specialist(query) — arithmetic / symbolic math\n\n"
        "Memory tools:\n"
        "  • agent_core_memory(action='record', content='...') — save a fact about the user\n"
        "  • agent_core_memory(action='retrieve', query='...') — search past memories\n\n"
        "Guidelines:\n"
        "  - Route factual lookups to ask_kb_specialist; do not try to answer them yourself.\n"
        "  - Route any calculation to ask_math_specialist.\n"
        "  - When the user shares personal info (name, preferences, goals), record it immediately.\n"
        "  - When a question seems to require past context, retrieve first before answering.\n"
        f"{ltm_context}{stm_history}"
    )

    agent = Agent(
        system_prompt=system_prompt,
        model=model,
        tools=[ask_kb_specialist, ask_math_specialist, *memory_provider.tools],
    )

    response      = agent(prompt)
    response_text = str(response)

    try:
        memory_client.create_event(
            memory_id=MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            messages=[
                (prompt, "USER"),
                (response_text, "ASSISTANT"),
            ],
        )
        logger.debug("STM: turn saved for session=%s", session_id)
    except Exception as e:
        logger.warning("STM save failed (non-fatal): %s", e)

    return {"result": response_text, "session_id": session_id}


if __name__ == "__main__":
    app.run()
