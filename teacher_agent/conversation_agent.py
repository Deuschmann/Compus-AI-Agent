from __future__ import annotations

import os
import json
import re
from pathlib import Path

from teacher_agent.chatflow_client import (
    ChatflowRequest,
    DifyChatflowClient,
    build_chatflow_inputs,
)
from teacher_agent.exam_module import ExamRequest, generate_exam
from teacher_agent.knowledge_db import get_item, read_pages, search_items, search_pages
from teacher_agent.permissions import denial_message, is_allowed
from teacher_agent.ppt_module import PPTRequest, generate_ppt
from teacher_agent.quiz_module import (
    export_attempt_markdown,
    export_score_markdown,
    get_attempt,
    get_attempt_questions,
    list_quiz_sets,
    start_quiz,
    submit_quiz,
)
from teacher_agent.schemas import ClassProfile, ensure_output_dir, slugify
from teacher_agent.session_store import get_session, update_dify_conversation_id
from teacher_agent.student_practice_module import (
    load_student_profile,
)
from teacher_agent.yml_llm_client import YmlLLMClient


DEFAULT_PROFILE_PATH = Path("examples/class_profile.json")


def handle_teacher_message(
    message: str,
    *,
    profile: ClassProfile,
    user_role: str = "teacher",
    user_id: str = "teacher_demo",
    session_id: str = "",
) -> dict:
    intent = detect_intent(message)

    if user_role == "student" and intent == "exam":
        return {
            "intent": "denied",
            "content": denial_message(user_role, intent),
            "artifacts": [],
        }

    if not is_allowed(user_role, intent):
        return {
            "intent": "denied",
            "content": denial_message(user_role, intent),
            "artifacts": [],
        }

    if intent == "ppt":
        return _handle_ppt(message, profile, user_role, user_id, session_id)
    if intent == "exam":
        return _handle_exam(message, profile, user_role, user_id, session_id)
    if intent == "teacher_quiz":
        return _handle_teacher_quiz(message, user_role, user_id)
    if intent == "quiz_submit":
        return _handle_quiz_submit(message, user_role, user_id)
    if intent == "student_quiz":
        return _handle_student_quiz(message, user_id, user_role, session_id)
    if intent == "student_summary":
        target_user_id = _extract_student_id(message) if user_role == "teacher" else None
        return _handle_student_summary(target_user_id or user_id)
    if intent == "class_insights":
        return _handle_class_insights(profile)
    if intent == "read_pdf":
        return _handle_read_pdf(message, user_role)
    if intent == "kb_search":
        return _handle_kb_search(message, user_role)
    if intent == "chatflow":
        return _handle_chatflow(message, user_role, user_id, session_id)

    return {
        "intent": "help",
        "content": (
            "我可以处理这些老师端任务：\n"
            "1. 生成课件，例如：生成一份关于 Logistic 回归的 PPT。\n"
            "2. 生成试卷，例如：出一份关于多重检验校正的 100 分试卷。\n"
            "3. 查知识库，例如：查一下数据库里和基因组有关的资料。\n"
            "4. 读取 PDF，例如：读取 条目ID 第 1 页。\n"
        ),
        "artifacts": [],
    }


def detect_intent(message: str) -> str:
    text = message.lower()
    if any(keyword in text for keyword in ["帮助", "help", "能做什么"]):
        return "help"
    if any(keyword in text for keyword in ["提交答案", "交卷", "批改"]) or (
        "评分" in text and _extract_uuid(message)
    ):
        return "quiz_submit"
    if any(keyword in text for keyword in ["课后习题", "课后题", "小测", "作业", "套题", "抽题", "可评分"]):
        return "teacher_quiz" if "老师" in text or "作业" in text or "小测" in text or "套题" in text else "student_quiz"
    if any(keyword in text for keyword in ["期中", "期末"]) and any(
        marker in text for marker in ["题", "测验", "考试", "练习"]
    ):
        return "teacher_quiz"
    if any(keyword in text for keyword in ["模拟题", "练习题", "自测", "练习"]):
        return "student_quiz"
    if any(keyword in text for keyword in ["ppt", "课件", "幻灯片", "slides"]):
        return "ppt"
    if any(keyword in text for keyword in ["试卷", "考卷", "测验", "考试", "latex", "pdf"]):
        return "exam"
    if any(keyword in text for keyword in ["同学疑难", "学生情况", "班级情况", "全班", "学生薄弱", "教学建议"]):
        return "class_insights"
    if any(keyword in text for keyword in ["我的不足", "哪里薄弱", "薄弱点", "弱点", "掌握情况", "哪里不会"]):
        return "student_summary"
    if any(keyword in text for keyword in ["读取", "阅读", "第"]) and "页" in text:
        return "read_pdf"
    if any(keyword in text for keyword in ["知识库", "数据库", "查", "搜索", "找"]):
        return "kb_search"
    if any(keyword in text for keyword in ["解释", "讲解", "为什么", "怎么理解", "答疑", "没听懂", "不会"]):
        return "chatflow"
    return "chatflow"


