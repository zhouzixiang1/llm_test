"""流水线编排：单条循环、停止控制、重启恢复、SSE 广播。"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import shutil
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

from agnes_client import (
    chat_simple,
    create_video_task,
    download_video,
    generate_image,
    log_with_context,
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
        # 投递到活跃的 Webhook
        self._deliver_to_webhooks(event, data or {})

    @staticmethod
    def _deliver_to_webhooks(event: str, data: dict[str, Any]) -> None:
        try:
            webhooks = db.list_active_webhooks()
        except Exception:
            return
        for wh in webhooks:
            events_str = wh.get("events", "")
            events_list = [e.strip() for e in events_str.split(",") if e.strip()]
            if events_list and event not in events_list:
                continue
            wh_id = wh["id"]
            wh_url = wh["url"]
            wh_secret = wh.get("secret", "")

            def _send(wh_url: str = wh_url, wh_secret: str = wh_secret, event: str = event, data: dict[str, Any] = data) -> None:
                try:
                    body = json.dumps(
                        {"event": event, "data": data, "timestamp": datetime.now(timezone.utc).isoformat()},
                        ensure_ascii=False,
                    )
                    headers = {"Content-Type": "application/json"}
                    if wh_secret:
                        sig = hmac.new(
                            wh_secret.encode("utf-8"),
                            body.encode("utf-8"),
                            hashlib.sha256,
                        ).hexdigest()
                        headers["X-Webhook-Signature"] = sig
                    requests.post(wh_url, data=body.encode("utf-8"), headers=headers, timeout=10)
                except Exception:
                    logger.warning("Webhook 投递失败 url=%s", wh_url, exc_info=True)

            t = threading.Thread(target=_send, daemon=True)
            t.start()


event_bus = EventBus()

STYLE_PRESETS = {
    "none": "",
    "cinematic": ", cinematic lighting, dramatic composition, film grain, anamorphic lens",
    "anime": ", anime style, cel shading, vibrant colors, detailed anime illustration",
    "watercolor": ", watercolor painting style, soft washes, flowing colors, paper texture",
    "photorealistic": ", photorealistic, ultra detailed, 8k, DSLR quality, natural lighting",
    "scifi": ", science fiction, futuristic, neon glow, cyberpunk atmosphere, holographic elements",
}

VARIATION_STYLES = [
    "oil painting, impasto brushwork, rich earthy tones, classical composition",
    "Japanese ukiyo-e woodblock print style, flat colors, bold outlines",
    "pixel art, 16-bit retro game aesthetic, limited color palette",
    "Art Nouveau, flowing organic lines, decorative floral motifs",
    "surrealist, dreamlike, impossible geometry, melting forms",
    "noir, high contrast black and white, dramatic shadows, film noir atmosphere",
    "pop art, Ben-Day dots, bold primary colors, comic book aesthetic",
    "stained glass, translucent colored panels, leading outlines, cathedral light",
    "minimalist, clean geometric shapes, negative space, muted palette",
    "baroque, ornate detailing, chiaroscuro lighting, dramatic framing",
    "vaporwave, pastel gradients, retro computing, neoclassical statues",
    "linocut printmaking, bold carved lines, high contrast, textured",
]


class PipelineController:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self.current_item_id: str | None = None
        self._consecutive_failures: int = 0
        self._last_error: str = ""
        self._last_error_time: str = ""
        self._session_start_time: float | None = None
        self._session_item_count: int = 0

    def is_shutting_down(self) -> bool:
        return self._shutdown_event.is_set()

    @property
    def running(self) -> bool:
        return get_running() and not self.is_shutting_down()

    @property
    def stop_after_item(self) -> bool:
        return get_stop_after_item()

    def get_status(self) -> dict[str, Any]:
        from agnes_client import api_circuit_breaker
        return {
            "running": self.running,
            "stop_after_item": self.stop_after_item,
            "total_count": db.count_items(),
            "failed_log_count": db.count_failure_logs(),
            "current_item_id": self.current_item_id,
            "theme": db.get_setting("theme", ""),
            "consecutive_failures": self._consecutive_failures,
            "last_error": self._last_error,
            "last_error_time": self._last_error_time,
            "api_circuit_breaker": api_circuit_breaker.get_status(),
            "image_size": db.get_setting("image_size", "1024x768"),
            "video_width": db.get_setting("video_width", "1152"),
            "video_height": db.get_setting("video_height", "768"),
            "video_num_frames": db.get_setting("video_num_frames", "81"),
            "video_frame_rate": db.get_setting("video_frame_rate", "24"),
            "batch_limit": db.get_setting("batch_limit", "0"),
            "schedule_start": db.get_setting("schedule_start", ""),
            "schedule_end": db.get_setting("schedule_end", ""),
            "style_preset": db.get_setting("style_preset", "none"),
            "variation_mode": db.get_setting("variation_mode", "off"),
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
            self._session_start_time = time.monotonic()
            self._session_item_count = 0
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
        # 启动时自动清理
        cleanup_on_startup = db.get_setting("cleanup_on_startup", "false")
        retention_days = db.get_setting("retention_days", "0")
        if cleanup_on_startup == "true" and int(retention_days) > 0:
            deleted = db.cleanup_old_items(int(retention_days))
            logger.info("启动时自动清理：%d 条过期条目已删除", len(deleted))
        event_bus.publish("status_updated", self.get_status())

    def _run_loop(self) -> None:
        try:
            incomplete = db.get_incomplete_item()
            if incomplete and not self.is_shutting_down():
                try:
                    _process_item(incomplete["id"], resume=True)
                    self._consecutive_failures = 0
                except Exception:
                    self._consecutive_failures += 1
                    self._last_error = "恢复未完成条目失败"
                    self._last_error_time = db._now()
                    logger.exception("流水线异常 (恢复未完成条目)")
                if get_stop_after_item() or self.is_shutting_down():
                    set_running(False)
                    return

            while get_running() and not get_stop_after_item() and not self.is_shutting_down():
                # 计划窗口检查
                if not _wait_for_schedule(self._shutdown_event):
                    set_running(False)
                    break

                # 批量限制检查
                batch_limit = int(db.get_setting("batch_limit", "0"))
                if batch_limit > 0 and self._session_item_count >= batch_limit:
                    logger.info("已达到批量限制 %d 条，流水线停止", batch_limit)
                    set_running(False)
                    event_bus.publish("status_updated", self.get_status())
                    break

                item = db.create_item()
                self._session_item_count += 1
                event_bus.publish("item_created", db.item_to_public(item))
                try:
                    _process_item(item["id"], resume=False)
                    self._consecutive_failures = 0
                except Exception:
                    self._consecutive_failures += 1
                    self._last_error = f"条目 #{item.get('seq', '?')} 处理失败"
                    self._last_error_time = db._now()
                    logger.exception("流水线异常 (新条目)")
                if get_stop_after_item() or self.is_shutting_down():
                    set_running(False)
                    break
                if self._consecutive_failures > 0:
                    backoff = min(30 * (2 ** (self._consecutive_failures - 1)), 300)
                    logger.info("连续失败 %d 次，等待 %ds 后重试", self._consecutive_failures, backoff)
                    if self._shutdown_event.wait(backoff):
                        set_running(False)
                        break
                else:
                    if self._shutdown_event.wait(2):
                        set_running(False)
                        break
        except Exception:
            logger.exception("流水线运行时严重异常")
        finally:
            self.current_item_id = None
            if not self.is_shutting_down():
                event_bus.publish("status_updated", self.get_status())


def _check_schedule_window() -> bool:
    """检查当前时间是否在计划窗口内。返回 True 表示可以继续生成。"""
    schedule_start = db.get_setting("schedule_start", "")
    schedule_end = db.get_setting("schedule_end", "")
    if not schedule_start.strip() or not schedule_end.strip():
        return True
    try:
        now = datetime.now(timezone(timedelta(hours=8)))  # 使用 UTC+8
        current_minutes = now.hour * 60 + now.minute
        start_parts = schedule_start.strip().split(":")
        end_parts = schedule_end.strip().split(":")
        start_minutes = int(start_parts[0]) * 60 + int(start_parts[1])
        end_minutes = int(end_parts[0]) * 60 + int(end_parts[1])
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes < end_minutes
        else:
            # 跨午夜，如 22:00 ~ 06:00
            return current_minutes >= start_minutes or current_minutes < end_minutes
    except (ValueError, IndexError):
        logger.warning("计划时间格式无效: start=%s end=%s", schedule_start, schedule_end)
        return True


def _wait_for_schedule(shutdown_event: threading.Event) -> bool:
    """等待直到进入计划窗口。返回 True 表示应该继续，False 表示收到关闭信号。"""
    while not _check_schedule_window():
        logger.info("当前时间不在计划窗口内，等待 60 秒后重新检查")
        if shutdown_event.wait(60):
            return False
    return True


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


def _check_disk_space(min_mb: int = 500) -> None:
    """磁盘空间不足时抛出 RuntimeError。"""
    usage = shutil.disk_usage(db.OUTPUT_DIR)
    available_mb = usage.free / (1024 * 1024)
    if available_mb < min_mb:
        raise RuntimeError(
            f"磁盘空间不足: 剩余 {available_mb:.0f}MB，需要至少 {min_mb}MB"
        )


def _generate_image_prompt(theme: str, previous_context: dict[str, Any] | None = None) -> dict[str, str]:
    recent = db.get_recent_titles()
    recent_text = ", ".join(recent) if recent else "无"
    theme_text = theme.strip() if theme.strip() else "随机创意主题"
    style_preset = db.get_setting("style_preset", "none")
    style_modifier = STYLE_PRESETS.get(style_preset, "")

    # 构建风格变化上下文
    variation_section = ""
    suggested_style = ""
    if previous_context:
        prev_title = previous_context.get("title", "")
        prev_prompt = previous_context.get("image_prompt", "")
        variation_section = f"""
