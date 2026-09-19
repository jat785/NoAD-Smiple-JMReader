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

# 启动自检处理过的问题（已自动修复 / 修不好的），同样给诊断用
_repair_notes: list[str] = []

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


def _check_and_repair(conn: sqlite3.Connection) -> list[str]:
    """启动自检 + 自愈，返回没能修好的问题（空列表表示健康）。

    SQLite 的索引与表失去同步时，**查询不会报任何错，只是少返回数据**。
    现场遇到的就是：``idx_dl_album_time`` 少 3 条，于是

        SELECT * FROM downloaded_album ORDER BY downloaded_at DESC  ->  2 行
        SELECT * FROM downloaded_album                              ->  5 行
        SELECT COUNT(*) FROM downloaded_album                       ->  2

    同一张表、同一条连接，走索引和不走索引结果不一样。用户看到的
    「磁盘上 5 部、列表只有 2 部」正是这个 —— 数据一条没丢，是索引烂了。

    没人会往"文件损坏"上想，所以这里启动时顺手查一次，坏了就 REINDEX。
    ``quick_check`` 只做 B 树结构的快速校验，不逐页比对，开销很小。
    """
    try:
        problems = [str(r[0]) for r in conn.execute("PRAGMA quick_check")]
    except sqlite3.DatabaseError as exc:
        return [f"quick_check 执行失败：{exc}"]

    if not problems or problems == ["ok"]:
        return []

    try:
        conn.execute("REINDEX")
        conn.commit()
        after = [str(r[0]) for r in conn.execute("PRAGMA quick_check")]
    except sqlite3.DatabaseError as exc:
        return problems + [f"REINDEX 失败：{exc}"]

    if after == ["ok"]:
        # 修好了：把原始问题记下来，/api/health 里能看到曾经坏过
        return [f"已自动修复：{p}" for p in problems]
    return problems


def _scrub_legacy_plaintext(conn: sqlite3.Connection) -> list[str]:
    """清掉旧版本留在空闲页里的明文凭据。

    背景：早期版本把 ``{"username": ..., "cookies": {"AVS": ...}}`` 以**明文 JSON**
    存进 kv 表。现在读写都走密封令牌，但 SQLite 的 UPDATE/INSERT 只写新行，
    **旧行的字节仍留在数据页里**，直到那页被复用或库被重写为止。

    实测在库文件和 -wal 里都能直接搜到完整的旧明文（含可用的 AVS 令牌）。
    光把行改成密文是不够的，必须把底层字节也抹掉。

    做法：
    1. ``VACUUM`` —— 重建整个库文件，彻底丢弃空闲页
    2. ``wal_checkpoint(TRUNCATE)`` —— 把 WAL 截断，清掉里面的旧副本

    检测不只看当前行：**已经迁移过、但旧字节还留在页里的库也要清**，
    所以直接按字节找明文特征（``"cookies"`` 只可能出现在旧格式里）。
    只扫 kv 那几页的开销可以忽略，而且清理完就不再触发。
    """
    notes: list[str] = []
    marker = b'"cookies"'
    try:
        db_bytes = config.DB_PATH.read_bytes()
        wal_path = config.DB_PATH.with_name(config.DB_PATH.name + "-wal")
        wal_bytes = wal_path.read_bytes() if wal_path.exists() else b""
    except OSError:
        return notes

    if marker not in db_bytes and marker not in wal_bytes:
        return notes

    try:
        conn.execute("VACUUM")
        conn.commit()
        notes.append("已清除旧版本残留的明文登录凭据（VACUUM 重建数据库）")
    except sqlite3.DatabaseError as exc:
        notes.append(f"明文凭据清理失败：{exc}")
        return notes

    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.DatabaseError:
        pass
    return notes


def _connection() -> sqlite3.Connection:
    global _conn, _journal_mode, _repair_notes
    with _lock:
        if _conn is None:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.commit()
            # 覆盖删除：UPDATE/DELETE 留下的旧字节会在页内被清零，
            # 否则每次轮换登录凭据都会在库里留下一份"已删除"的明文。
            try:
                conn.execute("PRAGMA secure_delete=ON")
            except sqlite3.DatabaseError:
                pass
            _journal_mode = _pick_journal_mode(conn)
            conn.execute("PRAGMA synchronous=NORMAL")
            _repair_notes = _check_and_repair(conn)
            _repair_notes += _scrub_legacy_plaintext(conn)
            _conn = conn
        return _conn


def journal_mode() -> str:
    """当前生效的日志模式（WAL / DELETE / …）。"""
    _connection()
    return _journal_mode


def repair_notes() -> list[str]:
    """本次启动自检发现并处理过的问题，空列表表示一切正常。"""
    _connection()
    return list(_repair_notes)


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
