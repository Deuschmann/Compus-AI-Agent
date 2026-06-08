from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class KnowledgePointStat:
    name: str
    mastery: float | None = None
    common_errors: list[str] = field(default_factory=list)
    priority: str = "medium"


@dataclass
class ClassProfile:
    course_name: str = "生物统计学"
    class_name: str = "默认班级"
    class_size: int | None = None
    weak_points: list[KnowledgePointStat] = field(default_factory=list)
    strong_points: list[str] = field(default_factory=list)
    teacher_notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClassProfile":
        weak_points = [
            KnowledgePointStat(
                name=item["name"],
                mastery=item.get("mastery"),
                common_errors=list(item.get("common_errors", [])),
                priority=item.get("priority", "medium"),
            )
            for item in data.get("weak_points", [])
        ]
        return cls(
            course_name=data.get("course_name", "生物统计学"),
            class_name=data.get("class_name", "默认班级"),
            class_size=data.get("class_size"),
            weak_points=weak_points,
            strong_points=list(data.get("strong_points", [])),
            teacher_notes=list(data.get("teacher_notes", [])),
        )


@dataclass
class OutputBundle:
    source_path: Path
    artifact_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def ensure_output_dir(output_dir: str | Path) -> Path:
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def slugify(text: str) -> str:
    safe = []
    for char in text.strip():
        if char.isalnum():
            safe.append(char)
        elif char in {" ", "-", "_"}:
            safe.append("_")
    slug = "".join(safe).strip("_")
    return slug or "artifact"
