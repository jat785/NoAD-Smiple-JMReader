"""下载服务：单 worker 串行队列 + ZIP 存储。

**关于并发**
禁漫站点运营不易，本模块刻意保持"礼貌"：

- 同一时刻只跑 **1 个** 下载任务（``JMREADER_DOWNLOAD_CONCURRENCY``，默认 1）
- 章节按顺序下，页与页之间 sleep ``JMREADER_DOWNLOAD_INTERVAL``（默认 1.2 秒）
- 失败重试之间有退避

请不要为了"快"去调大这些值。

**存储结构**::

    data/downloads/<album_id>_<标题>/
        01_第01话.zip        # ZIP_STORED，图片原样装入，零重编码零损失
        02_第02话.zip
        metadata.json        # 我们自己读的索引（同时可用于重建数据库）
        cover.jpg
"""

from __future__ import annotations

import json
import queue
import re
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Optional

from .. import config, db
from . import jmclient

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(raw: Any, fallback: str = "untitled", maxlen: int = 80) -> str:
    text = _ILLEGAL.sub("_", str(raw or "")).strip()
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > maxlen:
        text = text[:maxlen].rstrip(" .")
    return text or fallback


# ------------------------------------------------------------------ 任务状态

_tasks: dict[str, dict] = {}
_queues: dict[str, "queue.Queue[str]"] = {}
_lock = threading.RLock()
_worker: Optional[threading.Thread] = None


def _now() -> int:
    return int(time.time())


def _snapshot(task: dict) -> dict:
    return {k: v for k, v in task.items() if not k.startswith("_")}


def list_tasks() -> list[dict]:
    with _lock:
        tasks = [_snapshot(t) for t in _tasks.values()]
    tasks.sort(key=lambda t: t.get("created_at", 0), reverse=True)
    return tasks


def get_task(task_id: str) -> Optional[dict]:
    with _lock:
        task = _tasks.get(task_id)
        return _snapshot(task) if task else None


def cancel(task_id: str) -> bool:
    with _lock:
        task = _tasks.get(task_id)
        if not task or task["status"] in {"done", "error", "canceled"}:
            return False
        task["_cancel"] = True
        if task["status"] == "queued":
            task["status"] = "canceled"
            task["message"] = "已取消（尚未开始）"
            task["finished_at"] = _now()
        return True


def enqueue(album_id: str, chapter_ids: Optional[list[str]] = None) -> dict:
    """把一部漫画（或其中几话）加入下载队列。"""
    album_id = str(album_id)

    with _lock:
        for t in _tasks.values():
            if t["album_id"] == album_id and t["status"] in {"queued", "running"}:
                raise RuntimeError("这部漫画已经在下载队列里了")

    detail = jmclient.album(album_id)
    chapters = detail["chapters"]
    if chapter_ids:
        wanted = {str(c) for c in chapter_ids}
        chapters = [c for c in chapters if c["chapter_id"] in wanted]
    if not chapters:
        raise RuntimeError("没有可下载的章节")

    task_id = uuid.uuid4().hex[:12]
    task = {
        "task_id": task_id,
        "album_id": album_id,
        "title": detail["title"],
        "author": (detail["authors"] or [""])[0],
        "cover": detail["cover"],
        "status": "queued",
        "message": "排队中",
        "total_chapters": len(chapters),
        "done_chapters": 0,
        "total_pages": 0,
        "done_pages": 0,
        "failed_chapters": [],
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "_chapters": chapters,
        "_cancel": False,
    }

    with _lock:
        _tasks[task_id] = task
        _queues.setdefault("default", queue.Queue()).put(task_id)

    _ensure_worker()
    return _snapshot(task)


# ------------------------------------------------------------------ worker

def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_loop, name="jmreader-downloader", daemon=True)
        _worker.start()


def _worker_loop() -> None:
    q = _queues.setdefault("default", queue.Queue())
    while True:
        task_id = q.get()
        try:
            with _lock:
                task = _tasks.get(task_id)
                if task is None or task["status"] == "canceled":
                    continue
                task["status"] = "running"
                task["started_at"] = _now()
                task["message"] = "准备中"
            _run_task(task_id)
        except Exception as exc:  # noqa: BLE001
            with _lock:
                task = _tasks.get(task_id)
                if task is not None:
                    task["status"] = "error"
                    task["message"] = f"{type(exc).__name__}: {exc}"
                    task["finished_at"] = _now()
        finally:
            q.task_done()


