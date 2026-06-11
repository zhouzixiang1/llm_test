"""Agnes AI 统一 API 客户端。"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import requests
import urllib3.util.connection as urllib3_connection

logger = logging.getLogger(__name__)

API_BASE = "https://apihub.agnes-ai.com"
DEFAULT_API_KEY = "sk-Qwbtdv3WHqxPpyHsnTJ3lV1pW1MmILPprBPGkDracOeALUy3"

TEXT_MODEL = "agnes-2.0-flash"
IMAGE_MODEL = "agnes-image-2.1-flash"
VIDEO_MODEL = "agnes-video-v2.0"

RETRYABLE_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def get_api_key() -> str:
    return os.environ.get("AGNES_API_KEY", DEFAULT_API_KEY)


def _auth_headers(*, json_content: bool = True) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {get_api_key()}"}
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def _api_request(
    method: str,
    url: str,
    *,
    retries: int = 5,
    timeout: tuple[float, float] | float = (30, 180),
    max_total_time: float = 600,
    **kwargs: Any,
) -> requests.Response:
    last_error: Exception | None = None
    headers = kwargs.pop("headers", None) or {}
    start_time = time.monotonic()

    with requests.Session() as session:
        for attempt in range(1, retries + 1):
            if time.monotonic() - start_time > max_total_time:
                raise RuntimeError(f"API 请求总耗时超过 {max_total_time}s: {last_error}") from last_error
            try:
                with _prefer_ipv4():
                    resp = session.request(
                        method,
                        url,
                        headers=headers,
                        timeout=timeout,
                        **kwargs,
                    )
                resp.raise_for_status()
                return resp
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if 400 <= status < 500 and status not in (408, 429):
                    raise
                last_error = e
            except RETRYABLE_EXCEPTIONS as e:
                last_error = e

            logger.warning("API 请求失败 (%s/%s) %s %s: %s", attempt, retries, method, url, last_error)
            if attempt < retries:
                remaining = max_total_time - (time.monotonic() - start_time)
                if remaining <= 0:
                    raise RuntimeError(f"API 请求总耗时超过 {max_total_time}s: {last_error}") from last_error
                time.sleep(min(attempt * 3, 15, remaining))

    raise RuntimeError(f"API 请求失败: {last_error}") from last_error


def chat(
    messages: list[dict[str, Any]],
    *,
    system: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 1024,
    enable_thinking: bool = False,
) -> str:
    payload_messages = list(messages)
    if system and not any(m.get("role") == "system" for m in payload_messages):
        payload_messages.insert(0, {"role": "system", "content": system})

    payload: dict[str, Any] = {
        "model": TEXT_MODEL,
        "messages": payload_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if enable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": True}

    resp = _api_request(
        "POST",
        f"{API_BASE}/v1/chat/completions",
        headers=_auth_headers(),
        json=payload,
        timeout=(30, 180),
        retries=5,
    )
    return resp.json()["choices"][0]["message"]["content"]


def chat_simple(prompt: str, **kwargs: Any) -> str:
    return chat([{"role": "user", "content": prompt}], **kwargs)


def generate_image(
    prompt: str,
    output_path: str | Path,
    *,
    size: str = "1024x768",
    image_url: str | None = None,
) -> dict[str, str]:
    payload: dict[str, Any] = {
        "model": IMAGE_MODEL,
        "prompt": prompt,
        "size": size,
        "extra_body": {"response_format": "url"},
    }
    if image_url:
        payload["extra_body"]["image"] = [image_url]

    resp = _api_request(
        "POST",
        f"{API_BASE}/v1/images/generations",
        headers=_auth_headers(),
        json=payload,
        timeout=(30, 360),
        retries=5,
    )
    result = resp.json()
    data_list = result.get("data")
    if not data_list or not isinstance(data_list, list):
        raise RuntimeError(f"图片 API 响应缺少 data 数组: {result}")
    data = data_list[0]

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    remote_url = data.get("url")
    b64_json = data.get("b64_json")

    if remote_url:
        _download_file(remote_url, output_path)
        return {"local_path": str(output_path), "remote_url": remote_url}

    if b64_json:
        output_path.write_bytes(base64.b64decode(b64_json))
        return {"local_path": str(output_path), "remote_url": ""}

    raise RuntimeError(f"响应中未找到图片数据: {result}")


def create_video_task(
    prompt: str,
    *,
    image_url: str | None = None,
    height: int = 768,
    width: int = 1152,
    num_frames: int = 81,
    frame_rate: int = 24,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": VIDEO_MODEL,
        "prompt": prompt,
        "height": height,
        "width": width,
        "num_frames": num_frames,
        "frame_rate": frame_rate,
    }
    if image_url:
        payload["image"] = image_url

    resp = _api_request(
        "POST",
        f"{API_BASE}/v1/videos",
        headers=_auth_headers(),
        json=payload,
        timeout=(30, 60),
        retries=5,
    )
    return resp.json()


def poll_video_result(
    video_id: str,
    *,
    interval: int = 10,
    max_wait: int = 600,
    on_progress: Callable[[int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    url = f"{API_BASE}/agnesapi"
    headers = {"Authorization": f"Bearer {get_api_key()}"}
    params = {"video_id": video_id, "model_name": VIDEO_MODEL}

    elapsed = 0
    while elapsed < max_wait:
        if should_stop and should_stop():
            raise RuntimeError("服务正在关闭，停止视频轮询")

        resp = _api_request(
            "GET",
            url,
            headers=headers,
            params=params,
            timeout=(15, 60),
            retries=3,
        )
        result = resp.json()

        status = result.get("status", "unknown")
        progress = result.get("progress", 0)
        if on_progress:
            on_progress(progress, status)

        if status == "completed":
            video_url = result.get("remixed_from_video_id") or result.get("video_url") or result.get("url")
            if not video_url:
                raise RuntimeError(f"视频任务 completed 但响应中无 URL 字段: {list(result.keys())}")
            return result
        if status == "failed":
            error = result.get("error", "未知错误")
            raise RuntimeError(f"视频生成失败: {error}")

        if should_stop and should_stop():
            raise RuntimeError("服务正在关闭，停止视频轮询")

        time.sleep(interval)
        elapsed += interval

    raise TimeoutError(f"等待超时 ({max_wait}s)，video_id={video_id}")


@contextmanager
def _prefer_ipv4():
    orig = urllib3_connection.allowed_gai_family

    def _ipv4_only() -> int:
        return socket.AF_INET

    urllib3_connection.allowed_gai_family = _ipv4_only
    try:
        yield
    finally:
        urllib3_connection.allowed_gai_family = orig


def _requests_download(url: str, output_path: Path, *, timeout: int = 300) -> None:
    headers = {
        "Connection": "close",
        "User-Agent": "agnes-client/1.0",
    }
    tmp_path = output_path.with_suffix(output_path.suffix + ".part")
    with requests.Session() as session:
        with _prefer_ipv4():
            resp = session.get(url, headers=headers, timeout=timeout, stream=True)
        resp.raise_for_status()
        expected = int(resp.headers.get("Content-Length") or 0)
        written = 0
        try:
            with tmp_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
            if expected and written != expected:
                raise requests.RequestException(
                    f"下载大小不匹配: expected={expected}, got={written}"
                )
            tmp_path.replace(output_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise


def _curl_download(url: str, output_path: Path, *, timeout: int = 300) -> None:
    curl_bin = shutil.which("curl")
    if not curl_bin:
        raise RuntimeError("curl 不可用")

    tmp_path = output_path.with_suffix(output_path.suffix + ".part")
    try:
        subprocess.run(
            [
                curl_bin,
                "-fsSL",
                "--retry",
                "3",
                "--retry-delay",
                "2",
                "--connect-timeout",
                "30",
                "--max-time",
                str(timeout),
                "-o",
                str(tmp_path),
                url,
            ],
            check=True,
        )
        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            raise RuntimeError("curl 下载结果为空")
        tmp_path.replace(output_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _download_file(url: str, output_path: str | Path, *, timeout: int = 300, retries: int = 5) -> str:
    if not url or not isinstance(url, str) or not url.strip():
        raise RuntimeError(f"下载 URL 为空或无效: {url!r}")
    url = url.strip()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            _requests_download(url, output_path, timeout=timeout)
            return str(output_path)
        except requests.RequestException as e:
            last_error = e
            logger.warning("requests 下载失败 (%s/%s): %s", attempt, retries, e)
            if attempt < retries:
                time.sleep(min(attempt * 3, 15))

    logger.warning("requests 全部失败，尝试 curl 备用下载: %s", url)
    try:
        _curl_download(url, output_path, timeout=timeout)
        return str(output_path)
    except Exception as curl_error:
        raise RuntimeError(f"下载失败: requests={last_error}; curl={curl_error}") from curl_error


def download_video(video_url: str, output_path: str | Path, *, retries: int = 5) -> str:
    return _download_file(video_url, output_path, retries=retries)


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)
