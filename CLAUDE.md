# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Package management is via `uv` (lockfile: `uv.lock`, Python 3.12 pinned in `.python-version`). No test suite, linter, or formatter is configured.

- Install/sync deps: `uv sync`
- Add a dep: `uv add <pkg>`
- Run locally: `uv run main.py` — starts the AgentCore HTTP server on port 8080 that mimics the production runtime contract.
- Invoke locally: `curl -X POST http://localhost:8080/invocations -H 'Content-Type: application/json' -d '{"prompt": "..."}'`
- Deploy to AWS: `uv run agentcore launch` — CodeBuild builds an ARM64 container, pushes to ECR, and updates the runtime (agent name `alpha`, account/region pinned in `.bedrock_agentcore.yaml`).
- Invoke deployed runtime: `uv run agentcore invoke '{"prompt": "..."}'`
- Tail runtime logs: `aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT --log-stream-name-prefix "YYYY/MM/DD/[runtime-logs]" --follow`

## Architecture

Single-file app (`main.py`) stitching three layers:

1. **Bedrock AgentCore runtime** (`bedrock_agentcore.BedrockAgentCoreApp`) — AWS's hosting shim. `@app.entrypoint` registers the handler the managed runtime invokes; `app.run()` starts the local HTTP server with the same contract. The deployed container's CMD is `python -m main`, so the `if __name__ == "__main__": app.run()` block is required — without it, the container exits immediately and no logs are written.
2. **Strands agent** (`strands.Agent`) — agent loop, tool dispatch, model call cycle.
3. **Bedrock model** (`strands.models.BedrockModel`, currently `us.amazon.nova-2-lite-v1:0`) — inference via the same AWS account; the runtime execution role's IAM creds handle auth, so no API key is needed.

The agent and model are constructed **inside** `invoke_agent` on every request, so there is no cross-request state. If you add caching, memory, or long-lived resources, lift them out of the entrypoint.

Payload shape: `{"prompt": "...", "actor_id": "optional-user-id"}` — `prompt` is required; `actor_id` defaults to `"default-user"` and identifies the user across sessions for LTM. Not `query`.

The entrypoint accepts `(payload, context)`. Session ID resolution: `context.session_id` (runtime-injected in production) → `payload.session_id` (optional; pass in local curls to pin a session across requests, useful for testing STM) → fresh UUID.

### Memory

AgentCore memory is configured as `STM_AND_LTM` (id `alpha_memory-cQMHNRHuNG`, overrideable via `MEMORY_ID` env var). Strategies: `user_preferences` (USER_PREFERENCE), `conversation_facts` (SEMANTIC), `summary_builtin` (SUMMARIZATION). The first two share namespace `actor/{actorId}/sessions` so a single `retrieve_memories` call pulls from both.

- **STM (Short-Term Memory):** Each request saves a `(USER, ASSISTANT)` event via `MemoryClient.create_event`. Previous turns for the session are fetched via `MemoryClient.get_last_k_turns` (turn-grouped; prefer over raw `list_events`) and injected into the system prompt.
- **LTM (Long-Term Memory):** Extracted asynchronously from STM events by the AgentCore service. Retrieved before each response via `MemoryClient.retrieve_memories` and injected into the system prompt.
- **LTM namespace:** `actor/{actor_id}/sessions` (see `ltm_namespace()` in `main.py`). Pass this as the `namespace` arg to both `retrieve_memories` and `AgentCoreMemoryToolProvider`.
- **Agent tool:** `AgentCoreMemoryToolProvider` gives the agent the `agent_core_memory` tool, so it can record facts and search past memories on its own initiative (`action='record'` / `action='retrieve'`).

`MemoryClient` is constructed once at module level (singleton) — it is stateless and thread-safe. `AgentCoreMemoryToolProvider` is constructed per-request because it is bound to a specific `session_id` / `actor_id`.

### Streamlit UI (`app.py`)

Chat frontend that invokes the **deployed** runtime via `boto3.client("bedrock-agentcore").invoke_agent_runtime`. Run with `uv run streamlit run app.py`. Does not hit `main.py` locally — it always calls the AWS-hosted agent.

**Gotcha:** `AGENT_ARN` is hardcoded at the top of `app.py`. If you rename the agent or deploy to a new account, update it there (and `ACTOR_ID` / `REGION`).

### Tools

