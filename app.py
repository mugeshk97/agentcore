"""
app.py — Streamlit chat UI for the AgentCore alpha agent.
Clean, minimal design.
"""

import json
import uuid

import boto3
import streamlit as st

# ── Config ────────────────────────────────────────────────────────────────────
AGENT_ARN = "arn:aws:bedrock-agentcore:us-east-1:622004253969:runtime/alpha-8FJKWu7Xqm"
REGION    = "us-east-1"
ACTOR_ID  = "default-user"

# ── Page setup ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="AgentCore Chat",
    layout="centered",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
}

/* Hide Streamlit chrome */
#MainMenu, footer, header { visibility: hidden; }
[data-testid="stToolbar"] { display: none; }
[data-testid="stHeader"] { background: transparent !important; }

/* Sidebar border */
[data-testid="stSidebar"] {
    border-right: 1px solid #e9ecef;
}
[data-testid="stSidebar"] > div { padding-top: 1.5rem; }

/* Input textarea */
[data-testid="stChatInputTextArea"] {
    border: 1.5px solid #dee2e6 !important;
    border-radius: 10px !important;
    font-family: 'Inter', sans-serif !important;
    font-size: 0.92rem !important;
    min-height: 60px !important;
    padding: 14px 16px !important;
    box-shadow: none !important;
}
[data-testid="stChatInputTextArea"]:focus {
    border-color: #6366f1 !important;
    box-shadow: 0 0 0 3px rgba(99,102,241,0.08) !important;
}

/* Chat bubbles */
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) {
    background: #eef2ff;
    border-radius: 12px;
    padding: 4px 8px;
}
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"]) {
    background: #ffffff;
    border-radius: 12px;
    padding: 4px 8px;
    border: 1px solid #f0f0f0;
}

/* Sidebar labels */
.sidebar-label {
    font-size: 0.72rem;
    font-weight: 600;
    color: #6c757d;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 2px;
}
.sidebar-value {
    font-size: 0.82rem;
    color: #343a40;
    font-family: 'Courier New', monospace;
    background: #e9ecef;
    padding: 5px 10px;
    border-radius: 6px;
    margin-bottom: 14px;
    word-break: break-all;
}

/* Button */
.stButton > button {
    background: #6366f1 !important;
    color: white !important;
    border: none !important;
    border-radius: 8px;
    font-size: 0.84rem;
    font-weight: 500;
    padding: 8px 16px;
    width: 100%;
    transition: background 0.15s;
}
.stButton > button:hover { background: #4f46e5 !important; }

/* Page title */
.page-title {
    font-size: 1.2rem;
    font-weight: 600;
    color: #212529;
    margin-bottom: 2px;
}
.page-sub {
    font-size: 0.82rem;
    color: #868e96;
    margin-bottom: 1.5rem;
}
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
if "messages" not in st.session_state:
    st.session_state.messages = []
if "client" not in st.session_state:
    st.session_state.client = boto3.client("bedrock-agentcore", region_name=REGION)

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### AgentCore")
    st.markdown("---")

    st.markdown('<div class="sidebar-label">Agent</div>', unsafe_allow_html=True)
    st.markdown('<div class="sidebar-value">alpha</div>', unsafe_allow_html=True)

    st.markdown('<div class="sidebar-label">Actor</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="sidebar-value">{ACTOR_ID}</div>', unsafe_allow_html=True)

    st.markdown('<div class="sidebar-label">Session</div>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="sidebar-value">{st.session_state.session_id[:20]}…</div>',
        unsafe_allow_html=True,
    )

    st.markdown("")
    if st.button("New Session"):
        st.session_state.session_id = str(uuid.uuid4())
        st.session_state.messages = []
        st.rerun()

    st.markdown("---")
    st.markdown(
        '<small style="color:#adb5bd">Memory: STM + LTM<br>Model: Nova 2 Lite</small>',
        unsafe_allow_html=True,
    )

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown('<div class="page-title">Chat</div>', unsafe_allow_html=True)
st.markdown('<div class="page-sub">AWS Bedrock AgentCore · alpha agent</div>', unsafe_allow_html=True)

# ── Chat history ──────────────────────────────────────────────────────────────
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# ── Invoke ────────────────────────────────────────────────────────────────────
def invoke_agent(prompt: str) -> str:
    payload = json.dumps({
        "prompt":     prompt,
        "actor_id":  ACTOR_ID,
        "session_id": st.session_state.session_id,
    }).encode()

    resp = st.session_state.client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_ARN,
        runtimeSessionId=st.session_state.session_id,
        payload=payload,
        contentType="application/json",
        accept="application/json",
    )
    data = json.loads(resp["response"].read())
    return data.get("result", str(data))

# ── Input ─────────────────────────────────────────────────────────────────────
if prompt := st.chat_input("Message the agent…"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner(""):
            try:
                answer = invoke_agent(prompt)
            except Exception as e:
                answer = f"Error: {e}"
        st.markdown(answer)

    st.session_state.messages.append({"role": "assistant", "content": answer})
