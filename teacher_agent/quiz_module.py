from __future__ import annotations

import json
import random
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from teacher_agent.schemas import OutputBundle, ensure_output_dir, slugify


DEFAULT_QUIZ_DB_PATH = Path("data/quiz_bank.db")
DEFAULT_QUIZ_SEED_PATH = Path("examples/question_bank_seed.json")

QUESTION_TYPES = {"multiple_choice", "short_answer", "calculation", "code"}
TEACHER_USE_CASES = {"lesson", "midterm", "final", "assignment", "exam"}


@dataclass
class Question:
    id: str
    question_type: str
    module: str
    topic: str
    difficulty: str
    scope: str
    use_cases: list[str]
    points: int
    prompt: str
    choices: list[str] = field(default_factory=list)
    correct_answer: str = ""
    answer_keywords: list[str] = field(default_factory=list)
    rubric: list[dict[str, Any]] = field(default_factory=list)
    tolerance: float | None = None
    explanation: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class QuizSet:
    id: str
    title: str
    module: str
    use_case: str
    audience: str
    question_ids: list[str]
    description: str = ""


@dataclass
class QuizAttempt:
    id: str
    user_id: str
    user_role: str
    mode: str
    title: str
    status: str
    total_points: int
    score: float | None
    set_id: str = ""
    created_at: str = ""
    submitted_at: str = ""
    filters: dict[str, Any] = field(default_factory=dict)


