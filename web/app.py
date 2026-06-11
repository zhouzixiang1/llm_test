"""FastAPI Web 入口。"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import signal
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from web import db
from web.pipeline import controller, event_bus, get_running

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
async def api_items(limit: int = 100, offset: int = 0):
    items = [db.item_to_public(i) for i in db.list_items(limit=limit, offset=offset)]
    return {"items": items, "total": db.count_items()}


@app.get("/api/status")
async def api_status():
    return controller.get_status()


@app.post("/api/settings")
async def api_settings(body: SettingsBody):
    db.set_setting("theme", body.theme)
    return {"ok": True, "theme": body.theme}


@app.post("/api/stop")
async def api_stop():
    controller.request_stop()
    return {"ok": True, "message": "将在当前条目完成后停止"}


@app.post("/api/start")
async def api_start():
    controller.start()
    return {"ok": True, "message": "流水线已启动"}


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


@app.delete("/api/logs")
async def api_clear_logs():
    count = db.delete_all_failure_logs()
    event_bus.publish("logs_cleared", {})
    event_bus.publish("status_updated", controller.get_status())
    return {"ok": True, "deleted": count}


@app.delete("/api/items/{item_id}")
async def api_delete_item(item_id: str):
    _ensure_not_current(item_id)
    item = db.delete_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    if item["status"] != "completed":
        raise HTTPException(status_code=409, detail="只能删除已完成的条目")
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
