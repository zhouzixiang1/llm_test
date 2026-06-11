#!/usr/bin/env python3
"""调用 Agnes 文本模型 agnes-2.0-flash 进行对话生成。"""

import argparse
import json
import sys

import requests

from agnes_client import chat as api_chat


def main() -> None:
    parser = argparse.ArgumentParser(description="Agnes 文本模型 (agnes-2.0-flash) 调用脚本")
    parser.add_argument("prompt", nargs="?", default="你好，请用一句话介绍你自己。")
    parser.add_argument("--system", help="系统提示词")
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--thinking", action="store_true", help="开启 Thinking 模式")
    args = parser.parse_args()

    try:
        content = api_chat(
            [{"role": "user", "content": args.prompt}],
            system=args.system,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            enable_thinking=args.thinking,
        )
        print(content)
    except requests.HTTPError as e:
        print(f"API 请求失败: {e.response.status_code} {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as e:
        print(f"网络错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