def _handle_ppt(
    message: str,
    profile: ClassProfile,
    user_role: str,
    user_id: str,
    session_id: str,
) -> dict:
    topic = _extract_topic(message, fallback="生物统计学专题")
    yml_text, yml_error = _try_yml_llm_text(
        message,
        intent="ppt",
        user_role=user_role,
        user_id=user_id,
        context=_profile_context(profile) + _knowledge_context(message, user_role),
    )
    if yml_text:
        try:
            path = _write_ai_artifact(topic, "课件", yml_text)
        except RuntimeError as exc:
            yml_error = _combine_errors(yml_error, str(exc))
        else:
            return {
                "intent": "ppt",
                "content": f"已通过 AI_Agent.yml + 模型 API 生成“{topic}”课件草稿。",
                "artifacts": [{"label": path.name, "path": str(path)}],
                "source": "local_yml_llm",
            }
    dify_text, dify_error = _try_chatflow_text(
        message, user_role, user_id, session_id, return_error=True
    )
    if dify_text:
        try:
            path = _write_dify_artifact(topic, "课件", dify_text)
        except RuntimeError as exc:
            dify_error = _combine_errors(dify_error, str(exc))
        else:
            return {
                "intent": "ppt",
                "content": f"已通过 Dify Chatflow 生成“{topic}”课件草稿。",
                "artifacts": [{"label": path.name, "path": str(path)}],
                "source": "dify_chatflow",
            }
    if _dify_required():
        return _dify_required_response("ppt", _combine_errors(dify_error, yml_error))

    bundle = generate_ppt(
        PPTRequest(
            topic=topic,
            class_profile=profile,
            slide_count=_extract_number(message, default=10),
        )
    )
    artifacts = _bundle_artifacts(bundle)
    return {
        "intent": "ppt",
        "content": f"未配置或未成功调用 Dify Chatflow，已用本地模板生成“{topic}”课件。",
        "artifacts": artifacts,
        "source": "local_template",
    }


def _handle_exam(
    message: str,
    profile: ClassProfile,
    user_role: str,
    user_id: str,
    session_id: str,
) -> dict:
    topic = _extract_topic(message, fallback="生物统计学综合复习")
    points = _extract_points(message, default=100)
    yml_text, yml_error = _try_yml_llm_text(
        message,
        intent="exam",
        user_role=user_role,
        user_id=user_id,
        context=_profile_context(profile) + _knowledge_context(message, user_role),
    )
    if yml_text:
        try:
            path = _write_ai_artifact(topic, "试卷", yml_text)
        except RuntimeError as exc:
            yml_error = _combine_errors(yml_error, str(exc))
        else:
            return {
                "intent": "exam",
                "content": f"已通过 AI_Agent.yml + 模型 API 生成“{topic}”试卷草稿。",
                "artifacts": [{"label": path.name, "path": str(path)}],
                "source": "local_yml_llm",
            }
    dify_text, dify_error = _try_chatflow_text(
        message, user_role, user_id, session_id, return_error=True
    )
    if dify_text:
        try:
            path = _write_dify_artifact(topic, "试卷", dify_text)
        except RuntimeError as exc:
            dify_error = _combine_errors(dify_error, str(exc))
        else:
            return {
                "intent": "exam",
                "content": f"已通过 Dify Chatflow 生成“{topic}”试卷草稿。",
                "artifacts": [{"label": path.name, "path": str(path)}],
                "source": "dify_chatflow",
            }
    if _dify_required():
        return _dify_required_response("exam", _combine_errors(dify_error, yml_error))

    bundle = generate_exam(
        ExamRequest(
            title=f"{topic}专项试卷",
            class_profile=profile,
            topics=[topic],
            total_points=points,
        )
    )
    artifacts = _bundle_artifacts(bundle)
    return {
        "intent": "exam",
        "content": f"未配置或未成功调用 Dify Chatflow，已用本地模板生成“{topic}”试卷，满分 {points} 分。",
        "artifacts": artifacts,
        "source": "local_template",
    }


def _handle_student_quiz(
    message: str,
    user_id: str,
    user_role: str,
    session_id: str,
) -> dict:
    return _handle_student_structured_quiz(message, user_role, user_id)


def _handle_teacher_quiz(message: str, user_role: str, user_id: str) -> dict:
    if user_role != "teacher":
        return {
            "intent": "denied",
            "content": "学生权限不能开启老师课后小测、作业、期中或期末套题。可以生成自己的自由练习。",
            "artifacts": [],
        }
    try:
        set_id = _select_teacher_quiz_set(message)
        if set_id:
            attempt = start_quiz(
                user_id=user_id,
                user_role=user_role,
                mode="teacher",
                set_id=set_id,
            )
        else:
            attempt = start_quiz(
                user_id=user_id,
                user_role=user_role,
                mode="teacher",
                module=_extract_quiz_module(message),
                use_case=_extract_quiz_use_case(message) or "lesson",
                question_type=_extract_question_type(message),
                count=_extract_question_count(message, default=3),
            )
        bundle = export_attempt_markdown(attempt.id)
        questions = get_attempt_questions(attempt.id)
    except Exception as exc:
        return {
            "intent": "teacher_quiz",
            "content": f"创建老师习题失败：{exc}",
            "artifacts": [],
        }

    return {
        "intent": "teacher_quiz",
        "content": (
            f"已创建“{attempt.title}”。老师端套题保持少量固定题组，按课后小测、期中、期末区分。\n"
            f"作答记录 ID：{attempt.id}\n"
            f"题目数：{len(questions)}，总分：{attempt.total_points}\n"
            f"[打开交互式答题页](/quiz?attempt_id={attempt.id})\n"
            "学生完成后可在页面点击“确认并评分”，也可用“提交答案 attempt_id JSON答案”进行评分。"
        ),
        "artifacts": [{"label": bundle.source_path.name, "path": str(bundle.source_path)}],
        "source": "quiz_bank",
    }


