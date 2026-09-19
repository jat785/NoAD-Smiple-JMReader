"""设置：代理配置、访问地址与运行统计。

代理是个绕不开的运维问题 —— 有人要挂 Clash，有人直连就行，有人在公司网络里
必须走指定代理。所以做成设置页可改，而不是写死在配置文件里让每个人去翻文档。
"""

import socket

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import config
from ..services import jmclient

router = APIRouter(prefix="/api/settings", tags=["settings"])


class ProxyPayload(BaseModel):
    mode: str                     # auto | off | custom
    url: str = ""


# 这些网段列出来只会误导人：
#   198.18/198.19 —— Clash TUN 的 fake-ip 网关，从别的机器连不上
#   169.254       —— link-local，没意义
#   127.          —— 本机回环
_UNUSABLE_PREFIXES = ("198.18.", "198.19.", "169.254.", "127.")


def _usable(ip: str) -> bool:
    return bool(ip) and not ip.startswith(_UNUSABLE_PREFIXES)


def _rank(ip: str) -> tuple:
    """把最可能是"真实局域网地址"的排前面。"""
    if ip.startswith("192.168."):
        return (0, ip)
    if ip.startswith("10."):
        return (1, ip)
    if ip.startswith("172."):
        return (2, ip)
    return (3, ip)


def _lan_addresses() -> list[str]:
    """本机所有可能可用的 IPv4 地址。

    刻意**不排"最可能的那个"**：装了 ZeroTier / Radmin / Clash TUN 的机器上，
    "默认路由出口 IP" 很可能落在虚拟网卡上（实测这台机器就选中了 ZeroTier），
    任何自动排序都会理直气壮地指错。

    真正可靠的判断是「当前浏览器是用哪个地址打开这个页面的」，
    见下面 _access_info 里的 current_url。
    """
    addrs: set[str] = set()
    # UDP connect 不会真的发包，只是让系统按路由表选出出口 IP
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("223.5.5.5", 80))
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    return sorted((ip for ip in addrs if _usable(ip)), key=_rank)


def _access_info(request: Request) -> dict:
    """访问地址。

    刻意和「代理」分开展示：这两件事经常被混为一谈。
    ``0.0.0.0`` 是**监听**用的通配地址，不是一个能连过去的地址。
    """
    open_to_lan = config.HOST in ("0.0.0.0", "::")
    # 浏览器这次是用哪个地址打进来的 —— 这就是"别的机器该用哪个地址"的答案
    host_header = (request.headers.get("host") or "").strip()
    current_url = f"http://{host_header}" if host_header else ""
    return {
        "host": config.HOST,
        "port": config.PORT,
        "open_to_lan": open_to_lan,
        "local_url": f"http://127.0.0.1:{config.PORT}",
        "current_url": current_url,
        "from_localhost": host_header.startswith(("127.0.0.1", "localhost", "[::1]")),
        "lan_urls": [f"http://{ip}:{config.PORT}" for ip in _lan_addresses()] if open_to_lan else [],
    }


@router.get("", summary="读取设置与运行统计")
def read_settings(request: Request) -> dict:
    return {
        "proxy": jmclient.effective_proxy_setting(),
        "diagnostics": jmclient.proxy_diagnostics(),
        "access": _access_info(request),
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
