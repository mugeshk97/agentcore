"""
AgentCore alpha runtime — main.py

Architecture:
  BedrockAgentCoreApp  →  Strands Agent  →  Bedrock Nova model
                               ↕
                       AgentCore Memory (STM + LTM)

Memory best-practices applied:
  - MemoryClient singleton at module level (stateless, thread-safe)
  - `get_last_k_turns()` for proper STM turn-grouped history (not raw list_events)
  - `retrieve_memories()` for LTM semantic retrieval before each response
  - `create_event()` to persist each turn to STM after responding
  - `AgentCoreMemoryToolProvider` gives the agent proactive record/retrieve tools
  - Memory strategies (userPreferences + conversationFacts) enable async LTM extraction
  - `session_id` from context (runtime) or payload (local dev); `actor_id` from payload
"""

import os
import uuid
import logging

from bedrock_agentcore import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands_tools.agent_core_memory import AgentCoreMemoryToolProvider
from strands.models import BedrockModel
from strands import Agent

from shared.a2a_tools import make_a2a_tool


os.environ.setdefault("KNOWLEDGE_BASE_ID", "GLSSIBXSBD")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("MIN_SCORE", "0.3")

REGION    = os.environ.get("AWS_REGION", "us-east-1")
MEMORY_ID = os.environ.get("MEMORY_ID", "alpha_memory-cQMHNRHuNG")

KB_SPECIALIST_URL   = os.environ.get("KB_SPECIALIST_URL",   "http://127.0.0.1:9000/")
MATH_SPECIALIST_URL = os.environ.get("MATH_SPECIALIST_URL", "http://127.0.0.1:9001/")

ask_kb_specialist = make_a2a_tool(
    tool_name="ask_kb_specialist",
    tool_description=(
        "Delegate factual questions to the KB specialist. Use this "
        "whenever the user is asking for information that could be in "
        "a documented knowledge base."
    ),
    remote_name="kb_specialist",
    remote_description="KB-grounded factual Q&A",
    runtime_url=KB_SPECIALIST_URL,
    region=REGION,
)

ask_math_specialist = make_a2a_tool(
    tool_name="ask_math_specialist",
    tool_description=(
        "Delegate arithmetic or math expression evaluation to the math "
        "specialist. Use this whenever a numeric calculation is required."
    ),
    remote_name="math_specialist",
    remote_description="arithmetic & symbolic math",
    runtime_url=MATH_SPECIALIST_URL,
    region=REGION,
)


# Pattern: actor/{actorId}/sessions 
def ltm_namespace(actor_id: str) -> str:
    return f"actor/{actor_id}/sessions"

# ── logging ──────────────────────────────────────────────────────────────────
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
    """
    Main entrypoint invoked per /invocations request.

    context.session_id — set by the managed runtime per conversation.
    payload.session_id — fallback for local dev (lets you pin a session across curls).
    payload.actor_id   — identifies the user for LTM (cross-session recall).
    payload.prompt     — the user message.
    """
    prompt    = payload.get("prompt", "")
    actor_id  = payload.get("actor_id", "default-user")
    # Production: runtime injects session_id via context.
    # Local dev: pass "session_id" in the payload to reuse a session across calls.
    session_id = (
        (context.session_id if context and context.session_id else None)
        or payload.get("session_id")
        or str(uuid.uuid4())
    )
    namespace = ltm_namespace(actor_id)

    logger.info("invoke_agent | session=%s actor=%s", session_id, actor_id)

    # ── 1. Retrieve LTM context (cross-session long-term memories) ────────────
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

    # ── 2. Retrieve STM conversation history (current session) ────────────────
    stm_history = ""
    try:
        turns = memory_client.get_last_k_turns(
            memory_id=MEMORY_ID,
            actor_id=actor_id,
            session_id=session_id,
            k=10,  # last 10 turns (20 messages)
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

    # ── 3. Build memory tool provider (per-request, session-scoped) ───────────
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

    # ── 5. Invoke agent ───────────────────────────────────────────────────────
    response      = agent(prompt)
    response_text = str(response)

    # ── 6. Persist turn to STM → feeds async LTM extraction pipeline ─────────
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
