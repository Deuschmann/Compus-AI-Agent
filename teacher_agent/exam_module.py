from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from teacher_agent.schemas import ClassProfile, OutputBundle, ensure_output_dir, slugify


LATEX_COMPILE_TIMEOUT_SECONDS = 45


@dataclass
class ExamRequest:
    title: str
    class_profile: ClassProfile
    topics: list[str] = field(default_factory=list)
    duration_minutes: int = 90
    total_points: int = 100
    difficulty: str = "medium"
    output_dir: str | Path = "outputs"
    compile_pdf: bool = True


@dataclass
class ExamQuestion:
    section: str
    prompt: str
    points: int
    answer_hint: str


def build_exam_questions(request: ExamRequest) -> list[ExamQuestion]:
    weak_names = [item.name for item in request.class_profile.weak_points]
    topics = request.topics or weak_names or ["假设检验", "置信区间", "回归分析"]

    questions = [
        ExamQuestion(
            section="一、概念辨析",
            prompt=f"解释 {topics[0]} 的基本思想，并说明它和描述性统计的区别。",
            points=10,
            answer_hint="应包含研究问题、总体/样本、不确定性和推断目标。",
        ),
        ExamQuestion(
            section="一、概念辨析",
            prompt="说明 p 值的含义，并指出一个常见误解。",
            points=10,
            answer_hint="p 值不是原假设为真的概率，也不是效应大小。",
        ),
        ExamQuestion(
            section="二、方法选择",
            prompt="某研究比较同一批样本在干预前后的指标变化，应优先考虑哪类检验？说明理由。",
            points=12,
            answer_hint="配对设计；关注差值分布及检验假设。",
        ),
        ExamQuestion(
            section="二、方法选择",
            prompt="给定一个二分类结局和多个解释变量，如何选择合适模型？需要检查哪些条件？",
            points=14,
            answer_hint="可考虑 Logistic 回归；解释变量编码、共线性、样本量和模型诊断。",
        ),
    ]

    for weak_point in weak_names[:3]:
        questions.append(
            ExamQuestion(
                section="三、针对性补弱题",
                prompt=f"围绕“{weak_point}”设计一个生物医学研究场景，并说明应如何完成统计分析。",
                points=12,
                answer_hint="场景、变量类型、统计方法、结果解释和局限性都应覆盖。",
            )
        )

    questions.extend(
        [
            ExamQuestion(
                section="四、综合应用",
                prompt=(
                    "某研究收集两组患者的连续型生物标志物、年龄、性别和疾病结局。"
                    "请给出完整分析方案，包括描述、建模、结果解释和报告方式。"
                ),
                points=20,
                answer_hint="应体现研究问题、变量类型、混杂控制、效应量和置信区间。",
            ),
            ExamQuestion(
                section="五、结果解释",
                prompt=(
                    "如果某基因表达量与疾病风险的比值比为 1.45，95% 置信区间为 "
                    "[1.10, 1.91]，请解释其统计意义和实际意义。"
                ),
                points=10,
                answer_hint="方向、效应大小、区间不确定性和因果解释边界。",
            ),
        ]
    )

    return _scale_points(questions, request.total_points)


def generate_exam(request: ExamRequest) -> OutputBundle:
    output_dir = ensure_output_dir(request.output_dir)
    base_name = slugify(request.title)
    questions = build_exam_questions(request)

    tex_path = output_dir / f"{base_name}.tex"
    tex_path.write_text(_render_latex(request, questions), encoding="utf-8")

    pdf_path = _compile_latex(tex_path) if request.compile_pdf else None
    return OutputBundle(
        source_path=tex_path,
        artifact_path=pdf_path,
        metadata={
            "title": request.title,
            "question_count": len(questions),
            "pdf_available": pdf_path is not None,
        },
    )


