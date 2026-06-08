from langchain_core.messages import ToolMessage

from state import MessagesState
from tools_and_model import tools_by_name


def tool_node(state: MessagesState):
    """Execute tool calls requested by the last AI message."""

    result = []
    for tool_call in state["messages"][-1].tool_calls:
        tool = tools_by_name[tool_call["name"]]
        observation = tool.invoke(tool_call["args"])
        result.append(ToolMessage(content=str(observation), tool_call_id=tool_call["id"]))
    return {"messages": result}
