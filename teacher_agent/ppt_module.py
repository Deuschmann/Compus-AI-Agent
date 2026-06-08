from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from teacher_agent.schemas import ClassProfile, OutputBundle, ensure_output_dir, slugify


@dataclass
class PPTRequest:
    topic: str
    class_profile: ClassProfile
    learning_objectives: list[str] = field(default_factory=list)
    slide_count: int = 10
    output_dir: str | Path = "outputs"


@dataclass
class SlideSpec:
    title: str
    bullets: list[str]
    speaker_notes: str = ""


def build_slide_plan(request: PPTRequest) -> list[SlideSpec]:
    weak_points = request.class_profile.weak_points
    objectives = request.learning_objectives or [
        f"理解 {request.topic} 的核心概念",
        "能够解释方法适用条件",
        "能够根据真实问题选择合适的统计方法",
    ]

    slides = [
        SlideSpec(
            title=f"{request.topic}",
            bullets=[
                request.class_profile.course_name,
                f"面向：{request.class_profile.class_name}",
                "基于班级学习画像动态调整",
            ],
            speaker_notes="开场时说明本节课会重点照顾班级薄弱点。",
        ),
        SlideSpec(
            title="本节学习目标",
            bullets=objectives,
        ),
        SlideSpec(
            title="班级学习画像反馈",
            bullets=_profile_bullets(request.class_profile),
            speaker_notes="这里用于把后续讲解和学生真实困难连接起来。",
        ),
    ]

    slides.extend(_topic_specific_slides(request.topic))

    for item in weak_points:
        bullets = [
            f"当前掌握度：{_format_mastery(item.mastery)}",
            f"优先级：{item.priority}",
        ]
        bullets.extend(f"常见误区：{error}" for error in item.common_errors[:3])
        bullets.append("教学处理：先用直觉例子解释，再回到公式和适用条件。")
        slides.append(
            SlideSpec(
                title=f"重点补强：{item.name}",
                bullets=bullets,
                speaker_notes=f"围绕 {item.name} 安排一次即时提问或小练习。",
            )
        )

    slides.extend(
        [
            SlideSpec(
                title="核心概念框架",
                bullets=[
                    "研究问题：变量、总体、样本和假设",
                    "统计模型：假设、参数、估计和不确定性",
                    "解释结果：效应量、置信区间和实际意义",
                ],
            ),
            SlideSpec(
                title="课堂例题",
                bullets=[
                    f"围绕 {request.topic} 设计一个真实生物医学数据情境",
                    "先让学生判断变量类型和研究问题",
                    "再选择统计方法并解释结果",
                ],
            ),
            SlideSpec(
                title="即时练习",
                bullets=[
                    "1 道概念判断题",
                    "1 道方法选择题",
                    "1 道结果解释题",
                ],
            ),
            SlideSpec(
                title="课后巩固",
                bullets=[
                    "复盘今天最容易混淆的概念",
                    "完成针对薄弱点的短练习",
                    "把错题原因反馈给学习画像",
                ],
            ),
        ]
    )

    return slides[: max(4, request.slide_count)]


def _topic_specific_slides(topic: str) -> list[SlideSpec]:
    normalized = topic.lower()
    if "logistic" in normalized or "逻辑" in topic or "logit" in normalized:
        return [
            SlideSpec(
                title="Logistic 回归要解决的问题",
                bullets=[
                    "结局变量是二分类或事件发生/不发生",
                    "模型输出的是对数优势，而不是直接概率差",
                    "适合分析风险因素、诊断指标和疾病结局",
                ],
            ),
            SlideSpec(
                title="比值比的解释",
                bullets=[
                    "OR > 1 表示暴露组优势增加",
                    "OR < 1 表示暴露组优势降低",
                    "解释时要同时看置信区间和研究设计",
                ],
            ),
            SlideSpec(
                title="学生常见误区",
                bullets=[
                    "把 OR 直接解释成概率差",
                    "只看 p 值，不看效应大小",
                    "忽略混杂因素和模型诊断",
                ],
            ),
        ]
    if "多重" in topic or "fdr" in normalized or "fwer" in normalized:
        return [
            SlideSpec(
                title="为什么会有多重检验问题",
                bullets=[
                    "检验次数越多，偶然显著的概率越高",
                    "基因组数据常同时检验成千上万个基因",
                    "不校正会夸大发现数量",
                ],
            ),
            SlideSpec(
                title="FWER 与 FDR",
                bullets=[
                    "FWER 控制至少一个假阳性的概率",
                    "FDR 控制发现集合中的假阳性比例",
                    "高通量筛选中常优先考虑 FDR",
                ],
            ),
            SlideSpec(
                title="Benjamini-Hochberg 思路",
                bullets=[
                    "将 p 值从小到大排序",
                    "与逐步放宽的阈值比较",
                    "找到可接受的发现集合",
                ],
            ),
        ]
    if "配对" in topic or "paired" in normalized:
        return [
            SlideSpec(
                title="配对设计的核心",
                bullets=[
                    "同一对象或匹配对象形成一对观测",
                    "分析对象是每一对的差值",
                    "配对能减少个体差异带来的噪声",
                ],
            ),
            SlideSpec(
                title="配对 t 检验条件",
                bullets=[
                    "差值近似来自正态分布",
                    "配对关系来自设计而不是事后强行匹配",
                    "异常差值需要结合背景判断",
                ],
            ),
            SlideSpec(
                title="结果报告",
                bullets=[
                    "报告平均差值和置信区间",
                    "解释方向和实际意义",
                    "说明样本量和检验假设",
                ],
            ),
        ]
    return [
        SlideSpec(
            title=f"{topic} 的问题框架",
            bullets=[
                "先明确研究问题和变量类型",
                "再判断统计方法的适用条件",
                "最后解释结果的不确定性和实际意义",
            ],
        )
    ]


