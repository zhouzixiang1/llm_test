"""FastAPI Web 入口。"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import shutil
import signal
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from web import db
from web.pipeline import controller, event_bus, get_running
from agnes_client import api_circuit_breaker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

_prev_sigint = None
_prev_sigterm = None


def _handle_exit_signal(signum, frame) -> None:
    logger.info("收到退出信号 (%s)，正在关闭连接...", signum)
    controller.shutdown()
    prev = _prev_sigint if signum == signal.SIGINT else _prev_sigterm
    if callable(prev):
        prev(signum, frame)


class SettingsBody(BaseModel):
    theme: str = ""
    image_size: str = "1024x768"
    video_width: str = "1152"
    video_height: str = "768"
    video_num_frames: str = "81"
    video_frame_rate: str = "24"
    batch_limit: str = "0"
    schedule_start: str = ""
    schedule_end: str = ""
    style_preset: str = "none"
    retention_days: str = "0"
    cleanup_on_startup: str = "false"
    variation_mode: str = "style_prompt"


class TemplateBody(BaseModel):
    name: str
    image_prompt_template: str = ""
    video_prompt_template: str = ""
    style_modifiers: str = ""
    is_default: bool = False


class BatchBody(BaseModel):
    item_ids: list[str]
    action: str = "delete"


class WebhookBody(BaseModel):
    url: str
    events: str
    secret: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _prev_sigint, _prev_sigterm
    _prev_sigint = signal.getsignal(signal.SIGINT)
    _prev_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _handle_exit_signal)
    signal.signal(signal.SIGTERM, _handle_exit_signal)

    controller.recover_on_startup()
    if get_running():
        controller.start()
        logger.info("流水线已自动启动")
    else:
        logger.info("流水线处于停止状态，等待手动启动")
    yield
    controller.shutdown()
    logger.info("服务已关闭")


app = FastAPI(title="Agnes Pipeline", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/media", StaticFiles(directory=str(db.OUTPUT_DIR)), name="media")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/items")
async def api_items(limit: int = 100, offset: int = 0, search: str = "", status: str = ""):
    items = [db.item_to_public(i) for i in db.list_items(limit=limit, offset=offset, search=search, status=status)]
    return {"items": items, "total": db.count_items(search=search, status=status)}


@app.get("/api/status")
async def api_status():
    return controller.get_status()


@app.get("/api/health")
async def api_health():
    issues: list[str] = []
    result: dict = {"status": "ok"}

    # DB check
    try:
        db.count_items()
        result["db"] = "ok"
    except Exception as e:
        result["db"] = f"error: {e}"
        issues.append(f"db: {e}")

    # Disk check
    try:
        usage = shutil.disk_usage(db.OUTPUT_DIR)
        disk_free_mb = round(usage.free / (1024 * 1024), 1)
        result["disk_free_mb"] = disk_free_mb
        if disk_free_mb < 500:
            issues.append(f"disk_low: {disk_free_mb}MB free")
    except Exception as e:
        result["disk_free_mb"] = -1
        issues.append(f"disk: {e}")

    # Circuit breaker check
    cb_status = api_circuit_breaker.get_status()
    result["circuit_breaker"] = cb_status
    if cb_status["state"] != "closed":
        issues.append(f"circuit_breaker: {cb_status['state']}")

    if issues:
        result["status"] = "degraded"
        result["issues"] = issues
        return JSONResponse(content=result, status_code=503)

    return result


@app.post("/api/settings")
async def api_settings(body: SettingsBody):
    for key in (
        "theme", "image_size", "video_width", "video_height",
        "video_num_frames", "video_frame_rate",
        "batch_limit", "schedule_start", "schedule_end",
        "style_preset", "retention_days", "cleanup_on_startup", "variation_mode",
    ):
        value = getattr(body, key, None)
        if value is not None:
            db.set_setting(key, value)
    all_settings = db.get_settings()
    event_bus.publish("status_updated", controller.get_status())
    return {"ok": True, **{k: all_settings.get(k, "") for k in (
        "theme", "image_size", "video_width", "video_height",
        "video_num_frames", "video_frame_rate",
        "batch_limit", "schedule_start", "schedule_end",
        "style_preset", "retention_days", "cleanup_on_startup", "variation_mode",
    )}}


@app.post("/api/stop")
async def api_stop():
    controller.request_stop()
    return {"ok": True, "message": "将在当前条目完成后停止"}


@app.post("/api/start")
async def api_start():
    controller.start()
    return {"ok": True, "message": "流水线已启动"}


# ── Webhook 端点 ────────────────────────────────────────────────────────────


@app.get("/api/webhooks")
async def api_list_webhooks():
    return {"webhooks": db.list_webhooks()}


@app.post("/api/webhooks")
async def api_create_webhook(body: WebhookBody):
    wh = db.create_webhook(url=body.url, events=body.events, secret=body.secret)
    return {"ok": True, "webhook": wh}


@app.put("/api/webhooks/{webhook_id}")
async def api_update_webhook(webhook_id: str, body: WebhookBody):
    try:
        db.get_webhook(webhook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Webhook 不存在")
    wh = db.update_webhook(webhook_id, url=body.url, events=body.events, secret=body.secret)
    return {"ok": True, "webhook": wh}


@app.delete("/api/webhooks/{webhook_id}")
async def api_delete_webhook(webhook_id: str):
    try:
        db.get_webhook(webhook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Webhook 不存在")
    db.delete_webhook(webhook_id)
    return {"ok": True}


@app.post("/api/webhooks/{webhook_id}/test")
async def api_test_webhook(webhook_id: str):
    try:
        wh = db.get_webhook(webhook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Webhook 不存在")
    import hmac
    import hashlib
    from datetime import datetime, timezone
    body = json.dumps(
        {"event": "test", "data": {"message": "测试事件"}, "timestamp": datetime.now(timezone.utc).isoformat()},
        ensure_ascii=False,
    )
    headers = {"Content-Type": "application/json"}
    if wh.get("secret"):
        sig = hmac.new(
            wh["secret"].encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["X-Webhook-Signature"] = sig
    try:
        import requests as _requests
        resp = _requests.post(wh["url"], data=body.encode("utf-8"), headers=headers, timeout=10)
        return {"ok": True, "status_code": resp.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── 手动清理 ────────────────────────────────────────────────────────────────


@app.post("/api/cleanup")
async def api_cleanup():
    retention_days = int(db.get_setting("retention_days", "0"))
    if retention_days <= 0:
        return {"ok": True, "deleted": 0, "message": "保留策略已禁用（retention_days=0）"}
    deleted = db.cleanup_old_items(retention_days)
    event_bus.publish("status_updated", controller.get_status())
    return {"ok": True, "deleted": len(deleted)}


def _ensure_not_current(item_id: str) -> None:
    if controller.current_item_id == item_id:
        raise HTTPException(status_code=409, detail="无法删除正在处理的条目")


@app.get("/api/logs")
async def api_logs(limit: int = 100, offset: int = 0):
    logs = db.list_failure_logs(limit=limit, offset=offset)
    return {"logs": logs, "total": db.count_failure_logs()}


@app.delete("/api/logs/{log_id}")
async def api_delete_log(log_id: str):
    try:
        db.get_failure_log(log_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="日志不存在")
    db.delete_failure_log(log_id)
    event_bus.publish("log_deleted", {"id": log_id})
    event_bus.publish("status_updated", controller.get_status())
    return {"ok": True}


@app.post("/api/logs/{log_id}/retry")
async def api_retry_log(log_id: str):
    try:
        log = db.get_failure_log(log_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="日志不存在")
    item = db.create_item_from_failure(log)
    db.delete_failure_log(log_id)
    event_bus.publish("log_deleted", {"id": log_id})
    event_bus.publish("item_created", db.item_to_public(item))
    event_bus.publish("status_updated", controller.get_status())
    if not get_running():
        db.set_setting("running", "true")
        controller.start()
    return {"ok": True, "item_id": item["id"]}


@app.delete("/api/logs")
async def api_clear_logs():
    count = db.delete_all_failure_logs()
    event_bus.publish("logs_cleared", {})
    event_bus.publish("status_updated", controller.get_status())
    return {"ok": True, "deleted": count}


@app.post("/api/items/{item_id}/remix")
async def api_remix_item(item_id: str):
    try:
        parent = db.get_item(item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="条目不存在")
    if parent.get("status") != "completed" and not parent.get("image_url"):
        raise HTTPException(status_code=409, detail="只能对已完成（或有图片）的条目进行 Remix")
    item = db.create_item_from_remix(item_id, parent)
    event_bus.publish("item_created", db.item_to_public(item))
    event_bus.publish("status_updated", controller.get_status())
    if not get_running():
        db.set_setting("running", "true")
        controller.start()
    return {"ok": True, "item_id": item["id"]}


@app.delete("/api/items/{item_id}")
async def api_delete_item(item_id: str):
    _ensure_not_current(item_id)
    try:
        item = db.get_item(item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="条目不存在")
    if item["status"] != "completed":
        raise HTTPException(status_code=409, detail="只能删除已完成的条目")
    db.delete_item(item_id)
    db.remove_item_files(item_id, item.get("image_path"), item.get("video_path"))
    event_bus.publish("item_removed", {"id": item_id})
    event_bus.publish("status_updated", controller.get_status())
    return {"ok": True}


@app.delete("/api/items/completed")
async def api_delete_completed():
    items = db.delete_completed_items()
    for item in items:
        db.remove_item_files(item["id"], item.get("image_path"), item.get("video_path"))
    event_bus.publish("items_cleared", {"count": len(items)})
    event_bus.publish("status_updated", controller.get_status())
    return {"ok": True, "deleted": len(items)}


# ── 提示词模板 ──────────────────────────────────────────────────────────


@app.get("/api/templates")
async def api_list_templates():
    return {"templates": db.list_templates()}


@app.post("/api/templates")
async def api_create_template(body: TemplateBody):
    tmpl = db.create_template(
        name=body.name,
        image_prompt_template=body.image_prompt_template,
        video_prompt_template=body.video_prompt_template,
        style_modifiers=body.style_modifiers,
        is_default=body.is_default,
    )
    return {"ok": True, "template": tmpl}


@app.put("/api/templates/{template_id}")
async def api_update_template(template_id: str, body: TemplateBody):
    try:
        db.get_template(template_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="模板不存在")
    fields: dict = {"name": body.name}
    if body.image_prompt_template is not None:
        fields["image_prompt_template"] = body.image_prompt_template
    if body.video_prompt_template is not None:
        fields["video_prompt_template"] = body.video_prompt_template
    if body.style_modifiers is not None:
        fields["style_modifiers"] = body.style_modifiers
    if body.is_default is not None:
        fields["is_default"] = int(body.is_default)
    tmpl = db.update_template(template_id, **fields)
    return {"ok": True, "template": tmpl}


@app.delete("/api/templates/{template_id}")
async def api_delete_template(template_id: str):
    try:
        db.get_template(template_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="模板不存在")
    db.delete_template(template_id)
    return {"ok": True}


@app.post("/api/templates/{template_id}/default")
async def api_set_default_template(template_id: str):
    try:
        tmpl = db.set_default_template(template_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="模板不存在")
    return {"ok": True, "template": tmpl}


# ── 生成分析 ────────────────────────────────────────────────────────────


@app.get("/api/analytics")
async def api_analytics():
    return db.get_analytics()


# ── 批量操作 ─────────────────────────────────────────────────────────────


@app.post("/api/items/batch")
async def api_batch_items(body: BatchBody):
    if body.action == "delete":
        deleted = db.delete_items_batch(body.item_ids)
        for item in deleted:
            event_bus.publish("item_removed", {"id": item["id"]})
        event_bus.publish("status_updated", controller.get_status())
        return {"ok": True, "deleted": len(deleted)}
    raise HTTPException(status_code=400, detail=f"未知操作: {body.action}")


# ── 评分与收藏 ────────────────────────────────────────────────────────────


@app.post("/api/items/{item_id}/rate")
async def api_rate_item(item_id: str, rating: int = 0):
    try:
        item = db.update_item_rating(item_id, rating)
    except KeyError:
        raise HTTPException(status_code=404, detail="条目不存在")
    event_bus.publish("item_updated", db.item_to_public(item))
    return {"ok": True}


@app.post("/api/items/{item_id}/favorite")
async def api_toggle_favorite(item_id: str):
    try:
        item = db.toggle_item_favorite(item_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="条目不存在")
    event_bus.publish("item_updated", db.item_to_public(item))
    return {"ok": True, "favorite": item.get("favorite", 0)}


# ── 导出 ──────────────────────────────────────────────────────────────────


@app.get("/api/export/csv")
async def api_export_csv():
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "seq", "title", "image_prompt", "video_prompt", "status", "created_at", "rating", "favorite"])
    for item in db.list_items(limit=10000):
        writer.writerow([
            item["id"], item["seq"], item.get("title", ""),
            item.get("image_prompt", ""), item.get("video_prompt", ""),
            item["status"], item.get("created_at", ""),
            item.get("rating", 0), item.get("favorite", 0),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=agnes-export.csv"},
    )


@app.get("/api/export/zip")
async def api_export_zip():
    import io
    import zipfile
    import json as _json
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        items = db.list_items(limit=10000)
        zf.writestr("metadata.json", _json.dumps(items, ensure_ascii=False, indent=2, default=str))
        for item in items:
            for key in ("image_path", "video_path"):
                path = item.get(key)
                if not path:
                    continue
                p = Path(path) if Path(path).is_absolute() else db.OUTPUT_DIR / Path(path).name
                if p.exists():
                    zf.write(p, f"media/{p.name}")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=agnes-export.zip"},
    )


@app.get("/api/events")
async def api_events(request: Request):
    q = event_bus.subscribe()

    async def stream():
        try:
            status = controller.get_status()
            yield f"event: status_updated\ndata: {json.dumps(status, ensure_ascii=False)}\n\n"
            while not controller.is_shutting_down():
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(
                        asyncio.to_thread(q.get, True, 1),
                        timeout=2,
                    )
                except (asyncio.TimeoutError, queue.Empty):
                    yield ": keepalive\n\n"
                    continue
                if payload is None:
                    break
                event = payload["event"]
                data = json.dumps(payload.get("data", {}), ensure_ascii=False)
                yield f"event: {event}\ndata: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            event_bus.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web.app:app", host="0.0.0.0", port=8010, reload=True)
