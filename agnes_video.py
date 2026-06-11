#!/usr/bin/env python3
"""调用 Agnes 视频模型 agnes-video-v2.0 进行文生视频/图生视频。"""

import argparse
import sys

import requests

from agnes_client import create_video_task, download_video, poll_video_result


def main() -> None:
    parser = argparse.ArgumentParser(description="Agnes 视频模型 (agnes-video-v2.0) 调用脚本")
    parser.add_argument(
        "--prompt",
        default=(
            "A cinematic shot of a cat walking on the beach at sunset, "
            "soft ocean waves, warm golden lighting, realistic motion"
        ),
    )
    parser.add_argument("--image", help="图生视频时的输入图片 URL")
    parser.add_argument("--num-frames", type=int, default=121)
    parser.add_argument("--frame-rate", type=int, default=24)
    parser.add_argument("-o", "--output", default="output.mp4")
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--max-wait", type=int, default=600)
    args = parser.parse_args()

    try:
        print("正在创建视频生成任务...", file=sys.stderr)
        task = create_video_task(
            args.prompt,
            image_url=args.image,
            num_frames=args.num_frames,
            frame_rate=args.frame_rate,
        )
        video_id = task.get("video_id") or task.get("id")
        if not video_id:
            raise RuntimeError(f"未获取到 video_id: {task}")
        print(f"任务已创建, video_id: {video_id}", file=sys.stderr)

        def on_progress(progress, status):
            print(f"状态: {status}, 进度: {progress}%", file=sys.stderr)

        result = poll_video_result(
            video_id,
            interval=args.poll_interval,
            max_wait=args.max_wait,
            on_progress=on_progress,
        )
        video_url = result.get("remixed_from_video_id")
        if not video_url:
            raise RuntimeError(f"任务完成但未返回视频 URL: {result}")
        print(f"视频 URL: {video_url}")
        download_video(video_url, args.output)
        print(f"视频已保存: {args.output}")
    except requests.HTTPError as e:
        print(f"API 请求失败: {e.response.status_code} {e.response.text}", file=sys.stderr)
        sys.exit(1)
    except (requests.RequestException, TimeoutError, RuntimeError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
