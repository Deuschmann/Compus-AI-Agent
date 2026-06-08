from langchain_core.messages import SystemMessage

from state import MessagesState
from tools_and_model import get_model_with_tools


def llm_call(state: MessagesState):
    """Ask the LLM to answer directly or request a tool call."""

    model_with_tools = get_model_with_tools()
    return {
        "messages": [
            model_with_tools.invoke(
                [
                    SystemMessage(
                        content="You are a helpful assistant tasked with performing arithmetic on a set of inputs."
                    )
                ]
                + state["messages"]
            )
        ],
        "llm_calls": state.get("llm_calls", 0) + 1,
    }
