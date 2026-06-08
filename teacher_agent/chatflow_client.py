from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import requests


DEFAULT_CHATFLOW_URL = "http://localhost/v1/chat-messages"


@dataclass
class ChatflowRequest:
    query: str
    user: str
    inputs: dict[str, Any] = field(default_factory=dict)
    conversation_id: str | None = None
    response_mode: str = "blocking"


class DifyChatflowClient:
    """Small adapter reserved for future Dify Chatflow calls."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_url: str | None = None,
        timeout: int = 120,
    ) -> None:
        self.api_key = _normalize_api_key(
            api_key or os.getenv("DIFY_CHATFLOW_API_KEY") or ""
        )
        self.api_url = _normalize_chatflow_url(
            api_url or os.getenv("DIFY_CHATFLOW_API_URL", DEFAULT_CHATFLOW_URL)
        )
        self.timeout = timeout

    def send(self, request: ChatflowRequest) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError(
                "缺少 Dify Chatflow API Key。请设置 DIFY_CHATFLOW_API_KEY。"
            )

        payload: dict[str, Any] = {
            "inputs": request.inputs,
            "query": request.query,
            "response_mode": request.response_mode,
            "user": request.user,
        }
        if request.conversation_id:
            payload["conversation_id"] = request.conversation_id

        response = requests.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout,
        )
        if not response.ok:
            detail = response.text.strip()
            if len(detail) > 500:
                detail = detail[:500] + "..."
            raise RuntimeError(
                f"Dify Chatflow 调用失败：HTTP {response.status_code} {response.reason}. {detail}"
            )
        return response.json()


def build_chatflow_inputs(
    *,
    user_role: str,
    user_id: str,
    session_id: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build inputs expected by the current AI_Agent.yml Chatflow.

    AI_Agent.yml currently uses `power = 0` for student branches and non-zero for
    teacher branches. We also include explicit role fields for future yml updates.
    """

    inputs: dict[str, Any] = {
        "power": 0 if user_role == "student" else 1,
        "user_role": user_role,
        "user_id": user_id,
        "session_id": session_id,
    }
    if extra:
        inputs.update(extra)
    return inputs


def is_chatflow_configured() -> bool:
    return bool(os.getenv("DIFY_CHATFLOW_API_KEY"))


def _normalize_api_key(api_key: str) -> str:
    cleaned = api_key.strip()
    if cleaned.lower().startswith("bearer "):
        return cleaned[7:].strip()
    return cleaned


def _normalize_chatflow_url(api_url: str) -> str:
    """Accept either the full chat endpoint or a Dify API base URL."""
    cleaned = api_url.rstrip("/")
    if cleaned.endswith("/chat-messages"):
        return cleaned
    if cleaned.endswith("/v1"):
        return f"{cleaned}/chat-messages"
    return cleaned