def _handle_student_structured_quiz(message: str, user_role: str, user_id: str) -> dict:
    module = _extract_quiz_module(message)
    try:
        attempt = start_quiz(
            user_id=user_id,
            user_role=user_role,
            mode="student",
            module=module,
            topic="" if module else _extract_topic(message, fallback=""),
            use_case="practice",
            question_type=_extract_question_type(message),
            count=_extract_question_count(message, default=5),
        )
        bundle = export_attempt_markdown(attempt.id)
        questions = get_attempt_questions(attempt.id)
    except Exception as exc:
        return {
            "intent": "student_quiz",
            "content": f"创建学生自由练习失败：{exc}",
            "artifacts": [],
        }

    return {
        "intent": "student_quiz",
        "content": (
            f"已从题库随机抽取“{attempt.title}”。学生练习模式更自由，可以按模块、知识点、题型和数量筛选。\n"
            f"作答记录 ID：{attempt.id}\n"
            f"题目数：{len(questions)}，总分：{attempt.total_points}\n"
            f"[打开交互式答题页](/quiz?attempt_id={attempt.id})\n"
            "完成后可在页面点击“确认并评分”，也可发送“提交答案 attempt_id {question_id: 答案}”自动评分。"
        ),
        "artifacts": [{"label": bundle.source_path.name, "path": str(bundle.source_path)}],
        "source": "quiz_bank",
    }


def _handle_quiz_submit(message: str, user_role: str, user_id: str) -> dict:
    attempt_id = _extract_uuid(message)
    if not attempt_id:
        return {
            "intent": "quiz_submit",
            "content": "请提供作答记录 ID，例如：提交答案 123e4567-e89b-12d3-a456-426614174000 {\"q_id\": \"B\"}",
            "artifacts": [],
        }
    answers = _extract_answers_json(message)
    if not answers:
        return {
            "intent": "quiz_submit",
            "content": "请在消息中附上答案 JSON，例如：{\"q_fdr_mc_001\": \"B\", \"q_fdr_calc_001\": \"0.04\"}",
            "artifacts": [],
        }
    attempt = get_attempt(attempt_id)
    if not attempt:
        return {"intent": "quiz_submit", "content": "没有找到这个作答记录。", "artifacts": []}
    if user_role == "student" and attempt.user_id != user_id:
        return {
            "intent": "denied",
            "content": "学生只能提交和查看自己的练习作答。",
            "artifacts": [],
        }
    try:
        result = submit_quiz(attempt_id, answers)
        bundle = export_score_markdown(result)
    except Exception as exc:
        return {"intent": "quiz_submit", "content": f"评分失败：{exc}", "artifacts": []}
    return {
        "intent": "quiz_submit",
        "content": (
            f"已完成评分：{result['score']} / {result['total_points']}，"
            f"得分率 {result['percentage']}%。"
        ),
        "artifacts": [{"label": bundle.source_path.name, "path": str(bundle.source_path)}],
        "source": "quiz_bank",
    }


def _handle_student_summary(user_id: str) -> dict:
    try:
        profile = load_student_profile(user_id)
    except KeyError as exc:
        return {"intent": "student_summary", "content": str(exc), "artifacts": []}

    weak_points = profile.get("weak_points", [])
    strong_points = profile.get("strong_points", [])
    lines = [f"{profile.get('name', user_id)} 的学习画像：", ""]
    if strong_points:
        lines.append("擅长：")
        lines.extend(f"- {item}" for item in strong_points)
        lines.append("")
    if weak_points:
        lines.append("需要重点补强：")
        for item in weak_points:
            errors = "、".join(item.get("recent_errors", [])) or "暂无具体错因记录"
            lines.append(f"- {item['name']}：掌握度 {item.get('mastery', '未知')}；常见问题：{errors}")
    else:
        lines.append("目前没有记录到明显薄弱点。")
    lines.extend(["", "建议：先针对最低掌握度知识点做短练习，再看错因解释。"])
    return {"intent": "student_summary", "content": "\n".join(lines), "artifacts": []}


def _handle_class_insights(profile: ClassProfile) -> dict:
    lines = [
        f"{profile.class_name} 的班级学习情况：",
        "",
    ]
    if profile.class_size:
        scale = "大课" if profile.class_size >= 100 else "小课"
        lines.append(f"班级规模：{profile.class_size} 人（{scale}）")
        if profile.class_size >= 100:
            lines.append("组织建议：优先使用分层讲解、自动化小测和助教分组反馈。")
        else:
            lines.append("组织建议：适合增加课堂追问、个别点名讲解和小组板演。")
        lines.append("")
    if profile.weak_points:
        lines.append("主要疑难点：")
        for item in profile.weak_points:
            errors = "、".join(item.common_errors) or "暂无具体错因记录"
            lines.append(
                f"- {item.name}：掌握度 {_format_mastery(item.mastery)}；优先级 {item.priority}；常见问题：{errors}"
            )
            lines.append(f"  教材例题改编训练：{_textbook_training_suggestion(item.name)}")
            lines.append(f"  正例：{_positive_example_for_weak_point(item.name)}")
            lines.append(f"  反例/易错例：{_negative_example_for_weak_point(item.name)}")
    else:
        lines.append("暂未记录班级薄弱点。")
    if profile.strong_points:
        lines.extend(["", f"班级优势：{'、'.join(profile.strong_points)}"])
    if profile.teacher_notes:
        lines.append("")
        lines.append("教学调整建议：")
        lines.extend(f"- {note}" for note in profile.teacher_notes)
    student_lines = _student_roster_lines()
    if student_lines:
        lines.append("")
        lines.append("学生个体画像：")
        lines.extend(student_lines)
    return {"intent": "class_insights", "content": "\n".join(lines), "artifacts": []}


