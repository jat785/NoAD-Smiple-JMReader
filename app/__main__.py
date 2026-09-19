"""``python -m app`` 启动入口。"""

from __future__ import annotations

import uvicorn

from . import config


def main() -> None:
    print()
    print("  JMReader 正在启动 …")
    print(f"  打开浏览器访问： http://{config.HOST}:{config.PORT}")
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