def _scale_points(questions: list[ExamQuestion], total_points: int) -> list[ExamQuestion]:
    current_total = sum(question.points for question in questions)
    if current_total == total_points:
        return questions

    ratio = total_points / current_total
    scaled = []
    running_total = 0
    for question in questions[:-1]:
        points = max(1, round(question.points * ratio))
        running_total += points
        scaled.append(
            ExamQuestion(
                section=question.section,
                prompt=question.prompt,
                points=points,
                answer_hint=question.answer_hint,
            )
        )

    last = questions[-1]
    scaled.append(
        ExamQuestion(
            section=last.section,
            prompt=last.prompt,
            points=max(1, total_points - running_total),
            answer_hint=last.answer_hint,
        )
    )
    return scaled


def _render_latex(request: ExamRequest, questions: list[ExamQuestion]) -> str:
    grouped: dict[str, list[ExamQuestion]] = {}
    for question in questions:
        grouped.setdefault(question.section, []).append(question)

    body_lines = []
    question_index = 1
    for section, section_questions in grouped.items():
        body_lines.extend([f"\\section*{{{_latex_escape(section)}}}", "\\begin{enumerate}"])
        for question in section_questions:
            body_lines.extend(
                [
                    f"\\item [{question_index}.] ({question.points} 分) "
                    f"{_latex_escape(question.prompt)}",
                    "\\vspace{2.2cm}",
                    f"\\textbf{{参考要点：}} {_latex_escape(question.answer_hint)}",
                    "\\vspace{0.5cm}",
                ]
            )
            question_index += 1
        body_lines.append("\\end{enumerate}")

    topics = "、".join(request.topics) if request.topics else "综合复习"
    weak_points = "、".join(item.name for item in request.class_profile.weak_points[:5]) or "暂无"

    return f"""\\documentclass[UTF8,12pt]{{ctexart}}
\\usepackage[a4paper,margin=2.2cm]{{geometry}}
\\usepackage{{amsmath,amssymb}}
\\usepackage{{enumitem}}
\\usepackage{{fancyhdr}}
\\pagestyle{{fancy}}
\\fancyhf{{}}
\\lhead{{{_latex_escape(request.class_profile.course_name)}}}
\\rhead{{{_latex_escape(request.class_profile.class_name)}}}
\\cfoot{{\\thepage}}

\\title{{{_latex_escape(request.title)}}}
\\author{{AI 教学 Agent 生成草稿}}
\\date{{}}

\\begin{{document}}
\\maketitle

\\noindent
\\textbf{{考试时间：}} {request.duration_minutes} 分钟\\quad
\\textbf{{满分：}} {request.total_points} 分\\quad
\\textbf{{难度：}} {_latex_escape(request.difficulty)}

\\noindent
\\textbf{{覆盖主题：}} {_latex_escape(topics)}

\\noindent
\\textbf{{针对班级薄弱点：}} {_latex_escape(weak_points)}

\\vspace{{0.5cm}}

{chr(10).join(body_lines)}

\\end{{document}}
"""


def _compile_latex(tex_path: Path) -> Path | None:
    compiler = shutil.which("xelatex") or shutil.which("pdflatex")
    tectonic = shutil.which("tectonic")

    if not compiler and tectonic:
        return _compile_with_tectonic(tex_path, tectonic)

    if not compiler:
        return None

    command = [
        compiler,
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-output-directory",
        str(tex_path.parent),
        str(tex_path),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=LATEX_COMPILE_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return tex_path.with_suffix(".pdf")


def _compile_with_tectonic(tex_path: Path, compiler: str) -> Path | None:
    command = [
        compiler,
        "-o",
        str(tex_path.parent),
        str(tex_path),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=LATEX_COMPILE_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return tex_path.with_suffix(".pdf")


def _latex_escape(text: str) -> str:
    replacements = {
        "\\": "\\textbackslash{}",
        "&": "\\&",
        "%": "\\%",
        "$": "\\$",
        "#": "\\#",
        "_": "\\_",
        "{": "\\{",
        "}": "\\}",
        "~": "\\textasciitilde{}",
        "^": "\\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in str(text))