def _textbook_training_suggestion(topic: str) -> str:
    if any(key in topic for key in ["多重检验", "FDR", "FWER"]):
        return "选取中文教材中“多个假设检验/多重比较校正”的典型例题，改换为基因或指标批量检验数据，让学生分别判断应控制 FWER 还是 FDR。"
    if any(key in topic for key in ["配对", "独立样本"]):
        return "选取中文教材中“配对 t 检验与独立样本 t 检验”的例题，保留样本量和均值结构，改换为前后测量与两组独立样本两个情境。"
    if any(key in topic for key in ["Logistic", "OR", "比值比"]):
        return "选取中文教材中“病例对照研究/Logistic 回归 OR 解释”的例题，改换暴露因素和结局，要求学生区分比值、概率和风险。"
    return "选取中文教材同一知识点下的典型例题，改换数据、情境和问法，让学生先判断适用条件，再完成计算和解释。"


def _positive_example_for_weak_point(topic: str) -> str:
    if any(key in topic for key in ["多重检验", "FDR", "FWER"]):
        return "1000 次基因检验用于探索候选基因时，说明选择 FDR 的理由，并报告调整后 p 值。"
    if any(key in topic for key in ["配对", "独立样本"]):
        return "同一批学生训练前后成绩比较时，先计算每人的差值，再做配对 t 检验。"
    if any(key in topic for key in ["Logistic", "OR", "比值比"]):
        return "OR=2.4 时表述为暴露组结局发生的比值是非暴露组的 2.4 倍，并同时查看置信区间。"
    return "先说明统计方法的适用条件，再代入教材例题改编数据，并给出结论限制。"


def _negative_example_for_weak_point(topic: str) -> str:
    if any(key in topic for key in ["多重检验", "FDR", "FWER"]):
        return "看到 20 个原始 p 值小于 0.05 就直接宣布 20 个发现可靠，忽略多重检验校正。"
    if any(key in topic for key in ["配对", "独立样本"]):
        return "把同一受试者治疗前后数据当成两组独立样本，直接套用独立样本 t 检验。"
    if any(key in topic for key in ["Logistic", "OR", "比值比"]):
        return "把 OR=2.4 直接说成患病概率增加 2.4 倍，或只看显著性不看置信区间。"
    return "只套公式不检查适用条件，或把统计显著直接解释为因果结论。"


def _handle_kb_search(message: str, user_role: str) -> dict:
    query = _extract_search_query(message)
    item_rows = _filter_visible_items(_search_items_with_variants(query, limit=10), user_role)[:5]
    page_rows = _filter_visible_pages(_search_pages_with_variants(query, limit=10), user_role)[:5]

    lines = [f"知识库搜索：{query}"]
    if item_rows:
        lines.append("\n条目：")
        for row in item_rows:
            lines.append(f"- {row['title']} ({row['id']})")
    if page_rows:
        lines.append("\nPDF 页面：")
        for row in page_rows:
            snippet = _clean_snippet(row["text"], max_length=120)
            lines.append(f"- {row['title']} 第 {row['page_number']} 页：{snippet}")
    if not item_rows and not page_rows:
        lines.append("没有找到匹配结果。")

    return {"intent": "kb_search", "content": "\n".join(lines), "artifacts": []}


def _handle_read_pdf(message: str, user_role: str) -> dict:
    item_id = _extract_uuid(message)
    page = _extract_page(message)
    if not item_id:
        return {
            "intent": "read_pdf",
            "content": "请提供要读取的条目 ID，例如：读取 123e4567-e89b-12d3-a456-426614174000 第 1 页。",
            "artifacts": [],
        }

    item = get_item(item_id)
    if not item:
        return {
            "intent": "read_pdf",
            "content": "没有找到这个知识库条目。",
            "artifacts": [],
        }
    if user_role == "student" and item["source_type"] == "exam":
        return {
            "intent": "denied",
            "content": "学生权限不能读取老师考试资料。可以读取公开教材或生成自己的模拟练习。",
            "artifacts": [],
        }

    rows = read_pages(item_id, start_page=page, page_count=1)
    if not rows:
        return {
            "intent": "read_pdf",
            "content": "没有找到这一页的文本。可能这个条目还没有导入 PDF，或页码不存在。",
            "artifacts": [],
        }

    row = rows[0]
    return {
        "intent": "read_pdf",
        "content": f"第 {row['page_number']} 页：\n\n{_clean_snippet(row['text'], max_length=2000)}",
        "artifacts": [],
    }


