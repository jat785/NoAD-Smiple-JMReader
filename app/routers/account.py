"""账号与收藏夹。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import build_state
from ..services import jmclient

router = APIRouter(prefix="/api", tags=["account"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)
    # 登录时是否顺便把凭据存下来（会话过期后自动重新登录）。默认不存。
    remember: bool = False


class RememberBody(BaseModel):
    enabled: bool


@router.get("/account", summary="当前登录状态")
def account() -> dict:
    """登录状态。

    ``logged_in`` 只代表本地存着一份会话，**不代表服务端还认它**；
    服务端口径见 ``expired``（某次真实请求拿到 401 之后才会置上）。

    同时带上代码指纹与"是否需要重启"。前端据此判断自己是不是在跟一个
    没有重启的旧后端说话 —— 那种情况下新字段会莫名其妙地缺失。
    """
    data = jmclient.session_state()
    data["build"] = build_state()
    return data


@router.post("/login", summary="登录")
def login(body: LoginBody) -> dict:
    try:
        jmclient.set_remember(body.remember)
        return jmclient.login(body.username, body.password)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"登录失败：{exc}") from exc


@router.post("/logout", summary="退出登录")
def logout() -> dict:
    jmclient.logout()
    return {"ok": True}


@router.post("/remember", summary="开关「记住密码」（会话过期后自动重登）")
def remember(body: RememberBody) -> dict:
    return {"ok": True, "enabled": jmclient.set_remember(body.enabled)}


@router.get("/favorites", summary="收藏夹（需登录）")
def favorites(
    page: int = Query(default=1, ge=1),
    folder_id: str = Query(default="0"),
    order: str = Query(default="mr", description="mr 收藏时间 / mp 更新时间"),
) -> dict:
    try:
        return jmclient.favorites(page=page, folder_id=folder_id, order=order)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