上一轮生成的内容：
- 标题：{prev_title}
- 图片提示词：{prev_prompt}

**重要要求**：在上一轮内容的基础上，选择一个完全不同的视觉风格重新创作。保持主体或场景的关联性，但风格、光照、色调、构图必须显著不同。不要沿用上一轮使用的任何风格关键词。"""
        # 从风格池中轮换选取建议
        style_index = int(db.get_setting("_variation_style_index", "0"))
        idx = style_index % len(VARIATION_STYLES)
        suggested_style = f"\n建议尝试的风格方向（可参考或自由发挥）：{VARIATION_STYLES[idx]}"
        db.set_setting("_variation_style_index", str(style_index + 1))

    # 检查是否有默认模板
    tmpl = db.get_default_template()
    if tmpl and tmpl.get("image_prompt_template"):
        # 使用模板，替换 {theme} 占位符
        tmpl_text = tmpl["image_prompt_template"].replace("{theme}", theme_text)
        style = tmpl.get("style_modifiers", "").strip()
        prompt = f"""{tmpl_text}
已有标题（避免重复）：{recent_text}
{variation_section}{suggested_style}
只返回 JSON 对象，格式：{{"title":"...", "image_prompt":"..."}}
{"风格修饰：" + style if style else ""}
image_prompt 需包含：主体+场景+风格+光照+构图，全英文。"""
    else:
        prompt = f"""你是一位电影视觉总监和叙事设计师。根据主题「{theme_text}」，构思一个有强烈故事感的场景，并生成 1 个英文文生图提示词。

