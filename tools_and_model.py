import os
from functools import lru_cache

from langchain_core.tools import tool


@tool
def multiply(a: int, b: int) -> int:
    """Multiply two integers."""

    return a * b


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""

    return a + b


@tool
def divide(a: int, b: int) -> float:
    """Divide two integers."""

    return a / b


tools = [add, multiply, divide]
tools_by_name = {item.name: item for item in tools}


@lru_cache(maxsize=1)
def get_model_with_tools():
    """Build the chat model only when the agent is invoked."""

    try:
        from langchain.chat_models import init_chat_model
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "缺少 langchain 依赖。请先安装 langchain 和对应模型 provider，"
            "或改用已安装 provider 的 ChatModel。"
        ) from exc

    model_name = os.getenv("AGENT_MODEL", "deepseek-chat")
    model_provider = os.getenv("AGENT_MODEL_PROVIDER", "openai")
    temperature = float(os.getenv("AGENT_TEMPERATURE", "0"))
    base_url = (
        os.getenv("AGENT_BASE_URL")
        or os.getenv("LLM_API_BASE_URL")
        or os.getenv("OPENAI_API_BASE")
        or os.getenv("OPENAI_BASE_URL")
        or "https://api.deepseek.com"
    )
    api_key = (
        os.getenv("AGENT_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )

    model_kwargs = {
        "model": model_name,
        "model_provider": model_provider,
        "temperature": temperature,
    }
    if api_key:
        model_kwargs["api_key"] = api_key
    if base_url:
        model_kwargs["base_url"] = base_url

    model = init_chat_model(**model_kwargs)
    return model.bind_tools(tools)