def generate_ppt(request: PPTRequest) -> OutputBundle:
    output_dir = ensure_output_dir(request.output_dir)
    base_name = slugify(f"{request.topic}_课件")
    slides = build_slide_plan(request)

    markdown_path = output_dir / f"{base_name}.md"
    markdown_path.write_text(_render_markdown(request, slides), encoding="utf-8")

    pptx_path = _try_render_pptx(output_dir / f"{base_name}.pptx", slides)
    return OutputBundle(
        source_path=markdown_path,
        artifact_path=pptx_path,
        metadata={
            "topic": request.topic,
            "slide_count": len(slides),
            "pptx_available": pptx_path is not None,
        },
    )


def _profile_bullets(profile: ClassProfile) -> list[str]:
    bullets = []
    if profile.weak_points:
        weak = "、".join(item.name for item in profile.weak_points[:4])
        bullets.append(f"主要薄弱点：{weak}")
    if profile.strong_points:
        bullets.append(f"优势基础：{'、'.join(profile.strong_points[:4])}")
    bullets.extend(profile.teacher_notes[:3])
    return bullets or ["暂无画像数据，先按章节基础目标授课。"]


def _format_mastery(value: float | None) -> str:
    if value is None:
        return "未知"
    return f"{value:.0%}"


def _render_markdown(request: PPTRequest, slides: list[SlideSpec]) -> str:
    lines = [
        f"# {request.topic} 课件",
        "",
        f"- 课程：{request.class_profile.course_name}",
        f"- 班级：{request.class_profile.class_name}",
        f"- 幻灯片数：{len(slides)}",
        "",
    ]
    for index, slide in enumerate(slides, start=1):
        lines.extend([f"## {index}. {slide.title}", ""])
        lines.extend(f"- {bullet}" for bullet in slide.bullets)
        if slide.speaker_notes:
            lines.extend(["", f"> 讲者备注：{slide.speaker_notes}"])
        lines.append("")
    return "\n".join(lines)


def _try_render_pptx(path: Path, slides: list[SlideSpec]) -> Path | None:
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
    except ModuleNotFoundError:
        return None

    presentation = Presentation()
    presentation.slide_width = Inches(13.333)
    presentation.slide_height = Inches(7.5)

    for index, slide in enumerate(slides):
        layout = presentation.slide_layouts[0] if index == 0 else presentation.slide_layouts[1]
        ppt_slide = presentation.slides.add_slide(layout)
        ppt_slide.shapes.title.text = slide.title

        placeholder = ppt_slide.placeholders[1]
        text_frame = placeholder.text_frame
        text_frame.clear()
        for bullet_index, bullet in enumerate(slide.bullets):
            paragraph = text_frame.paragraphs[0] if bullet_index == 0 else text_frame.add_paragraph()
            paragraph.text = bullet
            paragraph.level = 0
            paragraph.font.size = Pt(24 if index == 0 else 20)
            paragraph.font.name = "Microsoft YaHei"

        if slide.speaker_notes:
            notes = ppt_slide.notes_slide.notes_text_frame
            notes.text = slide.speaker_notes

    presentation.save(path)
    return path
