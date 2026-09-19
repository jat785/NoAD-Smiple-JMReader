"""全局配置。

所有路径都可以用环境变量覆盖，方便把数据目录放到别处（比如 NAS）。
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """极简 .env 读取，不引入额外依赖。

    只做 ``KEY=VALUE``，忽略空行与 ``#`` 注释；**已存在的环境变量优先**，
    所以命令行里显式设置的变量不会被文件覆盖。
    """
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")


def _env_path(key: str, default: Path) -> Path:
    raw = os.environ.get(key, "").strip()
    return Path(raw).expanduser().resolve() if raw else default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


# ---------------------------------------------------------------- 路径
DATA_DIR = _env_path("JMREADER_DATA_DIR", BASE_DIR / "data")
DOWNLOAD_DIR = DATA_DIR / "downloads"
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "jmreader.db"
# 登录凭据（Cookie）没有单独的文件，就存在 DB_PATH 的 kv 表里（键 "session"）。
# 这里曾经有过一个 COOKIE_PATH = DATA_DIR / "cookies.json"，但它从来没有
# 被任何代码用过 —— 只会让人以为备份/清理凭据要动两个地方。
WEB_DIR = BASE_DIR / "web"

# ---------------------------------------------------------------- 服务
HOST = os.environ.get("JMREADER_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = _env_int("JMREADER_PORT", 8756)

# 代理。留空则表示沿用 jmcomic 的自动探测（读系统代理）。
PROXY = os.environ.get("JMREADER_PROXY", "").strip()

# ---------------------------------------------------------------- 下载
# 禁漫站点运营不易：默认单任务串行，且每张图之间留出间隔。
# 请不要为了"快"把这些值调大。
DOWNLOAD_CONCURRENCY = max(1, _env_int("JMREADER_DOWNLOAD_CONCURRENCY", 1))
DOWNLOAD_INTERVAL = max(0.0, _env_float("JMREADER_DOWNLOAD_INTERVAL", 1.2))
DOWNLOAD_MAX_RETRY = max(1, _env_int("JMREADER_DOWNLOAD_MAX_RETRY", 3))

# ---------------------------------------------------------------- 首页
# 默认页展示几部随机漫画
HOME_RANDOM_COUNT = max(1, _env_int("JMREADER_HOME_RANDOM_COUNT", 10))
# 随机推荐池的缓存时长（秒）。池子只是一批漫画 ID，不要求新鲜，
# 所以尽量放长 —— 这是全项目最大的一处周期性出网请求。
RANDOM_POOL_TTL = max(0, _env_int("JMREADER_RANDOM_POOL_TTL", 21600))
# 官方推荐位固定只有 30 条且不随机，所以额外抓这么多个「随机分类 × 随机页码」
# 来把池子撑大（每个来源 80 条）。设为 0 就退回只用官方推荐位。
HOME_POOL_SOURCES = max(0, _env_int("JMREADER_HOME_POOL_SOURCES", 5))
HOME_POOL_MAX_PAGE = max(1, _env_int("JMREADER_HOME_POOL_MAX_PAGE", 30))

# ---------------------------------------------------------------- 出网限速
# 全进程范围内两次上游请求之间的最小间隔（秒）。这是对禁漫负担的**硬上限**，
# 无论多少并发都不会突破。0.2 → 最多 5 请求/秒。
# 作为对照：禁漫自己的网页一次列表就要加载 80 张缩略图。
MIN_REQUEST_INTERVAL = max(0.0, _env_float("JMREADER_MIN_REQUEST_INTERVAL", 0.2))

# ---------------------------------------------------------------- 只读接口缓存
# 分类树基本不变，放长一点。详情页来回进出时也不必反复回源。
CATEGORIES_TTL = max(0, _env_int("JMREADER_CATEGORIES_TTL", 21600))
ALBUM_TTL = max(0, _env_int("JMREADER_ALBUM_TTL", 1800))

# ---------------------------------------------------------------- 图片缓存
# 在线看图时，解扰后的单页会缓存在本地，避免反复回源。
PAGE_CACHE_MAX_FILES = max(0, _env_int("JMREADER_PAGE_CACHE_MAX_FILES", 3000))
COVER_CACHE_MAX_FILES = max(0, _env_int("JMREADER_COVER_CACHE_MAX_FILES", 4000))
# photo 详情的内存缓存时长（秒）
PHOTO_META_TTL = max(0, _env_int("JMREADER_PHOTO_META_TTL", 1800))


def ensure_dirs() -> None:
    """创建运行期需要的目录。"""
    for path in (DATA_DIR, DOWNLOAD_DIR, CACHE_DIR, CACHE_DIR / "pages"):
        path.mkdir(parents=True, exist_ok=True)
