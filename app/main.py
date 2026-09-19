"""FastAPI 应用入口。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from jmcomic.jm_exception import JmcomicException

from . import __version__, config, db
from .routers import account, browse, history, library, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("jmreader")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.ensure_dirs()
    db.init()
    logger.info("数据目录：%s", config.DATA_DIR)
    logger.info("下载目录：%s", config.DOWNLOAD_DIR)
    yield


app = FastAPI(
    title="JMReader",
    description="禁漫天堂第三方阅读器 —— 漫画 only、无广告、带本地历史与下载。",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(browse.router)
app.include_router(account.router)
app.include_router(history.router)
app.include_router(library.router)
app.include_router(settings.router)


@app.middleware("http")
async def _api_response_headers(request: Request, call_next):
    """统一处理 API 响应头。

    1. 给文本类响应补 ``charset=utf-8``。

    JSON 按 RFC 8259 本来就是 UTF-8，浏览器也默认按 UTF-8 解析，
    但不少客户端（比如 PowerShell 的 Invoke-RestMethod）在没有 charset 时会
    退化成 Latin-1，把「劇情向」解成 9 个乱码字符，进而导致标签搜索搜不到东西。
    ``application/javascript`` 同理 —— 浏览器没事，脚本/命令行工具会中招。

    2. API 一律禁用缓存。

    这些都是实时数据。万一被浏览器或中间代理复用旧响应，就会变成
    「明明扫描到 4 部，列表却只有 1 部」这种极难排查的幽灵问题 ——
    现象看着像后端 bug，实际数据早就写进去了。
    """
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if "charset" not in content_type.lower() and (
        content_type.startswith("application/json")
        or content_type.startswith("application/javascript")
    ):
        response.headers["content-type"] = f"{content_type}; charset=utf-8"

    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/api/health", tags=["meta"], summary="健康检查")
def health() -> dict:
    from .services import jmclient

    return {
        "ok": True,
        "version": __version__,
        "logged_in": jmclient.is_logged_in(),
        "username": jmclient.current_user(),
        # 对禁漫的实际请求量。看这个数就知道自己给站点添了多少负担。
        "upstream": jmclient.stats(),
    }


@app.exception_handler(JmcomicException)
async def _jmcomic_error(_request: Request, exc: JmcomicException) -> JSONResponse:
    """把 jmcomic 的异常翻译成 502，前端好统一提示。"""
    logger.warning("禁漫接口异常：%s", exc)
    return JSONResponse(status_code=502, content={"detail": f"禁漫接口异常：{exc}"})


# 静态前端挂在最后，保证 /api 优先匹配。
if config.WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
else:  # pragma: no cover - 仅在前端目录缺失时
    logger.warning("前端目录不存在：%s", config.WEB_DIR)
