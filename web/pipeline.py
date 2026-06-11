"""流水线编排：单条循环、停止控制、重启恢复、SSE 广播。"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any

import requests

from agnes_client import (
    chat_simple,
    create_video_task,
    download_video,
    generate_image,
    parse_json_object,
    poll_video_result,
)
from web import db

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    "pending": "等待中",
    "generating_prompts": "生成提示词",
    "generating_image": "生成图片",
    "generating_video_prompt": "生成视频提示词",
    "generating_video": "生成视频",
    "completed": "已完成",
    "failed": "失败",
}


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._closed = False

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            if not self._closed:
                self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def close_all(self) -> None:
        with self._lock:
            self._closed = True
            for q in self._subscribers:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
            self._subscribers.clear()

    def publish(self, event: str, data: dict[str, Any] | None = None) -> None:
        if self._closed:
            return
        payload = {"event": event, "data": data or {}}
        with self._lock:
            dead: list[queue.Queue] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)


event_bus = EventBus()


class PipelineController:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self.current_item_id: str | None = None

    def is_shutting_down(self) -> bool:
        return self._shutdown_event.is_set()

    @property
    def running(self) -> bool:
        return get_running() and not self.is_shutting_down()

    @property
    def stop_after_item(self) -> bool:
        return get_stop_after_item()

    def get_status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "stop_after_item": self.stop_after_item,
            "total_count": db.count_items(),
            "failed_log_count": db.count_failure_logs(),
            "current_item_id": self.current_item_id,
            "theme": db.get_setting("theme", ""),
        }

    def request_stop(self) -> None:
        set_stop_after_item(True)
        event_bus.publish("status_updated", self.get_status())

    def start(self) -> None:
        with self._lock:
            if self.is_shutting_down():
                return
            set_running(True)
            set_stop_after_item(False)
            if self._thread and self._thread.is_alive():
                event_bus.publish("status_updated", self.get_status())
                return
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="pipeline")
            self._thread.start()
        event_bus.publish("status_updated", self.get_status())

    def shutdown(self) -> None:
        if self._shutdown_event.is_set():
            return
        logger.info("正在停止流水线...")
        self._shutdown_event.set()
        set_running(False)
        set_stop_after_item(True)
        event_bus.close_all()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=10)
            if thread.is_alive():
                logger.warning("流水线线程未在 10s 内结束，将在进程退出时强制终止")
        self.current_item_id = None

    def recover_on_startup(self) -> None:
        db.init_db()
        incomplete = db.get_incomplete_item()
        if incomplete:
            logger.info("发现未完成记录 seq=%s status=%s", incomplete["seq"], incomplete["status"])
        event_bus.publish("status_updated", self.get_status())

    def _run_loop(self) -> None:
        try:
            incomplete = db.get_incomplete_item()
            if incomplete and not self.is_shutting_down():
                _process_item(incomplete["id"], resume=True)
                if get_stop_after_item() or self.is_shutting_down():
                    set_running(False)
                    return

            while get_running() and not get_stop_after_item() and not self.is_shutting_down():
                item = db.create_item()
                event_bus.publish("item_created", db.item_to_public(item))
                _process_item(item["id"], resume=False)
                if get_stop_after_item() or self.is_shutting_down():
                    set_running(False)
                    break
                if self._shutdown_event.wait(2):
                    set_running(False)
                    break
        except Exception:
            logger.exception("流水线异常退出")
        finally:
            self.current_item_id = None
            if not self.is_shutting_down():
                event_bus.publish("status_updated", self.get_status())


controller = PipelineController()


def get_running() -> bool:
    return db.get_setting("running", "true").lower() == "true"


def set_running(value: bool) -> None:
    db.set_setting("running", "true" if value else "false")


def get_stop_after_item() -> bool:
    return db.get_setting("stop_after_item", "false").lower() == "true"


def set_stop_after_item(value: bool) -> None:
    db.set_setting("stop_after_item", "true" if value else "false")


def _emit_item(item: dict[str, Any]) -> None:
    event_bus.publish("item_updated", db.item_to_public(item))


def _generate_image_prompt(theme: str) -> dict[str, str]:
    recent = db.get_recent_titles()
    recent_text = ", ".join(recent) if recent else "无"
    theme_text = theme.strip() if theme.strip() else "随机创意主题"
    prompt = f"""你是创意导演。根据主题「{theme_text}」，生成 1 个高密度英文文生图提示词。
已有标题（避免重复）：{recent_text}
只返回 JSON 对象，格式：{{"title":"...", "image_prompt":"..."}}
image_prompt 需包含：主体+场景+风格+光照+构图，全英文。"""

    for attempt in range(3):
        try:
            raw = chat_simple(prompt, temperature=0.8, max_tokens=512)
            data = parse_json_object(raw)
            title = str(data.get("title", "")).strip()
            image_prompt = str(data.get("image_prompt", "")).strip()
            if title and image_prompt:
                return {"title": title, "image_prompt": image_prompt}
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("解析图片提示词失败 attempt=%s: %s", attempt + 1, e)
        except (requests.RequestException, RuntimeError) as e:
            logger.warning("调用文本 API 失败 attempt=%s: %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(min((attempt + 1) * 5, 15))
    raise RuntimeError("无法生成有效的图片提示词")


def _generate_video_prompt(image_prompt: str) -> str:
    prompt = f"""Based on this image prompt: "{image_prompt}"