def _handle_chatflow(message: str, user_role: str, user_id: str, session_id: str) -> dict:
    yml_text, yml_error = _try_yml_llm_text(
        message,
        intent="chatflow",
        user_role=user_role,
        user_id=user_id,
        context=_student_context(user_id) if user_role == "student" else "",
    )
    if yml_text:
        return {
            "intent": "chatflow",
            "content": yml_text,
            "artifacts": [],
            "source": "local_yml_llm",
        }
    dify_text, error = _try_chatflow_text(
        message,
        user_role,
        user_id,
        session_id,
        return_error=True,
    )
    if not dify_text:
        return {
            "intent": "chatflow",
            "content": (
                "Chatflow 接口已预留，但当前还没有可用配置或调用失败。\n"
                f"原因：{_combine_errors(error, yml_error)}\n"
                "你仍可以使用本地功能：生成课件、生成试卷、查知识库、学生模拟练习。"
            ),
            "artifacts": [],
        }

    return {
        "intent": "chatflow",
        "content": dify_text,
        "artifacts": [],
        "source": "dify_chatflow",
    }


def _try_chatflow_text(
    message: str,
    user_role: str,
    user_id: str,
    session_id: str,
    *,
    return_error: bool = False,
):
    try:
        dify_conversation_id = ""
        if session_id:
            session = get_session(session_id)
            if session:
                dify_conversation_id = session.dify_conversation_id
        payload = DifyChatflowClient().send(
            ChatflowRequest(
                query=message,
                user=user_id,
                conversation_id=dify_conversation_id or None,
                inputs=build_chatflow_inputs(
                    user_role=user_role,
                    user_id=user_id,
                    session_id=session_id,
                ),
            )
        )
    except Exception as exc:
        if return_error:
            return None, str(exc)
        return None

    answer = payload.get("answer") or payload.get("data", {}).get("answer")
    if not answer:
        if return_error:
            return None, f"Dify Chatflow 未返回 answer 字段，返回字段：{', '.join(payload.keys())}"
        return None
    if _is_placeholder_chatflow_answer(answer):
        if return_error:
            return None, f"Dify Chatflow 返回占位内容：{answer!r}"
        return None
    answer = _clean_generated_text(answer)
    if _is_placeholder_chatflow_answer(answer):
        if return_error:
            return None, "Dify Chatflow 清洗后只剩占位内容。"
        return None
    returned_conversation_id = (
        payload.get("conversation_id")
        or payload.get("data", {}).get("conversation_id")
        or ""
    )
    if session_id and returned_conversation_id:
        update_dify_conversation_id(session_id, returned_conversation_id)
    if return_error:
        return answer, None
    return answer


def _try_yml_llm_text(
    message: str,
    *,
    intent: str,
    user_role: str,
    user_id: str,
    context: str = "",
) -> tuple[str | None, str | None]:
    try:
        text = YmlLLMClient().send(
            intent=intent,
            user_role=user_role,
            user_id=user_id,
            message=message,
            context=context,
        )
    except Exception as exc:
        return None, str(exc)
    return text, None


def _write_dify_artifact(topic: str, artifact_type: str, content: str) -> Path:
    output_dir = ensure_output_dir("outputs")
    path = output_dir / f"{slugify(topic or 'dify')}_Dify_{artifact_type}.md"
    content = _clean_generated_text(content)
    _validate_generated_content(content)
    path.write_text(content, encoding="utf-8")
    return path


def _write_ai_artifact(topic: str, artifact_type: str, content: str) -> Path:
    output_dir = ensure_output_dir("outputs")
    path = output_dir / f"{slugify(topic or 'ai')}_AI_{artifact_type}.md"
    content = _clean_generated_text(content)
    _validate_generated_content(content)
    path.write_text(content, encoding="utf-8")
    return path


def _dify_required() -> bool:
    require_dify = os.getenv("AGENT_REQUIRE_DIFY", "").lower() in {"1", "true", "yes", "on"}
    return require_dify or not _local_fallback_enabled()


def _local_fallback_enabled() -> bool:
    return os.getenv("AGENT_ALLOW_LOCAL_FALLBACK", "").lower() in {"1", "true", "yes", "on"}


def _dify_required_response(intent: str, error: str | None = None) -> dict:
    reason = f"\n具体原因：{error}" if error else ""
    return {
        "intent": intent,
        "content": (
            "当前内容生成必须由 Dify Chatflow 完成，因此不会使用本地模板伪装成 AI 结果。\n"
            "但 Dify Chatflow 没有配置或调用失败。请检查 DIFY_CHATFLOW_API_KEY "
            "和 DIFY_CHATFLOW_API_URL，并确认 AI_Agent.yml 已作为 Chatflow 导入 Dify。\n"
            "如果只是想离线测试页面和权限，可临时设置 AGENT_ALLOW_LOCAL_FALLBACK=1。"
            f"{reason}"
        ),
        "artifacts": [],
        "source": "dify_required_failed",
    }


