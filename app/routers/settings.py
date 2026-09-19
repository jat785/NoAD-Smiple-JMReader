"""设置：代理配置与运行统计。

代理是个绕不开的运维问题 —— 有人要挂 Clash，有人直连就行，有人在公司网络里
必须走指定代理。所以做成设置页可改，而不是写死在配置文件里让每个人去翻文档。
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import jmclient

router = APIRouter(prefix="/api/settings", tags=["settings"])


class ProxyPayload(BaseModel):
    mode: str                     # auto | off | custom
    url: str = ""


@router.get("", summary="读取设置与运行统计")
def read_settings() -> dict:
    return {
        "proxy": jmclient.effective_proxy_setting(),
        "upstream": jmclient.stats(),
    }


@router.post("/proxy", summary="保存代理设置（立即生效）")
def save_proxy(payload: ProxyPayload) -> dict:
    try:
        setting = jmclient.set_proxy_setting(payload.mode, payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "proxy": setting}


@router.post("/proxy/test", summary="测试当前设置能否连通禁漫")
def test_proxy() -> dict:
    return jmclient.test_connection()
