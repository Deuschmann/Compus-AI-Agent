from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from teacher_agent.schemas import OutputBundle, ensure_output_dir, slugify


DEFAULT_STUDENT_PROFILES_PATH = Path("examples/student_profiles.json")


@dataclass
class StudentPracticeRequest:
    student_id: str
    topic: str = ""
    question_count: int = 6
    output_dir: str | Path = "outputs"
    profiles_path: str | Path = DEFAULT_STUDENT_PROFILES_PATH


def generate_student_practice(request: StudentPracticeRequest) -> OutputBundle:
    profile = load_student_profile(request.student_id, request.profiles_path)
    weak_points = profile.get("weak_points", [])
    target_points = _select_target_points(weak_points, request.topic)

    output_dir = ensure_output_dir(request.output_dir)
    base_name = slugify(f"{request.student_id}_{request.topic or '个人薄弱点'}_模拟练习")
    path = output_dir / f"{base_name}.md"
    path.write_text(
        _render_practice(profile, target_points, request.question_count),
        encoding="utf-8",
    )
    return OutputBundle(
        source_path=path,
        metadata={
            "student_id": request.student_id,
            "question_count": request.question_count,
            "target_points": [item["name"] for item in target_points],
        },
    )


def load_student_profile(
    student_id: str,
    profiles_path: str | Path = DEFAULT_STUDENT_PROFILES_PATH,
) -> dict:
    profiles = json.loads(Path(profiles_path).read_text(encoding="utf-8"))
    if student_id not in profiles:
        raise KeyError(f"没有找到学生画像：{student_id}")
    return profiles[student_id]


def _select_target_points(weak_points: list[dict], topic: str) -> list[dict]:
    if topic:
        selected = [item for item in weak_points if topic in item.get("name", "")]
        if selected:
            return selected
    return weak_points[:3]


def _render_practice(profile: dict, target_points: list[dict], question_count: int) -> str:
    lines = [
        f"# {profile.get('name', profile['student_id'])} 的个性化模拟练习",
        "",
        "这份练习只根据该学生自己的薄弱点生成，不包含老师正式考试题。",
        "",
        "## 针对薄弱点",
        "",
    ]
    for item in target_points:
        lines.append(f"- {item['name']}，当前掌握度：{item.get('mastery', '未知')}")
    lines.extend(["", "## 练习题", ""])

    if not target_points:
        target_points = [{"name": "综合复习", "recent_errors": []}]

    for index in range(1, question_count + 1):
        point = target_points[(index - 1) % len(target_points)]
        errors = point.get("recent_errors", [])
        error_note = f"注意避免：{'、'.join(errors)}" if errors else "注意解释你的判断理由。"
        lines.extend(
            [
                f"### {index}. {point['name']}",
                "",
                f"请围绕“{point['name']}”完成一道概念解释或方法选择题，并说明理由。",
                "",
                f"参考提醒：{error_note}",
                "",
            ]
        )
    return "\n".join(lines)