Tools come from `strands_tools` (currently `retrieve`). `retrieve` hits a Bedrock Knowledge Base — it reads `KNOWLEDGE_BASE_ID`, `AWS_REGION`, and `MIN_SCORE` from env (defaults set via `os.environ.setdefault(...)` at the top of `main.py`). To add a tool, import it and append to `tools=[...]` in the `Agent(...)` call.

### Deployment config

`.bedrock_agentcore.yaml` is the source of truth for deployment state (agent name, ARN, ECR repo, execution roles, memory config). Edit via `agentcore configure` rather than hand-editing.

### IAM

Two roles, both auto-created on first launch:
- **Runtime execution role** (`AmazonBedrockAgentCoreSDKRuntime-*`) — assumed by the running container. Needs ECR read, CloudWatch Logs write, Bedrock `InvokeModel`, and `bedrock-agent-runtime:Retrieve` against the Knowledge Base for the `retrieve` tool.
- **CodeBuild role** (`AmazonBedrockAgentCoreSDKCodeBuild-*`) — builds the image.

If `agentcore launch` reuses a pre-existing runtime role from a prior SDK version, it may be missing newer permissions (e.g. ECR pull). Attach missing permissions as an inline policy rather than deleting the role.

### Secrets / env vars at deploy time

`.env` is in `.dockerignore` and is **not** bundled into the image. To pass values to the deployed runtime, use `agentcore launch --env KEY=VALUE` (repeat per var) or fetch from AWS Secrets Manager at module load. Non-secret config with defaults in `main.py` (`KNOWLEDGE_BASE_ID`, `MIN_SCORE`) does not need to be passed.

## Reference docs

- `docs/memory-setup.md` — AgentCore memory resource, strategies, namespaces, rebuild recipe, and gotchas.
- `docs/iam-policies.md` — complete IAM surface of the deployed system: runtime role, CodeBuild role, KB role, AOSS data access policy, failure modes, and hardening path.
- `docs/permissions.md` — IAM + AOSS data access policies for the KB pipeline (three principals, two policy surfaces).
- `docs/kb-setup-troubleshooting.md` — failure modes hit during first-run KB setup (AOSS dual-auth 403s). Read before debugging `retrieve` tool permission issues.

## Diagnostics

- List memories: `aws bedrock-agentcore-control list-memories --region us-east-1`
- Inspect a memory (strategies, namespaces, expiry): `aws bedrock-agentcore-control get-memory --memory-id <id> --region us-east-1`
- List actual deployed runtimes: `aws bedrock-agentcore-control list-agent-runtimes --region us-east-1`
- Inspect runtime IAM policy: `aws iam get-role-policy --role-name AmazonBedrockAgentCoreSDKRuntime-us-east-1-8ed3f6ad68 --policy-name BedrockAgentCoreRuntimeExecutionPolicy-alpha`
- Smoke-test KB retrieve with local creds: `aws bedrock-agent-runtime retrieve --knowledge-base-id <id> --retrieval-query '{"text":"..."}' --region us-east-1 --retrieval-configuration '{"vectorSearchConfiguration":{"numberOfResults":3}}'`

## Gotchas

- **Silent memory failures.** Every `MemoryClient` call in `main.py` is wrapped in `try/except` and logged only at `WARNING`. If memory appears dead, first `list-memories` to confirm the ID exists and inspect the runtime role's `BedrockAgentCoreMemory` statement — don't start by re-reading code.
- **Memory ID must be synced in 4 places.** `main.py:36`, `.bedrock_agentcore.yaml` (`memory_id`), the runtime IAM policy's `Resource` (statement Sid `BedrockAgentCoreMemory`), and the baked container image. The yaml does **not** auto-inject into the container — redeploy with `agentcore launch` after changing `MEMORY_ID`.
- **Runtime ARN drift.** `.bedrock_agentcore.yaml`'s `agent_id`/`agent_arn` and `app.py`'s `AGENT_ARN` can diverge from the real runtime. Verify against `list-agent-runtimes` before debugging invoke failures.
- **Always set `level=` on `basicConfig`.** Default is `WARNING`, which hides `logger.info`/`.debug` in CloudWatch. Pick `INFO` or `DEBUG` explicitly; `DEBUG` at root turns on verbose boto3/botocore logs too, so prefer `INFO` at root and selectively `DEBUG` the module logger if you need memory breadcrumbs without the HTTP noise.
- **Observability gap for direct memory calls.** Only the `agent_core_memory` *tool* emits OTel spans visible in the observability panel. Direct `MemoryClient.create_event` / `retrieve_memories` / `get_last_k_turns` calls show up only as raw `logger.*` output in CloudWatch Logs — debug them there, not in the trace view.
