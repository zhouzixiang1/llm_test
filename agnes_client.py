"""Agnes AI 统一 API 客户端。"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import requests
import urllib3.util.connection as urllib3_connection

logger = logging.getLogger(__name__)


# ── 结构化日志 ────────────────────────────────────────────────────────────────


class JsonFormatter(logging.Formatter):
    """自定义 JSON 日志格式化器，支持 item_id、step、duration_ms 上下文字段。"""

    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        log_record: dict[str, Any] = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "item_id"):
            log_record["item_id"] = record.item_id
        if hasattr(record, "step"):
            log_record["step"] = record.step
        if hasattr(record, "duration_ms"):
            log_record["duration_ms"] = record.duration_ms
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return _json.dumps(log_record, ensure_ascii=False)


def log_with_context(
    level: int,
    msg: str,
    *,
    item_id: str | None = None,
    step: str | None = None,
    duration_ms: int | None = None,
    **kwargs: Any,
) -> None:
    """带上下文信息的结构化日志。"""
    extra: dict[str, Any] = {}
    if item_id is not None:
        extra["item_id"] = item_id
    if step is not None:
        extra["step"] = step
    if duration_ms is not None:
        extra["duration_ms"] = duration_ms
    logger.log(level, msg, extra=extra, **kwargs)


# ── 熔断器 ──────────────────────────────────────────────────────────────────

class CircuitBreaker:
    """连续失败达到阈值后停止请求，冷却后试探恢复。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 120.0) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._failure_count = 0
        self._state = self.CLOSED
        self._last_failure_time: float = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        return self._state

    def allow_request(self) -> bool:
        with self._lock:
            if self._state == self.CLOSED:
                return True
            if self._state == self.OPEN:
                if time.monotonic() - self._last_failure_time >= self._cooldown_seconds:
                    self._state = self.HALF_OPEN
                    return True
                return False
            return True  # HALF_OPEN 允许一次试探

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._state = self.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self._failure_threshold:
                if self._state != self.OPEN:
                    logger.warning(
                        "熔断器开启: 连续失败 %d 次，冷却 %ds",
                        self._failure_count, self._cooldown_seconds,
                    )
                self._state = self.OPEN

    def get_status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "failure_count": self._failure_count,
            "cooldown_seconds": self._cooldown_seconds,
        }


api_circuit_breaker = CircuitBreaker(failure_threshold=5, cooldown_seconds=120.0)

API_BASE = os.environ.get("AGNES_API_BASE", "https://apihub.agnes-ai.com")
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
    if not api_circuit_breaker.allow_request():
        raise RuntimeError(
            f"API 熔断器已开启，冷却中 (state={api_circuit_breaker.state})"
        )
    last_error: Exception | None = None
    headers = kwargs.pop("headers", None) or {}
    start_time = time.monotonic()

    with requests.Session() as session:
        for attempt in range(1, retries + 1):
            if time.monotonic() - start_time > max_total_time:
                api_circuit_breaker.record_failure()
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
                api_circuit_breaker.record_success()
                return resp
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if 400 <= status < 500 and status not in (408, 429):
                    try:
                        detail = e.response.json() if e.response is not None else {}
                    except Exception:
                        detail = e.response.text[:500] if e.response is not None else ""
                    raise RuntimeError(
                        f"API 请求被拒绝 ({status}): {detail}"
                    ) from e
                last_error = e
            except RETRYABLE_EXCEPTIONS as e:
                last_error = e

            logger.warning("API 请求失败 (%s/%s) %s %s: %s", attempt, retries, method, url, last_error)
            if attempt < retries:
                remaining = max_total_time - (time.monotonic() - start_time)
                if remaining <= 0:
                    api_circuit_breaker.record_failure()
                    raise RuntimeError(f"API 请求总耗时超过 {max_total_time}s: {last_error}") from last_error
                time.sleep(min(attempt * 3, 15, remaining))

    api_circuit_breaker.record_failure()
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


def _get_max_frames(width: int, height: int) -> int:
    """根据分辨率档位返回最大帧数限制。"""
    max_dim = max(width, height)
    if max_dim > 1440:
        return 169
    if max_dim > 854:
        return 409
    return 961


def _clamp_frames(num_frames: int, max_frames: int) -> int:
    """将帧数钳制到 ≤ max_frames 的最大合法值 (8n+1)。"""
    if num_frames <= max_frames:
        return num_frames
    n = (max_frames - 1) // 8
    return 8 * n + 1


def create_video_task(
    prompt: str,
    *,
    image_url: str | None = None,
    height: int = 768,
    width: int = 1152,
    num_frames: int = 81,
    frame_rate: int = 24,
) -> dict[str, Any]:
    max_frames = _get_max_frames(width, height)
    if num_frames < 9 or num_frames > max_frames:
        num_frames = _clamp_frames(max_frames, max_frames)
        logger.info("num_frames 已自动调整为 %d（分辨率 %dx%d 最大 %d 帧）", num_frames, width, height, max_frames)
    if (num_frames - 1) % 8 != 0:
        n = (num_frames - 1) // 8
        num_frames = 8 * n + 1
    if width < 256 or width > 2048 or width % 64 != 0:
        raise ValueError(f"width 必须是 64 的倍数且在 256-2048 之间，当前值: {width}")
    if height < 256 or height > 2048 or height % 64 != 0:
        raise ValueError(f"height 必须是 64 的倍数且在 256-2048 之间，当前值: {height}")

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
        timeout=(30, 120),
        retries=3,
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