**核心目标**：生成的图片必须是一个"故事定格"——画面捕捉了叙事中的关键瞬间，观众能感受到之前发生了什么、之后将要发生什么。

**场景构思要求**：
- 必须包含至少一个有清晰情绪/表情的角色或拟人主体
- 场景应暗示更大的世界观（通过背景细节、环境元素、光影暗示）
- 构图应创造张力或悬念（非常规角度、前景遮挡、纵深层次）
- 光影要有叙事功能（不均匀光照、明暗对比、戏剧性光束）
- 画面中应有一个视觉焦点吸引注意力，同时留有供视频展开的空间

已有标题（避免重复）：{recent_text}
{variation_section}{suggested_style}
只返回 JSON 对象，格式：{{"title":"...", "image_prompt":"..."}}
title 用中文，3-8 个字，要像电影片名一样有画面感。
image_prompt 需包含：角色描述+情绪+场景叙事+世界观细节+光照氛围+构图+镜头语言，全英文，150-300 词。"""

    for attempt in range(3):
        try:
            raw = chat_simple(prompt, temperature=0.8, max_tokens=768)
            data = parse_json_object(raw)
            title = str(data.get("title", "")).strip()
            image_prompt = str(data.get("image_prompt", "")).strip()
            if title and image_prompt:
                if style_modifier:
                    image_prompt += style_modifier
                return {"title": title, "image_prompt": image_prompt}
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("解析图片提示词失败 attempt=%s: %s", attempt + 1, e)
        except (requests.RequestException, RuntimeError) as e:
            logger.warning("调用文本 API 失败 attempt=%s: %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(min((attempt + 1) * 5, 15))
    raise RuntimeError("无法生成有效的图片提示词")


def _generate_video_prompt(image_prompt: str) -> str:
    video_num_frames = int(db.get_setting("video_num_frames", "81"))
    video_frame_rate = int(db.get_setting("video_frame_rate", "24"))
    duration_sec = round(video_num_frames / video_frame_rate, 1)
    prompt = f"""You are a cinematic video director and motion storyteller. Based on this image description:

