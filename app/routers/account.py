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


class SaveCredsBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


@router.get("/account", summary="当前登录状态")
def account() -> dict:
    """登录状态。

    ``logged_in`` 只代表本地存着一份会话，**不代表服务端还认它**；
    服务端口径见 ``expired``（某次真实请求拿到 401 之后才会置上）。

    ``auto_relogin`` 是"记住密码"的**实际能力** —— 开关开着且真的存着密码才为真。
    只有一个开关位而没密码时它是 false，界面据此提示需要重新登录一次。

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
    """开关「记住密码」。

    返回 ``active`` 表示**是否真的生效**。请求打开、但没有已保存的密码时
    ``active`` 为 false 并给出 ``reason`` —— 因为密码只能来自登录那一次，
    光翻一个开关是没有用的。
    """
    return {"ok": True, **jmclient.toggle_remember(body.enabled)}


@router.post("/credentials", summary="为已登录的账号补存密码（开启「记住密码」用）")
def save_credentials(body: SaveCredsBody) -> dict:
    """已登录用户想开启「记住密码」时用。

    会**先验证这对凭据确实能登录**再保存 —— 存一对错密码只会让后续
    自动重登白白失败并触发退避，还会白白多打一次登录请求。
    """
    if not jmclient.is_logged_in():
        raise HTTPException(status_code=401, detail="请先登录禁漫账号")

    try:
        jmclient.login(body.username, body.password)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"账号或密码不正确：{exc}") from exc

    if not jmclient.save_credentials(body.username, body.password):
        raise HTTPException(status_code=400, detail="当前平台无法安全保存密码")
    return {"ok": True, "active": True}


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
