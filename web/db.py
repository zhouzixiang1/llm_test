"""SQLite 数据层。"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent / "data" / "app.db"
OUTPUT_DIR = Path(__file__).resolve().parent / "data" / "outputs"

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
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
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        _migrate_failed_items(conn)


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


def list_items(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM items ORDER BY seq DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def count_items() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()
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