"{image_prompt}"

Write a detailed English video motion prompt for image-to-video generation (approximately {duration_sec} seconds of footage at {video_frame_rate}fps = {video_num_frames} frames).

**Craft a vivid, focused scene that brings the still image to life in a short cinematic moment.**

Structure your prompt to describe:
- **Opening**: Establish the atmosphere. Describe how the scene comes alive from the still image. Include ambient motion (wind, light shifts, particles), the character's first subtle movements (breathing, glancing), and camera behavior.
- **Development**: Build the moment. The character or subject takes meaningful action. Environmental elements respond (light transforms, objects move, atmosphere shifts). Camera movement becomes more dynamic.
- **Resolution**: Deliver a memorable final impression. A dramatic reveal, a decisive action, or a breathtaking environmental transformation. The final frames should leave a lasting impact.

**Guidelines:**
- Describe specific, vivid motions — not abstract concepts
- Include temporal language ("gradually", "suddenly", "slowly building to")
- Specify camera movements (dolly in/out, pan left/right, crane up/down, rack focus)
- Include atmospheric changes (lighting shifts, particle effects)
- Character actions should have clear emotional intent
- Aim for 100-200 words of rich, cinematic description
- Output plain text only, no JSON, no section headers — weave it into one flowing paragraph"""

    for attempt in range(3):
        try:
            result = chat_simple(prompt, temperature=0.75, max_tokens=1024).strip()
            if result:
                return result
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("解析视频提示词失败 attempt=%s: %s", attempt + 1, e)
        except (requests.RequestException, RuntimeError) as e:
            logger.warning("调用文本 API 生成视频提示词失败 attempt=%s: %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(min((attempt + 1) * 5, 15))
    raise RuntimeError("无法生成有效的视频提示词")


def _archive_item_failure(item_id: str, step: str, error: str, theme: str) -> None:
    item = db.get_item(item_id)
    log = db.archive_item_atomic(item_id, item, step=step, error=error, theme=theme)
    event_bus.publish("log_created", log)
    event_bus.publish("item_removed", {"id": item_id})
    logger.warning("条目失败已归档 seq=%s step=%s: %s", item.get("seq"), step, error)


def _process_item(item_id: str, *, resume: bool) -> None:
    controller.current_item_id = item_id
    item = db.update_item(item_id, started_at=db._now())
    theme = db.get_setting("theme", "")
    variation_mode = db.get_setting("variation_mode", "off")
    current_step = item.get("status") or "pending"

    # 缓存上一轮已完成条目，供风格变化使用
    _last_completed = None
    if variation_mode in ("style_prompt", "style_prompt_img2img"):
        _last_completed = db.get_last_completed_item()

    try:
        if not resume or not item.get("image_prompt"):
            current_step = "generating_prompts"
            item = db.update_item(item_id, status="generating_prompts")
            _emit_item(item)

            # 构建风格变化上下文
            previous_context = None
            if _last_completed:
                previous_context = {
                    "title": _last_completed.get("title", ""),
                    "image_prompt": _last_completed.get("image_prompt", ""),
                }

            _step_start = time.monotonic()
            prompts = _generate_image_prompt(theme, previous_context=previous_context)
            _elapsed_ms = int((time.monotonic() - _step_start) * 1000)
            log_with_context(logging.INFO, "图片提示词生成完成", item_id=item_id, step="generate_image_prompt", duration_ms=_elapsed_ms)
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
            # Remix: 使用父条目图片作为 img2img 源
            remix_image_url = item.get("image_url") if item.get("parent_id") else None
            # 风格变化 img2img: 使用上轮已完成条目的图片作为参考
            variation_image_url = None
            if variation_mode == "style_prompt_img2img" and not remix_image_url and _last_completed:
                variation_image_url = _last_completed.get("image_url")
            img_ref_url = remix_image_url or variation_image_url
            image_size = db.get_setting("image_size", "1024x768")
            _step_start = time.monotonic()
            img_result = generate_image(item["image_prompt"], image_path, size=image_size, image_url=img_ref_url)
            _elapsed_ms = int((time.monotonic() - _step_start) * 1000)
            log_with_context(logging.INFO, "图片生成完成", item_id=item_id, step="generate_image", duration_ms=_elapsed_ms)
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
            _step_start = time.monotonic()
            video_prompt = _generate_video_prompt(item["image_prompt"])
            _elapsed_ms = int((time.monotonic() - _step_start) * 1000)
            log_with_context(logging.INFO, "视频提示词生成完成", item_id=item_id, step="generate_video_prompt", duration_ms=_elapsed_ms)
            item = db.update_item(item_id, video_prompt=video_prompt, status="generating_video")
            _emit_item(item)
        elif item["status"] == "generating_video_prompt":
            item = db.update_item(item_id, status="generating_video")
            _emit_item(item)

        item = db.get_item(item_id)
        video_path = Path(item.get("video_path") or db.OUTPUT_DIR / f"{item_id}.mp4")

        if video_path.exists() and item.get("video_url"):
            item = db.update_item(item_id, status="completed", video_progress=100, completed_at=db._now())
            _emit_item(item)
            return

        current_step = "generating_video"
        # video_progress=100 + 有 video_id = 视频已生成但下载未完成，不重置进度
        if not (item.get("video_id") and item.get("video_progress", 0) >= 100):
            item = db.update_item(item_id, status="generating_video", video_progress=0)
            _emit_item(item)

        video_id = item.get("video_id")
        if not video_id:
            video_width = int(db.get_setting("video_width", "1152"))
            video_height = int(db.get_setting("video_height", "768"))
            video_num_frames = int(db.get_setting("video_num_frames", "81"))
            video_frame_rate = int(db.get_setting("video_frame_rate", "24"))
            _step_start = time.monotonic()
            task = create_video_task(
                item["video_prompt"],
                image_url=item["image_url"],
                height=video_height,
                width=video_width,
                num_frames=video_num_frames,
                frame_rate=video_frame_rate,
            )
            _elapsed_ms = int((time.monotonic() - _step_start) * 1000)
            log_with_context(logging.INFO, "视频任务创建完成", item_id=item_id, step="create_video_task", duration_ms=_elapsed_ms)
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

        _step_start = time.monotonic()
        result = poll_video_result(
            video_id,
            interval=10,
            max_wait=600,
            on_progress=on_progress,
            should_stop=_should_stop_polling,
        )
        _elapsed_ms = int((time.monotonic() - _step_start) * 1000)
        log_with_context(logging.INFO, "视频轮询完成", item_id=item_id, step="poll_video_result", duration_ms=_elapsed_ms)
        remote_video_url = result.get("remixed_from_video_id")
        # 防御性 fallback：若 API 未来版本改字段名
        if not remote_video_url:
            remote_video_url = result.get("video_url") or result.get("url")
        if not remote_video_url or not isinstance(remote_video_url, str) or not remote_video_url.startswith("http"):
            raise RuntimeError(f"任务完成但未返回有效视频 URL: {result}")

        download_ok = False
        _check_disk_space(500)
        _step_start = time.monotonic()
        try:
            download_video(remote_video_url, video_path)
            local_path = str(video_path)
            download_ok = True
        except RuntimeError as e:
            logger.warning("本地下载失败，保留远程 URL: %s", e)
            local_path = item.get("video_path") or ""
        _elapsed_ms = int((time.monotonic() - _step_start) * 1000)
        log_with_context(logging.INFO, "视频下载完成", item_id=item_id, step="download_video", duration_ms=_elapsed_ms)

        # 验证本地文件是否真实存在
        local_file_exists = bool(local_path) and Path(local_path).exists()
        if not download_ok and not local_file_exists:
            logger.warning("视频下载失败且无本地文件，仅保留远程 URL")

        item = db.update_item(
            item_id,
            video_path=local_path if download_ok or local_file_exists else None,
            video_url=remote_video_url,
            video_download_ok=1 if (download_ok or local_file_exists) else 0,
            status="completed",
            video_progress=100,
            completed_at=db._now(),
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
