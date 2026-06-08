from __future__ import annotations

import argparse
import json
from pathlib import Path

from teacher_agent.exam_module import ExamRequest, generate_exam
from teacher_agent.knowledge_db import (
    DEFAULT_DB_PATH,
    KnowledgeItem,
    add_item,
    delete_item,
    get_item,
    import_pdf,
    init_db,
    list_items,
    read_pages,
    row_to_dict,
    search_items,
    search_pages,
    seed_items,
)
from teacher_agent.ppt_module import PPTRequest, generate_ppt
from teacher_agent.quiz_module import (
    DEFAULT_QUIZ_DB_PATH,
    DEFAULT_QUIZ_SEED_PATH,
    export_attempt_markdown,
    export_score_markdown,
    get_attempt_questions,
    list_quiz_sets,
    seed_question_bank,
    start_quiz,
    submit_quiz,
)
from teacher_agent.schemas import ClassProfile


def load_class_profile(path: str | Path) -> ClassProfile:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return ClassProfile.from_dict(data)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Teacher-side generator for Bio Agent")
    parser.add_argument(
        "--profile",
        default="examples/class_profile.json",
        help="班级画像 JSON 文件",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs",
        help="输出目录",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="知识库 SQLite 数据库路径",
    )
    parser.add_argument(
        "--quiz-db",
        default=str(DEFAULT_QUIZ_DB_PATH),
        help="题库 SQLite 数据库路径",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    ppt = subparsers.add_parser("ppt", help="生成教师课件")
    ppt.add_argument("--topic", required=True, help="课件主题")
    ppt.add_argument("--slide-count", type=int, default=10, help="幻灯片页数")
    ppt.add_argument(
        "--objective",
        action="append",
        default=[],
        help="学习目标，可重复传入",
    )

    exam = subparsers.add_parser("exam", help="生成 LaTeX 试卷，并在有 LaTeX 环境时编译 PDF")
    exam.add_argument("--title", required=True, help="试卷标题")
    exam.add_argument("--topic", action="append", default=[], help="考试主题，可重复传入")
    exam.add_argument("--duration", type=int, default=90, help="考试时长，单位分钟")
    exam.add_argument("--points", type=int, default=100, help="总分")
    exam.add_argument("--difficulty", default="medium", help="难度")
    exam.add_argument(
        "--no-pdf",
        action="store_true",
        help="只生成 .tex，不尝试编译 PDF",
    )

    kb = subparsers.add_parser("kb", help="手动查阅、增删本地知识库")
    kb_subparsers = kb.add_subparsers(dest="kb_command", required=True)

    kb_subparsers.add_parser("init", help="初始化知识库数据库")

    seed = kb_subparsers.add_parser("seed", help="导入 JSON 种子数据")
    seed.add_argument(
        "--file",
        default="examples/knowledge_seed.json",
        help="种子数据 JSON 文件",
    )

    kb_list = kb_subparsers.add_parser("list", help="列出知识库条目")
    kb_list.add_argument("--limit", type=int, default=20)
    kb_list.add_argument("--type", default=None, help="按 source_type 过滤")
    kb_list.add_argument("--language", default=None, help="按语言过滤：zh/en")

    search = kb_subparsers.add_parser("search", help="搜索知识库")
    search.add_argument("query", help="搜索关键词")
    search.add_argument("--limit", type=int, default=20)

    search_pdf = kb_subparsers.add_parser("search-pages", help="搜索已导入 PDF 页面文本")
    search_pdf.add_argument("query", help="搜索关键词")
    search_pdf.add_argument("--limit", type=int, default=10)

    show = kb_subparsers.add_parser("show", help="查看条目详情")
    show.add_argument("id", help="知识库条目 ID")

    read = kb_subparsers.add_parser("read", help="按页阅读已导入 PDF 文本")
    read.add_argument("id", help="知识库条目 ID")
    read.add_argument("--page", type=int, default=1, help="起始页码")
    read.add_argument("--pages", type=int, default=1, help="读取页数")

    add = kb_subparsers.add_parser("add", help="新增知识库条目")
    add.add_argument("--title", required=True)
    add.add_argument("--type", default="textbook", dest="source_type")
    add.add_argument("--language", default="zh")
    add.add_argument("--topic", action="append", default=[])
    add.add_argument("--url", default="")
    add.add_argument("--authors", default="")
    add.add_argument("--year", default="")
    add.add_argument("--summary", default="")
    add.add_argument("--content", default="")
    add.add_argument("--tag", action="append", default=[])

    delete = kb_subparsers.add_parser("delete", help="删除知识库条目")
    delete.add_argument("id", help="知识库条目 ID")

    import_pdf_parser = kb_subparsers.add_parser("import-pdf", help="导入本地或远程 PDF 并抽取文本")
    import_pdf_parser.add_argument("--title", required=True)
    import_pdf_parser.add_argument("--file", default=None, help="本地 PDF 路径")
    import_pdf_parser.add_argument("--url", default="", help="远程 PDF URL")
    import_pdf_parser.add_argument("--type", default="textbook", dest="source_type")
    import_pdf_parser.add_argument("--language", default="zh")
    import_pdf_parser.add_argument("--topic", action="append", default=[])
    import_pdf_parser.add_argument("--authors", default="")
    import_pdf_parser.add_argument("--year", default="")
    import_pdf_parser.add_argument("--summary", default="")
    import_pdf_parser.add_argument("--tag", action="append", default=[])

    quiz = subparsers.add_parser("quiz", help="题库、抽题和自动评分")
    quiz_subparsers = quiz.add_subparsers(dest="quiz_command", required=True)

    quiz_subparsers.add_parser("init", help="初始化题库并导入示例题")

    quiz_seed = quiz_subparsers.add_parser("seed", help="导入题库 JSON")
    quiz_seed.add_argument("--file", default=str(DEFAULT_QUIZ_SEED_PATH), help="题库 JSON 文件")
    quiz_seed.add_argument("--clear", action="store_true", help="清空旧题库和作答记录后导入")

    quiz_sets = quiz_subparsers.add_parser("sets", help="列出老师套题")
    quiz_sets.add_argument("--audience", default=None, help="按 audience 过滤，例如 teacher")

    quiz_start = quiz_subparsers.add_parser("start", help="开始一次老师套题或学生自由练习")
    quiz_start.add_argument("--mode", choices=["teacher", "student"], default="student")
    quiz_start.add_argument("--user-role", choices=["teacher", "student"], default="student")
    quiz_start.add_argument("--user-id", default="student_demo")
    quiz_start.add_argument("--set-id", default="", help="老师套题 ID，例如 lesson_fdr")
    quiz_start.add_argument("--module", default="", help="按模块筛选，例如 Logistic 回归")
    quiz_start.add_argument("--topic", default="", help="按知识点筛选")
    quiz_start.add_argument("--use-case", default="", help="lesson/midterm/final/practice")
    quiz_start.add_argument("--type", default="", dest="question_type", help="题型筛选")
    quiz_start.add_argument("--count", type=int, default=5, help="自由抽题数量")
    quiz_start.add_argument("--seed", type=int, default=None, help="随机种子，便于复现实验")

    quiz_submit = quiz_subparsers.add_parser("submit", help="提交答案并评分")
    quiz_submit.add_argument("--attempt-id", required=True)
    quiz_submit.add_argument("--answers-file", required=True, help="答案 JSON，键为 question_id")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "kb":
        return handle_kb_command(args)
    if args.command == "quiz":
        return handle_quiz_command(args)

    profile = load_class_profile(args.profile)

    if args.command == "ppt":
        bundle = generate_ppt(
            PPTRequest(
                topic=args.topic,
                class_profile=profile,
                learning_objectives=args.objective,
                slide_count=args.slide_count,
                output_dir=args.output_dir,
            )
        )
    else:
        bundle = generate_exam(
            ExamRequest(
                title=args.title,
                class_profile=profile,
                topics=args.topic,
                duration_minutes=args.duration,
                total_points=args.points,
                difficulty=args.difficulty,
                output_dir=args.output_dir,
                compile_pdf=not args.no_pdf,
            )
        )

    print(f"source: {bundle.source_path}")
    if bundle.artifact_path:
        print(f"artifact: {bundle.artifact_path}")
    else:
        print("artifact: not generated")
    print(f"metadata: {json.dumps(bundle.metadata, ensure_ascii=False)}")
    return 0


def handle_kb_command(args: argparse.Namespace) -> int:
    db_path = args.db

    if args.kb_command == "init":
        init_db(db_path)
        print(f"initialized: {db_path}")
        return 0

    if args.kb_command == "seed":
        items = [
            KnowledgeItem(
                title=item["title"],
                source_type=item.get("source_type", "textbook"),
                language=item.get("language", "zh"),
                topics=list(item.get("topics", [])),
                url=item.get("url", ""),
                authors=item.get("authors", ""),
                year=item.get("year", ""),
                summary=item.get("summary", ""),
                content=item.get("content", ""),
                tags=list(item.get("tags", [])),
            )
            for item in json.loads(Path(args.file).read_text(encoding="utf-8"))
        ]
        count = seed_items(items, db_path)
        print(f"seeded: {count}")
        return 0

    if args.kb_command == "list":
        rows = list_items(
            db_path,
            limit=args.limit,
            source_type=args.type,
            language=args.language,
        )
        _print_rows(rows)
        return 0

    if args.kb_command == "search":
        rows = search_items(args.query, db_path, limit=args.limit)
        _print_rows(rows)
        return 0

    if args.kb_command == "search-pages":
        rows = search_pages(args.query, db_path, limit=args.limit)
        _print_page_rows(rows, args.query)
        return 0

    if args.kb_command == "show":
        row = get_item(args.id, db_path)
        if not row:
            print("not found")
            return 1
        print(json.dumps(row_to_dict(row), ensure_ascii=False, indent=2))
        return 0

    if args.kb_command == "read":
        rows = read_pages(
            args.id,
            db_path,
            start_page=args.page,
            page_count=args.pages,
        )
        if not rows:
            print("no page text found")
            return 1
        for row in rows:
            print(f"\n--- page {row['page_number']} ---\n")
            print(row["text"])
        return 0

    if args.kb_command == "add":
        item_id = add_item(
            KnowledgeItem(
                title=args.title,
                source_type=args.source_type,
                language=args.language,
                topics=args.topic,
                url=args.url,
                authors=args.authors,
                year=args.year,
                summary=args.summary,
                content=args.content,
                tags=args.tag,
            ),
            db_path,
        )
        print(f"added: {item_id}")
        return 0

    if args.kb_command == "delete":
        deleted = delete_item(args.id, db_path)
        print("deleted" if deleted else "not found")
        return 0 if deleted else 1

    if args.kb_command == "import-pdf":
        if not args.file and not args.url:
            print("import-pdf requires --file or --url")
            return 1
        item_id = import_pdf(
            title=args.title,
            file_path=args.file,
            url=args.url,
            source_type=args.source_type,
            language=args.language,
            topics=args.topic,
            authors=args.authors,
            year=args.year,
            summary=args.summary,
            tags=args.tag,
            db_path=db_path,
        )
        print(f"imported: {item_id}")
        return 0

    raise ValueError(f"Unknown kb command: {args.kb_command}")


def handle_quiz_command(args: argparse.Namespace) -> int:
    quiz_db_path = args.quiz_db

    if args.quiz_command == "init":
        stats = seed_question_bank(DEFAULT_QUIZ_SEED_PATH, quiz_db_path)
        print(f"initialized quiz bank: {quiz_db_path}")
        print(f"metadata: {json.dumps(stats, ensure_ascii=False)}")
        return 0

    if args.quiz_command == "seed":
        stats = seed_question_bank(args.file, quiz_db_path, clear_existing=args.clear)
        print(f"seeded quiz bank: {quiz_db_path}")
        print(f"metadata: {json.dumps(stats, ensure_ascii=False)}")
        return 0

    if args.quiz_command == "sets":
        quiz_sets = list_quiz_sets(quiz_db_path, audience=args.audience)
        if not quiz_sets:
            print("no quiz sets")
            return 0
        for quiz_set in quiz_sets:
            print(
                f"{quiz_set.id}\t{quiz_set.title}\t{quiz_set.module}\t"
                f"{quiz_set.use_case}\t{len(quiz_set.question_ids)}题\t{quiz_set.description}"
            )
        return 0

    if args.quiz_command == "start":
        attempt = start_quiz(
            user_id=args.user_id,
            user_role=args.user_role,
            mode=args.mode,
            set_id=args.set_id,
            module=args.module,
            topic=args.topic,
            use_case=args.use_case,
            question_type=args.question_type,
            count=args.count,
            db_path=quiz_db_path,
            random_seed=args.seed,
        )
        bundle = export_attempt_markdown(attempt.id, quiz_db_path, args.output_dir)
        questions = get_attempt_questions(attempt.id, quiz_db_path)
        print(f"attempt_id: {attempt.id}")
        print(f"title: {attempt.title}")
        print(f"questions: {len(questions)}")
        print(f"total_points: {attempt.total_points}")
        print(f"source: {bundle.source_path}")
        return 0

    if args.quiz_command == "submit":
        answers = json.loads(Path(args.answers_file).read_text(encoding="utf-8"))
        score_result = submit_quiz(args.attempt_id, answers, quiz_db_path)
        bundle = export_score_markdown(score_result, args.output_dir)
        print(f"attempt_id: {score_result['attempt_id']}")
        print(f"score: {score_result['score']} / {score_result['total_points']}")
        print(f"percentage: {score_result['percentage']}%")
        print(f"source: {bundle.source_path}")
        return 0

    raise ValueError(f"Unknown quiz command: {args.quiz_command}")


def _print_rows(rows) -> None:
    if not rows:
        print("no items")
        return

    for row in rows:
        data = row_to_dict(row)
        topics = "、".join(data.get("topics", []))
        print(
            f"{data['id']}\t{data['title']}\t{data['source_type']}\t"
            f"{data['language']}\t{topics}\t{data.get('url', '')}"
        )


def _print_page_rows(rows, query: str) -> None:
    if not rows:
        print("no page matches")
        return

    for row in rows:
        snippet = _make_snippet(row["text"], query)
        print(f"{row['item_id']}\tpage {row['page_number']}\t{row['title']}\t{snippet}")


def _make_snippet(text: str, query: str, width: int = 120) -> str:
    index = text.lower().find(query.lower())
    if index < 0:
        return text[:width].replace("\n", " ")
    start = max(0, index - width // 2)
    end = min(len(text), index + len(query) + width // 2)
    return text[start:end].replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
