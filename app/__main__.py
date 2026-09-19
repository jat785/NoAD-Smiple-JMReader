"""``python -m app`` 启动入口。"""

from __future__ import annotations

import uvicorn

from . import config

# 通配监听地址。它们的含义是"监听所有网卡"，**本身不是能连过去的地址**。
_BIND_WILDCARDS = ("0.0.0.0", "::", "")


def _startup_hint() -> list[str]:
    """启动时该提示用户打开哪个地址。

    直接拼接 ``http://{HOST}:{PORT}`` 在默认的 ``127.0.0.1`` 下没问题，
    但按 README 推荐把 ``JMREADER_HOST`` 改成 ``0.0.0.0`` 之后，
    就会打印出一个**根本连不上**的 ``http://0.0.0.0:8756`` —— 用户会
    以为服务没起来。所以通配地址这里改成列出本机的局域网地址。
    """
    port = config.PORT
    if config.HOST not in _BIND_WILDCARDS:
        return [f"打开浏览器访问： http://{config.HOST}:{port}"]

    # 与设置页「访问地址」卡片复用同一套判断（过滤掉 Clash fake-ip 等）
    try:
        from .routers.settings import _lan_addresses

        addrs = _lan_addresses()
    except Exception:  # noqa: BLE001 —— 拿不到地址不该妨碍启动
        addrs = []

    lines = [f"本机打开： http://127.0.0.1:{port}"]
    if addrs:
        lines.append("局域网里的其它设备可以访问：")
        lines.extend(f"    http://{ip}:{port}" for ip in addrs)
    else:
        lines.append(f"（正在监听所有网卡，端口 {port}；用本机的局域网 IP 访问）")
    return lines


def main() -> None:
    print()
    print("  JMReader 正在启动 …")
    for line in _startup_hint():
        print(f"  {line}")
    print("  停止服务请按 Ctrl+C")
    print()
    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
