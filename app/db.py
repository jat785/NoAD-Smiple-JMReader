"""SQLite 存储层。

三张业务表 + 一张键值表，全部放在一个 db 文件里，方便备份和迁移：

- ``history``            本地观看历史（带章节与页码，用于"继续阅读"）
- ``downloaded_album``   已下载漫画的索引（元数据 + 目录 + 体积统计）
- ``downloaded_chapter`` 已下载章节的索引（每个 zip 一行）
- ``kv``                 登录 Cookie 等零散状态
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    album_id      TEXT PRIMARY KEY,
    album_title   TEXT    NOT NULL DEFAULT '',
    album_author  TEXT    NOT NULL DEFAULT '',
    cover_url     TEXT    NOT NULL DEFAULT '',
    chapter_id    TEXT    NOT NULL DEFAULT '',
    chapter_title TEXT    NOT NULL DEFAULT '',
    chapter_index INTEGER NOT NULL DEFAULT 0,
    page_index    INTEGER NOT NULL DEFAULT 0,
    page_count    INTEGER NOT NULL DEFAULT 0,
    updated_at    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_history_updated ON history (updated_at DESC);

CREATE TABLE IF NOT EXISTS downloaded_album (
    album_id      TEXT PRIMARY KEY,
    title         TEXT    NOT NULL DEFAULT '',
    author        TEXT    NOT NULL DEFAULT '',
    tags_json     TEXT    NOT NULL DEFAULT '[]',
    dir_path      TEXT    NOT NULL DEFAULT '',
    cover_path    TEXT    NOT NULL DEFAULT '',
    chapter_count INTEGER NOT NULL DEFAULT 0,
    page_count    INTEGER NOT NULL DEFAULT 0,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    downloaded_at INTEGER NOT NULL DEFAULT 0,
    updated_at    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dl_album_time ON downloaded_album (downloaded_at DESC);

CREATE TABLE IF NOT EXISTS downloaded_chapter (
    album_id      TEXT    NOT NULL,
    chapter_id    TEXT    NOT NULL,
    chapter_index INTEGER NOT NULL DEFAULT 0,
    title         TEXT    NOT NULL DEFAULT '',
    zip_path      TEXT    NOT NULL DEFAULT '',
    page_count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (album_id, chapter_id)
);
CREATE INDEX IF NOT EXISTS idx_dl_chapter_album ON downloaded_chapter (album_id, chapter_index);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

_conn: Optional[sqlite3.Connection] = None
_lock = threading.RLock()


def _connection() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.executescript(_SCHEMA)
            _conn.commit()
        return _conn


def init() -> None:
    """建表（幂等）。"""
    _connection()


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        cur = _connection().execute(sql, tuple(params))
        return cur.fetchall()


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with _lock:
        conn = _connection()
        conn.execute(sql, tuple(params))
        conn.commit()


# ------------------------------------------------------------------ 键值

def kv_get(key: str, default: Optional[str] = None) -> Optional[str]:
    row = query_one("SELECT v FROM kv WHERE k = ?", (key,))
    return row["v"] if row else default


def kv_get_json(key: str, default: Any = None) -> Any:
    raw = kv_get(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def kv_set(key: str, value: Any) -> None:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    execute(
        "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, value),
    )


def kv_delete(key: str) -> None:
    execute("DELETE FROM kv WHERE k = ?", (key,))


# ------------------------------------------------------------------ 观看历史

def upsert_history(
    *,
    album_id: str,
    album_title: str = "",
    album_author: str = "",
    cover_url: str = "",
    chapter_id: str = "",
    chapter_title: str = "",
    chapter_index: int = 0,
    page_index: int = 0,
    page_count: int = 0,
) -> None:
    """记录/更新一条观看历史（同一部漫画只保留一条，覆盖式更新）。"""
    execute(
        """
        INSERT INTO history (album_id, album_title, album_author, cover_url,
                             chapter_id, chapter_title, chapter_index,
                             page_index, page_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(album_id) DO UPDATE SET
            album_title   = CASE WHEN excluded.album_title  != '' THEN excluded.album_title  ELSE history.album_title  END,
            album_author  = CASE WHEN excluded.album_author != '' THEN excluded.album_author ELSE history.album_author END,
            cover_url     = CASE WHEN excluded.cover_url    != '' THEN excluded.cover_url    ELSE history.cover_url    END,
            chapter_id    = CASE WHEN excluded.chapter_id   != '' THEN excluded.chapter_id   ELSE history.chapter_id   END,
            chapter_title = CASE WHEN excluded.chapter_title!= '' THEN excluded.chapter_title ELSE history.chapter_title END,
            chapter_index = excluded.chapter_index,
            page_index    = excluded.page_index,
            page_count    = CASE WHEN excluded.page_count != 0 THEN excluded.page_count ELSE history.page_count END,
            updated_at    = excluded.updated_at
        """,
        (
            str(album_id), album_title, album_author, cover_url,
            str(chapter_id), chapter_title, int(chapter_index),
            int(page_index), int(page_count), int(time.time()),
        ),
    )


def list_history(limit: int = 60, offset: int = 0) -> list[dict]:
    rows = query(
        "SELECT * FROM history ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        (max(1, limit), max(0, offset)),
    )
    return [dict(r) for r in rows]


def get_history(album_id: str) -> Optional[dict]:
    row = query_one("SELECT * FROM history WHERE album_id = ?", (str(album_id),))
    return dict(row) if row else None


def delete_history(album_id: str) -> None:
    execute("DELETE FROM history WHERE album_id = ?", (str(album_id),))


def clear_history() -> None:
    execute("DELETE FROM history")


# ------------------------------------------------------------------ 已下载索引

def upsert_downloaded_album(
    *,
    album_id: str,
    title: str = "",
    author: str = "",
    tags: Optional[list[str]] = None,
    dir_path: str = "",
    cover_path: str = "",
) -> None:
    execute(
        """
        INSERT INTO downloaded_album (album_id, title, author, tags_json, dir_path,
                                      cover_path, downloaded_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(album_id) DO UPDATE SET
            title      = CASE WHEN excluded.title  != '' THEN excluded.title  ELSE downloaded_album.title  END,
            author     = CASE WHEN excluded.author != '' THEN excluded.author ELSE downloaded_album.author END,
            tags_json  = excluded.tags_json,
            dir_path   = CASE WHEN excluded.dir_path   != '' THEN excluded.dir_path   ELSE downloaded_album.dir_path   END,
            cover_path = CASE WHEN excluded.cover_path != '' THEN excluded.cover_path ELSE downloaded_album.cover_path END,
            updated_at = excluded.updated_at
        """,
        (
            str(album_id), title, author,
            json.dumps(tags or [], ensure_ascii=False),
            dir_path, cover_path,
            int(time.time()), int(time.time()),
        ),
    )


def refresh_downloaded_album_stats(album_id: str) -> None:
    """根据章节表重算章节数 / 总页数 / 总体积。"""
    row = query_one(
        "SELECT COUNT(*) AS c, COALESCE(SUM(page_count), 0) AS p FROM downloaded_chapter WHERE album_id = ?",
        (str(album_id),),
    )
    chapters = int(row["c"]) if row else 0
    pages = int(row["p"]) if row else 0

    album_row = query_one("SELECT dir_path FROM downloaded_album WHERE album_id = ?", (str(album_id),))
    size = 0
    if album_row and album_row["dir_path"]:
        from pathlib import Path
        base = Path(album_row["dir_path"])
        if base.is_dir():
            for f in base.rglob("*.zip"):
                try:
                    size += f.stat().st_size
                except OSError:
                    pass

    execute(
        "UPDATE downloaded_album SET chapter_count = ?, page_count = ?, size_bytes = ?, updated_at = ? WHERE album_id = ?",
        (chapters, pages, size, int(time.time()), str(album_id)),
    )


def list_downloaded_albums(limit: int = 200, offset: int = 0) -> list[dict]:
    rows = query(
        "SELECT * FROM downloaded_album ORDER BY downloaded_at DESC LIMIT ? OFFSET ?",
        (max(1, limit), max(0, offset)),
    )
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["tags"] = json.loads(d.pop("tags_json") or "[]")
        except (TypeError, ValueError):
            d["tags"] = []
        out.append(d)
    return out


def get_downloaded_album(album_id: str) -> Optional[dict]:
    row = query_one("SELECT * FROM downloaded_album WHERE album_id = ?", (str(album_id),))
    if not row:
        return None
    d = dict(row)
    try:
        d["tags"] = json.loads(d.pop("tags_json") or "[]")
    except (TypeError, ValueError):
        d["tags"] = []
    return d


def delete_downloaded_album(album_id: str) -> None:
    execute("DELETE FROM downloaded_chapter WHERE album_id = ?", (str(album_id),))
    execute("DELETE FROM downloaded_album WHERE album_id = ?", (str(album_id),))


def upsert_downloaded_chapter(
    *,
    album_id: str,
    chapter_id: str,
    chapter_index: int = 0,
    title: str = "",
    zip_path: str = "",
    page_count: int = 0,
) -> None:
    execute(
        """
        INSERT INTO downloaded_chapter (album_id, chapter_id, chapter_index, title, zip_path, page_count)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(album_id, chapter_id) DO UPDATE SET
            chapter_index = excluded.chapter_index,
            title         = excluded.title,
            zip_path      = excluded.zip_path,
            page_count    = excluded.page_count
        """,
        (str(album_id), str(chapter_id), int(chapter_index), title, zip_path, int(page_count)),
    )


def list_downloaded_chapters(album_id: str) -> list[dict]:
    rows = query(
        "SELECT * FROM downloaded_chapter WHERE album_id = ? ORDER BY chapter_index ASC",
        (str(album_id),),
    )
    return [dict(r) for r in rows]


def find_chapter(chapter_id: str) -> Optional[dict]:
    row = query_one("SELECT * FROM downloaded_chapter WHERE chapter_id = ?", (str(chapter_id),))
    return dict(row) if row else None
