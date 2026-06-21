"""SQLite 数据层。"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("AGNES_DB_PATH", str(Path(__file__).resolve().parent / "data" / "app.db")))
OUTPUT_DIR = Path(os.environ.get("AGNES_OUTPUT_DIR", str(Path(__file__).resolve().parent / "data" / "outputs")))

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_pragmas() -> None:
    """Set SQLite PRAGMAs outside any transaction.

    These cannot be set inside a transaction (e.g. inside _connect())
    because SQLite prohibits changing safety level mid-transaction.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _set_pragmas()
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                seq INTEGER NOT NULL,
                title TEXT,
                image_prompt TEXT,
                video_prompt TEXT,
                image_path TEXT,
                image_url TEXT,
                video_path TEXT,
                video_url TEXT,
                video_id TEXT,
                video_progress INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS failure_logs (
                id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                step TEXT NOT NULL,
                title TEXT,
                image_prompt TEXT,
                video_prompt TEXT,
                error TEXT NOT NULL,
                theme TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        defaults = {
            "theme": "",
            "running": "true",
            "stop_after_item": "false",
            "image_size": "1024x768",
            "video_width": "1152",
            "video_height": "768",
            "video_num_frames": "81",
            "video_frame_rate": "24",
            "batch_limit": "0",
            "schedule_start": "",
            "schedule_end": "",
            "style_preset": "none",
            "retention_days": "0",
            "cleanup_on_startup": "false",
            "variation_mode": "style_prompt",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        _migrate_failed_items(conn)
        _run_schema_migrations(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_status ON items(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_seq ON items(seq)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_created_at ON items(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_failure_logs_created_at ON failure_logs(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_failure_logs_step ON failure_logs(step)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_completed_at ON items(completed_at)")


def _migrate_failed_items(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT * FROM items WHERE status = 'failed'"
    ).fetchall()
    for row in rows:
        item = _row_to_dict(row)
        _archive_failure_conn(
            conn,
            item,
            step=item.get("status") or "failed",
            error=item.get("error") or "未知错误",
            theme="",
        )
        conn.execute("DELETE FROM items WHERE id = ?", (item["id"],))
        remove_item_files(item["id"], item.get("image_path"), item.get("video_path"))


# ── Schema 迁移系统 ─────────────────────────────────────────────────────────

SCHEMA_VERSION_KEY = "schema_version"

MIGRATIONS: dict[int, Any] = {}


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """v1: 添加 video_download_ok, parent_id, started_at, completed_at 列。"""
    for col_def in [
        "ADD COLUMN video_download_ok INTEGER DEFAULT 0",
        "ADD COLUMN parent_id TEXT",
        "ADD COLUMN started_at TEXT",
        "ADD COLUMN completed_at TEXT",
    ]:
        try:
            conn.execute(f"ALTER TABLE items {col_def}")
        except sqlite3.OperationalError:
            pass


MIGRATIONS[1] = _migrate_v1


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """v2: 创建 prompt_templates 表。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prompt_templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            image_prompt_template TEXT,
            video_prompt_template TEXT,
            style_modifiers TEXT,
            is_default BOOLEAN DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


MIGRATIONS[2] = _migrate_v2


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v3: 添加 tags, favorite, rating 列。"""
    for col_def in [
        "ADD COLUMN tags TEXT DEFAULT ''",
        "ADD COLUMN favorite INTEGER DEFAULT 0",
        "ADD COLUMN rating INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(f"ALTER TABLE items {col_def}")
        except sqlite3.OperationalError:
            pass


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """v4: 创建 webhooks 表。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS webhooks (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            events TEXT NOT NULL,
            secret TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


MIGRATIONS[4] = _migrate_v4


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """v5: 添加 variation_mode 设置（风格变化模式）。"""
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ("variation_mode", "style_prompt"),
    )


MIGRATIONS[5] = _migrate_v5


def _migrate_v6(conn: sqlite3.Connection) -> None:
    """v6: 修复 video_num_frames 默认值，确保不超过 720p 最大帧数 409 且满足 8n+1。"""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'video_num_frames'"
    ).fetchone()
    if row:
        val = int(row["value"])
        cap = min(val, 409)
        n = (cap - 1) // 8
        corrected = 8 * n + 1
        if corrected != val:
            conn.execute(
                "UPDATE settings SET value = ? WHERE key = 'video_num_frames'",
                (str(corrected),),
            )


MIGRATIONS[6] = _migrate_v6


MIGRATIONS[3] = _migrate_v3


def _run_schema_migrations(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SCHEMA_VERSION_KEY,)
    ).fetchone()
    version = int(row["value"]) if row else 0
    for ver in sorted(MIGRATIONS.keys()):
        if ver > version:
            logger.info("执行 schema 迁移 v%d", ver)
            MIGRATIONS[ver](conn)
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (SCHEMA_VERSION_KEY, str(ver)),
            )


@contextmanager
def _connect():
    with _lock:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def get_setting(key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_settings() -> dict[str, str]:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def create_item_from_failure(log: dict[str, Any]) -> dict[str, Any]:
    """从失败日志创建新条目，恢复已有的提示词以跳过已完成的步骤。"""
    item_id = str(uuid.uuid4())
    now = _now()
    # 有图片提示词说明该步骤已成功，从生成图片阶段恢复；否则从头开始
    status = "generating_image" if log.get("image_prompt") else "pending"
    with _connect() as conn:
        seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM items").fetchone()
        seq = seq_row["next_seq"]
        conn.execute(
            """
            INSERT INTO items (id, seq, title, image_prompt, video_prompt, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                seq,
                log.get("title"),
                log.get("image_prompt"),
                log.get("video_prompt"),
                status,
                now,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_dict(row)


def create_item_from_remix(parent_id: str, parent: dict[str, Any]) -> dict[str, Any]:
    """从已完成条目创建 Remix 条目，保留父条目的图片用于 img2img。"""
    item_id = str(uuid.uuid4())
    now = _now()
    title = f"Remix: {parent.get('title', '')}"
    image_prompt = parent.get("image_prompt", "")
    image_url = parent.get("image_url", "")
    status = "generating_image"
    with _connect() as conn:
        seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM items").fetchone()
        seq = seq_row["next_seq"]
        conn.execute(
            """
            INSERT INTO items (id, seq, title, image_prompt, image_url, parent_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (item_id, seq, title, image_prompt, image_url, parent_id, status, now, now),
        )
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_dict(row)


def create_item() -> dict[str, Any]:
    item_id = str(uuid.uuid4())
    now = _now()
    with _connect() as conn:
        seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM items").fetchone()
        seq = seq_row["next_seq"]
        conn.execute(
            """
            INSERT INTO items (id, seq, status, created_at, updated_at)
            VALUES (?, ?, 'pending', ?, ?)
            """,
            (item_id, seq, now, now),
        )
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_dict(row)


def update_item(item_id: str, **fields: Any) -> dict[str, Any]:
    if not fields:
        return get_item(item_id)
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [item_id]
    with _connect() as conn:
        conn.execute(f"UPDATE items SET {cols} WHERE id = ?", values)
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_dict(row)


def get_item(item_id: str) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise KeyError(item_id)
        return _row_to_dict(row)


def list_items(limit: int = 100, offset: int = 0, search: str = "", status: str = "") -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: list[Any] = []
    if search:
        conditions.append("(title LIKE ? OR image_prompt LIKE ? OR video_prompt LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if status:
        conditions.append("status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM items{where} ORDER BY seq DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def count_items(search: str = "", status: str = "") -> int:
    conditions: list[str] = []
    params: list[Any] = []
    if search:
        conditions.append("(title LIKE ? OR image_prompt LIKE ? OR video_prompt LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])
    if status:
        conditions.append("status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    with _connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM items{where}", params).fetchone()
        return row["c"]


def get_incomplete_item() -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM items
            WHERE status NOT IN ('completed', 'failed')
            ORDER BY seq ASC
            LIMIT 1
            """
        ).fetchone()
        return _row_to_dict(row) if row else None


def get_recent_titles(limit: int = 20) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT title FROM items
            WHERE title IS NOT NULL AND title != ''
            ORDER BY seq DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [row["title"] for row in rows]


def get_last_completed_item() -> dict[str, Any] | None:
    """返回最近一条已完成的条目，用于风格变化上下文。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, title, image_prompt, image_url FROM items "
            "WHERE status = 'completed' AND image_prompt IS NOT NULL "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return _row_to_dict(row) if row else None


def item_to_public(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    for path_key in ("image_path", "video_path"):
        path = result.get(path_key)
        if not path:
            continue
        p = Path(path)
        if not p.is_absolute():
            p = OUTPUT_DIR / p
        if p.exists():
            try:
                rel = p.relative_to(OUTPUT_DIR)
            except ValueError:
                rel = p.name
            result[path_key.replace("_path", "_media")] = f"/media/{rel.as_posix()}"
    return result


def remove_item_files(
    item_id: str,
    image_path: str | None = None,
    video_path: str | None = None,
) -> None:
    paths: list[Path] = []
    if image_path:
        paths.append(Path(image_path))
    else:
        paths.append(OUTPUT_DIR / f"{item_id}.png")
    if video_path:
        paths.append(Path(video_path))
    else:
        paths.append(OUTPUT_DIR / f"{item_id}.mp4")
    for p in paths:
        if not p.is_absolute():
            p = OUTPUT_DIR / p.name
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


def archive_item_atomic(
    item_id: str,
    item: dict[str, Any],
    *,
    step: str,
    error: str,
    theme: str = "",
) -> dict[str, Any]:
    """原子归档：单个事务完成 archive + delete，事务后再删文件。"""
    with _connect() as conn:
        log = _archive_failure_conn(conn, item, step=step, error=error, theme=theme)
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    remove_item_files(item_id, item.get("image_path"), item.get("video_path"))
    return log


def _archive_failure_conn(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    *,
    step: str,
    error: str,
    theme: str,
) -> dict[str, Any]:
    log_id = str(uuid.uuid4())
    now = _now()
    conn.execute(
        """
        INSERT INTO failure_logs (
            id, item_id, seq, step, title, image_prompt, video_prompt, error, theme, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            log_id,
            item["id"],
            item["seq"],
            step,
            item.get("title"),
            item.get("image_prompt"),
            item.get("video_prompt"),
            error,
            theme,
            now,
        ),
    )
    row = conn.execute("SELECT * FROM failure_logs WHERE id = ?", (log_id,)).fetchone()
    return _row_to_dict(row)


def archive_failure(
    item: dict[str, Any],
    *,
    step: str,
    error: str,
    theme: str = "",
) -> dict[str, Any]:
    with _connect() as conn:
        log = _archive_failure_conn(conn, item, step=step, error=error, theme=theme)
        return log


def list_failure_logs(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM failure_logs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def count_failure_logs() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM failure_logs").fetchone()
        return row["c"]


def get_failure_log(log_id: str) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM failure_logs WHERE id = ?", (log_id,)).fetchone()
        if not row:
            raise KeyError(log_id)
        return _row_to_dict(row)


def delete_failure_log(log_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM failure_logs WHERE id = ?", (log_id,))


def delete_all_failure_logs() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM failure_logs").fetchone()
        count = row["c"]
        conn.execute("DELETE FROM failure_logs")
        return count


def delete_item(item_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return None
        item = _row_to_dict(row)
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        return item


def delete_completed_items() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM items WHERE status = 'completed'").fetchall()
        items = [_row_to_dict(r) for r in rows]
        conn.execute("DELETE FROM items WHERE status = 'completed'")
        return items


def delete_items_batch(item_ids: list[str]) -> list[dict[str, Any]]:
    """批量删除 item 及其文件。"""
    deleted: list[dict[str, Any]] = []
    with _connect() as conn:
        for item_id in item_ids:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
            if row:
                item = _row_to_dict(row)
                conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
                deleted.append(item)
    for item in deleted:
        remove_item_files(item["id"], item.get("image_path"), item.get("video_path"))
    return deleted


def update_item_rating(item_id: str, rating: int) -> dict[str, Any]:
    return update_item(item_id, rating=max(0, min(5, rating)))


def toggle_item_favorite(item_id: str) -> dict[str, Any]:
    item = get_item(item_id)
    return update_item(item_id, favorite=0 if item.get("favorite") else 1)


# ── 提示词模板 CRUD ──────────────────────────────────────────────────────


def create_template(
    *,
    name: str,
    image_prompt_template: str = "",
    video_prompt_template: str = "",
    style_modifiers: str = "",
    is_default: bool = False,
) -> dict[str, Any]:
    tmpl_id = str(uuid.uuid4())
    now = _now()
    with _connect() as conn:
        if is_default:
            conn.execute("UPDATE prompt_templates SET is_default = 0 WHERE is_default = 1")
        conn.execute(
            """
            INSERT INTO prompt_templates
                (id, name, image_prompt_template, video_prompt_template, style_modifiers, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tmpl_id, name, image_prompt_template, video_prompt_template, style_modifiers, int(is_default), now, now),
        )
        row = conn.execute("SELECT * FROM prompt_templates WHERE id = ?", (tmpl_id,)).fetchone()
        return _row_to_dict(row)


def list_templates() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM prompt_templates ORDER BY is_default DESC, updated_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_template(template_id: str) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM prompt_templates WHERE id = ?", (template_id,)).fetchone()
        if not row:
            raise KeyError(template_id)
        return _row_to_dict(row)


def update_template(template_id: str, **fields: Any) -> dict[str, Any]:
    if not fields:
        return get_template(template_id)
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [template_id]
    with _connect() as conn:
        if fields.get("is_default"):
            conn.execute("UPDATE prompt_templates SET is_default = 0 WHERE is_default = 1")
        conn.execute(f"UPDATE prompt_templates SET {cols} WHERE id = ?", values)
        row = conn.execute("SELECT * FROM prompt_templates WHERE id = ?", (template_id,)).fetchone()
        return _row_to_dict(row)


def delete_template(template_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM prompt_templates WHERE id = ?", (template_id,))


def get_default_template() -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM prompt_templates WHERE is_default = 1 LIMIT 1"
        ).fetchone()
        return _row_to_dict(row) if row else None


def set_default_template(template_id: str) -> dict[str, Any]:
    with _connect() as conn:
        conn.execute("UPDATE prompt_templates SET is_default = 0 WHERE is_default = 1")
        now = _now()
        conn.execute(
            "UPDATE prompt_templates SET is_default = 1, updated_at = ? WHERE id = ?",
            (now, template_id),
        )
        row = conn.execute("SELECT * FROM prompt_templates WHERE id = ?", (template_id,)).fetchone()
        if not row:
            raise KeyError(template_id)
        return _row_to_dict(row)


# ── 生成分析 ────────────────────────────────────────────────────────────


def get_analytics() -> dict[str, Any]:
    """计算生成分析统计。"""
    with _connect() as conn:
        completed_row = conn.execute(
            "SELECT COUNT(*) AS c FROM items WHERE status = 'completed'"
        ).fetchone()
        total_completed = completed_row["c"]

        failed_row = conn.execute("SELECT COUNT(*) AS c FROM failure_logs").fetchone()
        total_failed = failed_row["c"]

        total = total_completed + total_failed
        success_rate = round((total_completed / total * 100), 1) if total > 0 else 0.0

        avg_row = conn.execute(
            """
            SELECT AVG(
                (julianday(completed_at) - julianday(started_at)) * 86400
            ) AS avg_seconds
            FROM items
            WHERE status = 'completed' AND started_at IS NOT NULL AND completed_at IS NOT NULL
            """
        ).fetchone()
        avg_seconds = avg_row["avg_seconds"]
        avg_generation_time = round(avg_seconds, 1) if avg_seconds else 0.0

        daily_rows = conn.execute(
            """
            SELECT DATE(created_at) AS day, COUNT(*) AS count
            FROM items
            WHERE status = 'completed'
              AND created_at >= DATE('now', '-6 days')
            GROUP BY DATE(created_at)
            ORDER BY day
            """
        ).fetchall()
        daily_counts = {row["day"]: row["count"] for row in daily_rows}

        return {
            "total_completed": total_completed,
            "total_failed": total_failed,
            "success_rate": success_rate,
            "avg_generation_time": avg_generation_time,
            "daily_counts": daily_counts,
        }


# ── Webhook CRUD ────────────────────────────────────────────────────────────


def create_webhook(url: str, events: str, secret: str = "") -> dict[str, Any]:
    webhook_id = str(uuid.uuid4())
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO webhooks (id, url, events, secret, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (webhook_id, url, events, secret, now, now),
        )
        row = conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
        return _row_to_dict(row)


def list_webhooks() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM webhooks ORDER BY created_at DESC").fetchall()
        return [_row_to_dict(r) for r in rows]


def get_webhook(webhook_id: str) -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
        if not row:
            raise KeyError(webhook_id)
        return _row_to_dict(row)


def update_webhook(webhook_id: str, **fields: Any) -> dict[str, Any]:
    if not fields:
        return get_webhook(webhook_id)
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [webhook_id]
    with _connect() as conn:
        conn.execute(f"UPDATE webhooks SET {cols} WHERE id = ?", values)
        row = conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone()
        if not row:
            raise KeyError(webhook_id)
        return _row_to_dict(row)


def delete_webhook(webhook_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,))


def list_active_webhooks() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM webhooks WHERE active = 1").fetchall()
        return [_row_to_dict(r) for r in rows]


# ── 自动清理 ────────────────────────────────────────────────────────────────


def cleanup_old_items(days: int) -> list[dict[str, Any]]:
    """删除 status='completed' 且 completed_at 早于指定天数的条目及其文件。"""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM items
            WHERE status = 'completed'
              AND completed_at IS NOT NULL
              AND completed_at < datetime('now', ? || ' days')
            """,
            (f"-{days}",),
        ).fetchall()
        items = [_row_to_dict(r) for r in rows]
        conn.execute(
            """
            DELETE FROM items
            WHERE status = 'completed'
              AND completed_at IS NOT NULL
              AND completed_at < datetime('now', ? || ' days')
            """,
            (f"-{days}",),
        )
    for item in items:
        remove_item_files(item["id"], item.get("image_path"), item.get("video_path"))
    return items
