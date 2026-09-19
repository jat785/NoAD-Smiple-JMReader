"""浏览与阅读：首页随机、搜索、分类、详情、章节、图片代理。

所有端点都写成同步 ``def``，FastAPI 会自动丢进线程池执行 ——
因为 jmcomic 是同步阻塞库，这样不需要我们手写 ``to_thread``。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response

from ..services import jmclient

router = APIRouter(prefix="/api", tags=["browse"])

_IMMUTABLE = {"Cache-Control": "public, max-age=86400"}


def _validate_id(value: str, label: str) -> str:
    """jmcomic 只接受纯数字车号，提前拦掉能给出清楚的报错。"""
    text = str(value).strip()
    if not text.isdigit():
        raise HTTPException(status_code=400, detail=f"{label} 必须是数字车号，收到：{value!r}")
    return text


@router.get("/home", summary="默认页：随机 N 部漫画")
def home(count: int = Query(default=0, ge=0, le=50)) -> dict:
    items = jmclient.home_random(count or None)
    return {"items": items, "count": len(items)}


@router.get("/random", summary="再来一批随机漫画")
def random_albums(count: int = Query(default=0, ge=0, le=50)) -> dict:
    # 每次调用都重新采样（池子本身有缓存，不会额外打站点）
    items = jmclient.home_random(count or None)
    return {"items": items, "count": len(items)}


@router.get("/search", summary="搜索：关键词 / 标签 / 作者 / 作品 / 人物")
def search(
    q: str = Query(..., min_length=1, description="搜索词"),
    page: int = Query(default=1, ge=1),
    order: str = Query(default="mr", description="mr最新 mv最多观看 mp最多图片 tf最多爱心 tr评分 md评论"),
    time: str = Query(default="a", description="a全部 t日 w周 m月"),
    by: str = Query(default="site", description="site关键词 tag标签 author作者 work作品 actor人物"),
) -> dict:
    return jmclient.search(q, page=page, order=order, time_range=time, by=by)


@router.get("/categories", summary="分类树 + 标签区块")
def categories() -> dict:
    return jmclient.categories()


@router.get("/tags", summary="内置标签表（按禁漫的分组）")
def tags() -> dict:
    return jmclient.tags()


@router.get("/tag-search", summary="多标签筛选（本地做交集/并集）")
def tag_search(
    tags: str = Query(..., min_length=1, description="标签，逗号分隔"),
    mode: str = Query(default="and", description="and=同时具备 or=任一即可"),
    order: str = Query(default="mr", description="mr mv mp tf tr md"),
    time: str = Query(default="a", description="a t w m"),
    page: int = Query(default=1, ge=1),
) -> dict:
    tag_list = [t for t in tags.split(",") if t.strip()]
    try:
        return jmclient.tag_search(tag_list, mode=mode, order=order, time_range=time, page=page)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/rankings", summary="排行榜 / 分类列表")
def rankings(
    category: str = Query(default="0", description="分类 slug，0 表示全部"),
    order: str = Query(default="mv_w", description="mr mv mv_w mv_m mv_t mp tf"),
    page: int = Query(default=1, ge=1),
) -> dict:
    return jmclient.rankings(category=category, order=order, page=page)


@router.get("/lookup/{jm_id}", summary="按 JM 号（车牌号）精确定位")
def lookup(jm_id: str) -> dict:
    jid = _validate_id(jm_id, "JM 号")
    return jmclient.lookup_album(jid)


@router.get("/album/{album_id}", summary="本子详情")
def album(album_id: str) -> dict:
    return jmclient.album(_validate_id(album_id, "本子 id"))


@router.get("/chapter/{chapter_id}", summary="章节信息与页列表")
def chapter(chapter_id: str, album_id: Optional[str] = None) -> dict:
    cid = _validate_id(chapter_id, "章节 id")
    aid = _validate_id(album_id, "本子 id") if album_id else None
    return jmclient.chapter(cid, album_id=aid)


@router.get("/page/{photo_id}/{index}", summary="章节单页图片（后端代理 + 解扰）")
def page(photo_id: str, index: int) -> Response:
    pid = _validate_id(photo_id, "章节 id")
    try:
        data, content_type = jmclient.page_bytes(pid, index)
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(content=data, media_type=content_type, headers=_IMMUTABLE)


@router.get("/cover/{album_id}", summary="封面图（后端代理）")
def cover(album_id: str) -> Response:
    aid = _validate_id(album_id, "本子 id")
    data, content_type = jmclient.cover_bytes(aid)
    return Response(content=data, media_type=content_type, headers=_IMMUTABLE)
