from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests
import yaml


DEFAULT_YML_PATH = Path("AI_Agent.yml")
DEFAULT_MODEL = "deepseek-chat"

INTENT_NODE_IDS = {
    "ppt": "17804732655651",
    "exam": "llm",
    "student_quiz": "17804731933141",
    "student_chat": "17804731767760",
    "teacher_chat": "1780473332505",
    "class_insights": "1780473332505",
}


class YmlLLMClient:
    """Use AI_Agent.yml prompts with an OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_base: str | None = None,
        model: str | None = None,
        timeout: int = 120,
    ) -> None:
        self.api_key = _normalize_api_key(
            api_key or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        )
        self.api_bases = [
            _select_api_base(
                api_base,
                os.getenv("LLM_API_BASE_URL"),
                os.getenv("OPENAI_API_BASE"),
                os.getenv("OPENAI_BASE_URL"),
                "https://api.deepseek.com",
            )
        ]
        self.model = model or os.getenv("LLM_MODEL") or DEFAULT_MODEL
        self.timeout = timeout

    def send(
        self,
        *,
        intent: str,
        user_role: str,
        user_id: str,
        message: str,
        context: str = "",
    ) -> str:
        if not self.api_key:
            raise RuntimeError(
                "缺少模型 API Key。请设置 LLM_API_KEY 或 OPENAI_API_KEY。"
            )

        system_prompt = load_prompt_for_intent(intent, user_role=user_role)
        user_prompt = (
            f"用户角色：{user_role}\n"
            f"用户ID：{user_id}\n"
            f"用户请求：{message}\n"
        )
        if context:
            user_prompt += f"\n可用本地上下文：\n{context}\n"

        errors = []
        for api_base in self.api_bases:
            response = requests.post(
                _chat_completions_url(api_base),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.7,
                },
                timeout=self.timeout,
            )
            if not response.ok:
                detail = response.text.strip()
                if len(detail) > 300:
                    detail = detail[:300] + "..."
                errors.append(
                    f"{_redact_url(api_base)} -> HTTP {response.status_code} {response.reason}. {detail}"
                )
                continue
            payload = response.json()
            try:
                return payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"模型响应格式不符合预期：{payload}") from exc

        raise RuntimeError("本地 YML+LLM 调用失败：" + "；".join(errors))


def is_yml_llm_configured() -> bool:
    return bool(os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY"))


def load_prompt_for_intent(
    intent: str,
    *,
    user_role: str,
    yml_path: Path = DEFAULT_YML_PATH,
) -> str:
    node_id = _node_id_for_intent(intent, user_role=user_role)
    data = yaml.safe_load(yml_path.read_text(encoding="utf-8"))
    for node in data["workflow"]["graph"]["nodes"]:
        if node["id"] == node_id:
            return _extract_system_prompt(node["data"])
    raise RuntimeError(f"AI_Agent.yml 中没有找到节点 {node_id}")


def _node_id_for_intent(intent: str, *, user_role: str) -> str:
    if intent == "chatflow":
        return (
            INTENT_NODE_IDS["student_chat"]
            if user_role == "student"
            else INTENT_NODE_IDS["teacher_chat"]
        )
    return INTENT_NODE_IDS.get(intent, "student_chat")


def _extract_system_prompt(node_data: dict[str, Any]) -> str:
    prompts = node_data.get("prompt_template") or node_data.get("prompt_templates") or []
    for prompt in prompts:
        if prompt.get("role") == "system" and prompt.get("text"):
            return prompt["text"]
    return (
        "你是生物统计学教学 Agent。请根据用户角色、用户请求和上下文，"
        "用中文给出严谨、清晰、可执行的回答。"
    )


def _chat_completions_url(api_base: str) -> str:
    cleaned = api_base.rstrip("/")
    if cleaned.endswith("/chat/completions"):
        return cleaned
    return f"{cleaned}/chat/completions"


def _select_api_base(*values: str | None) -> str:
    for value in values:
        if not value:
            continue
        return value.rstrip("/")
    return "https://api.deepseek.com"


def _redact_url(api_base: str) -> str:
    return api_base.rstrip("/")


def _normalize_api_key(api_key: str) -> str:
    cleaned = api_key.strip()
    if cleaned.lower().startswith("bearer "):
        return cleaned[7:].strip()
    return cleaned
