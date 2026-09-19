"""SQLite 存储层。

三张业务表 + 一张键值表，全部放在一个 db 文件里，方便备份和迁移：

- ``history``            本地观看历史（带章节与页码，用于"继续阅读"）
- ``downloaded_album``   已下载漫画的索引（元数据 + 目录 + 体积统计）
- ``downloaded_chapter`` 已下载章节的索引（每个 zip 一行）
- ``kv``                 登录 Cookie 等零散状态
"""

from __future__ import annotations

import json
import os
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

# 实际生效的日志模式，供 /api/health 之类的诊断用
_journal_mode: str = "?"

_SELFTEST_KEY = "_journal_selftest"


def _write_read_ok(conn: sqlite3.Connection) -> bool:
    """写一行立刻读回来。读不到就说明这块盘的共享内存/文件锁不靠谱。"""
    token = os.urandom(8).hex()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (_SELFTEST_KEY, token)
        )
        conn.commit()
        row = conn.execute("SELECT v FROM kv WHERE k = ?", (_SELFTEST_KEY,)).fetchone()
        ok = row is not None and row[0] == token
        conn.execute("DELETE FROM kv WHERE k = ?", (_SELFTEST_KEY,))
        conn.commit()
        return ok
    except sqlite3.DatabaseError:
        return False


def _pick_journal_mode(conn: sqlite3.Connection) -> str:
    """挑一个在这块盘上**真的能用**的日志模式。

    默认想用 WAL，但它依赖 ``-shm`` 上的共享内存与可靠的文件锁。放在网络盘、
    NAS 共享目录、部分容器卷上时这两样会失效，而且是**静默**失效：

        写入确实追加进了 WAL，但读连接一直停在旧快照上。

    表现出来就是「扫描说写了 5 部，列表永远只显示 2 部」，而且
    ``downloaded_album`` 与 ``downloaded_chapter`` 的行数会对不上 ——
    看着像查询 bug，其实是提交丢了，极难排查。

    所以这里不能想当然地开 WAL：写完读回来验一次，读不到就退回不需要
    共享内存的 rollback journal。数据目录在本地盘时 WAL 依然会被选中。
    """
    forced = os.environ.get("JMREADER_SQLITE_JOURNAL", "").strip().lower()
    if forced in ("delete", "truncate", "persist", "memory"):
        conn.execute(f"PRAGMA journal_mode={forced.upper()}")
        return forced.upper()

    if forced == "wal":
        conn.execute("PRAGMA journal_mode=WAL")
        return "WAL"

    try:
        got = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        if not got or str(got[0]).lower() != "wal":
            # 盘本身就不支持 WAL，SQLite 会保持原来的模式
            return str(got[0]).upper() if got else "DELETE"
    except sqlite3.DatabaseError:
        return "DELETE"

    if _write_read_ok(conn):
        return "WAL"

    # 共享内存没起作用：退回 rollback journal（不依赖 -shm）
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.DatabaseError:
        pass
    return "DELETE"


def _connection() -> sqlite3.Connection:
    global _conn, _journal_mode
    with _lock:
        if _conn is None:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.commit()
            _journal_mode = _pick_journal_mode(conn)
            conn.execute("PRAGMA synchronous=NORMAL")
            _conn = conn
        return _conn


def journal_mode() -> str:
    """当前生效的日志模式（WAL / DELETE / …）。"""
    _connection()
    return _journal_mode


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
