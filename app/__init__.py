"""JMReader —— 禁漫天堂第三方阅读器（漫画 only，无广告）。

后端包。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

__version__ = "0.1.0"


def _compute_build_id() -> str:
    """按**源文件内容**算一个指纹。

    踩过的坑：把新代码部署过去、但服务没有重启，Python 早就把旧模块加载进
    内存了 —— 改文件对已运行的进程没有任何影响。表现是一堆莫名其妙的症状：
    新接口 405、新字段拿不到、界面文案不对。而 ``__version__`` 是手写的，
    不重启也不变，帮不上忙。

    所以这里按文件内容算指纹：文件一变，指纹就变。
    """
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()

    for path in sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts):
        try:
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        except OSError:
            continue

    # 前端也算进来：只改了 app.js 同样会让"界面不对"
    web = root.parent / "web"
    if web.is_dir():
        for path in sorted(web.glob("*")):
            if path.is_file():
                try:
                    digest.update(b"web/")
                    digest.update(path.name.encode("utf-8"))
                    digest.update(b"\0")
                    digest.update(path.read_bytes())
                    digest.update(b"\0")
                except OSError:
                    continue

    return digest.hexdigest()[:12]


# 进程启动时**加载进内存的那份代码**的指纹。这个值一旦定下来就不再变，
# 因为正在运行的模块就是这一份 —— 磁盘后面怎么改都与它无关。
BUILD_ID = _compute_build_id()


def build_id() -> str:
    """正在运行的这份代码的指纹。"""
    return BUILD_ID


def build_id_live() -> str:
    """**重新读磁盘**算一次指纹，用于和 ``BUILD_ID`` 比对。

    两者不同 = 磁盘上已经是新代码，但进程还跑着旧的（部署完没重启）。
    """
    try:
        return _compute_build_id()
    except OSError:
        return BUILD_ID


def is_stale() -> bool:
    return _compute_build_id() != BUILD_ID


def build_state() -> dict:
    on_disk = build_id_live()
    return {
        "running": BUILD_ID,
        "on_disk": on_disk,
        "stale": on_disk != BUILD_ID,
    }