def _run_task(task_id: str) -> None:
    with _lock:
        task = _tasks[task_id]
        chapters = list(task["_chapters"])
        album_id = task["album_id"]
        title = task["title"]

    detail = jmclient.album(album_id)
    # 目录名收窄到 64 字符：Windows 的 260 字符路径上限很容易被长标题顶爆
    album_dir = config.DOWNLOAD_DIR / f"{safe_name(album_id, 'album', maxlen=16)}_{safe_name(title, maxlen=64)}"
    album_dir.mkdir(parents=True, exist_ok=True)

    done_records: list[dict] = []
    for chapter in chapters:
        if _is_canceled(task_id):
            _finish(task_id, "canceled", "已取消")
            return

        chapter_id = chapter["chapter_id"]
        chapter_index = chapter["index"]
        chapter_title = chapter["title"]
        _set_message(task_id, f"下载中：{chapter_title}")

        try:
            record = _download_chapter(
                task_id=task_id,
                album_id=album_id,
                chapter_id=chapter_id,
                chapter_index=chapter_index,
                chapter_title=chapter_title,
                album_dir=album_dir,
            )
        except _Canceled:
            _finish(task_id, "canceled", "已取消")
            return
        except Exception as exc:  # noqa: BLE001
            with _lock:
                task["failed_chapters"].append(chapter_title)
                task["message"] = f"{chapter_title} 失败：{exc}"
            done_records.append({
                "chapter_id": chapter_id,
                "index": chapter_index,
                "title": chapter_title,
                "file": "",
                "page_count": 0,
                "failed": True,
            })
            continue

        done_records.append(record)
        with _lock:
            task["done_chapters"] += 1

    # 封面（失败不影响整体）
    cover_name = ""
    try:
        data, _ctype = jmclient.cover_bytes(album_id)
        (album_dir / "cover.jpg").write_bytes(data)
        cover_name = "cover.jpg"
    except Exception:  # noqa: BLE001
        pass

    # 汇总元数据
    metadata = {
        "album_id": album_id,
        "title": title,
        "author": task.get("author", ""),
        "authors": detail.get("authors", []),
        "tags": detail.get("tags", []),
        "description": detail.get("description", ""),
        "cover": cover_name,
        "source": f"https://18comic.vip/album/{album_id}/",
        "downloaded_at": _now(),
        "chapters": done_records,
    }
    (album_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 写索引
    db.upsert_downloaded_album(
        album_id=album_id,
        title=title,
        author=task.get("author", ""),
        tags=detail.get("tags", []),
        dir_path=str(album_dir),
        cover_path=str(album_dir / cover_name) if cover_name else "",
    )
    for rec in done_records:
        if rec.get("failed") or not rec.get("file"):
            continue
        db.upsert_downloaded_chapter(
            album_id=album_id,
            chapter_id=rec["chapter_id"],
            chapter_index=rec["index"],
            title=rec["title"],
            zip_path=str(album_dir / rec["file"]),
            page_count=rec["page_count"],
        )
    db.refresh_downloaded_album_stats(album_id)

    failed = task.get("failed_chapters") or []
    if failed:
        _finish(task_id, "done", f"完成，但有 {len(failed)} 话失败")
    else:
        _finish(task_id, "done", "全部完成")


class _Canceled(Exception):
    pass


def _is_canceled(task_id: str) -> bool:
    with _lock:
        task = _tasks.get(task_id)
        return bool(task and task.get("_cancel"))


def _set_message(task_id: str, message: str) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if task:
            task["message"] = message


def _finish(task_id: str, status: str, message: str) -> None:
    with _lock:
        task = _tasks.get(task_id)
        if task:
            task["status"] = status
            task["message"] = message
            task["finished_at"] = _now()


def _download_chapter(
    *,
    task_id: str,
    album_id: str,
    chapter_id: str,
    chapter_index: int,
    chapter_title: str,
    album_dir: Path,
) -> dict:
    info = jmclient.chapter(chapter_id, album_id)
    page_count = info["page_count"]
    if page_count <= 0:
        raise RuntimeError("章节没有图片")

    zip_name = f"{chapter_index:02d}_{safe_name(chapter_title, f'chapter{chapter_index}', maxlen=48)}.zip"
    zip_path = album_dir / zip_name
    tmp_path = album_dir / (zip_name + ".part")
    tmp_dir = config.CACHE_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    with _lock:
        task = _tasks[task_id]
        task["total_pages"] += page_count

    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
            for index in range(1, page_count + 1):
                if _is_canceled(task_id):
                    raise _Canceled()

                # 先问出这一页的真实后缀：PIL 落盘靠扩展名判断格式，临时文件也必须带对
                raw_name = jmclient.page_filename(chapter_id, index)
                suffix = Path(raw_name).suffix or ".webp"
                tmp_file = tmp_dir / f"{chapter_id}_{index:05d}{suffix}"

                last_error: Optional[Exception] = None
                for attempt in range(1, config.DOWNLOAD_MAX_RETRY + 1):
                    try:
                        jmclient.save_page(chapter_id, index, str(tmp_file))
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_error = exc
                        tmp_file.unlink(missing_ok=True)
                        if attempt < config.DOWNLOAD_MAX_RETRY:
                            time.sleep(min(2.0 * attempt, 6.0))
                else:
                    raise RuntimeError(f"第 {index} 页下载失败：{last_error}")

                zf.write(tmp_file, arcname=f"{index:05d}{suffix}")
                tmp_file.unlink(missing_ok=True)

                with _lock:
                    task["done_pages"] += 1

                # 礼貌间隔
                if config.DOWNLOAD_INTERVAL > 0:
                    time.sleep(config.DOWNLOAD_INTERVAL)

        tmp_path.replace(zip_path)
    except _Canceled:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        for leftover in tmp_dir.glob(f"{chapter_id}_*"):
            leftover.unlink(missing_ok=True)

    return {
        "chapter_id": chapter_id,
        "index": chapter_index,
        "title": chapter_title,
        "file": zip_name,
        "page_count": page_count,
        "failed": False,
    }


# ------------------------------------------------------------------ 本地库维护

def delete_downloaded(album_id: str) -> None:
    """删除某部漫画的本地文件与索引。"""
    album_id = str(album_id)
    record = db.get_downloaded_album(album_id)
    if record and record.get("dir_path"):
        target = Path(record["dir_path"])
        # 只允许删 downloads 目录内的东西，防止路径被篡改
        try:
            target.resolve().relative_to(config.DOWNLOAD_DIR.resolve())
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
        except ValueError:
            pass
    db.delete_downloaded_album(album_id)


def rescan_library() -> dict:
    """扫描下载目录里的 metadata.json，重建索引。

    用户手动搬动/改名过下载目录时，用这个把数据库修复回来。
    """
    added, skipped = 0, 0
    root = config.DOWNLOAD_DIR
    if not root.is_dir():
        return {"added": 0, "skipped": 0}

    for meta_file in root.glob("*/metadata.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped += 1
            continue

        album_id = str(meta.get("album_id") or "").strip()
        if not album_id:
            skipped += 1
            continue

        album_dir = meta_file.parent
        cover_name = meta.get("cover") or ""
        db.upsert_downloaded_album(
            album_id=album_id,
            title=str(meta.get("title") or ""),
            author=str(meta.get("author") or ""),
            tags=meta.get("tags") or [],
            dir_path=str(album_dir),
            cover_path=str(album_dir / cover_name) if cover_name else "",
        )
        for rec in meta.get("chapters") or []:
            if not isinstance(rec, dict) or not rec.get("file"):
                continue
            zip_path = album_dir / str(rec["file"])
            if not zip_path.exists():
                continue
            db.upsert_downloaded_chapter(
                album_id=album_id,
                chapter_id=str(rec.get("chapter_id") or ""),
                chapter_index=int(rec.get("index") or 0),
                title=str(rec.get("title") or ""),
                zip_path=str(zip_path),
                page_count=int(rec.get("page_count") or 0),
            )
        db.refresh_downloaded_album_stats(album_id)
        added += 1
    return {"added": added, "skipped": skipped}


# ------------------------------------------------------------------ 从 ZIP 读图

def read_zip_page(zip_path: str, index: int) -> tuple[bytes, str, int]:
    """读取 ZIP 里的第 ``index`` 页（1 起）。返回 (字节, content-type, 总页数)。

    只解压需要的那一页 —— 这正是 ZIP 相对 PDF 的核心优势。
    """
    path = Path(zip_path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到章节文件：{zip_path}")

    with zipfile.ZipFile(path) as zf:
        names = sorted(n for n in zf.namelist() if not n.endswith("/"))
        total = len(names)
        if index < 1 or index > total:
            raise IndexError(f"页码超出范围: {index} / {total}")
        name = names[index - 1]
        data = zf.read(name)

    suffix = Path(name).suffix.lower()
    content_type = jmclient._CONTENT_TYPES.get(suffix, "application/octet-stream")
    return data, content_type, total