def _extract_topic(message: str, fallback: str) -> str:
    patterns = [
        r"关于(.+?)(?:的|，|,|。|$)",
        r"主题[:：]\s*(.+?)(?:，|,|。|$)",
        r"围绕(.+?)(?:的|，|,|。|$)",
        r"解释(?:一下)?(.+?)(?:机制|是什么|什么意思|的意思|，|,|。|$)",
        r"讲(?:一下)?(.+?)(?:机制|是什么|什么意思|的意思|，|,|。|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, message)
        if match:
            return match.group(1).strip()
    cleaned = message.strip(" ，。,.")
    for token in [
        "请",
        "帮我",
        "给我",
        "生成",
        "做",
        "一份",
        "一个",
        "个",
        "一下",
        "老师",
        "学生",
        "中文",
        "完整",
        "课件",
        "PPT",
        "ppt",
        "幻灯片",
        "slides",
        "试卷",
        "考卷",
        "测验卷",
        "测验",
        "考试",
        "题目",
        "题",
        "解释",
        "讲解",
        "机制",
        "是什么",
        "什么意思",
        "意思",
    ]:
        cleaned = cleaned.replace(token, "")
    cleaned = cleaned.strip(" 的，。,.：:")
    if cleaned:
        return cleaned
    return fallback


def _extract_search_query(message: str) -> str:
    relation_match = re.search(
        r"(?:和|与|关于)\s*(.+?)\s*(?:有关|相关|的资料|资料|内容|$)",
        message,
    )
    if relation_match:
        query = relation_match.group(1).strip(" ，。,.")
        if query:
            return query

    cleaned = message
    for keyword in [
        "查一下",
        "查找",
        "搜索",
        "查询",
        "知识库",
        "数据库",
        "里面",
        "里",
        "中",
        "有关的",
        "有关",
        "相关的",
        "相关",
        "资料",
    ]:
        cleaned = cleaned.replace(keyword, "")
    return cleaned.strip(" ，。,.") or message.strip()


def _extract_number(message: str, default: int) -> int:
    match = re.search(r"(\d+)\s*(?:页|张| slides?)", message, flags=re.IGNORECASE)
    return int(match.group(1)) if match else default


def _extract_points(message: str, default: int) -> int:
    match = re.search(r"(\d+)\s*分", message)
    return int(match.group(1)) if match else default


def _extract_question_count(message: str, default: int) -> int:
    match = re.search(r"(\d+)\s*(?:道|题|个)", message)
    return int(match.group(1)) if match else default


def _extract_quiz_module(message: str) -> str:
    module_aliases = [
        ("多重检验校正", ["多重检验", "fdr", "fwer", "调整后 p", "调整后p"]),
        ("Logistic 回归", ["logistic", "or", "比值比"]),
        ("研究设计与 t 检验", ["配对", "独立样本", "t检验", "t 检验"]),
        ("卡方检验", ["卡方", "chi", "χ"]),
        ("遗传学统计", ["遗传", "基因型", "等位基因"]),
        ("综合复习", ["综合", "方法选择"]),
    ]
    lowered = message.lower()
    for module, aliases in module_aliases:
        if any(alias.lower() in lowered for alias in aliases):
            return module
    return ""


def _extract_quiz_use_case(message: str) -> str:
    if "期中" in message:
        return "midterm"
    if "期末" in message:
        return "final"
    if any(keyword in message for keyword in ["课后", "小测", "课堂"]):
        return "lesson"
    if "作业" in message:
        return "assignment"
    if "考试" in message:
        return "exam"
    return ""


def _extract_question_type(message: str) -> str:
    mapping = {
        "选择题": "multiple_choice",
        "问答题": "short_answer",
        "简答题": "short_answer",
        "计算题": "calculation",
        "代码题": "code",
    }
    for keyword, question_type in mapping.items():
        if keyword in message:
            return question_type
    return ""


def _select_teacher_quiz_set(message: str) -> str:
    explicit = re.search(r"\b(?:lesson|midterm|final)_[A-Za-z0-9_-]+\b", message)
    if explicit:
        return explicit.group(0)
    use_case = _extract_quiz_use_case(message)
    module = _extract_quiz_module(message)
    sets = list_quiz_sets(audience="teacher")
    if use_case in {"midterm", "final"}:
        for quiz_set in sets:
            if quiz_set.use_case == use_case:
                return quiz_set.id
    if use_case == "lesson" and module:
        for quiz_set in sets:
            if quiz_set.use_case == "lesson" and (
                module in quiz_set.module or quiz_set.module in module
            ):
                return quiz_set.id
    return ""


def _extract_answers_json(message: str) -> dict[str, str]:
    start = message.find("{")
    end = message.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        raw = json.loads(message[start : end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _extract_page(message: str) -> int:
    match = re.search(r"第\s*(\d+)\s*页", message)
    return int(match.group(1)) if match else 1


def _extract_uuid(message: str) -> str | None:
    match = re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        message,
    )
    return match.group(0) if match else None


def _extract_student_id(message: str) -> str | None:
    match = re.search(r"\bstudent_[A-Za-z0-9_-]+\b", message)
    return match.group(0) if match else None


def _format_mastery(value: float | None) -> str:
    if value is None:
        return "未知"
    return f"{value:.0%}"


def _profile_context(profile: ClassProfile) -> str:
    weak_points = [
        f"- {item.name}: mastery={_format_mastery(item.mastery)}, priority={item.priority}, "
        f"errors={'、'.join(item.common_errors)}"
        for item in profile.weak_points
    ]
    return (
        f"班级：{profile.class_name}\n"
        f"班级人数：{profile.class_size or '未知'}\n"
        f"班级优势：{'、'.join(profile.strong_points)}\n"
        f"班级薄弱点：\n" + "\n".join(weak_points) + "\n"
        f"老师备注：{'；'.join(profile.teacher_notes)}\n"
    )


def _student_context(user_id: str) -> str:
    try:
        profile = load_student_profile(user_id)
    except KeyError:
        return ""
    weak_points = profile.get("weak_points", [])
    strong_points = profile.get("strong_points", [])
    lines = [
        f"学生：{profile.get('name', user_id)}",
        f"擅长：{'、'.join(strong_points)}",
        "薄弱点：",
    ]
    for item in weak_points:
        lines.append(
            f"- {item.get('name')}: mastery={item.get('mastery')}, "
            f"errors={'、'.join(item.get('recent_errors', []))}"
        )
    return "\n".join(lines) + "\n"


def _student_roster_lines() -> list[str]:
    try:
        import json

        data = json.loads(Path("examples/student_profiles.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    lines = []
    for student_id, profile in data.items():
        weak_points = profile.get("weak_points", [])
        weakest = sorted(
            weak_points,
            key=lambda item: item.get("mastery", 1),
        )[:2]
        weak_text = "；".join(
            f"{item.get('name')}({item.get('mastery', '未知')})"
            for item in weakest
        ) or "暂无明显薄弱点"
        lines.append(f"- {profile.get('name', student_id)} ({student_id})：{weak_text}")
    return lines


def _knowledge_context(message: str, user_role: str) -> str:
    rows = _filter_visible_pages(
        _search_pages_with_variants(_extract_search_query(message), limit=3),
        user_role,
    )
    if not rows:
        return ""
    lines = ["知识库相关片段："]
    for row in rows:
        snippet = _clean_snippet(row["text"], max_length=300)
        lines.append(f"- {row['title']} 第 {row['page_number']} 页：{snippet}")
    return "\n".join(lines) + "\n"


def _combine_errors(*errors: str | None) -> str:
    return "；".join(error for error in errors if error) or "未知错误"


def _is_placeholder_chatflow_answer(answer: str) -> bool:
    return answer.strip() in {"//", "null", "None"}


def _clean_generated_text(text: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.IGNORECASE | re.DOTALL)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()
    cleaned = _convert_formula_code_fences(cleaned)
    marp_match = re.search(r"(?m)^---\s*\nmarp:", cleaned)
    if marp_match and marp_match.start() > 0:
        cleaned = cleaned[marp_match.start() :].strip()
    cleaned = _strip_conversational_preface(cleaned)
    if cleaned.startswith("---\nmarp:"):
        design_note = re.search(r"(?m)^###\s*课件设计说明", cleaned)
        if design_note:
            cleaned = cleaned[: design_note.start()].strip()
    if cleaned.count("```") % 2 == 1:
        cleaned = re.sub(r"(?m)^```\s*$", "", cleaned).strip()
    cleaned = re.sub(r"(?m)^(\s*[-*]\s*)[❌✅]\s*", r"\1", cleaned)
    cleaned = re.sub(r"(?m)^(#{1,6})\s*-\s+", r"\1 ", cleaned)
    replacements = {
        "❌": "-",
        "✅": "-",
        "→": "->",
        "≥": ">=",
        "≤": "<=",
        "≈": "约等于",
        "…": "...",
        "₀": "0",
        "₁": "1",
        "₂": "2",
        "₃": "3",
        "₄": "4",
        "₅": "5",
        "₆": "6",
        "₇": "7",
        "₈": "8",
        "₉": "9",
    }
    for old, new in replacements.items():
        cleaned = cleaned.replace(old, new)
    cleaned = re.sub(r"\bt([0-9]+\.[0-9]+)\((\d+)\)", r"t_{\1}(\2)", cleaned)
    cleaned = re.sub(r"(?m)^(\s*[-*]\s*)-\s+", r"\1", cleaned)
    cleaned = _normalize_common_latex(cleaned)
    return cleaned


def _strip_conversational_preface(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith("---\nmarp:"):
        return stripped
    first_line = stripped.splitlines()[0].strip() if stripped.splitlines() else ""
    preface_patterns = [
        r"^好的[，,]",
        r"^当然[，,]",
        r"^可以[，,]",
        r"^以下是",
        r"^下面是",
        r"^根据您?的要求",
        r"^我将",
        r"^已为",
    ]
    if not any(re.search(pattern, first_line) for pattern in preface_patterns):
        return text
    heading = re.search(r"(?m)^(#{1,6}\s+.+|\*\*[^*\n]+[:：]?[^*\n]*\*\*)\s*$", stripped)
    if heading and heading.start() > 0:
        return stripped[heading.start() :].strip()
    divider_heading = re.search(r"(?m)^---+\s*\n\s*(#{1,6}\s+.+)$", stripped)
    if divider_heading:
        return stripped[divider_heading.start(1) :].strip()
    return text


def _convert_formula_code_fences(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        if _looks_like_formula_block(body):
            return f"$$\n{_plain_formula_to_latex(body)}\n$$"
        return match.group(0)

    return re.sub(r"```(?:text|math|latex)?\s*\n(.*?)\n```", replace, text, flags=re.DOTALL)


def _looks_like_formula_block(value: str) -> bool:
    compact = value.strip()
    if "\n" in compact and len(compact.splitlines()) > 3:
        return False
    return bool(
        re.search(r"(chi|χ|\\chi|sum|\\sum|frac|\\frac|\^|_|=|<|>|α|alpha)", compact)
        and re.search(r"(\d|O|E|p|df|OR|χ|chi|\\chi)", compact)
    )


def _plain_formula_to_latex(value: str) -> str:
    result = value.strip()
    result = re.sub(r"\bchi\s*(?:\^?2|²)", r"\\chi^2", result, flags=re.IGNORECASE)
    result = result.replace("χ²", r"\chi^2").replace("χ^2", r"\chi^2")
    result = re.sub(r"\bsum\b", r"\\sum", result, flags=re.IGNORECASE)
    result = result.replace("·", r"\\cdot ")
    result = result.replace("×", r"\\times ")
    return result


def _normalize_common_latex(text: str) -> str:
    parts = re.split(r"(\$\$.*?\$\$|\$.*?\$|`[^`]*`)", text, flags=re.DOTALL)
    normalized: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            normalized.append(part)
            continue
        part = re.sub(
            r"(?m)^(\s*)(?:chi|χ)\s*(?:\^?2|²)\s*=\s*sum\s+(.+)$",
            lambda match: f"{match.group(1)}$$\\chi^2 = \\sum {match.group(2).strip()}$$",
            part,
            flags=re.IGNORECASE,
        )
        part = re.sub(
            r"(?m)^(\s*)χ\^?2\s*=\s*(.+)$",
            lambda match: f"{match.group(1)}$$\\chi^2 = {match.group(2).strip()}$$",
            part,
        )
        part = re.sub(r"χ²\s*=\s*([0-9.]+)", r"$\\chi^2 = \1$", part)
        part = re.sub(r"χ\^2\s*=\s*([0-9.]+)", r"$\\chi^2 = \1$", part)
        part = re.sub(r"χ²_\{([^}]+)\}", r"$\\chi^2_{\1}$", part)
        part = re.sub(r"χ²", r"$\\chi^2$", part)
        part = re.sub(r"\bα\s*=\s*([0-9.]+)", r"$\\alpha = \1$", part)
        part = re.sub(r"\bp\s*([<>=]+)\s*([0-9.]+)", r"$p \1 \2$", part)
        part = re.sub(r"\bOR\s*=\s*([0-9.]+)", r"$OR = \1$", part)
        part = re.sub(r"\bdf\s*=\s*([0-9]+)", r"$df = \1$", part)
        part = re.sub(r"期望频数\s*([<>]=?)\s*([0-9.]+)", r"期望频数 $\1 \2$", part)
        part = re.sub(r"(\d+/\d+)\s*=\s*([0-9.]+)%", r"$\1 = \2\\%$", part)
        part = re.sub(r"\b(\d+)\s*×\s*(\d+)\b", r"$\1 \\times \2$", part)
        normalized.append(part)
    return "".join(normalized)


def _validate_generated_content(content: str) -> None:
    stripped = content.strip()
    if not stripped or _is_placeholder_chatflow_answer(stripped):
        raise RuntimeError("模型返回为空或占位内容，未写入成品文件。")
    if "<think" in stripped.lower() or "\ufffd" in stripped:
        raise RuntimeError("模型返回内容包含推理标签或乱码替换符，未写入成品文件。")
    if re.search(r"(?m)(\|\s*\.\.\.\s*\|)|(^\s*\.\.\.\s*$)", stripped):
        raise RuntimeError("模型返回内容包含明显占位省略内容，未写入成品文件。")
    if re.search(r"(?m)^\s*\.{3,}\s*$", stripped):
        raise RuntimeError("模型返回内容包含明显占位答题线，未写入成品文件。")
    if stripped.count("```") % 2 == 1:
        raise RuntimeError("模型返回内容包含未闭合代码围栏，未写入成品文件。")
    marp_match = re.search(r"(?m)^---\s*\nmarp:", stripped)
    if marp_match and marp_match.start() != 0:
        raise RuntimeError("Marp 课件 frontmatter 不在文件开头，未写入成品文件。")


def _bundle_artifacts(bundle) -> list[dict]:
    artifacts = [{"label": bundle.source_path.name, "path": str(bundle.source_path)}]
    if bundle.artifact_path:
        artifacts.append({"label": bundle.artifact_path.name, "path": str(bundle.artifact_path)})
    return artifacts


def _filter_visible_items(rows, user_role: str):
    if user_role != "student":
        return list(rows)
    return [row for row in rows if row["source_type"] != "exam"]


def _filter_visible_pages(rows, user_role: str):
    if user_role != "student":
        return list(rows)
    return [row for row in rows if row["source_type"] != "exam"]


def _clean_snippet(text: str, *, max_length: int) -> str:
    cleaned = text.replace("\n", " ")
    cleaned = re.sub(
        r"[^A-Za-z0-9\u4e00-\u9fff\s，。；：、（）()《》<>“”\"'.,;:!?！？/\\+\-=%αβγμσ√]",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_length] or "该页文本抽取质量较低，建议打开原 PDF 查看。"


def _search_items_with_variants(query: str, *, limit: int):
    rows = []
    seen = set()
    for candidate in _search_query_candidates(query):
        for row in search_items(candidate, limit=limit):
            if row["id"] in seen:
                continue
            rows.append(row)
            seen.add(row["id"])
            if len(rows) >= limit:
                return rows
    return rows


def _search_pages_with_variants(query: str, *, limit: int):
    rows = []
    seen = set()
    for candidate in _search_query_candidates(query):
        for row in search_pages(candidate, limit=limit):
            key = (row["item_id"], row["page_number"])
            if key in seen:
                continue
            rows.append(row)
            seen.add(key)
            if len(rows) >= limit:
                return rows
    return rows


def _search_query_candidates(query: str) -> list[str]:
    candidates = [query]
    if query.endswith("学") and len(query) > 2:
        candidates.append(query[:-1])
    if "基因组学" in query:
        candidates.append(query.replace("基因组学", "基因组"))
    cleaned = [item.strip() for item in candidates if item.strip()]
    return list(dict.fromkeys(cleaned))
