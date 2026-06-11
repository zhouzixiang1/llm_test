#!/usr/bin/env python3
"""调用 Agnes 图片模型 agnes-image-2.1-flash 进行文生图/图生图。"""

import argparse
import sys

import requests

from agnes_client import generate_image


def main() -> None:
    parser = argparse.ArgumentParser(description="Agnes 图片模型 (agnes-image-2.1-flash) 调用脚本")
    parser.add_argument(
        "--prompt",
        default="A luminous floating city above a misty canyon at sunrise, cinematic realism",
    )
    parser.add_argument("--size", default="1024x768")
    parser.add_argument("--image", help="图生图时的输入图片 URL")
    parser.add_argument("-o", "--output", default="output.png")
    args = parser.parse_args()

    try:
        result = generate_image(
            args.prompt,
            args.output,
            size=args.size,
            image_url=args.image,
        )
        print(f"图片已保存: {result['local_path']}")
        if result.get("remote_url"):
            print(f"图片 URL: {result['remote_url']}")
    except requests.HTTPError as e:
        print(f"API 请求失败: {e.response.status_code} {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as e:
        print(f"网络错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
