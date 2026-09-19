"""账号与收藏夹。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..services import jmclient

router = APIRouter(prefix="/api", tags=["account"])


class LoginBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


@router.get("/account", summary="当前登录状态")
def account() -> dict:
    return {"logged_in": jmclient.is_logged_in(), "username": jmclient.current_user()}


@router.post("/login", summary="登录（Cookie 会持久化到本地）")
def login(body: LoginBody) -> dict:
    try:
        return jmclient.login(body.username, body.password)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"登录失败：{exc}") from exc


@router.post("/logout", summary="退出登录")
def logout() -> dict:
    jmclient.logout()
    return {"ok": True}


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
