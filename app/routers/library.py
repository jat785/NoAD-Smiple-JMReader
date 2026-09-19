"""下载任务 + 本地已下载漫画库。

存储结构见 ``app/services/downloader.py``：一章一个 ZIP（ZIP_STORED，零重编码），
外加 ``metadata.json`` 作为索引。

注意：``/library/rescan`` 必须声明在 ``/library/{album_id}`` 之前，
否则 FastAPI 会把 "rescan" 当成 album_id。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

from .. import config, db
from ..services import downloader

router = APIRouter(prefix="/api", tags=["library"])

_CTYPE_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class DownloadBody(BaseModel):
    album_id: str
    chapter_ids: Optional[list[str]] = None


# ------------------------------------------------------------------ 下载任务

@router.post("/download", summary="把一部漫画加入下载队列")
def create_download(body: DownloadBody) -> dict:
    try:
        task = downloader.enqueue(body.album_id, body.chapter_ids)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"task": task}


@router.get("/download/tasks", summary="下载任务列表")
def list_tasks() -> dict:
    tasks = downloader.list_tasks()
    return {"tasks": tasks, "count": len(tasks)}


@router.delete("/download/tasks/{task_id}", summary="取消下载任务")
def cancel_task(task_id: str) -> dict:
    ok = downloader.cancel(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已结束")
    return {"ok": True}


# ------------------------------------------------------------------ 本地库

@router.get("/library", summary="已下载漫画列表")
def list_library(
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    q: str = Query(default="", description="按标题/作者过滤"),
) -> dict:
    items = db.list_downloaded_albums(limit=limit, offset=offset)
    keyword = q.strip().lower()
    if keyword:
        items = [
            it for it in items
            if keyword in (it.get("title") or "").lower()
            or keyword in (it.get("author") or "").lower()
        ]

    # 顺带数一下磁盘上有几部（只 glob 一层，很快）。
    # 有了这个数，列表就能自己发现"磁盘上有 4 部、索引里只有 1 部"，
    # 而不是静悄悄地显示空列表让人以为是 bug。
    disk_count = 0
    try:
        if config.DOWNLOAD_DIR.is_dir():
            disk_count = sum(1 for _ in config.DOWNLOAD_DIR.glob("*/metadata.json"))
    except OSError:
        pass

    return {"items": items, "count": len(items), "disk_count": disk_count}


@router.post("/library/rescan", summary="重新扫描下载目录，修复索引")
def rescan() -> dict:
    return downloader.rescan_library()


@router.get("/library/{album_id}", summary="本地已下载漫画的详情")
def library_detail(album_id: str) -> dict:
    album = db.get_downloaded_album(album_id)
    if not album:
        raise HTTPException(status_code=404, detail="本地没有这部漫画")
    chapters = db.list_downloaded_chapters(album_id)
    for ch in chapters:
        ch["pages"] = [
            f"/api/library/{album_id}/{ch['chapter_id']}/page/{i}"
            for i in range(1, int(ch.get("page_count") or 0) + 1)
        ]
    album["chapters"] = chapters
    return album


@router.delete("/library/{album_id}", summary="删除本地已下载的漫画（含文件）")
def delete_library(album_id: str) -> dict:
    if not db.get_downloaded_album(album_id):
        raise HTTPException(status_code=404, detail="本地没有这部漫画")
    downloader.delete_downloaded(album_id)
    return {"ok": True}


@router.get("/library/{album_id}/cover", summary="本地封面")
def library_cover(album_id: str) -> Response:
    album = db.get_downloaded_album(album_id)
    if not album or not album.get("cover_path"):
        raise HTTPException(status_code=404, detail="没有封面")
    path = Path(album["cover_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="封面文件不存在")
    ctype = _CTYPE_BY_SUFFIX.get(path.suffix.lower(), "image/jpeg")
    return Response(
        content=path.read_bytes(),
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/library/{album_id}/{chapter_id}/page/{index}", summary="读取本地 ZIP 里的某一页")
def library_page(album_id: str, chapter_id: str, index: int) -> Response:
    chapter = db.find_chapter(chapter_id)
    if not chapter or str(chapter["album_id"]) != str(album_id):
        raise HTTPException(status_code=404, detail="本地没有这一话")
    try:
        data, content_type, _total = downloader.read_zip_page(chapter["zip_path"], index)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
