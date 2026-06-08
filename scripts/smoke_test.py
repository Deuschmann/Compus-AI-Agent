from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from teacher_agent.chatflow_client import build_chatflow_inputs
from teacher_agent.conversation_agent import handle_teacher_message
from teacher_agent.schemas import ClassProfile
from teacher_agent.yml_llm_client import load_prompt_for_intent


def main() -> int:
    saved_env = {
        key: os.environ.get(key)
        for key in [
            "DIFY_CHATFLOW_API_KEY",
            "DIFY_CHATFLOW_API_URL",
            "AGENT_REQUIRE_DIFY",
            "AGENT_ALLOW_LOCAL_FALLBACK",
        ]
    }
    try:
        os.environ.pop("DIFY_CHATFLOW_API_KEY", None)
        os.environ.pop("DIFY_CHATFLOW_API_URL", None)
        os.environ.pop("AGENT_REQUIRE_DIFY", None)
        os.environ.pop("AGENT_ALLOW_LOCAL_FALLBACK", None)
        _check_yml()
        _check_yml_prompt_mapping()
        _check_chatflow_inputs()
        _check_dify_required_by_default()
        os.environ["AGENT_ALLOW_LOCAL_FALLBACK"] = "1"
        _check_permissions_and_generation()
        print("smoke test passed")
        return 0
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _check_yml() -> None:
    data = yaml.safe_load(Path("AI_Agent.yml").read_text(encoding="utf-8"))
    assert data["app"]["mode"] == "advanced-chat"
    start_nodes = [
        node
        for node in data["workflow"]["graph"]["nodes"]
        if node["data"].get("type") == "start"
    ]
    assert start_nodes
    variables = {item["variable"] for item in start_nodes[0]["data"]["variables"]}
    assert {"power", "user_role", "user_id", "session_id"}.issubset(variables)


def _check_chatflow_inputs() -> None:
    student = build_chatflow_inputs(user_role="student", user_id="student_demo")
    teacher = build_chatflow_inputs(user_role="teacher", user_id="teacher_demo")
    assert student["power"] == 0
    assert teacher["power"] == 1


def _check_yml_prompt_mapping() -> None:
    cases = [
        ("ppt", "teacher"),
        ("exam", "teacher"),
        ("student_quiz", "student"),
        ("chatflow", "student"),
        ("chatflow", "teacher"),
        ("class_insights", "teacher"),
    ]
    for intent, role in cases:
        prompt = load_prompt_for_intent(intent, user_role=role)
        assert prompt.strip()


def _check_permissions_and_generation() -> None:
    profile = ClassProfile.from_dict(
        json.loads(Path("examples/class_profile.json").read_text(encoding="utf-8"))
    )
    denied = handle_teacher_message(
        "帮我生成一份关于 Logistic 回归的正式试卷",
        profile=profile,
        user_role="student",
        user_id="student_demo",
        session_id="smoke-student",
    )
    assert denied["intent"] == "denied"

    practice = handle_teacher_message(
        "给我3道关于 Logistic 回归的模拟题",
        profile=profile,
        user_role="student",
        user_id="student_demo",
        session_id="smoke-student",
    )
    assert practice["intent"] == "student_quiz"
    assert Path(practice["artifacts"][0]["path"]).exists()

    exam = handle_teacher_message(
        "帮我生成一份关于 Logistic 回归的正式试卷",
        profile=profile,
        user_role="teacher",
        user_id="teacher_demo",
        session_id="smoke-teacher",
    )
    assert exam["intent"] == "exam"
    assert Path(exam["artifacts"][0]["path"]).exists()


def _check_dify_required_by_default() -> None:
    profile = ClassProfile.from_dict(
        json.loads(Path("examples/class_profile.json").read_text(encoding="utf-8"))
    )
    response = handle_teacher_message(
        "帮我生成一份关于 Logistic 回归的正式试卷",
        profile=profile,
        user_role="teacher",
        user_id="teacher_demo",
        session_id="smoke-teacher",
    )
    assert response["source"] == "dify_required_failed"
    assert not response["artifacts"]


if __name__ == "__main__":
    raise SystemExit(main())
