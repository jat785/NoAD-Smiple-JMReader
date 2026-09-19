"""本地观看历史。

历史完全保存在本地 SQLite，不依赖禁漫服务端的 ``/watch_list``：

- 记录到**章节 + 页码**，因此可以"继续阅读"
- 同一部漫画只保留最近一条（打开新的章节会覆盖）
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from .. import db

router = APIRouter(prefix="/api", tags=["history"])


class HistoryBody(BaseModel):
    album_id: str
    album_title: str = ""
    album_author: str = ""
    cover_url: str = ""
    chapter_id: str = ""
    chapter_title: str = ""
    chapter_index: int = 0
    page_index: int = 1
    page_count: int = 0


@router.get("/history", summary="观看历史列表")
def list_history(limit: int = Query(default=60, ge=1, le=500), offset: int = Query(default=0, ge=0)) -> dict:
    items = db.list_history(limit=limit, offset=offset)
    return {"items": items, "count": len(items)}


@router.get("/history/{album_id}", summary="某部漫画的阅读进度")
def get_history(album_id: str) -> dict:
    record = db.get_history(album_id)
    return {"item": record}


@router.post("/history", summary="上报阅读进度")
def report_history(body: HistoryBody) -> dict:
    db.upsert_history(
        album_id=body.album_id,
        album_title=body.album_title,
        album_author=body.album_author,
        cover_url=body.cover_url,
        chapter_id=body.chapter_id,
        chapter_title=body.chapter_title,
        chapter_index=body.chapter_index,
        page_index=body.page_index,
        page_count=body.page_count,
    )
    return {"ok": True}


@router.delete("/history/{album_id}", summary="删除一条历史")
def delete_history(album_id: str) -> dict:
    db.delete_history(album_id)
    return {"ok": True}


@router.delete("/history", summary="清空历史")
def clear_history() -> dict:
    db.clear_history()
    return {"ok": True}
