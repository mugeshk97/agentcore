"""A2A-over-AgentCore client helpers."""

from __future__ import annotations

import logging
from typing import Any, Generator
from uuid import uuid4

import boto3
import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Message,
    Part,
    Role,
    TextPart,
)
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from strands import tool

logger = logging.getLogger(__name__)


class SigV4HttpxAuth(httpx.Auth):
    """Signs each outbound httpx request with AWS SigV4.

    Credentials are resolved lazily per call via boto3.Session — this
    picks up rotating task-role creds inside an AgentCore container
    without us having to cache or refresh anything.
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


def _stub_agent_card(url: str, name: str, description: str) -> AgentCard:
    """Hand-built AgentCard — skips GET /.well-known/... discovery.

    The card only needs the URL and enough metadata to satisfy the a2a
    ClientFactory. The remote server ignores the card during message
    dispatch; it is used locally to pick a transport.
    """
    return AgentCard(
        name=name,
        description=description,
        url=url,
        version="0.1.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="main",
                name=name,
                description=description,
                tags=["main"],
            )
        ],
    )


def _extract_text(event: Any) -> str:
    if isinstance(event, Message):
        parts = event.parts or []
        return "\n".join(
            p.root.text
            for p in parts
            if hasattr(p.root, "text")
        ) or "(empty message)"
    if isinstance(event, tuple) and len(event) == 2:
        task, _update = event
        artifacts = getattr(task, "artifacts", None) or []
        texts: list[str] = []
        for artifact in artifacts:
            for part in artifact.parts or []:
                inner = getattr(part, "root", part)
                text = getattr(inner, "text", None)
                if text:
                    texts.append(text)
        if texts:
            return "\n".join(texts)
        status = getattr(task, "status", None)
        state = getattr(status, "state", None)
        return f"(task completed with state={state!r}, no text artifact)"
    return f"(unexpected event type: {type(event).__name__})"


def make_a2a_tool(
    *,
    tool_name: str,
    tool_description: str,
    remote_name: str,
    remote_description: str,
    runtime_url: str,
    region: str,
):
    """Return a Strands @tool that proxies a single query to a remote
    A2A agent. Auto-selects SigV4 auth for https://, plain http otherwise.
    """
    use_sigv4 = runtime_url.startswith("https://")

    @tool(name=tool_name, description=tool_description)
    async def _proxy(query: str) -> str:
        auth = (
            SigV4HttpxAuth("bedrock-agentcore", region)
            if use_sigv4
            else None
        )
        try:
            async with httpx.AsyncClient(
                auth=auth, timeout=300.0
            ) as http_client:
                card = _stub_agent_card(
                    url=runtime_url,
                    name=remote_name,
                    description=remote_description,
                )
                factory = ClientFactory(
                    ClientConfig(httpx_client=http_client, streaming=False)
                )
                client = factory.create(card)
                message = Message(
                    kind="message",
                    role=Role.user,
                    message_id=uuid4().hex,
                    parts=[Part(TextPart(kind="text", text=query))],
                )
                async for event in client.send_message(message):
                    return _extract_text(event)
                return f"(no response from {tool_name})"
        except Exception as exc:  # noqa: BLE001 - surface to LLM
            logger.exception("a2a tool %s failed", tool_name)
            return f"Specialist {tool_name} call failed: {exc}"

    return _proxy