def connect(db_path: str | Path = DEFAULT_QUIZ_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_quiz_db(db_path: str | Path = DEFAULT_QUIZ_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
                id TEXT PRIMARY KEY,
                question_type TEXT NOT NULL,
                module TEXT NOT NULL,
                topic TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                scope TEXT NOT NULL,
                use_cases_json TEXT NOT NULL,
                points INTEGER NOT NULL,
                prompt TEXT NOT NULL,
                choices_json TEXT NOT NULL,
                correct_answer TEXT NOT NULL,
                answer_keywords_json TEXT NOT NULL,
                rubric_json TEXT NOT NULL,
                tolerance REAL,
                explanation TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_sets (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                module TEXT NOT NULL,
                use_case TEXT NOT NULL,
                audience TEXT NOT NULL,
                question_ids_json TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quiz_attempts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                user_role TEXT NOT NULL,
                mode TEXT NOT NULL,
                set_id TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                total_points INTEGER NOT NULL,
                score REAL,
                filters_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                submitted_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attempt_questions (
                attempt_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                points INTEGER NOT NULL,
                PRIMARY KEY (attempt_id, question_id),
                FOREIGN KEY(attempt_id) REFERENCES quiz_attempts(id) ON DELETE CASCADE,
                FOREIGN KEY(question_id) REFERENCES questions(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS answer_scores (
                attempt_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                answer TEXT NOT NULL,
                score REAL NOT NULL,
                max_score INTEGER NOT NULL,
                feedback TEXT NOT NULL,
                grading_method TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (attempt_id, question_id),
                FOREIGN KEY(attempt_id) REFERENCES quiz_attempts(id) ON DELETE CASCADE,
                FOREIGN KEY(question_id) REFERENCES questions(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_questions_module ON questions(module)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_questions_topic ON questions(topic)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_questions_scope ON questions(scope)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attempts_user ON quiz_attempts(user_id)")


def seed_question_bank(
    seed_path: str | Path = DEFAULT_QUIZ_SEED_PATH,
    db_path: str | Path = DEFAULT_QUIZ_DB_PATH,
    *,
    clear_existing: bool = False,
) -> dict[str, int]:
    init_quiz_db(db_path)
    data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    now = _now()
    with connect(db_path) as conn:
        if clear_existing:
            conn.execute("DELETE FROM answer_scores")
            conn.execute("DELETE FROM attempt_questions")
            conn.execute("DELETE FROM quiz_attempts")
            conn.execute("DELETE FROM quiz_sets")
            conn.execute("DELETE FROM questions")

        for item in data.get("questions", []):
            question = _question_from_dict(item)
            conn.execute(
                """
                INSERT OR REPLACE INTO questions (
                    id, question_type, module, topic, difficulty, scope, use_cases_json,
                    points, prompt, choices_json, correct_answer, answer_keywords_json,
                    rubric_json, tolerance, explanation, tags_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    question.id,
                    question.question_type,
                    question.module,
                    question.topic,
                    question.difficulty,
                    question.scope,
                    _json(question.use_cases),
                    question.points,
                    question.prompt,
                    _json(question.choices),
                    question.correct_answer,
                    _json(question.answer_keywords),
                    _json(question.rubric),
                    question.tolerance,
                    question.explanation,
                    _json(question.tags),
                    now,
                    now,
                ),
            )

        for item in data.get("quiz_sets", []):
            quiz_set = _quiz_set_from_dict(item)
            conn.execute(
                """
                INSERT OR REPLACE INTO quiz_sets (
                    id, title, module, use_case, audience, question_ids_json,
                    description, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quiz_set.id,
                    quiz_set.title,
                    quiz_set.module,
                    quiz_set.use_case,
                    quiz_set.audience,
                    _json(quiz_set.question_ids),
                    quiz_set.description,
                    now,
                    now,
                ),
            )

    return {
        "questions": len(data.get("questions", [])),
        "quiz_sets": len(data.get("quiz_sets", [])),
    }


def list_quiz_sets(
    db_path: str | Path = DEFAULT_QUIZ_DB_PATH,
    *,
    audience: str | None = None,
) -> list[QuizSet]:
    _ensure_seeded(db_path)
    clauses = []
    params: list[str] = []
    if audience:
        clauses.append("audience = ?")
        params.append(audience)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM quiz_sets {where} ORDER BY use_case, module, title",
            params,
        ).fetchall()
    return [_row_to_quiz_set(row) for row in rows]


def list_questions(
    db_path: str | Path = DEFAULT_QUIZ_DB_PATH,
    *,
    module: str = "",
    topic: str = "",
    use_case: str = "",
    question_type: str = "",
    visible_to_role: str = "teacher",
    limit: int = 50,
) -> list[Question]:
    _ensure_seeded(db_path)
    questions = _all_questions(db_path)
    questions = _filter_questions(
        questions,
        module=module,
        topic=topic,
        use_case=use_case,
        question_type=question_type,
        visible_to_role=visible_to_role,
    )
    return questions[:limit]


def start_quiz(
    *,
    user_id: str,
    user_role: str,
    mode: str,
    set_id: str = "",
    module: str = "",
    topic: str = "",
    use_case: str = "",
    question_type: str = "",
    count: int = 5,
    db_path: str | Path = DEFAULT_QUIZ_DB_PATH,
    random_seed: int | None = None,
) -> QuizAttempt:
    _ensure_seeded(db_path)
    if mode not in {"teacher", "student"}:
        raise ValueError("mode must be teacher or student")
    if user_role == "student" and mode == "teacher":
        raise PermissionError("学生不能开启老师作业、考试或正式套题。")

    if set_id:
        quiz_set = get_quiz_set(set_id, db_path)
        if not quiz_set:
            raise KeyError(f"没有找到套题：{set_id}")
        if user_role == "student" and quiz_set.audience == "teacher":
            raise PermissionError("学生不能开启老师套题。")
        questions = _questions_by_ids(quiz_set.question_ids, db_path)
        title = quiz_set.title
        filters = {
            "set_id": set_id,
            "module": quiz_set.module,
            "use_case": quiz_set.use_case,
            "audience": quiz_set.audience,
        }
    else:
        target_use_case = use_case or ("practice" if mode == "student" else "lesson")
        visible_role = "student" if mode == "student" else user_role
        pool = list_questions(
            db_path,
            module=module,
            topic=topic,
            use_case=target_use_case,
            question_type=question_type,
            visible_to_role=visible_role,
            limit=500,
        )
        if not pool:
            raise ValueError("没有找到符合条件的题目。")
        rng = random.Random(random_seed)
        count = max(1, min(count, len(pool)))
        questions = rng.sample(pool, count)
        title = _build_attempt_title(mode, module, topic, target_use_case)
        filters = {
            "module": module,
            "topic": topic,
            "use_case": target_use_case,
            "question_type": question_type,
            "count": count,
        }

    if not questions:
        raise ValueError("套题中没有可用题目。")

    now = _now()
    attempt = QuizAttempt(
        id=str(uuid.uuid4()),
        user_id=user_id,
        user_role=user_role,
        mode=mode,
        set_id=set_id,
        title=title,
        status="assigned" if mode == "teacher" else "practice",
        total_points=sum(question.points for question in questions),
        score=None,
        created_at=now,
        submitted_at="",
        filters=filters,
    )
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO quiz_attempts (
                id, user_id, user_role, mode, set_id, title, status,
                total_points, score, filters_json, created_at, submitted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt.id,
                attempt.user_id,
                attempt.user_role,
                attempt.mode,
                attempt.set_id,
                attempt.title,
                attempt.status,
                attempt.total_points,
                attempt.score,
                _json(attempt.filters),
                attempt.created_at,
                attempt.submitted_at,
            ),
        )
        conn.executemany(
            """
            INSERT INTO attempt_questions (attempt_id, question_id, position, points)
            VALUES (?, ?, ?, ?)
            """,
            [
                (attempt.id, question.id, index, question.points)
                for index, question in enumerate(questions, start=1)
            ],
        )
    return attempt


def get_quiz_set(
    set_id: str,
    db_path: str | Path = DEFAULT_QUIZ_DB_PATH,
) -> QuizSet | None:
    _ensure_seeded(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM quiz_sets WHERE id = ?", (set_id,)).fetchone()
    return _row_to_quiz_set(row) if row else None


def get_attempt(
    attempt_id: str,
    db_path: str | Path = DEFAULT_QUIZ_DB_PATH,
) -> QuizAttempt | None:
    _ensure_seeded(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM quiz_attempts WHERE id = ?",
            (attempt_id,),
        ).fetchone()
    return _row_to_attempt(row) if row else None


def get_attempt_questions(
    attempt_id: str,
    db_path: str | Path = DEFAULT_QUIZ_DB_PATH,
) -> list[Question]:
    _ensure_seeded(db_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT questions.*
            FROM attempt_questions
            JOIN questions ON questions.id = attempt_questions.question_id
            WHERE attempt_questions.attempt_id = ?
            ORDER BY attempt_questions.position
            """,
            (attempt_id,),
        ).fetchall()
    return [_row_to_question(row) for row in rows]


def submit_quiz(
    attempt_id: str,
    answers: dict[str, str],
    db_path: str | Path = DEFAULT_QUIZ_DB_PATH,
) -> dict[str, Any]:
    attempt = get_attempt(attempt_id, db_path)
    if not attempt:
        raise KeyError(f"没有找到作答记录：{attempt_id}")
    questions = get_attempt_questions(attempt_id, db_path)
    if not questions:
        raise ValueError("这个作答记录没有题目。")

    now = _now()
    details = []
    total_score = 0.0
    with connect(db_path) as conn:
        for question in questions:
            answer = str(answers.get(question.id, "")).strip()
            score_detail = grade_answer(question, answer)
            total_score += score_detail["score"]
            details.append(score_detail)
            conn.execute(
                """
                INSERT OR REPLACE INTO answer_scores (
                    attempt_id, question_id, answer, score, max_score, feedback,
                    grading_method, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    question.id,
                    answer,
                    score_detail["score"],
                    question.points,
                    score_detail["feedback"],
                    score_detail["grading_method"],
                    now,
                ),
            )
        conn.execute(
            """
            UPDATE quiz_attempts
            SET status = 'submitted', score = ?, submitted_at = ?
            WHERE id = ?
            """,
            (total_score, now, attempt_id),
        )

    return {
        "attempt_id": attempt_id,
        "title": attempt.title,
        "score": round(total_score, 2),
        "total_points": attempt.total_points,
        "percentage": round(total_score / attempt.total_points * 100, 1)
        if attempt.total_points
        else 0,
        "details": details,
    }


def grade_answer(question: Question, answer: str) -> dict[str, Any]:
    if question.question_type == "multiple_choice":
        score, feedback = _grade_multiple_choice(question, answer)
        method = "rule_choice"
    elif question.question_type == "calculation":
        score, feedback = _grade_calculation(question, answer)
        method = "rule_numeric"
    elif question.question_type in {"short_answer", "code"}:
        score, feedback = _grade_rubric(question, answer)
        method = "rule_rubric"
    else:
        score = 0.0
        feedback = "未知题型，暂不能自动评分。"
        method = "unsupported"
    return {
        "question_id": question.id,
        "question_type": question.question_type,
        "score": round(score, 2),
        "max_score": question.points,
        "feedback": feedback,
        "grading_method": method,
        "explanation": question.explanation,
    }


def export_attempt_markdown(
    attempt_id: str,
    db_path: str | Path = DEFAULT_QUIZ_DB_PATH,
    output_dir: str | Path = "outputs",
) -> OutputBundle:
    attempt = get_attempt(attempt_id, db_path)
    if not attempt:
        raise KeyError(f"没有找到作答记录：{attempt_id}")
    questions = get_attempt_questions(attempt_id, db_path)
    output_path = ensure_output_dir(output_dir) / f"{slugify(attempt.title)}_{attempt.id[:8]}_题目.md"
    output_path.write_text(_render_attempt_markdown(attempt, questions), encoding="utf-8")
    return OutputBundle(
        source_path=output_path,
        metadata={
            "attempt_id": attempt.id,
            "question_count": len(questions),
            "total_points": attempt.total_points,
        },
    )


def export_score_markdown(
    score_result: dict[str, Any],
    output_dir: str | Path = "outputs",
) -> OutputBundle:
    title = score_result.get("title") or "测验评分"
    output_path = (
        ensure_output_dir(output_dir)
        / f"{slugify(title)}_{str(score_result['attempt_id'])[:8]}_评分.md"
    )
    output_path.write_text(_render_score_markdown(score_result), encoding="utf-8")
    return OutputBundle(
        source_path=output_path,
        metadata={
            "attempt_id": score_result["attempt_id"],
            "score": score_result["score"],
            "total_points": score_result["total_points"],
        },
    )


def _grade_multiple_choice(question: Question, answer: str) -> tuple[float, str]:
    expected = _normalize_choice(question.correct_answer)
    actual = _normalize_choice(answer)
    if actual and actual == expected:
        return float(question.points), "选择正确。"
    return 0.0, f"选择不正确。正确答案是 {question.correct_answer}。"


def _grade_calculation(question: Question, answer: str) -> tuple[float, str]:
    expected = _first_number(question.correct_answer)
    actual = _first_number(answer)
    if expected is None or actual is None:
        return 0.0, f"未识别到可比较的数值。参考答案：{question.correct_answer}。"
    tolerance = question.tolerance if question.tolerance is not None else 0.0
    if abs(actual - expected) <= tolerance:
        return float(question.points), "计算结果正确。"
    return (
        0.0,
        f"计算结果不正确。你的答案为 {actual:g}，参考答案为 {expected:g}，允许误差 {tolerance:g}。",
    )


def _grade_rubric(question: Question, answer: str) -> tuple[float, str]:
    if not answer.strip():
        return 0.0, "未提交答案。"
    normalized = _normalize_text(answer)
    earned = 0.0
    feedback_parts = []
    if question.rubric:
        for criterion in question.rubric:
            points = float(criterion.get("points", 0))
            keyword_groups = criterion.get("keyword_groups") or []
            if keyword_groups:
                groups = [
                    [str(alias) for alias in group]
                    for group in keyword_groups
                    if isinstance(group, list) and group
                ]
                matched_groups = [
                    group
                    for group in groups
                    if any(_normalize_text(alias) in normalized for alias in group)
                ]
                criterion_score = points * len(matched_groups) / len(groups) if groups else 0.0
            else:
                keywords = [str(item) for item in criterion.get("keywords", [])]
                matched = [keyword for keyword in keywords if _normalize_text(keyword) in normalized]
                criterion_score = points * len(matched) / len(keywords) if keywords else 0.0
            if keyword_groups:
                total_items = len(keyword_groups)
                matched_items = round(criterion_score / points * total_items) if points else 0
            elif criterion.get("keywords"):
                total_items = len(criterion.get("keywords", []))
                matched_items = len(matched)
            else:
                total_items = 0
                matched_items = 0
            earned += criterion_score
            status = "覆盖" if criterion_score >= points * 0.67 else "不足"
            feedback_parts.append(
                f"{criterion.get('label', '评分点')}：{status}，覆盖 {matched_items}/{total_items}，得 {criterion_score:.1f}/{points:.1f} 分"
            )
    elif question.answer_keywords:
        matched = [
            keyword
            for keyword in question.answer_keywords
            if _normalize_text(keyword) in normalized
        ]
        earned = question.points * len(matched) / len(question.answer_keywords)
        feedback_parts.append(
            f"关键词覆盖 {len(matched)}/{len(question.answer_keywords)}：{'、'.join(matched) or '暂无'}"
        )
    else:
        earned = 0.0
        feedback_parts.append("没有配置 rubric 或关键词，暂不能自动评分。")
    earned = max(0.0, min(float(question.points), earned))
    return earned, "；".join(feedback_parts)


def _filter_questions(
    questions: list[Question],
    *,
    module: str = "",
    topic: str = "",
    use_case: str = "",
    question_type: str = "",
    visible_to_role: str = "teacher",
) -> list[Question]:
    filtered = []
    for question in questions:
        if visible_to_role == "student" and question.scope == "teacher":
            continue
        if module and module not in question.module and module not in question.topic:
            continue
        if topic and topic not in question.topic and topic not in question.module:
            continue
        if use_case and use_case not in question.use_cases:
            continue
        if question_type and question.question_type != question_type:
            continue
        filtered.append(question)
    return filtered


def _questions_by_ids(
    question_ids: list[str],
    db_path: str | Path = DEFAULT_QUIZ_DB_PATH,
) -> list[Question]:
    if not question_ids:
        return []
    placeholders = ",".join("?" for _ in question_ids)
    with connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM questions WHERE id IN ({placeholders})",
            question_ids,
        ).fetchall()
    by_id = {_row_to_question(row).id: _row_to_question(row) for row in rows}
    return [by_id[item] for item in question_ids if item in by_id]


def _all_questions(db_path: str | Path = DEFAULT_QUIZ_DB_PATH) -> list[Question]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM questions ORDER BY module, topic, id").fetchall()
    return [_row_to_question(row) for row in rows]


def _render_attempt_markdown(attempt: QuizAttempt, questions: list[Question]) -> str:
    lines = [
        f"# {attempt.title}",
        "",
        f"- attempt_id: `{attempt.id}`",
        f"- mode: `{attempt.mode}`",
        f"- total_points: {attempt.total_points}",
        "",
        "## 题目",
        "",
    ]
    for index, question in enumerate(questions, start=1):
        lines.extend(
            [
                f"### {index}. [{question.question_type}] {question.module} / {question.topic}",
                "",
                f"分值：{question.points}",
                "",
                question.prompt,
                "",
            ]
        )
        if question.choices:
            lines.extend(f"- {choice}" for choice in question.choices)
            lines.append("")
        lines.extend([f"作答 question_id：`{question.id}`", ""])
    lines.extend(
        [
            "## 提交答案格式",
            "",
            "在 CLI 中使用 JSON 文件提交，格式如下：",
            "",
            "```json",
            json.dumps({question.id: "" for question in questions}, ensure_ascii=False, indent=2),
            "```",
        ]
    )
    return "\n".join(lines)


def _render_score_markdown(score_result: dict[str, Any]) -> str:
    lines = [
        f"# {score_result['title']}评分结果",
        "",
        f"- attempt_id: `{score_result['attempt_id']}`",
        f"- score: {score_result['score']} / {score_result['total_points']}",
        f"- percentage: {score_result['percentage']}%",
        "",
        "## 分题反馈",
        "",
    ]
    for index, detail in enumerate(score_result["details"], start=1):
        lines.extend(
            [
                f"### {index}. {detail['question_id']}",
                "",
                f"- 得分：{detail['score']} / {detail['max_score']}",
                f"- 评分方式：{detail['grading_method']}",
                f"- 反馈：{detail['feedback']}",
                f"- 参考说明：{detail['explanation']}",
                "",
            ]
        )
    return "\n".join(lines)


def _build_attempt_title(mode: str, module: str, topic: str, use_case: str) -> str:
    target = module or topic or "综合"
    if mode == "student":
        return f"{target}自由练习"
    use_case_label = {
        "lesson": "课后小测",
        "midterm": "期中测验",
        "final": "期末测验",
        "assignment": "作业",
        "exam": "考试",
    }.get(use_case, "老师习题")
    return f"{target}{use_case_label}"


def _question_from_dict(item: dict[str, Any]) -> Question:
    question_type = item.get("question_type", "")
    if question_type not in QUESTION_TYPES:
        raise ValueError(f"Unsupported question_type: {question_type}")
    return Question(
        id=item["id"],
        question_type=question_type,
        module=item.get("module", ""),
        topic=item.get("topic", ""),
        difficulty=item.get("difficulty", "medium"),
        scope=item.get("scope", "both"),
        use_cases=list(item.get("use_cases", [])),
        points=int(item.get("points", 0)),
        prompt=item["prompt"],
        choices=list(item.get("choices", [])),
        correct_answer=str(item.get("correct_answer", "")),
        answer_keywords=list(item.get("answer_keywords", [])),
        rubric=list(item.get("rubric", [])),
        tolerance=item.get("tolerance"),
        explanation=item.get("explanation", ""),
        tags=list(item.get("tags", [])),
    )


def _quiz_set_from_dict(item: dict[str, Any]) -> QuizSet:
    return QuizSet(
        id=item["id"],
        title=item["title"],
        module=item.get("module", ""),
        use_case=item.get("use_case", ""),
        audience=item.get("audience", "teacher"),
        question_ids=list(item.get("question_ids", [])),
        description=item.get("description", ""),
    )


def _row_to_question(row: sqlite3.Row) -> Question:
    return Question(
        id=row["id"],
        question_type=row["question_type"],
        module=row["module"],
        topic=row["topic"],
        difficulty=row["difficulty"],
        scope=row["scope"],
        use_cases=json.loads(row["use_cases_json"]),
        points=row["points"],
        prompt=row["prompt"],
        choices=json.loads(row["choices_json"]),
        correct_answer=row["correct_answer"],
        answer_keywords=json.loads(row["answer_keywords_json"]),
        rubric=json.loads(row["rubric_json"]),
        tolerance=row["tolerance"],
        explanation=row["explanation"],
        tags=json.loads(row["tags_json"]),
    )


def _row_to_quiz_set(row: sqlite3.Row) -> QuizSet:
    return QuizSet(
        id=row["id"],
        title=row["title"],
        module=row["module"],
        use_case=row["use_case"],
        audience=row["audience"],
        question_ids=json.loads(row["question_ids_json"]),
        description=row["description"],
    )


def _row_to_attempt(row: sqlite3.Row) -> QuizAttempt:
    return QuizAttempt(
        id=row["id"],
        user_id=row["user_id"],
        user_role=row["user_role"],
        mode=row["mode"],
        set_id=row["set_id"],
        title=row["title"],
        status=row["status"],
        total_points=row["total_points"],
        score=row["score"],
        created_at=row["created_at"],
        submitted_at=row["submitted_at"],
        filters=json.loads(row["filters_json"]),
    )


def _ensure_seeded(db_path: str | Path = DEFAULT_QUIZ_DB_PATH) -> None:
    init_quiz_db(db_path)
    with connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    if count == 0 and DEFAULT_QUIZ_SEED_PATH.exists():
        seed_question_bank(DEFAULT_QUIZ_SEED_PATH, db_path)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_choice(value: str) -> str:
    value = value.strip().upper()
    match = re.match(r"([A-Z])", value)
    return match.group(1) if match else value


def _first_number(value: str) -> float | None:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value)
    return float(match.group(0)) if match else None


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()
