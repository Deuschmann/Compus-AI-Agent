import argparse
import os
import sys
from typing import Any

import requests


DEFAULT_DIFY_API_URL = "http://localhost/v1/workflows/run"
DEFAULT_USER_ID = "teacher_local_test"


class DifyWorkflowError(RuntimeError):
    """Raised when the Dify workflow request fails or returns an invalid payload."""


def _extract_output_text(payload: dict[str, Any], output_name: str = "text") -> str:
    outputs = payload.get("data", {}).get("outputs", {})
    output_text = outputs.get(output_name)

    if output_text is None:
        available = ", ".join(outputs.keys()) or "none"
        raise DifyWorkflowError(
            f"工作流返回中没有 outputs.{output_name}；可用输出字段: {available}"
        )

    return str(output_text)


def call_dify_workflow(
    ques: str,
    *,
    api_key: str | None = None,
    api_url: str | None = None,
    user: str = DEFAULT_USER_ID,
    timeout: int = 120,
    output_name: str = "text",
) -> str:
    """Call the Bio_Agent Dify workflow.

    Bio_Agent.yml 的开始节点变量名是 `ques`，结束节点输出变量名是 `text`。
    """

    api_key = api_key or os.getenv("DIFY_API_KEY")
    api_url = api_url or os.getenv("DIFY_API_URL", DEFAULT_DIFY_API_URL)

    if not api_key:
        raise DifyWorkflowError(
            "缺少 Dify API Key。请设置环境变量 DIFY_API_KEY，或传入 --api-key。"
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = {
        "inputs": {
            "ques": ques,
        },
        "response_mode": "blocking",
        "user": user,
    }

    try:
        response = requests.post(api_url, headers=headers, json=data, timeout=timeout)
    except requests.RequestException as exc:
        raise DifyWorkflowError(f"Dify 请求失败: {exc}") from exc

    if not response.ok:
        raise DifyWorkflowError(
            f"Dify 调用失败，状态码: {response.status_code}\n{response.text}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise DifyWorkflowError(f"Dify 返回的不是合法 JSON:\n{response.text}") from exc

    return _extract_output_text(payload, output_name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="调用 Bio_Agent Dify 工作流")
    parser.add_argument(
        "ques",
        nargs="?",
        default="帮我出一份关于配对t检验的简答题",
        help="传给 Dify 开始节点 `ques` 的问题文本",
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help=f"Dify workflow API 地址，默认读取 DIFY_API_URL 或 {DEFAULT_DIFY_API_URL}",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Dify App API Key，默认读取 DIFY_API_KEY",
    )
    parser.add_argument(
        "--user",
        default=DEFAULT_USER_ID,
        help=f"Dify 调用者标识，默认 {DEFAULT_USER_ID}",
    )
    parser.add_argument(
        "--output-name",
        default="text",
        help="结束节点输出变量名，Bio_Agent.yml 中默认为 text",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="请求超时时间，单位秒",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        output_text = call_dify_workflow(
            args.ques,
            api_key=args.api_key,
            api_url=args.api_url,
            user=args.user,
            timeout=args.timeout,
            output_name=args.output_name,
        )
    except DifyWorkflowError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(output_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
