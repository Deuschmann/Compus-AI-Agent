from __future__ import annotations

import argparse
import errno
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from teacher_agent.chatflow_client import is_chatflow_configured
from teacher_agent.conversation_agent import handle_teacher_message
from teacher_agent.quiz_module import grade_answer, get_attempt, get_attempt_questions, start_quiz, submit_quiz
from teacher_agent.schemas import ClassProfile
from teacher_agent.session_store import (
    add_message,
    create_session,
    delete_session,
    get_session,
    get_messages,
    init_session_db,
    list_sessions,
    update_session,
)
from teacher_agent.yml_llm_client import is_yml_llm_configured


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PROFILE_PATH = Path("examples/class_profile.json")
CODE_RUN_TIMEOUT_SECONDS = 30


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    init_session_db()
    server = ReusableThreadingHTTPServer((host, port), TeacherAgentHandler)
    print(f"Teacher Agent UI: http://{host}:{port}")
    server.serve_forever()


class TeacherAgentHandler(BaseHTTPRequestHandler):
    server_version = "TeacherAgent/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(INDEX_HTML)
            return
        if path == "/quiz":
            query = parse_qs(urlparse(self.path).query)
            attempt_id = query.get("attempt_id", [""])[0]
            if not attempt_id:
                self._send_html(_quiz_error_html("缺少 attempt_id"))
                return
            self._send_html(_quiz_page_html(attempt_id))
            return
        if path == "/practice":
            self._send_html(_practice_page_html())
            return
        if path == "/api/sessions":
            self._send_json([session.__dict__ for session in list_sessions()])
            return
        if path == "/api/status":
            self._send_json(_status_payload())
            return
        if path.startswith("/api/sessions/") and path.endswith("/messages"):
            session_id = unquote(path.split("/")[3])
            messages = [
                {
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": row["created_at"],
                }
                for row in get_messages(session_id, limit=200)
            ]
            self._send_json(messages)
            return
        if path.startswith("/api/sessions/") and path.endswith("/artifact"):
            session_id = unquote(path.split("/")[3])
            query = parse_qs(urlparse(self.path).query)
            artifact_path = query.get("path", [""])[0]
            self._send_artifact(session_id, artifact_path)
            return
        if path.startswith("/api/quiz/attempts/"):
            attempt_id = unquote(path.split("/")[4])
            payload = _quiz_attempt_payload(attempt_id)
            if not payload:
                self._send_json({"error": "attempt not found"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(payload)
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._read_json()
        if path == "/api/sessions":
            title = body.get("title") or "新的老师会话"
            user_role = body.get("user_role") or "teacher"
            user_id = body.get("user_id") or (
                "student_demo" if user_role == "student" else "teacher_demo"
            )
            session = create_session(title, user_role=user_role, user_id=user_id)
            self._send_json(session.__dict__, status=HTTPStatus.CREATED)
            return
        if path.startswith("/api/sessions/") and path.endswith("/messages"):
            session_id = unquote(path.split("/")[3])
            message = (body.get("message") or "").strip()
            if not message:
                self._send_json({"error": "empty message"}, status=HTTPStatus.BAD_REQUEST)
                return
            session = get_session(session_id)
            if not session:
                self._send_json({"error": "session not found"}, status=HTTPStatus.NOT_FOUND)
                return
            add_message(session_id, "user", message)
            response = handle_teacher_message(
                message,
                profile=_load_profile(),
                user_role=session.user_role,
                user_id=session.user_id,
                session_id=session.id,
            )
            add_message(session_id, "assistant", _format_agent_response(response))
            self._send_json(response)
            return
        if path == "/api/quiz/start":
            try:
                attempt = start_quiz(
                    user_id=(body.get("user_id") or "student_demo").strip(),
                    user_role=(body.get("user_role") or "student").strip(),
                    mode=(body.get("mode") or "student").strip(),
                    set_id=(body.get("set_id") or "").strip(),
                    module=(body.get("module") or "").strip(),
                    topic=(body.get("topic") or "").strip(),
                    use_case=(body.get("use_case") or "practice").strip(),
                    question_type=(body.get("question_type") or "").strip(),
                    count=int(body.get("count") or 5),
                )
            except PermissionError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(
                {
                    "attempt_id": attempt.id,
                    "title": attempt.title,
                    "total_points": attempt.total_points,
                    "quiz_url": f"/quiz?attempt_id={quote(attempt.id)}",
                },
                status=HTTPStatus.CREATED,
            )
            return
        if path == "/api/quiz/code/check":
            attempt_id = (body.get("attempt_id") or "").strip()
            question_id = (body.get("question_id") or "").strip()
            code = str(body.get("code") or "")
            if not attempt_id or not question_id:
                self._send_json(
                    {"error": "attempt_id and question_id are required"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                result = _check_code_answer(attempt_id, question_id, code)
            except KeyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(result)
            return
        if path == "/api/quiz/code/run":
            attempt_id = (body.get("attempt_id") or "").strip()
            question_id = (body.get("question_id") or "").strip()
            code = str(body.get("code") or "")
            if not attempt_id or not question_id:
                self._send_json(
                    {"error": "attempt_id and question_id are required"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                result = _run_code_answer(attempt_id, question_id, code)
            except KeyError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(result)
            return
        if path.startswith("/api/quiz/attempts/") and path.endswith("/submit"):
            attempt_id = unquote(path.split("/")[4])
            answers = body.get("answers") or {}
            if not isinstance(answers, dict):
                self._send_json({"error": "answers must be an object"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                result = submit_quiz(attempt_id, {str(k): str(v) for k, v in answers.items()})
            except KeyError:
                self._send_json({"error": "attempt not found"}, status=HTTPStatus.NOT_FOUND)
                return
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(result)
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/sessions/"):
            session_id = unquote(path.split("/")[3])
            deleted = delete_session(session_id)
            self._send_json({"deleted": deleted}, status=HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        body = self._read_json()
        if path.startswith("/api/sessions/"):
            session_id = unquote(path.split("/")[3])
            updated = update_session(
                session_id,
                title=(body.get("title") or "").strip() or None,
                user_role=(body.get("user_role") or "").strip() or None,
                user_id=(body.get("user_id") or "").strip() or None,
            )
            if not updated:
                self._send_json({"error": "session not found"}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_json(updated.__dict__)
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        return

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def _send_json(self, data, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_artifact(self, session_id: str, artifact_path: str) -> None:
        session = get_session(session_id)
        if not session:
            self._send_json({"error": "session not found"}, status=HTTPStatus.NOT_FOUND)
            return

        path = Path(artifact_path)
        if not _is_allowed_artifact_path(path, session.user_role, session.user_id):
            self._send_json({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
            return
        if not path.exists():
            self._send_json({"error": "file not found"}, status=HTTPStatus.NOT_FOUND)
            return

        if path.suffix.lower() == ".md":
            self._send_html(_markdown_preview_html(path.name, path.read_text(encoding="utf-8")))
            return

        payload = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content_type.startswith("text/"):
            content_type = f"{content_type}; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Content-Disposition",
            f"inline; filename*=UTF-8''{quote(path.name)}",
        )
        self.end_headers()
        self.wfile.write(payload)


def _load_profile() -> ClassProfile:
    data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    return ClassProfile.from_dict(data)


def _format_agent_response(response: dict) -> str:
    content = response["content"]
    artifacts = response.get("artifacts") or []
    if not artifacts:
        return content
    artifact_lines = [f"- {item['label']}: {item['path']}" for item in artifacts]
    return content + "\n\n生成文件：\n" + "\n".join(artifact_lines)


def _status_payload() -> dict:
    allow_local_fallback = os.getenv("AGENT_ALLOW_LOCAL_FALLBACK", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return {
        "chatflow_configured": is_chatflow_configured(),
        "yml_llm_configured": is_yml_llm_configured(),
        "require_dify": (
            os.getenv("AGENT_REQUIRE_DIFY", "").lower() in {"1", "true", "yes", "on"}
            or not allow_local_fallback
        ),
        "allow_local_fallback": allow_local_fallback,
        "chatflow_url": os.getenv("DIFY_CHATFLOW_API_URL", "http://localhost/v1/chat-messages"),
    }


def _quiz_attempt_payload(attempt_id: str) -> dict | None:
    attempt = get_attempt(attempt_id)
    if not attempt:
        return None
    questions = get_attempt_questions(attempt_id)
    return {
        "attempt_id": attempt.id,
        "title": attempt.title,
        "mode": attempt.mode,
        "status": attempt.status,
        "total_points": attempt.total_points,
        "questions": [
            {
                "id": question.id,
                "question_type": question.question_type,
                "module": question.module,
                "topic": question.topic,
                "points": question.points,
                "prompt": question.prompt,
                "choices": question.choices,
            }
            for question in questions
        ],
    }


def _check_code_answer(attempt_id: str, question_id: str, code: str) -> dict:
    attempt = get_attempt(attempt_id)
    if not attempt:
        raise KeyError("attempt not found")
    questions = get_attempt_questions(attempt_id)
    question = next((item for item in questions if item.id == question_id), None)
    if not question:
        raise KeyError("question not found in attempt")
    if question.question_type != "code":
        raise ValueError("只有代码题支持代码检查")

    language = _detect_code_language(question.topic, code)
    syntax = _check_code_syntax(language, code)
    rubric_preview = grade_answer(question, code)
    return {
        "question_id": question.id,
        "language": language,
        "syntax": syntax,
        "rubric_preview": rubric_preview,
    }


def _run_code_answer(attempt_id: str, question_id: str, code: str) -> dict:
    attempt = get_attempt(attempt_id)
    if not attempt:
        raise KeyError("attempt not found")
    questions = get_attempt_questions(attempt_id)
    question = next((item for item in questions if item.id == question_id), None)
    if not question:
        raise KeyError("question not found in attempt")
    if question.question_type != "code":
        raise ValueError("只有代码题支持运行代码")
    if not code.strip():
        return {
            "question_id": question_id,
            "language": "text",
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "还没有输入代码。",
            "timed_out": False,
        }

    language = _detect_code_language(question.topic, code)
    if language == "python":
        return _run_python_code(question.id, code)
    if language == "r":
        return _run_r_code(question.id, code)
    return {
        "question_id": question_id,
        "language": language,
        "ok": False,
        "exit_code": None,
        "stdout": "",
        "stderr": "暂未识别代码语言，不能运行。",
        "timed_out": False,
    }


def _detect_code_language(topic: str, code: str) -> str:
    lowered_topic = topic.lower()
    lowered_code = code.lower()
    if "r 语言" in topic or re.search(r"\bglm\s*\(", code) or "family = binomial" in lowered_code:
        return "r"
    if "python" in lowered_topic or "statsmodels" in lowered_code or "import " in lowered_code:
        return "python"
    return "text"


def _check_code_syntax(language: str, code: str) -> dict:
    if not code.strip():
        return {"ok": False, "message": "还没有输入代码。"}
    if language == "python":
        return _check_python_syntax(code)
    if language == "r":
        return _check_r_syntax(code)
    return {"ok": None, "message": "暂未识别语言，只做 rubric 结构检查。"}


def _check_python_syntax(code: str) -> dict:
    try:
        compile(code, "<student_answer>", "exec")
    except SyntaxError as exc:
        return {
            "ok": False,
            "message": f"Python 语法错误：第 {exc.lineno or '?'} 行，{exc.msg}",
        }
    return {"ok": True, "message": "Python 语法检查通过。"}


def _run_python_code(question_id: str, code: str) -> dict:
    script = _python_fixture(question_id) + "\n" + code + "\n"
    return _run_temp_script(
        command=[sys.executable],
        suffix=".py",
        script=script,
        language="python",
    )


def _run_r_code(question_id: str, code: str) -> dict:
    rscript = shutil.which("Rscript")
    if not rscript:
        return {
            "language": "r",
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "本机没有找到 Rscript，不能运行 R 代码。",
            "timed_out": False,
        }
    script = _r_fixture(question_id) + "\n" + code + "\n"
    return _run_temp_script(
        command=[rscript],
        suffix=".R",
        script=script,
        language="r",
    )


def _run_temp_script(
    *,
    command: list[str],
    suffix: str,
    script: str,
    language: str,
) -> dict:
    with tempfile.TemporaryDirectory() as temp_dir:
        script_path = Path(temp_dir) / f"student_answer{suffix}"
        script_path.write_text(script, encoding="utf-8")
        try:
            result = subprocess.run(
                command + [str(script_path)],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                timeout=CODE_RUN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "language": language,
                "ok": False,
                "exit_code": None,
                "stdout": _clean_snippet(exc.stdout or "", max_length=2000),
                "stderr": f"运行超时：代码超过 {CODE_RUN_TIMEOUT_SECONDS} 秒仍未结束。",
                "timed_out": True,
            }
    return {
        "language": language,
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "stdout": _clean_snippet(result.stdout or "", max_length=4000),
        "stderr": _clean_snippet(result.stderr or "", max_length=4000),
        "timed_out": False,
    }


def _python_fixture(question_id: str) -> str:
    if question_id == "q_logistic_code_001":
        return """\
try:
    import pandas as pd
    df = pd.DataFrame({
        "y": [0, 1, 0, 1, 0, 1, 0, 1, 1, 0, 1, 0],
        "age": [23, 45, 31, 52, 47, 28, 36, 59, 41, 33, 50, 39],
        "sex": [0, 1, 1, 0, 1, 0, 0, 1, 0, 1, 1, 0],
        "exposure": [0, 1, 0, 1, 1, 0, 1, 1, 0, 0, 1, 0],
    })
    y = df["y"]
except Exception as fixture_error:
    print("示例数据准备失败:", fixture_error)
"""
    return ""


def _r_fixture(question_id: str) -> str:
    if question_id == "q_logistic_code_r_001":
        return """\
df <- data.frame(
  y = c(0, 1, 0, 1, 0, 1, 0, 1, 1, 0, 1, 0),
  age = c(23, 45, 31, 52, 47, 28, 36, 59, 41, 33, 50, 39),
  sex = c(0, 1, 1, 0, 1, 0, 0, 1, 0, 1, 1, 0),
  exposure = c(0, 1, 0, 1, 1, 0, 1, 1, 0, 0, 1, 0)
)
"""
    return ""


def _check_r_syntax(code: str) -> dict:
    rscript = shutil.which("Rscript")
    if not rscript:
        return {
            "ok": None,
            "message": "本机没有找到 Rscript，暂不能做 R 语法检查；已完成 rubric 结构检查。",
        }
    with tempfile.NamedTemporaryFile("w", suffix=".R", encoding="utf-8", delete=False) as handle:
        handle.write(code)
        temp_path = handle.name
    try:
        result = subprocess.run(
            [rscript, "-e", f"parse(file={json.dumps(temp_path)})"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "R 语法检查超时。"}
    finally:
        try:
            Path(temp_path).unlink()
        except OSError:
            pass
    if result.returncode == 0:
        return {"ok": True, "message": "R 语法检查通过。"}
    message = (result.stderr or result.stdout or "R 语法检查失败。").strip()
    return {"ok": False, "message": _clean_snippet(message, max_length=300)}


def _clean_snippet(text: str, *, max_length: int) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= max_length:
        return cleaned
    return cleaned[: max_length - 3].rstrip() + "..."


def _quiz_error_html(message: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Quiz</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #17202a; }}
    main {{ max-width: 760px; margin: 64px auto; padding: 24px; background: #fff; border: 1px solid #dfe4ea; border-radius: 8px; }}
  </style>
</head>
<body><main>{html.escape(message)}</main></body>
</html>"""


def _quiz_page_html(attempt_id: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>可评分习题</title>
  <script src="https://cdn.jsdelivr.net/npm/monaco-editor@0.49.0/min/vs/loader.js"></script>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: #f6f7f9;
      color: #17202a;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
      line-height: 1.55;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 2;
      background: #ffffff;
      border-bottom: 1px solid #dfe4ea;
    }}
    .top {{
      max-width: 980px;
      margin: 0 auto;
      padding: 14px 18px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }}
    .brand-line {{
      color: #667085;
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 3px;
    }}
    h1 {{
      margin: 0;
      font-size: 20px;
      line-height: 1.25;
    }}
    .meta {{
      color: #667085;
      font-size: 13px;
      margin-top: 3px;
    }}
    main {{
      max-width: 980px;
      margin: 0 auto;
      padding: 22px 18px 56px;
    }}
    .question {{
      background: #ffffff;
      border: 1px solid #dfe4ea;
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 14px;
    }}
    .question-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
      margin-bottom: 10px;
    }}
    .question-title {{
      font-weight: 700;
      font-size: 15px;
    }}
    .pill {{
      border: 1px solid #d0d7de;
      border-radius: 999px;
      color: #57606a;
      background: #ffffff;
      padding: 2px 8px;
      white-space: nowrap;
      font-size: 12px;
    }}
    .prompt {{
      margin: 10px 0 12px;
      white-space: pre-wrap;
    }}
    .choices {{
      display: grid;
      gap: 8px;
      margin: 10px 0 12px;
    }}
    label.choice {{
      display: flex;
      align-items: flex-start;
      gap: 8px;
      padding: 9px 10px;
      border: 1px solid #d0d7de;
      border-radius: 6px;
      background: #ffffff;
      cursor: pointer;
    }}
    label.choice:has(input:checked) {{
      border-color: #1f6feb;
      background: #eaf2ff;
    }}
    input[type="text"],
    textarea {{
      width: 100%;
      border: 1px solid #ccd5df;
      border-radius: 6px;
      padding: 10px;
      font: inherit;
      background: #ffffff;
    }}
    textarea {{
      min-height: 108px;
      resize: vertical;
      font-family: inherit;
    }}
    textarea.code {{
      min-height: 180px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 13px;
      line-height: 1.45;
      tab-size: 4;
      white-space: pre;
      overflow-wrap: normal;
      overflow-x: auto;
    }}
    .code-shell {{
      border: 1px solid #30363d;
      border-radius: 8px;
      background: #1f1f1f;
      overflow: hidden;
      margin-top: 10px;
    }}
    .code-toolbar {{
      min-height: 34px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 0 10px;
      border-bottom: 1px solid #30363d;
      background: #252526;
      color: #c9d1d9;
      font-size: 12px;
    }}
    .code-language {{
      color: #9cdcfe;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    button.code-run {{
      height: 26px;
      border-color: #3c3c3c;
      background: #2d2d2d;
      color: #d4d4d4;
      font-size: 12px;
      padding: 0 9px;
    }}
    button.code-run:hover {{
      background: #37373d;
      border-color: #505050;
    }}
    .code-body {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      min-height: 220px;
    }}
    .line-gutter {{
      min-width: 46px;
      padding: 10px 8px 10px 0;
      text-align: right;
      color: #858585;
      background: #1f1f1f;
      border-right: 1px solid #2b2b2b;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 13px;
      line-height: 1.45;
      user-select: none;
      white-space: pre;
      overflow: hidden;
    }}
    .code-stack {{
      position: relative;
      min-width: 0;
      min-height: 220px;
      background: #1f1f1f;
    }}
    .monaco-mount {{
      display: none;
      width: 100%;
      min-height: 260px;
    }}
    .code-shell.monaco-ready .code-body {{
      display: block;
    }}
    .code-shell.monaco-ready .line-gutter,
    .code-shell.monaco-ready .code-highlight,
    .code-shell.monaco-ready textarea.code.editor {{
      display: none;
    }}
    .code-shell.monaco-ready .monaco-mount {{
      display: block;
    }}
    .code-highlight {{
      position: absolute;
      inset: 0;
      margin: 0;
      padding: 10px 12px;
      overflow: hidden;
      color: #d4d4d4;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 13px;
      line-height: 1.45;
      tab-size: 4;
      white-space: pre;
      pointer-events: none;
    }}
    .tok-keyword {{ color: #569cd6; }}
    .tok-function {{ color: #dcdcaa; }}
    .tok-string {{ color: #ce9178; }}
    .tok-comment {{ color: #6a9955; }}
    .tok-number {{ color: #b5cea8; }}
    .tok-operator {{ color: #c586c0; }}
    textarea.code.editor {{
      position: relative;
      z-index: 1;
      min-height: 220px;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: transparent;
      caret-color: #ffffff;
      padding: 10px 12px;
      resize: vertical;
      overflow: auto;
    }}
    textarea.code.editor::selection {{
      background: #264f78;
      color: transparent;
    }}
    textarea.code.editor::placeholder {{
      color: #858585;
      -webkit-text-fill-color: #858585;
    }}
    textarea.code.editor:focus {{
      box-shadow: none;
      border-color: transparent;
    }}
    .code-run-result {{
      display: none;
      border-top: 1px solid #30363d;
      padding: 10px;
      background: #252526;
      color: #d4d4d4;
      font-size: 13px;
    }}
    .code-run-result.visible {{ display: block; }}
    .code-run-result.ok {{ color: #89d185; }}
    .code-run-result.error {{ color: #f48771; }}
    .code-run-result.neutral {{ color: #dcdcaa; }}
    .run-output {{
      margin: 7px 0 0;
      padding: 8px;
      border-radius: 6px;
      background: #1f1f1f;
      color: #d4d4d4;
      overflow-x: auto;
      white-space: pre-wrap;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12px;
    }}
    input:focus,
    textarea:focus {{
      border-color: #1f6feb;
      box-shadow: 0 0 0 3px #1f6feb24;
      outline: none;
    }}
    button {{
      height: 36px;
      border: 1px solid #1f6feb;
      border-radius: 6px;
      padding: 0 14px;
      background: #1f6feb;
      color: #ffffff;
      cursor: pointer;
      font: inherit;
    }}
    button.secondary {{
      background: #ffffff;
      color: #17202a;
      border-color: #ccd5df;
    }}
    a.nav-button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 36px;
      border: 1px solid #ccd5df;
      border-radius: 6px;
      padding: 0 12px;
      background: #ffffff;
      color: #17202a;
      text-decoration: none;
      font-size: 14px;
      white-space: nowrap;
    }}
    .top-actions {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    button:disabled {{
      opacity: 0.6;
      cursor: wait;
    }}
    .actions {{
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      margin-top: 12px;
    }}
    .result {{
      display: none;
      margin-top: 12px;
      border-radius: 8px;
      padding: 12px;
      border: 1px solid #d0d7de;
      background: #f6f8fa;
    }}
    .result.visible {{ display: block; }}
    .result.correct {{
      border-color: #8ddb8c;
      background: #ecfdf0;
    }}
    .result.error {{
      border-color: #ffb4a8;
      background: #fff1f0;
    }}
    .result.partial {{
      border-color: #eac54f;
      background: #fff8c5;
    }}
    .status {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 2px 9px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 12px;
      margin-right: 8px;
    }}
    .status.correct {{ color: #1a7f37; background: #dafbe1; }}
    .status.error {{ color: #b42318; background: #ffe2df; }}
    .status.partial {{ color: #9a6700; background: #fff1b8; }}
    .score-line {{
      font-weight: 700;
      margin-bottom: 6px;
    }}
    .empty {{
      padding: 24px;
      text-align: center;
      color: #667085;
    }}
    @media (max-width: 680px) {{
      .top {{ align-items: flex-start; flex-direction: column; }}
      .top-actions {{ width: 100%; display: grid; grid-template-columns: 1fr; }}
      .question-head {{ flex-direction: column; }}
      .actions {{ flex-direction: column; }}
      button, a.nav-button {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="top">
      <div>
        <div class="brand-line">Teacher Agent · 可评分习题</div>
        <h1 id="title">可评分习题</h1>
        <div id="meta" class="meta">加载中</div>
      </div>
      <div class="top-actions">
        <a class="nav-button" href="/">返回会话</a>
        <button id="submitAll" type="button">上传并评分</button>
      </div>
    </div>
  </header>
  <main>
    <div id="questions" class="empty">正在加载题目...</div>
  </main>
  <script>
    const attemptId = {json.dumps(attempt_id)};
    let quiz = null;
    let latestResult = null;
    const monacoEditors = new Map();
    let monacoLoading = null;

    async function api(path, options = {{}}) {{
      const res = await fetch(path, {{
        headers: {{ "Content-Type": "application/json" }},
        ...options,
      }});
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }}

    function escapeHtml(value) {{
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }}

    function answerControl(question) {{
      if (question.question_type === "multiple_choice") {{
        return `<div class="choices">${{question.choices.map(choice => `
          <label class="choice">
            <input type="radio" name="${{question.id}}" value="${{escapeHtml(choice)}}" />
            <span>${{escapeHtml(choice)}}</span>
          </label>
        `).join("")}}</div>`;
      }}
      if (question.question_type === "calculation") {{
        return `<input type="text" data-answer-for="${{question.id}}" placeholder="输入计算结果" />`;
      }}
      if (question.question_type === "code") {{
        const language = question.topic.includes("R") ? "r" : question.topic.includes("Python") ? "python" : "code";
        return `
          <div class="code-shell" data-editor-shell="${{question.id}}">
            <div class="code-toolbar">
              <span class="code-language">${{language.toUpperCase()}}</span>
              <button class="code-run" type="button" onclick="runCode('${{question.id}}')">运行代码</button>
            </div>
            <div class="code-body">
              <div class="line-gutter" aria-hidden="true">1</div>
              <div class="code-stack">
                <pre class="code-highlight" aria-hidden="true"></pre>
                <textarea class="code editor" data-answer-for="${{question.id}}" data-answer-role="code" data-code-editor="1" data-language="${{language}}" spellcheck="false" autocapitalize="off" autocomplete="off" placeholder="在这里输入代码"></textarea>
                <div class="monaco-mount" data-monaco-for="${{question.id}}"></div>
              </div>
            </div>
            <div class="code-run-result" id="code-run-${{question.id}}"></div>
          </div>
        `;
      }}
      return `<textarea data-answer-for="${{question.id}}" placeholder="在这里输入答案"></textarea>`;
    }}

    function renderQuiz() {{
      document.getElementById("title").textContent = quiz.title;
      document.getElementById("meta").textContent =
        `attempt_id: ${{quiz.attempt_id}} · ${{quiz.questions.length}} 题 · ${{quiz.total_points}} 分`;
      document.getElementById("questions").className = "";
      document.getElementById("questions").innerHTML = quiz.questions.map((question, index) => `
        <section class="question" id="q-${{question.id}}">
          <div class="question-head">
            <div>
              <div class="question-title">${{index + 1}}. ${{escapeHtml(question.module)}} / ${{escapeHtml(question.topic)}}</div>
              <div class="meta">${{escapeHtml(question.question_type)}}</div>
            </div>
            <span class="pill">${{question.points}} 分</span>
          </div>
          <div class="prompt">${{escapeHtml(question.prompt)}}</div>
          ${{answerControl(question)}}
          <div class="actions">
            <button class="secondary" type="button" onclick="confirmOne('${{question.id}}')">提交本题</button>
          </div>
          <div class="result" id="result-${{question.id}}"></div>
        </section>
      `).join("");
      setupCodeEditors();
    }}

    function setupCodeEditors() {{
      document.querySelectorAll('textarea[data-code-editor="1"]').forEach(textarea => {{
        if (textarea.dataset.ready === "1") return;
        textarea.dataset.ready = "1";
        textarea.addEventListener("keydown", event => handleCodeKeydown(event, textarea));
        textarea.addEventListener("input", () => updateCodeEditor(textarea));
        textarea.addEventListener("scroll", () => syncEditorScroll(textarea));
        updateLineNumbers(textarea);
        updateCodeHighlight(textarea);
      }});
      setupMonacoEditors();
    }}

    function setupMonacoEditors() {{
      if (!window.require) return;
      if (!monacoLoading) {{
        window.require.config({{ paths: {{ vs: "https://cdn.jsdelivr.net/npm/monaco-editor@0.49.0/min/vs" }} }});
        monacoLoading = new Promise(resolve => {{
          window.require(["vs/editor/editor.main"], () => resolve(window.monaco));
        }});
      }}
      monacoLoading.then(monaco => {{
        registerMonacoLanguages(monaco);
        document.querySelectorAll('textarea[data-code-editor="1"]').forEach(textarea => {{
          const questionId = textarea.dataset.answerFor;
          if (!questionId || monacoEditors.has(questionId)) return;
          const shell = editorShell(textarea);
          const mount = shell ? shell.querySelector(`[data-monaco-for="${{CSS.escape(questionId)}}"]`) : null;
          if (!shell || !mount) return;
          shell.classList.add("monaco-ready");
          const language = textarea.dataset.language === "r" ? "r" : textarea.dataset.language === "python" ? "python" : "plaintext";
          const editor = monaco.editor.create(mount, {{
            value: textarea.value,
            language,
            theme: "vs-dark",
            fontSize: 14,
            lineHeight: 22,
            minimap: {{ enabled: false }},
            scrollBeyondLastLine: false,
            automaticLayout: true,
            tabSize: 4,
            insertSpaces: true,
            autoClosingBrackets: "always",
            autoClosingQuotes: "always",
            bracketPairColorization: {{ enabled: true }},
            guides: {{ bracketPairs: true, indentation: true }},
            suggestOnTriggerCharacters: true,
            quickSuggestions: true,
            wordBasedSuggestions: "off",
          }});
          editor.onDidChangeModelContent(() => {{
            textarea.value = editor.getValue();
            textarea.dispatchEvent(new Event("input", {{ bubbles: true }}));
          }});
          monacoEditors.set(questionId, editor);
        }});
      }}).catch(() => {{
        monacoLoading = null;
      }});
    }}

    function registerMonacoLanguages(monaco) {{
      if (window.__teacherAgentMonacoReady) return;
      window.__teacherAgentMonacoReady = true;
      monaco.languages.register({{ id: "r" }});
      monaco.languages.setMonarchTokensProvider("r", {{
        tokenizer: {{
          root: [
            [/#.*$/, "comment"],
            [/"([^"\\\\]|\\\\.)*$/, "string.invalid"],
            [/'([^'\\\\]|\\\\.)*$/, "string.invalid"],
            [/"/, "string", "@string_double"],
            [/'/, "string", "@string_single"],
            [/\\b(function|if|else|for|while|repeat|in|next|break|TRUE|FALSE|NULL|NA|NaN|Inf)\\b/, "keyword"],
            [/\\b(glm|summary|exp|coef|confint|data\\.frame|factor|as\\.numeric|print|c)\\b(?=\\s*\\()/, "predefined"],
            [/\\b\\d+(?:\\.\\d+)?\\b/, "number"],
            [/(<-|->|~|\\+|-|\\*|\\/|=)/, "operator"],
          ],
          string_double: [
            [/[^\\\\"]+/, "string"],
            [/\\\\./, "string.escape"],
            [/"/, "string", "@pop"],
          ],
          string_single: [
            [/[^\\\\']+/, "string"],
            [/\\\\./, "string.escape"],
            [/'/, "string", "@pop"],
          ],
        }},
      }});
      monaco.languages.registerCompletionItemProvider("r", {{
        provideCompletionItems: () => ({{
          suggestions: [
            {{
              label: "glm logistic",
              kind: monaco.languages.CompletionItemKind.Snippet,
              insertText: "glm(y ~ age + sex + exposure, data = df, family = binomial)",
              detail: "Logistic regression with glm",
            }},
            {{
              label: "summary",
              kind: monaco.languages.CompletionItemKind.Function,
              insertText: "summary(model)",
            }},
            {{
              label: "odds ratio",
              kind: monaco.languages.CompletionItemKind.Snippet,
              insertText: "exp(coef(model))",
            }},
          ],
        }}),
      }});
    }}

    function editorShell(textarea) {{
      return textarea.closest(".code-shell");
    }}

    function updateLineNumbers(textarea) {{
      const shell = editorShell(textarea);
      if (!shell) return;
      const gutter = shell.querySelector(".line-gutter");
      if (!gutter) return;
      const count = Math.max(1, textarea.value.split("\\n").length);
      gutter.textContent = Array.from({{ length: count }}, (_, index) => String(index + 1)).join("\\n");
      syncGutterScroll(textarea);
    }}

    function syncGutterScroll(textarea) {{
      const shell = editorShell(textarea);
      const gutter = shell ? shell.querySelector(".line-gutter") : null;
      if (gutter) gutter.scrollTop = textarea.scrollTop;
    }}

    function syncEditorScroll(textarea) {{
      syncGutterScroll(textarea);
      const shell = editorShell(textarea);
      const highlight = shell ? shell.querySelector(".code-highlight") : null;
      if (highlight) {{
        highlight.scrollTop = textarea.scrollTop;
        highlight.scrollLeft = textarea.scrollLeft;
      }}
    }}

    function updateCodeEditor(textarea) {{
      updateLineNumbers(textarea);
      updateCodeHighlight(textarea);
    }}

    function updateCodeHighlight(textarea) {{
      const shell = editorShell(textarea);
      const highlight = shell ? shell.querySelector(".code-highlight") : null;
      if (!highlight) return;
      const value = textarea.value || "";
      highlight.innerHTML = value ? highlightCode(value, textarea.dataset.language || "code") : "";
      syncEditorScroll(textarea);
    }}

    function highlightCode(value, language) {{
      return value.split("\\n").map(line => highlightLine(line, language)).join("\\n");
    }}

    function highlightLine(line, language) {{
      const commentIndex = findCommentIndex(line);
      const codePart = commentIndex >= 0 ? line.slice(0, commentIndex) : line;
      const commentPart = commentIndex >= 0 ? line.slice(commentIndex) : "";
      return highlightCodePart(codePart, language)
        + (commentPart ? `<span class="tok-comment">${{escapeHtml(commentPart)}}</span>` : "");
    }}

    function findCommentIndex(line) {{
      let quote = "";
      for (let index = 0; index < line.length; index += 1) {{
        const char = line[index];
        const previous = line[index - 1];
        if ((char === '"' || char === "'") && previous !== "\\\\") {{
          quote = quote === char ? "" : quote || char;
          continue;
        }}
        if (char === "#" && !quote) return index;
      }}
      return -1;
    }}

    function highlightCodePart(value, language) {{
      let output = "";
      let index = 0;
      while (index < value.length) {{
        const char = value[index];
        if (char === '"' || char === "'") {{
          const end = findStringEnd(value, index, char);
          output += `<span class="tok-string">${{escapeHtml(value.slice(index, end))}}</span>`;
          index = end;
          continue;
        }}
        let nextQuote = value.length;
        for (const quote of ['"', "'"]) {{
          const pos = value.indexOf(quote, index);
          if (pos >= 0) nextQuote = Math.min(nextQuote, pos);
        }}
        output += highlightPlainCode(value.slice(index, nextQuote), language);
        index = nextQuote;
      }}
      return output;
    }}

    function findStringEnd(value, start, quote) {{
      for (let index = start + 1; index < value.length; index += 1) {{
        if (value[index] === quote && value[index - 1] !== "\\\\") return index + 1;
      }}
      return value.length;
    }}

    function highlightPlainCode(value, language) {{
      const functions = language === "r"
        ? ["glm", "summary", "exp", "coef", "confint", "factor", "as.numeric"]
        : ["Logit", "fit", "summary", "add_constant", "DataFrame"];
      const keywords = language === "r"
        ? ["function", "if", "else", "for", "while", "in", "TRUE", "FALSE", "NULL", "family", "binomial", "data"]
        : ["import", "from", "as", "def", "return", "if", "else", "for", "while", "in", "True", "False", "None"];
      return escapeHtml(value)
        .replace(/(&lt;-|-&gt;|~|\\+|=)/g, '<span class="tok-operator">$1</span>')
        .replace(/\\b\\d+(?:\\.\\d+)?\\b/g, '<span class="tok-number">$&</span>')
        .replace(new RegExp(`\\\\b(${{functions.map(escapeRegex).join("|")}})(?=\\\\s*\\\\()`, "g"), '<span class="tok-function">$1</span>')
        .replace(new RegExp(`\\\\b(${{keywords.map(escapeRegex).join("|")}})\\\\b`, "g"), '<span class="tok-keyword">$1</span>');
    }}

    function escapeRegex(value) {{
      return value.replace(/[.*+?^${{}}()|[\\]\\\\]/g, "\\\\$&");
    }}

    function handleCodeKeydown(event, textarea) {{
      if (event.key === "Backspace" && deletePairAroundCursor(textarea)) {{
        event.preventDefault();
        return;
      }}
      if (jumpOverClosingPair(event, textarea)) {{
        event.preventDefault();
        return;
      }}
      if ((event.metaKey || event.ctrlKey) && event.key === "/") {{
        event.preventDefault();
        toggleLineComment(textarea);
        return;
      }}
      if (event.key === "Tab") {{
        event.preventDefault();
        if (event.shiftKey) {{
          unindentSelection(textarea);
          return;
        }}
        if (!completeSnippet(textarea)) indentSelection(textarea);
        return;
      }}
      if (event.key === "Enter") {{
        event.preventDefault();
        insertAutoIndent(textarea);
        return;
      }}
      if (!event.metaKey && !event.ctrlKey && !event.altKey && ["(", "[", "{{", String.fromCharCode(34), "'"].includes(event.key)) {{
        event.preventDefault();
        insertPair(textarea, event.key);
      }}
    }}

    function replaceRange(textarea, start, end, value, cursorOffset = value.length) {{
      const text = textarea.value;
      textarea.value = text.slice(0, start) + value + text.slice(end);
      const cursor = start + cursorOffset;
      textarea.selectionStart = cursor;
      textarea.selectionEnd = cursor;
      textarea.dispatchEvent(new Event("input", {{ bubbles: true }}));
      if (textarea.dataset.codeEditor === "1") updateCodeEditor(textarea);
    }}

    function currentLineBounds(textarea) {{
      const start = textarea.selectionStart;
      const text = textarea.value;
      const lineStart = text.lastIndexOf("\\n", start - 1) + 1;
      let lineEnd = text.indexOf("\\n", start);
      if (lineEnd < 0) lineEnd = text.length;
      return [lineStart, lineEnd];
    }}

    function selectedLineRange(textarea) {{
      const text = textarea.value;
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const lineStart = text.lastIndexOf("\\n", start - 1) + 1;
      let lineEnd = text.indexOf("\\n", end);
      if (lineEnd < 0) lineEnd = text.length;
      return [lineStart, lineEnd];
    }}

    function indentSelection(textarea) {{
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      if (start === end) {{
        replaceRange(textarea, start, end, "    ");
        return;
      }}
      const [lineStart, lineEnd] = selectedLineRange(textarea);
      const block = textarea.value.slice(lineStart, lineEnd);
      const indented = block.split("\\n").map(line => "    " + line).join("\\n");
      replaceRange(textarea, lineStart, lineEnd, indented, indented.length);
      textarea.selectionStart = lineStart;
      textarea.selectionEnd = lineStart + indented.length;
    }}

    function unindentSelection(textarea) {{
      const [lineStart, lineEnd] = selectedLineRange(textarea);
      const block = textarea.value.slice(lineStart, lineEnd);
      const unindented = block.split("\\n").map(line => line.replace(/^(    |\\t)/, "")).join("\\n");
      replaceRange(textarea, lineStart, lineEnd, unindented, unindented.length);
      textarea.selectionStart = lineStart;
      textarea.selectionEnd = lineStart + unindented.length;
    }}

    function insertAutoIndent(textarea) {{
      const [lineStart] = currentLineBounds(textarea);
      const beforeCursor = textarea.value.slice(lineStart, textarea.selectionStart);
      const baseIndent = beforeCursor.match(/^\\s*/)[0];
      const extraIndent = /(?:\\{{|\\(|\\[|:)\\s*$/.test(beforeCursor) ? "    " : "";
      replaceRange(textarea, textarea.selectionStart, textarea.selectionEnd, "\\n" + baseIndent + extraIndent);
    }}

    function insertPair(textarea, open) {{
      const close = matchingClose(open);
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const selected = textarea.value.slice(start, end);
      replaceRange(textarea, start, end, open + selected + close, 1 + selected.length);
    }}

    function matchingClose(open) {{
      const pairs = {{ "(": ")", "[": "]", "{{": "}}", [String.fromCharCode(34)]: String.fromCharCode(34), "'": "'" }};
      return pairs[open] || "";
    }}

    function deletePairAroundCursor(textarea) {{
      if (textarea.selectionStart !== textarea.selectionEnd) return false;
      const start = textarea.selectionStart;
      if (start <= 0 || start >= textarea.value.length) return false;
      const previous = textarea.value[start - 1];
      const next = textarea.value[start];
      if (matchingClose(previous) !== next) return false;
      replaceRange(textarea, start - 1, start + 1, "", 0);
      return true;
    }}

    function jumpOverClosingPair(event, textarea) {{
      if (event.metaKey || event.ctrlKey || event.altKey) return false;
      if (textarea.selectionStart !== textarea.selectionEnd) return false;
      const closers = [")", "]", "}}", String.fromCharCode(34), "'"];
      if (!closers.includes(event.key)) return false;
      const start = textarea.selectionStart;
      if (textarea.value[start] !== event.key) return false;
      textarea.selectionStart = start + 1;
      textarea.selectionEnd = start + 1;
      return true;
    }}

    function toggleLineComment(textarea) {{
      const [lineStart, lineEnd] = selectedLineRange(textarea);
      const block = textarea.value.slice(lineStart, lineEnd);
      const lines = block.split("\\n");
      const uncomment = lines.every(line => !line.trim() || /^\\s*#/.test(line));
      const updated = lines.map(line => {{
        if (!line.trim()) return line;
        return uncomment ? line.replace(/^(\\s*)#\\s?/, "$1") : line.replace(/^(\\s*)/, "$1# ");
      }}).join("\\n");
      replaceRange(textarea, lineStart, lineEnd, updated, updated.length);
      textarea.selectionStart = lineStart;
      textarea.selectionEnd = lineStart + updated.length;
    }}

    function completeSnippet(textarea) {{
      const start = textarea.selectionStart;
      const text = textarea.value;
      const prefixMatch = text.slice(0, start).match(/[A-Za-z_.]+$/);
      if (!prefixMatch) return false;
      const prefix = prefixMatch[0];
      if (prefix.length < 2) return false;
      const language = textarea.dataset.language || "code";
      const snippets = {{
        r: {{
          gl: "glm(y ~ age + sex + exposure, data = df, family = binomial)",
          glm: "glm(y ~ age + sex + exposure, data = df, family = binomial)",
          su: "summary(model)",
          exp: "exp(coef(model))",
          ci: "confint(model)"
        }},
        python: {{
          sm: "import statsmodels.api as sm",
          log: "model = sm.Logit(y, X).fit()",
          add: "X = sm.add_constant(df[[\\"age\\", \\"sex\\", \\"exposure\\"]])",
          sum: "result.summary()"
        }},
        code: {{}}
      }};
      const options = snippets[language] || snippets.code;
      const key = Object.keys(options).find(item => item.startsWith(prefix.toLowerCase()));
      if (!key) return false;
      const value = options[key];
      replaceRange(textarea, start - prefix.length, start, value);
      return true;
    }}

    function collectAnswers() {{
      const answers = {{}};
      quiz.questions.forEach(question => {{
        if (monacoEditors.has(question.id)) {{
          answers[question.id] = monacoEditors.get(question.id).getValue();
          return;
        }}
        if (question.question_type === "multiple_choice") {{
          const selected = document.querySelector(`input[name="${{CSS.escape(question.id)}}"]:checked`);
          answers[question.id] = selected ? selected.value : "";
          return;
        }}
        const input = document.querySelector(`[data-answer-for="${{CSS.escape(question.id)}}"]`);
        answers[question.id] = input ? input.value : "";
      }});
      return answers;
    }}

    function resultClass(detail) {{
      if (detail.score >= detail.max_score) return "correct";
      if (detail.score <= 0) return "error";
      return "partial";
    }}

    function resultLabel(detail) {{
      if (detail.score >= detail.max_score) return "CORRECT";
      if (detail.score <= 0) return "ERROR";
      return "PARTIAL";
    }}

    function renderResult(detail) {{
      const target = document.getElementById(`result-${{detail.question_id}}`);
      if (!target) return;
      const cls = resultClass(detail);
      target.className = `result visible ${{cls}}`;
      target.innerHTML = `
        <div class="score-line">
          <span class="status ${{cls}}">${{resultLabel(detail)}}</span>
          得分：${{detail.score}} / ${{detail.max_score}}
        </div>
        <div><strong>反馈：</strong>${{escapeHtml(detail.feedback)}}</div>
        <div><strong>题目解析：</strong>${{escapeHtml(detail.explanation || "暂无解析")}}</div>
      `;
    }}

    async function submitAnswers(questionId = "") {{
      const button = document.getElementById("submitAll");
      button.disabled = true;
      button.textContent = "评分中...";
      try {{
        const result = await api(`/api/quiz/attempts/${{encodeURIComponent(attemptId)}}/submit`, {{
          method: "POST",
          body: JSON.stringify({{ answers: collectAnswers() }}),
        }});
        latestResult = result;
        result.details.forEach(detail => {{
          if (!questionId || detail.question_id === questionId) renderResult(detail);
        }});
        document.getElementById("meta").textContent =
          `attempt_id: ${{quiz.attempt_id}} · 得分 ${{result.score}} / ${{result.total_points}} · ${{result.percentage}}%`;
      }} catch (error) {{
        alert(error.message);
      }} finally {{
        button.disabled = false;
        button.textContent = "上传并评分";
      }}
    }}

    window.confirmOne = questionId => submitAnswers(questionId);
    window.runCode = async questionId => {{
      const input = document.querySelector(`[data-answer-for="${{CSS.escape(questionId)}}"][data-answer-role="code"]`);
      const resultBox = document.getElementById(`code-run-${{questionId}}`);
      if (!input || !resultBox) return;
      const codeValue = monacoEditors.has(questionId) ? monacoEditors.get(questionId).getValue() : input.value;
      resultBox.className = "code-run-result visible neutral";
      resultBox.textContent = "正在运行代码...";
      try {{
        const result = await api("/api/quiz/code/run", {{
          method: "POST",
          body: JSON.stringify({{
            attempt_id: attemptId,
            question_id: questionId,
            code: codeValue
          }}),
        }});
        const cls = result.ok ? "ok" : "error";
        resultBox.className = `code-run-result visible ${{cls}}`;
        const stdout = result.stdout || "(无标准输出)";
        const stderr = result.stderr || "";
        resultBox.innerHTML = `
          <div><strong>运行状态：</strong>${{result.ok ? "通过" : "失败"}}，exit code: ${{result.exit_code ?? "timeout"}}</div>
          <div><strong>stdout</strong></div>
          <pre class="run-output">${{escapeHtml(stdout)}}</pre>
          ${{stderr ? `<div><strong>stderr</strong></div><pre class="run-output">${{escapeHtml(stderr)}}</pre>` : ""}}
        `;
      }} catch (error) {{
        resultBox.className = "code-run-result visible error";
        resultBox.textContent = error.message;
      }}
    }};
    document.getElementById("submitAll").onclick = () => submitAnswers("");

    api(`/api/quiz/attempts/${{encodeURIComponent(attemptId)}}`)
      .then(data => {{
        quiz = data;
        renderQuiz();
      }})
      .catch(error => {{
        document.getElementById("questions").textContent = error.message;
      }});
  </script>
</body>
</html>"""


def _practice_page_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>随机练习</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: #f6f7f9;
      color: #17202a;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
      line-height: 1.55;
    }
    header {
      background: #ffffff;
      border-bottom: 1px solid #dfe4ea;
    }
    .top {
      max-width: 980px;
      margin: 0 auto;
      padding: 14px 18px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .brand-line {
      color: #667085;
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 3px;
    }
    h1 {
      margin: 0;
      font-size: 20px;
      line-height: 1.25;
    }
    main {
      max-width: 980px;
      margin: 0 auto;
      padding: 22px 18px 56px;
    }
    .panel {
      background: #ffffff;
      border: 1px solid #dfe4ea;
      border-radius: 8px;
      padding: 16px;
    }
    .filters {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .field {
      display: grid;
      gap: 5px;
    }
    label {
      color: #667085;
      font-size: 12px;
      font-weight: 700;
    }
    select,
    input {
      height: 38px;
      border: 1px solid #ccd5df;
      border-radius: 6px;
      background: #ffffff;
      padding: 0 10px;
      font: inherit;
      min-width: 0;
    }
    select:focus,
    input:focus {
      border-color: #1f6feb;
      box-shadow: 0 0 0 3px #1f6feb24;
      outline: none;
    }
    .actions {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      margin-top: 16px;
    }
    button,
    a.nav-button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 38px;
      border: 1px solid #ccd5df;
      border-radius: 6px;
      padding: 0 14px;
      background: #ffffff;
      color: #17202a;
      text-decoration: none;
      font: inherit;
      cursor: pointer;
      white-space: nowrap;
    }
    button.primary {
      background: #1f6feb;
      border-color: #1f6feb;
      color: #ffffff;
    }
    button:disabled {
      opacity: 0.6;
      cursor: wait;
    }
    .message {
      margin-top: 14px;
      border-radius: 8px;
      padding: 12px;
      border: 1px solid #d0d7de;
      background: #f6f8fa;
      color: #57606a;
      display: none;
    }
    .message.visible { display: block; }
    .message.error {
      color: #b42318;
      border-color: #ffb4a8;
      background: #fff1f0;
    }
    @media (max-width: 720px) {
      .top { align-items: flex-start; flex-direction: column; }
      .filters { grid-template-columns: 1fr; }
      .actions { flex-direction: column; }
      button, a.nav-button { width: 100%; }
    }
  </style>
</head>
<body>
  <header>
    <div class="top">
      <div>
        <div class="brand-line">Teacher Agent · 学生刷题</div>
        <h1>随机练习</h1>
      </div>
      <a class="nav-button" href="/">返回会话</a>
    </div>
  </header>
  <main>
    <section class="panel">
      <div class="filters">
        <div class="field">
          <label for="studentId">学生 ID</label>
          <select id="studentId">
            <option value="student_demo">student_demo · 学生样例</option>
            <option value="student_or">student_or · OR 薄弱</option>
            <option value="student_fdr">student_fdr · FDR 薄弱</option>
          </select>
        </div>
        <div class="field">
          <label for="module">模块</label>
          <select id="module">
            <option value="">综合随机</option>
            <option value="多重检验校正">多重检验校正</option>
            <option value="Logistic 回归">Logistic 回归</option>
            <option value="研究设计与 t 检验">研究设计与 t 检验</option>
            <option value="卡方检验">卡方检验</option>
            <option value="遗传学统计">遗传学统计</option>
          </select>
        </div>
        <div class="field">
          <label for="questionType">题型</label>
          <select id="questionType">
            <option value="">不限题型</option>
            <option value="multiple_choice">选择题</option>
            <option value="short_answer">问答题</option>
            <option value="calculation">计算题</option>
            <option value="code">代码题</option>
          </select>
        </div>
        <div class="field">
          <label for="count">题目数量</label>
          <input id="count" type="number" min="1" max="20" value="5" />
        </div>
      </div>
      <div class="actions">
        <button id="startPractice" class="primary" type="button">生成随机习题</button>
      </div>
      <div id="message" class="message"></div>
    </section>
  </main>
  <script>
    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }

    function showMessage(text, isError = false) {
      const message = document.getElementById("message");
      message.textContent = text;
      message.className = "message visible" + (isError ? " error" : "");
    }

    document.getElementById("startPractice").onclick = async () => {
      const button = document.getElementById("startPractice");
      button.disabled = true;
      button.textContent = "生成中...";
      try {
        const payload = {
          mode: "student",
          user_role: "student",
          user_id: document.getElementById("studentId").value,
          module: document.getElementById("module").value,
          question_type: document.getElementById("questionType").value,
          count: Number(document.getElementById("count").value || 5),
          use_case: "practice"
        };
        const result = await api("/api/quiz/start", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        showMessage(`已生成：${result.title}，正在进入答题页...`);
        window.location.href = result.quiz_url;
      } catch (error) {
        showMessage(error.message, true);
      } finally {
        button.disabled = false;
        button.textContent = "生成随机习题";
      }
    };
  </script>
</body>
</html>"""


def _is_allowed_artifact_path(path: Path, user_role: str, user_id: str) -> bool:
    try:
        resolved = path.resolve()
        cwd = Path.cwd().resolve()
        output_dir = (cwd / "outputs").resolve()
        pdf_dir = (cwd / "data" / "pdfs").resolve()
    except OSError:
        return False

    if not (resolved.is_relative_to(output_dir) or resolved.is_relative_to(pdf_dir)):
        return False
    if user_role == "teacher":
        return True
    return user_id in path.name and "试卷" not in path.name and "考试" not in path.name


def _markdown_preview_html(title: str, markdown: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <script>
    window.MathJax = {{
      tex: {{
        inlineMath: [["\\\\(", "\\\\)"], ["$", "$"]],
        displayMath: [["\\\\[", "\\\\]"], ["$$", "$$"]],
        processEscapes: true
      }}
    }};
  </script>
  <script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"></script>
  <style>
    body {{
      margin: 0;
      background: #f6f7f9;
      color: #17202a;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.6;
    }}
    main {{
      max-width: 920px;
      margin: 0 auto;
      padding: 28px 22px 56px;
      background: #fff;
      min-height: 100vh;
      border-left: 1px solid #dfe4ea;
      border-right: 1px solid #dfe4ea;
    }}
    h1, h2, h3, h4, h5, h6 {{ line-height: 1.25; margin: 18px 0 10px; }}
    h1 {{ font-size: 28px; }}
    h2 {{ font-size: 22px; }}
    h3 {{ font-size: 18px; }}
    h4, h5, h6 {{ font-size: 16px; }}
    p {{ margin: 0 0 10px; }}
    ul, ol {{ margin: 0 0 12px 24px; padding: 0; }}
    blockquote {{
      margin: 12px 0;
      padding-left: 12px;
      border-left: 3px solid #d0d7de;
      color: #57606a;
    }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0 16px; }}
    th, td {{ border: 1px solid #d0d7de; padding: 6px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f6f8fa; }}
    code {{ background: #f6f8fa; border-radius: 4px; padding: 1px 4px; }}
    pre {{ overflow-x: auto; background: #f6f8fa; padding: 12px; border-radius: 6px; }}
    pre code {{ background: transparent; padding: 0; }}
    hr {{ border: 0; border-top: 1px solid #d0d7de; margin: 18px 0; }}
    .math-block {{ overflow-x: auto; margin: 12px 0; }}
    .formula {{
      display: block;
      width: fit-content;
      max-width: 100%;
      margin: 8px 0;
      padding: 8px 10px;
      border-radius: 6px;
      background: #f6f8fa;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      white-space: nowrap;
      overflow-x: auto;
    }}
    .formula.inline {{
      display: inline-flex;
      vertical-align: middle;
      margin: 0 2px;
      padding: 1px 4px;
      white-space: normal;
    }}
    .frac {{
      display: inline-flex;
      flex-direction: column;
      align-items: center;
      vertical-align: middle;
      line-height: 1.15;
      margin: 0 2px;
    }}
    .frac .num {{
      border-bottom: 1px solid currentColor;
      padding: 0 4px 2px;
    }}
    .frac .den {{
      padding: 2px 4px 0;
    }}
    .formula sub,
    .formula sup {{
      font-size: 0.75em;
      line-height: 0;
    }}
  </style>
</head>
<body>
  <main>
    {_render_markdown_html(markdown)}
  </main>
</body>
</html>"""


def _render_markdown_html(markdown: str) -> str:
    markdown = _normalize_markdown_math(markdown)
    lines = markdown.splitlines()
    rendered: list[str] = []
    paragraph: list[str] = []
    list_type: str | None = None
    in_code = False
    code_lines: list[str] = []
    in_math = False
    math_end = ""
    math_lines: list[str] = []

    def close_paragraph() -> None:
        if paragraph:
            rendered.append(f"<p>{_render_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_type
        if list_type:
            rendered.append(f"</{list_type}>")
            list_type = None

    def close_blocks() -> None:
        close_paragraph()
        close_list()

    def flush_code_block() -> None:
        body = chr(10).join(code_lines)
        if _looks_like_formula_block(body):
            rendered.append(f"<div class=\"math-block\">\\[{html.escape(_plain_formula_to_latex(body))}\\]</div>")
        else:
            rendered.append(f"<pre><code>{html.escape(body)}</code></pre>")

    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()

        if in_code:
            if stripped.startswith("```"):
                flush_code_block()
                code_lines = []
                in_code = False
            else:
                code_lines.append(raw)
            i += 1
            continue

        if in_math:
            if stripped == math_end:
                rendered.append(
                    f"<div class=\"math-block\">\\[{html.escape(chr(10).join(math_lines))}\\]</div>"
                )
                math_lines = []
                in_math = False
                math_end = ""
            else:
                math_lines.append(raw)
            i += 1
            continue

        if not stripped:
            close_blocks()
            i += 1
            continue

        if stripped.startswith("```"):
            close_blocks()
            in_code = True
            code_lines = []
            i += 1
            continue

        if stripped in {"\\[", "$$"}:
            close_blocks()
            in_math = True
            math_end = "\\]" if stripped == "\\[" else "$$"
            math_lines = []
            i += 1
            continue

        single_math = re.match(r"^(?:\\\[(.+)\\\]|\$\$(.+)\$\$)$", stripped)
        if single_math:
            close_blocks()
            rendered.append(
                f"<div class=\"math-block\">\\[{html.escape(single_math.group(1) or single_math.group(2))}\\]</div>"
            )
            i += 1
            continue

        if re.fullmatch(r"---+", stripped):
            close_blocks()
            rendered.append("<hr>")
            i += 1
            continue

        if _is_markdown_table(lines, i):
            close_blocks()
            table_rows = [lines[i]]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_rows.append(lines[i])
                i += 1
            rendered.append(_render_table_html(table_rows))
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            close_blocks()
            level = len(heading.group(1))
            rendered.append(f"<h{level}>{_render_inline(heading.group(2))}</h{level}>")
            i += 1
            continue

        unordered = re.match(r"^\s*[-*]\s+(.+)$", line)
        if unordered:
            close_paragraph()
            if list_type != "ul":
                close_list()
                list_type = "ul"
                rendered.append("<ul>")
            rendered.append(f"<li>{_render_inline(unordered.group(1))}</li>")
            i += 1
            continue

        ordered = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if ordered:
            close_paragraph()
            if list_type != "ol":
                close_list()
                list_type = "ol"
                rendered.append("<ol>")
            rendered.append(f"<li>{_render_inline(ordered.group(1))}</li>")
            i += 1
            continue

        quote = re.match(r"^>\s*(.+)$", line)
        if quote:
            close_blocks()
            rendered.append(f"<blockquote>{_render_inline(quote.group(1))}</blockquote>")
            i += 1
            continue

        close_list()
        paragraph.append(line)
        i += 1

    if in_code:
        flush_code_block()
    if in_math:
        rendered.append(f"<div class=\"math-block\">\\[{html.escape(chr(10).join(math_lines))}\\]</div>")
    close_blocks()
    return "\n".join(rendered)


def _normalize_markdown_math(markdown: str) -> str:
    def code_replacer(match: re.Match[str]) -> str:
        body = match.group(1).strip()
        if _looks_like_formula_block(body):
            return f"$$\n{_plain_formula_to_latex(body)}\n$$"
        return match.group(0)

    normalized = re.sub(
        r"```(?:text|math|latex)?\s*\n(.*?)\n```",
        code_replacer,
        markdown,
        flags=re.DOTALL,
    )

    parts = re.split(r"(\$\$.*?\$\$|\$.*?\$|`[^`]*`)", normalized, flags=re.DOTALL)
    output: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            output.append(part)
            continue
        part = re.sub(
            r"(?m)^(\s*)(?:chi|χ)\s*(?:\^?2|²)\s*=\s*sum\s+(.+)$",
            lambda match: f"{match.group(1)}$$\\chi^2 = \\sum {match.group(2).strip()}$$",
            part,
            flags=re.IGNORECASE,
        )
        part = re.sub(r"χ²", r"$\\chi^2$", part)
        part = re.sub(r"\bα\s*=\s*([0-9.]+)", r"$\\alpha = \1$", part)
        part = re.sub(r"\bp\s*([<>=]+)\s*([0-9.]+)", r"$p \1 \2$", part)
        output.append(part)
    return "".join(output)


def _looks_like_formula_block(value: str) -> bool:
    compact = value.strip()
    if not compact:
        return False
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


def _render_inline(value: str) -> str:
    escaped = html.escape(value)
    escaped = re.sub(
        r"\\\((.+?)\\\)",
        lambda match: f"<span class=\"math-inline\">\\({match.group(1)}\\)</span>",
        escaped,
    )
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda match: _render_link_html(match.group(1), match.group(2)),
        escaped,
    )
    return escaped


def _render_link_html(label: str, href: str) -> str:
    if href.startswith(("http://", "https://")):
        return f'<a href="{href}" target="_blank" rel="noopener">{label}</a>'
    return f'<a href="{href}">{label}</a>'


def _render_formula(value: str) -> str:
    return _render_formula_segment(_normalize_formula(value.strip()))


def _normalize_formula(value: str) -> str:
    replacements = {
        "\\left": "",
        "\\right": "",
        "\\mu": "μ",
        "\\alpha": "α",
        "\\neq": "≠",
        "\\leq": "≤",
        "\\geq": "≥",
        "\\times": "×",
        "\\cdot": "·",
    }
    result = value
    for old, new in replacements.items():
        result = result.replace(old, new)
    return re.sub(r"\s+", " ", result)


def _render_formula_segment(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value.startswith("\\frac", index):
            frac = _parse_frac(value, index + len("\\frac"))
            if frac:
                numerator, denominator, next_index = frac
                output.append(
                    '<span class="frac">'
                    f'<span class="num">{_render_formula_segment(numerator)}</span>'
                    f'<span class="den">{_render_formula_segment(denominator)}</span>'
                    "</span>"
                )
                index = next_index
                continue
        if value.startswith("\\text", index):
            group = _parse_group(value, index + len("\\text"))
            if group:
                text, next_index = group
                output.append(html.escape(text))
                index = next_index
                continue
        char = value[index]
        if char in {"_", "^"}:
            group = _parse_group(value, index + 1)
            tag = "sub" if char == "_" else "sup"
            if group:
                text, next_index = group
                output.append(f"<{tag}>{_render_formula_segment(text)}</{tag}>")
                index = next_index
                continue
            if index + 1 < len(value):
                output.append(f"<{tag}>{html.escape(value[index + 1])}</{tag}>")
                index += 2
                continue
        if char == "\\":
            index += 1
            continue
        output.append(html.escape(char))
        index += 1
    return "".join(output)


def _parse_frac(value: str, index: int) -> tuple[str, str, int] | None:
    first = _parse_group(value, index)
    if not first:
        return None
    numerator, next_index = first
    second = _parse_group(value, next_index)
    if not second:
        return None
    denominator, final_index = second
    return numerator, denominator, final_index


def _parse_group(value: str, index: int) -> tuple[str, int] | None:
    while index < len(value) and value[index].isspace():
        index += 1
    if index >= len(value) or value[index] != "{":
        return None
    depth = 0
    start = index + 1
    for pos in range(index, len(value)):
        if value[pos] == "{":
            depth += 1
        elif value[pos] == "}":
            depth -= 1
            if depth == 0:
                return value[start:pos], pos + 1
    return None


def _is_markdown_table(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    return lines[index].strip().startswith("|") and re.match(
        r"^\s*\|?[\s:|-]+\|[\s:|-]*$",
        lines[index + 1],
    )


def _render_table_html(rows: list[str]) -> str:
    parsed = [
        [cell.strip() for cell in row.strip().strip("|").split("|")]
        for row in rows
    ]
    header = parsed[0] if parsed else []
    body = parsed[1:]
    head_html = "".join(f"<th>{_render_inline(cell)}</th>" for cell in header)
    body_html = "".join(
        "<tr>" + "".join(f"<td>{_render_inline(cell)}</td>" for cell in row) + "</tr>"
        for row in body
    )
    return f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>"


INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Teacher Agent</title>
  <script>
    window.MathJax = {
      tex: {
        inlineMath: [["\\(", "\\)"], ["$", "$"]],
        displayMath: [["\\[", "\\]"], ["$$", "$$"]],
        processEscapes: true
      },
      options: {
        skipHtmlTags: ["script", "noscript", "style", "textarea", "pre", "code"]
      },
      startup: {
        typeset: false
      }
    };
  </script>
  <script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"></script>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f6f7f9;
      color: #17202a;
      letter-spacing: 0;
      overflow: hidden;
    }
    .app {
      display: grid;
      grid-template-columns: 340px minmax(0, 1fr);
      height: 100vh;
      min-height: 0;
      overflow: hidden;
    }
    .sidebar {
      border-right: 1px solid #dfe4ea;
      background: #ffffff;
      display: flex;
      flex-direction: column;
      min-width: 0;
      min-height: 0;
      overflow: hidden;
    }
    .sidebar-header {
      padding: 14px;
      display: flex;
      align-items: stretch;
      flex-direction: column;
      gap: 10px;
      border-bottom: 1px solid #edf0f3;
      flex: 0 0 auto;
    }
    .brand-row {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .brand {
      font-weight: 700;
      font-size: 16px;
      flex: 1;
    }
    .subtitle {
      color: #667085;
      font-size: 12px;
      margin-top: 2px;
    }
    button {
      border: 1px solid #ccd5df;
      background: #ffffff;
      color: #17202a;
      border-radius: 6px;
      height: 34px;
      padding: 0 12px;
      cursor: pointer;
      font-size: 14px;
      transition: background 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
    }
    button:hover {
      background: #f6f8fa;
      border-color: #aebdcc;
    }
    button.primary {
      background: #1f6feb;
      border-color: #1f6feb;
      color: #ffffff;
    }
    button.primary:hover {
      background: #185abc;
      border-color: #185abc;
    }
    a.nav-link-button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 34px;
      border: 1px solid #ccd5df;
      background: #ffffff;
      color: #17202a;
      border-radius: 6px;
      padding: 0 12px;
      cursor: pointer;
      font-size: 14px;
      text-decoration: none;
      white-space: nowrap;
    }
    a.nav-link-button:hover {
      background: #f6f8fa;
      border-color: #aebdcc;
    }
    a.nav-link-button.practice-entry {
      width: 100%;
      background: #eef6ff;
      border-color: #b8d7ff;
      color: #0f4fb5;
      font-weight: 700;
    }
    button.danger {
      color: #b42318;
      border-color: #f3b8b2;
    }
    button.danger:hover {
      background: #fff1f0;
      border-color: #e9857d;
    }
    select,
    input[type="text"] {
      height: 34px;
      border: 1px solid #ccd5df;
      border-radius: 6px;
      background: #ffffff;
      padding: 0 8px;
      font: inherit;
      min-width: 0;
    }
    select:focus,
    input[type="text"]:focus {
      border-color: #1f6feb;
      box-shadow: 0 0 0 3px #1f6feb24;
      outline: none;
    }
    .identity-panel {
      display: grid;
      gap: 8px;
      padding: 10px;
      border: 1px solid #edf0f3;
      border-radius: 8px;
      background: #f8fafc;
    }
    .field {
      display: grid;
      gap: 4px;
    }
    .field label {
      color: #667085;
      font-size: 12px;
      font-weight: 600;
    }
    .field-row {
      display: grid;
      grid-template-columns: 92px minmax(0, 1fr);
      gap: 8px;
    }
    .session-actions {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
    }
    .sessions {
      flex: 1 1 auto;
      min-height: 0;
      overflow-y: auto;
      padding: 8px;
      overscroll-behavior: contain;
    }
    .session {
      width: 100%;
      border: 1px solid transparent;
      text-align: left;
      background: transparent;
      height: auto;
      padding: 10px;
      border-radius: 6px;
      display: block;
    }
    .session:hover {
      background: #f6f8fa;
    }
    .session.active {
      background: #eaf2ff;
      border-color: #b8d7ff;
      color: #0f4fb5;
    }
    .session-title {
      font-weight: 600;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .session-time {
      font-size: 12px;
      color: #667085;
      margin-top: 4px;
    }
    .session-meta {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 6px;
    }
    .pill {
      border: 1px solid #d0d7de;
      border-radius: 999px;
      padding: 1px 7px;
      color: #57606a;
      background: #ffffff;
      font-size: 11px;
    }
    .main {
      display: grid;
      grid-template-rows: 64px minmax(0, 1fr) auto;
      min-width: 0;
      min-height: 0;
      height: 100vh;
      overflow: hidden;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 18px;
      border-bottom: 1px solid #dfe4ea;
      background: #ffffff;
      gap: 12px;
      min-width: 0;
      overflow: hidden;
    }
    .title {
      font-weight: 700;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .title-wrap {
      min-width: 0;
    }
    .current-meta {
      color: #667085;
      font-size: 12px;
      margin-top: 3px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .top-actions {
      display: flex;
      align-items: center;
      gap: 10px;
      flex: 1 1 auto;
      justify-content: flex-end;
      min-width: 0;
    }
    .rename-input {
      flex: 0 1 260px;
      width: min(260px, 28vw);
    }
    .status {
      min-width: 90px;
      border: 1px solid #d0d7de;
      border-radius: 999px;
      padding: 4px 9px;
      color: #57606a;
      font-size: 12px;
      text-align: center;
      white-space: nowrap;
    }
    .status.connected {
      color: #1a7f37;
      border-color: #aceebb;
      background: #dafbe1;
    }
    .status.required {
      color: #9a6700;
      border-color: #eac54f;
      background: #fff8c5;
    }
    .messages {
      min-height: 0;
      overflow-y: auto;
      padding: 22px;
      overscroll-behavior: contain;
    }
    .message {
      max-width: 880px;
      margin: 0 auto 14px auto;
      display: flex;
    }
    .bubble {
      border: 1px solid #dfe4ea;
      background: #ffffff;
      border-radius: 8px;
      padding: 12px 14px;
      line-height: 1.55;
      overflow-wrap: anywhere;
      width: fit-content;
      max-width: 100%;
    }
    .bubble.markdown p {
      margin: 0 0 8px;
    }
    .bubble.markdown p:last-child,
    .bubble.markdown ul:last-child,
    .bubble.markdown ol:last-child,
    .bubble.markdown table:last-child {
      margin-bottom: 0;
    }
    .bubble.markdown h1,
    .bubble.markdown h2,
    .bubble.markdown h3,
    .bubble.markdown h4,
    .bubble.markdown h5,
    .bubble.markdown h6 {
      margin: 10px 0 8px;
      line-height: 1.25;
    }
    .bubble.markdown h1 { font-size: 20px; }
    .bubble.markdown h2 { font-size: 17px; }
    .bubble.markdown h3 { font-size: 15px; }
    .bubble.markdown h4,
    .bubble.markdown h5,
    .bubble.markdown h6 { font-size: 14px; }
    .bubble.markdown hr {
      border: 0;
      border-top: 1px solid #d0d7de;
      margin: 12px 0;
    }
    .bubble.markdown .math-block {
      overflow-x: auto;
      margin: 10px 0;
      padding: 4px 0;
    }
    .formula {
      display: block;
      width: fit-content;
      max-width: 100%;
      margin: 8px 0;
      padding: 8px 10px;
      border-radius: 6px;
      background: #f6f8fa;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      white-space: nowrap;
      overflow-x: auto;
    }
    .formula.inline {
      display: inline-flex;
      vertical-align: middle;
      margin: 0 2px;
      padding: 1px 4px;
      white-space: normal;
    }
    .frac {
      display: inline-flex;
      flex-direction: column;
      align-items: center;
      vertical-align: middle;
      line-height: 1.15;
      margin: 0 2px;
    }
    .frac .num {
      border-bottom: 1px solid currentColor;
      padding: 0 4px 2px;
    }
    .frac .den {
      padding: 2px 4px 0;
    }
    .formula sub,
    .formula sup {
      font-size: 0.75em;
      line-height: 0;
    }
    .bubble.markdown ul,
    .bubble.markdown ol {
      margin: 0 0 10px 20px;
      padding: 0;
    }
    .bubble.markdown blockquote {
      margin: 8px 0;
      padding-left: 10px;
      border-left: 3px solid #d0d7de;
      color: #57606a;
    }
    .bubble.markdown code {
      background: #f6f8fa;
      border-radius: 4px;
      padding: 1px 4px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.92em;
    }
    .bubble.markdown pre {
      margin: 8px 0;
      padding: 10px;
      overflow-x: auto;
      border-radius: 6px;
      background: #f6f8fa;
    }
    .bubble.markdown pre code {
      padding: 0;
      background: transparent;
    }
    .bubble.markdown table {
      border-collapse: collapse;
      margin: 8px 0 12px;
      width: 100%;
      font-size: 14px;
    }
    .bubble.markdown th,
    .bubble.markdown td {
      border: 1px solid #d0d7de;
      padding: 5px 7px;
      text-align: left;
      vertical-align: top;
    }
    .bubble.markdown th {
      background: #f6f8fa;
      font-weight: 700;
    }
    .message.user {
      justify-content: flex-end;
    }
    .message.user .bubble {
      background: #1f6feb;
      color: #ffffff;
      border-color: #1f6feb;
    }
    .composer {
      padding: 14px 18px;
      border-top: 1px solid #dfe4ea;
      background: #ffffff;
      flex: 0 0 auto;
    }
    .composer-inner {
      max-width: 920px;
      margin: 0 auto;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: end;
    }
    textarea {
      resize: none;
      min-height: 52px;
      max-height: 160px;
      border: 1px solid #ccd5df;
      border-radius: 8px;
      padding: 12px;
      font: inherit;
      line-height: 1.4;
      outline: none;
    }
    textarea:focus {
      border-color: #1f6feb;
      box-shadow: 0 0 0 3px #1f6feb24;
    }
    .empty {
      color: #667085;
      text-align: center;
      margin-top: 15vh;
    }
    @media (max-width: 760px) {
      .app { grid-template-columns: 1fr; }
      .sidebar { display: none; }
      .topbar { height: auto; padding: 10px 12px; align-items: flex-start; }
      .top-actions { flex-wrap: wrap; justify-content: flex-end; }
      .rename-input { width: 100%; }
      .composer-inner { grid-template-columns: 1fr; }
      button.primary { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="sidebar-header">
        <div class="brand-row">
          <div>
            <div class="brand">Teacher Agent</div>
            <div class="subtitle">生物统计学教学工作台</div>
          </div>
        </div>
        <div class="identity-panel">
          <div class="field-row">
            <div class="field">
              <label for="roleSelect">角色</label>
              <select id="roleSelect" aria-label="角色">
                <option value="teacher">老师</option>
                <option value="student">学生</option>
              </select>
            </div>
            <div class="field">
              <label for="userSelect">用户 ID</label>
              <select id="userSelect" aria-label="用户 ID"></select>
            </div>
          </div>
          <div class="field">
            <label for="sessionTitleInput">会话名称</label>
            <input id="sessionTitleInput" type="text" placeholder="例如：小课课后辅导" />
          </div>
          <div class="session-actions">
            <button id="newSession" class="primary">新建会话</button>
            <button id="resetComposer" type="button">清空</button>
          </div>
          <a class="nav-link-button practice-entry" href="/practice">学生刷题 / 随机练习</a>
        </div>
      </div>
      <div id="sessions" class="sessions"></div>
    </aside>
    <main class="main">
      <div class="topbar">
        <div class="title-wrap">
          <div id="currentTitle" class="title">会话</div>
          <div id="currentMeta" class="current-meta">未选择会话</div>
        </div>
        <div class="top-actions">
          <input id="renameInput" class="rename-input" type="text" placeholder="重命名当前会话" />
          <button id="renameSession">保存</button>
          <span id="difyStatus" class="status">Dify 检查中</span>
          <button id="deleteSession" class="danger">删除</button>
        </div>
      </div>
      <div id="messages" class="messages"></div>
      <form id="composer" class="composer">
        <div class="composer-inner">
          <textarea id="messageInput" placeholder="输入中文命令" rows="2"></textarea>
          <button class="primary" type="submit">发送</button>
        </div>
      </form>
    </main>
  </div>
  <script>
    let sessions = [];
    let activeSessionId = null;

    const sessionsEl = document.getElementById("sessions");
    const messagesEl = document.getElementById("messages");
    const currentTitleEl = document.getElementById("currentTitle");
    const currentMetaEl = document.getElementById("currentMeta");
    const inputEl = document.getElementById("messageInput");
    const difyStatusEl = document.getElementById("difyStatus");
    const roleSelectEl = document.getElementById("roleSelect");
    const userSelectEl = document.getElementById("userSelect");
    const sessionTitleInputEl = document.getElementById("sessionTitleInput");
    const renameInputEl = document.getElementById("renameInput");

    const userOptions = {
      teacher: [
        { id: "teacher_demo", label: "teacher_demo · 任课老师" },
        { id: "teacher_e2e", label: "teacher_e2e · 测试老师" },
      ],
      student: [
        { id: "student_demo", label: "student_demo · 李同学" },
        { id: "student_or", label: "student_or · OR 薄弱" },
        { id: "student_fdr", label: "student_fdr · FDR 薄弱" },
      ],
    };

    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      if (!res.ok) throw new Error(await res.text());
      return res.json();
    }

    async function loadSessions() {
      updateUserOptions();
      await loadStatus();
      sessions = await api("/api/sessions");
      if (!sessions.length) {
        const created = await api("/api/sessions", {
          method: "POST",
          body: JSON.stringify({
            title: "老师会话",
            user_role: "teacher",
            user_id: "teacher_demo",
          }),
        });
        sessions = [created];
      }
      if (!activeSessionId || !sessions.some(s => s.id === activeSessionId)) {
        activeSessionId = sessions[0].id;
      }
      renderSessions();
      await loadMessages();
    }

    async function loadStatus() {
      const status = await api("/api/status");
      difyStatusEl.classList.remove("connected", "required");
      if (status.require_dify) {
        if (status.chatflow_configured) {
          difyStatusEl.textContent = status.yml_llm_configured ? "YML+LLM 优先" : "Dify 已连接";
        } else if (status.yml_llm_configured) {
          difyStatusEl.textContent = "YML+LLM";
        } else {
          difyStatusEl.textContent = "AI 未配置";
        }
        difyStatusEl.classList.add("required");
        return;
      }
      if (status.chatflow_configured) {
        difyStatusEl.textContent = "Dify 已连接";
        difyStatusEl.classList.add("connected");
      } else if (status.yml_llm_configured) {
        difyStatusEl.textContent = "YML+LLM";
        difyStatusEl.classList.add("connected");
      } else {
        difyStatusEl.textContent = "本地兜底";
      }
    }

    function renderSessions() {
      sessionsEl.innerHTML = "";
      sessions.forEach(session => {
        const button = document.createElement("button");
        button.className = "session" + (session.id === activeSessionId ? " active" : "");
        button.innerHTML = `
          <div class="session-title">${escapeHtml(session.title)}</div>
          <div class="session-time">${formatTime(session.updated_at)}</div>
          <div class="session-meta">
            <span class="pill">${roleLabel(session.user_role)}</span>
            <span class="pill">${escapeHtml(session.user_id || defaultUserId(session.user_role))}</span>
          </div>
        `;
        button.onclick = async () => {
          activeSessionId = session.id;
          renderSessions();
          await loadMessages();
        };
        sessionsEl.appendChild(button);
      });
      const active = sessions.find(s => s.id === activeSessionId);
      currentTitleEl.textContent = active ? active.title : "会话";
      currentMetaEl.textContent = active
        ? `${roleLabel(active.user_role)} · ${active.user_id || defaultUserId(active.user_role)} · ${formatTime(active.updated_at)}`
        : "未选择会话";
      renameInputEl.value = active ? active.title : "";
    }

    async function loadMessages() {
      if (!activeSessionId) return;
      const messages = await api(`/api/sessions/${activeSessionId}/messages`);
      renderMessages(messages);
    }

    function renderMessages(messages) {
      messagesEl.innerHTML = "";
      if (!messages.length) {
        messagesEl.innerHTML = `<div class="empty">可以输入：生成一份关于 Logistic 回归的 PPT</div>`;
        return;
      }
      messages.forEach(message => {
        const row = document.createElement("div");
        row.className = `message ${message.role}`;
        const bubble = document.createElement("div");
        bubble.className = "bubble";
        renderBubbleContent(bubble, message.content);
        row.appendChild(bubble);
        messagesEl.appendChild(row);
      });
      typesetMath(messagesEl);
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    document.getElementById("newSession").onclick = async () => {
      const role = roleSelectEl.value;
      const userId = userSelectEl.value || defaultUserId(role);
      const title = sessionTitleInputEl.value.trim() || defaultSessionTitle(role, userId);
      const created = await api("/api/sessions", {
        method: "POST",
        body: JSON.stringify({
          title,
          user_role: role,
          user_id: userId,
        }),
      });
      activeSessionId = created.id;
      await loadSessions();
    };

    document.getElementById("resetComposer").onclick = () => {
      sessionTitleInputEl.value = "";
      inputEl.value = "";
      inputEl.focus();
    };

    document.getElementById("renameSession").onclick = async () => {
      if (!activeSessionId) return;
      const title = renameInputEl.value.trim();
      if (!title) return;
      const updated = await api(`/api/sessions/${activeSessionId}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      });
      sessions = sessions.map(session => session.id === updated.id ? updated : session);
      renderSessions();
    };

    document.getElementById("deleteSession").onclick = async () => {
      if (!activeSessionId) return;
      await api(`/api/sessions/${activeSessionId}`, { method: "DELETE" });
      activeSessionId = null;
      await loadSessions();
    };

    roleSelectEl.onchange = () => {
      updateUserOptions();
      sessionTitleInputEl.placeholder = roleSelectEl.value === "student"
        ? "例如：student_or 课后练习"
        : "例如：30人小课学情分析";
    };

    function updateUserOptions() {
      const role = roleSelectEl.value || "teacher";
      const current = userSelectEl.value;
      userSelectEl.innerHTML = "";
      (userOptions[role] || userOptions.teacher).forEach(option => {
        const node = document.createElement("option");
        node.value = option.id;
        node.textContent = option.label;
        userSelectEl.appendChild(node);
      });
      if ([...userSelectEl.options].some(option => option.value === current)) {
        userSelectEl.value = current;
      }
    }

    function defaultUserId(role) {
      return role === "student" ? "student_demo" : "teacher_demo";
    }

    function roleLabel(role) {
      return role === "student" ? "学生" : "老师";
    }

    function defaultSessionTitle(role, userId) {
      return `${roleLabel(role)} · ${userId}`;
    }

    document.getElementById("composer").onsubmit = async event => {
      event.preventDefault();
      const text = inputEl.value.trim();
      if (!text || !activeSessionId) return;
      inputEl.value = "";
      await api(`/api/sessions/${activeSessionId}/messages`, {
        method: "POST",
        body: JSON.stringify({ message: text }),
      });
      await loadSessions();
    };

    function escapeHtml(value) {
      return value.replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;",
      }[char]));
    }

    function formatTime(value) {
      if (!value) return "";
      return new Date(value).toLocaleString("zh-CN", { hour12: false });
    }

    function renderBubbleContent(container, content) {
      container.classList.add("markdown");
      container.innerHTML = markdownToHtml(content);
    }

    function markdownToHtml(markdown) {
      markdown = normalizeMarkdownMath(markdown);
      const lines = markdown.split("\n");
      const html = [];
      let inCode = false;
      let codeLines = [];
      let listType = "";
      let inMath = false;
      let mathEnd = "";
      let mathLines = [];

      function closeList() {
        if (listType) {
          html.push(`</${listType}>`);
          listType = "";
        }
      }

      function flushCode() {
        const body = codeLines.join("\n");
        if (looksLikeFormulaBlock(body)) {
          html.push(`<div class="math-block">\\[${escapeHtml(plainFormulaToLatex(body))}\\]</div>`);
        } else {
          html.push(`<pre><code>${escapeHtml(body)}</code></pre>`);
        }
        codeLines = [];
      }

      function flushMath() {
        html.push(`<div class="math-block">\\[${escapeHtml(mathLines.join("\n"))}\\]</div>`);
        mathLines = [];
      }

      for (let i = 0; i < lines.length; i += 1) {
        const raw = lines[i];
        const line = raw.trimEnd();

        if (inMath) {
          if (line.trim() === mathEnd) {
            flushMath();
            inMath = false;
            mathEnd = "";
          } else {
            mathLines.push(raw);
          }
          continue;
        }

        if (line.trim().startsWith("```")) {
          if (inCode) {
            flushCode();
            inCode = false;
          } else {
            closeList();
            inCode = true;
            codeLines = [];
          }
          continue;
        }
        if (inCode) {
          codeLines.push(raw);
          continue;
        }

        if (line.trim() === "\\[" || line.trim() === "$$") {
          closeList();
          inMath = true;
          mathEnd = line.trim() === "\\[" ? "\\]" : "$$";
          mathLines = [];
          continue;
        }

        const singleMath = line.trim().match(/^(?:\\\[(.+)\\\]|\$\$(.+)\$\$)$/);
        if (singleMath) {
          closeList();
          html.push(`<div class="math-block">\\[${escapeHtml(singleMath[1] || singleMath[2])}\\]</div>`);
          continue;
        }

        if (/^\s*---+\s*$/.test(line)) {
          closeList();
          html.push("<hr>");
          continue;
        }

        if (isTableStart(lines, i)) {
          closeList();
          const tableRows = [];
          tableRows.push(lines[i]);
          i += 2;
          while (i < lines.length && lines[i].trim().startsWith("|")) {
            tableRows.push(lines[i]);
            i += 1;
          }
          i -= 1;
          html.push(renderTable(tableRows));
          continue;
        }

        const artifact = line.match(/^-\s+(.+?):\s+(outputs\/.+)$/);
        if (artifact) {
          closeList();
          const label = escapeHtml(artifact[1]);
          const path = artifact[2];
          const href = `/api/sessions/${activeSessionId}/artifact?path=${encodeURIComponent(path)}`;
          html.push(`<p><a href="${href}" target="_blank" rel="noopener">${label}</a></p>`);
          continue;
        }

        if (!line.trim()) {
          closeList();
          continue;
        }

        const heading = line.match(/^(#{1,6})\s+(.+)$/);
        if (heading) {
          closeList();
          const level = heading[1].length;
          html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
          continue;
        }

        const unordered = line.match(/^\s*[-*]\s+(.+)$/);
        if (unordered) {
          if (listType !== "ul") {
            closeList();
            listType = "ul";
            html.push("<ul>");
          }
          html.push(`<li>${renderInline(unordered[1])}</li>`);
          continue;
        }

        const ordered = line.match(/^\s*\d+\.\s+(.+)$/);
        if (ordered) {
          if (listType !== "ol") {
            closeList();
            listType = "ol";
            html.push("<ol>");
          }
          html.push(`<li>${renderInline(ordered[1])}</li>`);
          continue;
        }

        const quote = line.match(/^>\s*(.+)$/);
        if (quote) {
          closeList();
          html.push(`<blockquote>${renderInline(quote[1])}</blockquote>`);
          continue;
        }

        closeList();
        html.push(`<p>${renderInline(line)}</p>`);
      }

      if (inCode) flushCode();
      if (inMath) flushMath();
      closeList();
      return html.join("");
    }

    function normalizeMarkdownMath(markdown) {
      let normalized = markdown.replace(/```(?:text|math|latex)?\\s*\\n([\\s\\S]*?)\\n```/g, (full, body) => {
        const trimmed = body.trim();
        if (looksLikeFormulaBlock(trimmed)) {
          return `$$\n${plainFormulaToLatex(trimmed)}\n$$`;
        }
        return full;
      });

      return splitMathProtected(normalized).map((part, index) => {
        if (index % 2 === 1) return part;
        return part
          .replace(/^(\s*)(?:chi|χ)\s*(?:\^?2|²)\s*=\s*sum\s+(.+)$/gim, (_, indent, expr) => `${indent}$$\\chi^2 = \\sum ${expr.trim()}$$`)
          .replace(/χ²/g, "$\\chi^2$")
          .replace(/\bα\s*=\s*([0-9.]+)/g, "$\\alpha = $1")
          .replace(/\bp\s*([<>=]+)\s*([0-9.]+)/g, "$p $1 $2")
          .replace(/期望频数\s*([<>]=?)\s*([0-9.]+)/g, (_, op, number) => `期望频数 $${op} ${number}$`)
          .replace(/(\d+\/\d+)\s*=\s*([0-9.]+)%/g, (_, ratio, percent) => `$${ratio} = ${percent}\\\\%$`);
      }).join("");
    }

    function splitMathProtected(value) {
      return value.split(/(\$\$[\s\S]*?\$\$|\$[^$\n]+\$|`[^`]*`)/g);
    }

    function looksLikeFormulaBlock(value) {
      const compact = value.trim();
      if (!compact) return false;
      if (compact.split("\n").length > 3) return false;
      return /(chi|χ|\\chi|sum|\\sum|frac|\\frac|\^|_|=|<|>|α|alpha)/i.test(compact)
        && /(\d|O|E|p|df|OR|χ|chi|\\chi)/i.test(compact);
    }

    function plainFormulaToLatex(value) {
      return value.trim()
        .replace(/\bchi\s*(?:\^?2|²)/gi, "\\chi^2")
        .replace(/χ²|χ\^2/g, "\\chi^2")
        .replace(/\bsum\b/gi, "\\sum")
        .replace(/·/g, "\\cdot ")
        .replace(/×/g, "\\times ");
    }

    function renderInline(value) {
      let html = escapeHtml(value);
      html = html.replace(/\\\((.+?)\\\)/g, (_, expr) => `<span class="math-inline">\\(${expr}\\)</span>`);
      html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
      html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
      return html;
    }

    function renderFormula(value) {
      return renderFormulaSegment(normalizeFormula(value.trim()));
    }

    function normalizeFormula(value) {
      return value
        .replace(/\\left/g, "")
        .replace(/\\right/g, "")
        .replace(/\\mu/g, "μ")
        .replace(/\\alpha/g, "α")
        .replace(/\\neq/g, "≠")
        .replace(/\\leq/g, "≤")
        .replace(/\\geq/g, "≥")
        .replace(/\\times/g, "×")
        .replace(/\\cdot/g, "·")
        .replace(/\s+/g, " ");
    }

    function renderFormulaSegment(value) {
      let output = "";
      let index = 0;
      while (index < value.length) {
        if (value.startsWith("\\frac", index)) {
          const frac = parseFrac(value, index + "\\frac".length);
          if (frac) {
            output += `<span class="frac"><span class="num">${renderFormulaSegment(frac.numerator)}</span><span class="den">${renderFormulaSegment(frac.denominator)}</span></span>`;
            index = frac.nextIndex;
            continue;
          }
        }
        if (value.startsWith("\\text", index)) {
          const group = parseGroup(value, index + "\\text".length);
          if (group) {
            output += escapeHtml(group.text);
            index = group.nextIndex;
            continue;
          }
        }
        const char = value[index];
        if (char === "_" || char === "^") {
          const group = parseGroup(value, index + 1);
          const tag = char === "_" ? "sub" : "sup";
          if (group) {
            output += `<${tag}>${renderFormulaSegment(group.text)}</${tag}>`;
            index = group.nextIndex;
            continue;
          }
          if (index + 1 < value.length) {
            output += `<${tag}>${escapeHtml(value[index + 1])}</${tag}>`;
            index += 2;
            continue;
          }
        }
        if (char === "\\") {
          index += 1;
          continue;
        }
        output += escapeHtml(char);
        index += 1;
      }
      return output;
    }

    function parseFrac(value, index) {
      const first = parseGroup(value, index);
      if (!first) return null;
      const second = parseGroup(value, first.nextIndex);
      if (!second) return null;
      return { numerator: first.text, denominator: second.text, nextIndex: second.nextIndex };
    }

    function parseGroup(value, index) {
      while (index < value.length && /\s/.test(value[index])) index += 1;
      if (value[index] !== "{") return null;
      let depth = 0;
      const start = index + 1;
      for (let pos = index; pos < value.length; pos += 1) {
        if (value[pos] === "{") depth += 1;
        if (value[pos] === "}") {
          depth -= 1;
          if (depth === 0) {
            return { text: value.slice(start, pos), nextIndex: pos + 1 };
          }
        }
      }
      return null;
    }

    function typesetMath(root, attempt = 0) {
      if (!window.MathJax || !window.MathJax.typesetPromise) {
        if (attempt < 20) {
          window.setTimeout(() => typesetMath(root, attempt + 1), 150);
        }
        return;
      }
      window.MathJax.typesetPromise([root]).catch(() => {});
    }

    function isTableStart(lines, index) {
      if (!lines[index] || !lines[index + 1]) return false;
      return lines[index].trim().startsWith("|") && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[index + 1]);
    }

    function renderTable(rows) {
      const parsed = rows.map(row =>
        row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(cell => cell.trim())
      );
      const header = parsed[0] || [];
      const body = parsed.slice(1);
      const headHtml = `<thead><tr>${header.map(cell => `<th>${renderInline(cell)}</th>`).join("")}</tr></thead>`;
      const bodyHtml = `<tbody>${body.map(row => `<tr>${row.map(cell => `<td>${renderInline(cell)}</td>`).join("")}</tr>`).join("")}</tbody>`;
      return `<table>${headHtml}${bodyHtml}</table>`;
    }

    loadSessions().catch(error => {
      messagesEl.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
    });
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Teacher Agent web UI")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    try:
        run_server(args.host, args.port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(
                f"端口 {args.port} 已被占用。可以打开已有服务，或改用："
                f" python -m teacher_agent.web_app --port {args.port + 1}"
            )
            return 1
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
