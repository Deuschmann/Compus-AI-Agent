import argparse
import sys

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph

from end_logic import should_continue
from model_node import llm_call
from state import MessagesState
from tool_node import tool_node


def build_agent():
    agent_builder = StateGraph(MessagesState)

    agent_builder.add_node("llm_call", llm_call)
    agent_builder.add_node("tool_node", tool_node)

    agent_builder.add_edge(START, "llm_call")
    agent_builder.add_conditional_edges(
        "llm_call",
        should_continue,
        ["tool_node", END],
    )
    agent_builder.add_edge("tool_node", "llm_call")

    return agent_builder.compile()


agent = build_agent()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the LangGraph arithmetic agent")
    parser.add_argument(
        "message",
        nargs="?",
        default="Add 3 and 4.",
        help="User message to send to the agent",
    )
    parser.add_argument(
        "--show-graph",
        action="store_true",
        help="Print the graph Mermaid source instead of invoking the agent",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.show_graph:
        print(agent.get_graph(xray=True).draw_mermaid())
        return 0

    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=args.message)], "llm_calls": 0}
        )
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    for message in result["messages"]:
        message.pretty_print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
