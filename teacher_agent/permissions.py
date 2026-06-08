from __future__ import annotations


ROLE_PERMISSIONS = {
    "teacher": {
        "ppt",
        "exam",
        "kb_search",
        "read_pdf",
        "student_quiz",
        "teacher_quiz",
        "quiz_submit",
        "student_summary",
        "class_insights",
        "help",
        "chatflow",
    },
    "student": {
        "student_quiz",
        "quiz_submit",
        "student_summary",
        "kb_search",
        "read_pdf",
        "help",
        "chatflow",
    },
}


DENIAL_MESSAGES = {
    ("student", "exam"): (
        "你当前是学生权限，不能生成或读取老师用于考试的正式试卷。"
        "我可以根据你自己的薄弱点生成模拟练习题。"
    ),
    ("student", "ppt"): "你当前是学生权限，不能生成老师课件。可以请求知识点讲解或模拟练习。",
}


def is_allowed(user_role: str, intent: str) -> bool:
    return intent in ROLE_PERMISSIONS.get(user_role, set())


def denial_message(user_role: str, intent: str) -> str:
    return DENIAL_MESSAGES.get(
        (user_role, intent),
        f"当前角色 {user_role} 没有执行 {intent} 的权限。",
    )