Write a concise English video motion prompt for image-to-video.
Describe subtle camera movement and subject motion only. One paragraph, no JSON."""
    return chat_simple(prompt, temperature=0.7, max_tokens=256).strip()


def _archive_item_failure(item_id: str, step: str, error: str, theme: str) -> None:
    item = db.get_item(item_id)
    log = db.archive_item_atomic(item_id, item, step=step, error=error, theme=theme)
    event_bus.publish("log_created", log)
    event_bus.publish("item_removed", {"id": item_id})
    logger.warning("条目失败已归档 seq=%s step=%s: %s", item.get("seq"), step, error)


def _process_item(item_id: str, *, resume: bool) -> None:
    controller.current_item_id = item_id
    item = db.get_item(item_id)
    theme = db.get_setting("theme", "")
    current_step = item.get("status") or "pending"

    try:
        if not resume or not item.get("image_prompt"):
            current_step = "generating_prompts"
            item = db.update_item(item_id, status="generating_prompts")
            _emit_item(item)
            prompts = _generate_image_prompt(theme)
            item = db.update_item(
                item_id,
                title=prompts["title"],
                image_prompt=prompts["image_prompt"],
                status="generating_image",
            )
            _emit_item(item)
        else:
            if item["status"] in ("pending", "generating_prompts"):
                item = db.update_item(item_id, status="generating_image")
                _emit_item(item)

        item = db.get_item(item_id)
        image_path = Path(item.get("image_path") or db.OUTPUT_DIR / f"{item_id}.png")

        if not image_path.exists() or not item.get("image_url"):
            current_step = "generating_image"
            item = db.update_item(item_id, status="generating_image")
            _emit_item(item)
            img_result = generate_image(item["image_prompt"], image_path)
            item = db.update_item(
                item_id,
                image_path=img_result["local_path"],
                image_url=img_result["remote_url"],
                status="generating_video_prompt",
            )
            _emit_item(item)
        elif item["status"] == "generating_image":
            item = db.update_item(item_id, status="generating_video_prompt")
            _emit_item(item)

        item = db.get_item(item_id)
        if not item.get("video_prompt"):
            current_step = "generating_video_prompt"
            item = db.update_item(item_id, status="generating_video_prompt")
            _emit_item(item)
            video_prompt = _generate_video_prompt(item["image_prompt"])
            item = db.update_item(item_id, video_prompt=video_prompt, status="generating_video")
            _emit_item(item)
        elif item["status"] == "generating_video_prompt":
            item = db.update_item(item_id, status="generating_video")
            _emit_item(item)

        item = db.get_item(item_id)
        video_path = Path(item.get("video_path") or db.OUTPUT_DIR / f"{item_id}.mp4")

        if video_path.exists() and item.get("video_url"):
            item = db.update_item(item_id, status="completed", video_progress=100)
            _emit_item(item)
            return

        current_step = "generating_video"
        # video_progress=100 + 有 video_id = 视频已生成但下载未完成，不重置进度
        if not (item.get("video_id") and item.get("video_progress", 0) >= 100):
            item = db.update_item(item_id, status="generating_video", video_progress=0)
            _emit_item(item)

        video_id = item.get("video_id")
        if not video_id:
            task = create_video_task(
                item["video_prompt"],
                image_url=item["image_url"],
                num_frames=81,
                frame_rate=24,
            )
            video_id = task.get("video_id") or task.get("id")
            if not video_id:
                raise RuntimeError(f"未获取到 video_id: {task}")
            item = db.update_item(item_id, video_id=video_id)
            _emit_item(item)

        def on_progress(progress: int, status: str) -> None:
            if controller.is_shutting_down():
                return
            updated = db.update_item(item_id, video_progress=progress)
            _emit_item(updated)

        def _should_stop_polling() -> bool:
            return controller.is_shutting_down() or get_stop_after_item()

        result = poll_video_result(
            video_id,
            interval=10,
            max_wait=600,
            on_progress=on_progress,
            should_stop=_should_stop_polling,
        )
        remote_video_url = result.get("remixed_from_video_id")
        # 防御性 fallback：若 API 未来版本改字段名
        if not remote_video_url:
            remote_video_url = result.get("video_url") or result.get("url")
        if not remote_video_url or not isinstance(remote_video_url, str) or not remote_video_url.startswith("http"):
            raise RuntimeError(f"任务完成但未返回有效视频 URL: {result}")

        download_ok = False
        try:
            download_video(remote_video_url, video_path)
            local_path = str(video_path)
            download_ok = True
        except RuntimeError as e:
            logger.warning("本地下载失败，保留远程 URL: %s", e)
            local_path = item.get("video_path") or ""

        item = db.update_item(
            item_id,
            video_path=local_path if download_ok or (local_path and Path(local_path).exists()) else None,
            video_url=remote_video_url,
            status="completed",
            video_progress=100,
        )
        _emit_item(item)

    except (requests.RequestException, TimeoutError, RuntimeError, json.JSONDecodeError, KeyError, IndexError) as e:
        logger.exception("处理条目失败 id=%s step=%s", item_id, current_step)
        try:
            _archive_item_failure(item_id, current_step, str(e), theme)
        except KeyError:
            logger.warning("归档失败，条目可能已被删除 id=%s", item_id)
    finally:
        controller.current_item_id = None
        event_bus.publish("status_updated", controller.get_status())
