"""与禁漫通信的唯一出口，基于 jmcomic（MIT）。

设计要点
--------
1. 只用**移动端 app API**（`impl: api`）。它对 IP 地区限制宽松，且不需要过
   Cloudflare 的 HTML 页面；jmcomic 内置了该接口的加解密与自动换域名。
2. **线程局部 client + 共享 JmOption**。jmcomic 的 client 不是线程安全的，
   但 option（持有 cookie / 域名状态）需要共享。cookie 变更时 generation +1，
   各线程下次取用时自动重建。
3. **封面与章节图一律由后端代理**。实测图床域名的 TLS 证书校验会失败、且域名会
   轮换、还会挑 Referer，前端直连必然出问题。所以前端只认我们自己的 URL。
4. **解扰交给 jmcomic**（`download_image(..., decode_image=True)`），我们自己
   绝不去实现分段还原算法。

jmcomic 没有包装、由本模块补齐的接口
-----------------------------------
- ``GET /random_recommend`` 随机推荐池（实测固定返回 30 条）
- ``GET /categories``       分类树 + 标签区块（做筛选 UI 与标签选择器）
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from common import ProxyBuilder
from jmcomic import JmModuleConfig, JmOption, JmcomicText
from jmcomic.jm_exception import MissingAlbumPhotoException

from .. import config, db
from . import secretbox

logger = logging.getLogger("jmreader")

# ------------------------------------------------------------------ 常量

VALID_ORDERS = {"mr", "mv", "mp", "tf", "tr", "md"}
VALID_TIMES = {"t", "w", "m", "a"}

# 多标签筛选的硬性上限。禁漫不支持多 tag 查询，只能本地做集合运算，
# 而每个标签都要单独取页 —— 所以必须封顶，避免为了一个筛选把站点打一顿。
TAG_SEARCH_MAX_TAGS = 5    # 最多同时选几个标签
TAG_SEARCH_MAX_PAGES = 3   # 每个标签最多翻几页
TAG_SEARCH_CACHE_TTL = 3600  # 结果缓存 1 小时 —— 同一个筛选组合不该反复回源

# 标签搜索用 main_tag=3；其余类型仅供内部按需扩展
MAIN_TAG_SITE = 0
MAIN_TAG_WORK = 1
MAIN_TAG_AUTHOR = 2
MAIN_TAG_TAG = 3
MAIN_TAG_ACTOR = 4

_SESSION_KEY = "session"          # kv 里保存密封后的 {"username": ..., "cookies": {...}, "ts": ...}
_CRED_KEY = "credentials"         # kv 里保存密封后的 {"username": ..., "password": ...}
_REMEMBER_KEY = "remember_login"  # "1" / "0"
_FAIL_KEY = "login_failures"      # 自动重登连续失败计数，用于退避

# 自动重新登录的退避策略。
#
# 会话过期是服务端说了算（实测 1.8~3.7 小时之间失效），客户端只能重登。
# 但**绝不能在失败时循环重试** —— 密码改过的情况下那看起来就是撞库，
# 可能触发风控甚至锁号。所以：单次尝试 → 失败就退避 → 连续失败到上限
# 就彻底停手，转为让用户手动登录。
_RELOGIN_MAX_ATTEMPTS = 3         # 连续失败上限，超过就不再自动尝试
_RELOGIN_BACKOFF = (30, 300, 1800)  # 第 1/2/3 次失败后，至少等这么多秒才再试

_login_lock = threading.RLock()
_relogin_state: dict[str, Any] = {
    "last_attempt": 0.0,
    "failures": 0,
    "invalid_since": 0.0,
    "last_error": "",
    "last_ok": 0.0,
}

_CONTENT_TYPES = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

# ------------------------------------------------------------------ client 管理

_tls = threading.local()
_state_lock = threading.RLock()
_option: Optional[JmOption] = None
_generation = 0

_random_pool: dict[str, Any] = {"items": None, "ts": 0.0}
_photo_mem: dict[str, tuple[float, dict]] = {}

# ------------------------------------------------------------------ 出网限速

# 这是「不要给站点添负担」的硬保证：整个进程对禁漫的请求速率有上限，
# 不管有几个线程、几个来源在同时抓，都不可能突破它。
_throttle_lock = threading.Lock()
_last_request_at = 0.0
_request_count = 0        # 累计向禁漫发出的请求数，用来核对"负担"到底有多大
_started_at = time.time()


def _throttle() -> None:
    """在每次上游请求之前调用；必要时阻塞，把速率压到 MIN_REQUEST_INTERVAL 以下。

    顺带累计请求数 —— 这是唯一一处所有出网请求的必经之路，计数最准。
    """
    global _last_request_at, _request_count
    interval = config.MIN_REQUEST_INTERVAL
    with _throttle_lock:
        _request_count += 1
        if interval <= 0:
            return
        now = time.monotonic()
        wait = _last_request_at + interval - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_request_at = now


def stats() -> dict:
    """给 /api/health 用的运行统计。"""
    uptime = max(1.0, time.time() - _started_at)
    return {
        "upstream_requests": _request_count,
        "uptime_seconds": int(uptime),
        "requests_per_hour": round(_request_count / uptime * 3600, 1),
        "min_interval_seconds": config.MIN_REQUEST_INTERVAL,
    }


# ------------------------------------------------------------------ 代理设置

_PROXY_KV_KEY = "proxy_setting_v1"
PROXY_MODES = ("auto", "off", "custom")


def _normalize_proxy(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = f"http://{url}"
    return url


def _stored_proxy() -> dict:
    raw = db.kv_get(_PROXY_KV_KEY)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("mode") in PROXY_MODES:
                return {"mode": data["mode"], "url": _normalize_proxy(data.get("url") or "")}
        except (ValueError, TypeError):
            pass
    return {"mode": "auto", "url": ""}


def _system_proxy() -> dict:
    """当场探测系统代理。探测不到就返回 ``{}``（等于直连）。

    刻意不用 ``JmModuleConfig.DEFAULT_PROXIES`` —— 那是模块 import 时算一次的类属性，
    进程启动那一刻没探测到代理的话，它会永远停留在 ``{}``，之后无论用户怎么操作都不会刷新。
    """
    try:
        return ProxyBuilder.system_proxy() or {}
    except Exception:  # noqa: BLE001 —— 探测失败就当没有代理，不能因为这个让整个应用起不来
        return {}


def effective_proxy_setting() -> dict:
    """当前真正生效的代理设置。

    ``mode``: auto（跟随系统/配置）/ off（强制直连）/ custom（手动指定）
    ``proxy``: 实际要用的地址，空字符串表示直连
    ``source``: 这个值是谁定的，用来在设置页上解释清楚
    """
    stored = _stored_proxy()
    env_url = _normalize_proxy(config.PROXY)

    if stored["mode"] == "off":
        return {"mode": "off", "url": "", "proxy": "", "source": "设置页"}

    if stored["mode"] == "custom":
        url = stored["url"] or env_url
        if url:
            return {"mode": "custom", "url": stored["url"], "proxy": url, "source": "设置页"}
        # 选了手动却没填地址，退回自动
        return {"mode": "auto", "url": "", "proxy": env_url or _detect_system_proxy(),
                "source": ".env / 环境变量" if env_url else "系统代理"}

    if env_url:
        return {"mode": "auto", "url": "", "proxy": env_url, "source": ".env / 环境变量"}

    detected = _detect_system_proxy()
    return {
        "mode": "auto",
        "url": "",
        "proxy": detected,
        "source": "系统代理" if detected else "系统无代理（直连）",
    }


def _detect_system_proxy() -> str:
    """探测系统代理并归一化成带协议的地址；没有就返回空串。"""
    detected = _system_proxy()
    addr = detected.get("http") or detected.get("https") or ""
    return _normalize_proxy(addr)


def proxy_diagnostics() -> dict:
    """排查代理问题用的诊断信息。

    「设置页上显示什么」和「HTTP 层实际用了什么」经常不是一回事，
    排查时必须要能看到后者。
    """
    try:
        raw = urllib.request.getproxies()
    except Exception:  # noqa: BLE001
        raw = {}
    try:
        frozen = getattr(JmModuleConfig, "DEFAULT_PROXIES", None)
    except Exception:  # noqa: BLE001
        frozen = None

    eff = effective_proxy_setting()
    injected = client_proxies()

    return {
        "setting": eff,
        "injected": injected,
        "raw_env_proxies": raw,
        # 就是这个值冻在 import 时刻，害得 WinNAS 上怎么改都没反应
        "jmcomic_default_at_import": frozen,
    }


def set_proxy_setting(mode: str, url: str = "") -> dict:
    """保存代理设置。改完立刻作废现有 client，下一次请求就用新设置。"""
    if mode not in PROXY_MODES:
        raise ValueError(f"mode 必须是 {'/'.join(PROXY_MODES)} 之一，收到：{mode!r}")
    payload = {"mode": mode, "url": _normalize_proxy(url)}
    db.kv_set(_PROXY_KV_KEY, json.dumps(payload, ensure_ascii=False))
    _invalidate()
    return effective_proxy_setting()


def test_connection() -> dict:
    """按当前设置打一次最轻的请求，回报成没成、花了多久、**实际走的哪个代理**。

    最后一项很关键：报错时如果不告诉用户它到底走没走代理，
    用户只能对着"请求全部失败"干瞪眼。
    """
    eff = effective_proxy_setting()
    used = eff["proxy"] or "直连（没有走代理）"
    started = time.time()
    try:
        _throttle()
        data = get_client().req_api("/setting").res_data
        keys = len(data) if hasattr(data, "__len__") else 0
        return {
            "ok": True,
            "ms": int((time.time() - started) * 1000),
            "detail": f"连接正常（/setting 返回 {keys} 项）",
            "proxy_used": used,
        }
    except Exception as exc:  # noqa: BLE001 —— 就是要把失败原因原样报给用户
        return {
            "ok": False,
            "ms": int((time.time() - started) * 1000),
            "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
            "proxy_used": used,
        }


# ------------------------------------------------------------------ 通用 TTL 缓存

_ttl_cache: dict[Any, tuple[float, Any]] = {}
_ttl_lock = threading.Lock()


def _cached(key: Any, ttl: float, producer):
    """按 key 缓存 ``producer()`` 的结果 ``ttl`` 秒。

    目的是让「同一个页面来回进出」不再重复打站点。并发下可能会重复生产一次，
    对只读接口来说可以接受 —— 与其为了消重去持有锁阻塞所有请求，不如让限速闸门兜底。
    """
    now = time.time()
    with _ttl_lock:
        hit = _ttl_cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]

    value = producer()

    with _ttl_lock:
        if len(_ttl_cache) > 512:
            for k in [k for k, (ts, _) in _ttl_cache.items() if now - ts > 60]:
                _ttl_cache.pop(k, None)
        _ttl_cache[key] = (time.time(), value)
    return value


def _load_session() -> dict:
    """读取本地保存的会话。

    内容（含 username 与 Cookie）是**密封**存放的，库里看不到明文。
    遇到旧的明文格式会自动就地升级；解不开（换机器、换用户）就当作没登录。
    """
    raw = db.kv_get(_SESSION_KEY)
    if not raw:
        return {}

    payload = secretbox.unseal(raw)
    if payload is None:
        # 兼容早期版本留下的明文 JSON，读到就顺手加密回去
        try:
            legacy = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if isinstance(legacy, dict) and legacy:
            _save_session(legacy)
            return legacy
        return {}

    try:
        data = json.loads(payload.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_session(session: dict) -> None:
    token = secretbox.seal(json.dumps(session, ensure_ascii=False).encode("utf-8"))
    if token is None:
        # 密封不了就宁可不存 —— 绝不退回明文
        logger.warning("无法安全保存登录状态（密封后端不可用），本次不落盘")
        return
    db.kv_set(_SESSION_KEY, token)


def _build_option() -> JmOption:
    """在 jmcomic 默认配置上叠加我们的设置。"""
    raw = JmOption.default().deconstruct()

    # 数据落到我们的目录，而不是当前工作目录
    raw["dir_rule"]["base_dir"] = str(config.DOWNLOAD_DIR)

    # 就算万一走了 jmcomic 自带的下载器，也只允许单线程
    raw["download"]["threading"] = {"image": 1, "photo": 1}
    raw["download"]["image"]["decode"] = True

    postman = raw["client"]["postman"]
    meta = postman.setdefault("meta_data", {})

    # ⚠ 代理**故意不在这里设置**。
    #
    # 看起来这里是最自然的地方，但 jmcomic 的 JmOption.construct 会走
    # JmOption.merge_default_dict 做递归深合并，而它是这样写的：
    #
    #     for key, value in user_dict.items():
    #         if isinstance(value, dict) and isinstance(default_dict.get(key), dict):
    #             default_dict[key] = merge_default_dict(value, default_dict[key])
    #
    # 传 {} 进去时，循环体一次都不执行，于是**原样返回默认值**
    # （默认值就是 JmModuleConfig.DEFAULT_PROXIES）。也就是说空字典在这个地方
    # 根本无法表达"不使用代理"，会被系统代理悄悄顶掉。
    #
    # 所以代理改在 get_client() 里用 new_jm_client(proxies=...) 传 ——
    # 那条路是直接 meta_data.update(kwargs)，不经过合并，{} 就是 {}。

    # 注入已保存的登录 Cookie（jmcomic 的 ensure_have_cookies 见到 cookie 就不再重取）
    session = _load_session()
    cookies = session.get("cookies")
    if isinstance(cookies, dict) and cookies:
        meta["cookies"] = cookies

    return JmOption.construct(raw)


def client_proxies() -> dict:
    """要交给 HTTP 层的 proxies。``{}`` 表示直连。

    这是唯一可靠的"不使用代理"表达方式，原因见 _build_option 里的注释。
    """
    eff = effective_proxy_setting()
    if eff["mode"] == "off" or not eff["proxy"]:
        return {}
    return {"http": eff["proxy"], "https": eff["proxy"]}


def _get_option() -> JmOption:
    global _option
    with _state_lock:
        if _option is None:
            _option = _build_option()
        return _option


def get_client():
    """取当前线程的 client。"""
    gen = _generation
    client = getattr(_tls, "client", None)
    if client is not None and getattr(_tls, "generation", None) == gen:
        return client
    # 代理在这里显式下发，绕开 JmOption 那个会把空 dict 吃掉的有损合并
    client = _get_option().new_jm_client(proxies=client_proxies())
    _tls.client = client
    _tls.generation = gen
    return client


def _invalidate() -> None:
    """丢弃 option，让所有线程下次重建（cookie 变更后调用）。"""
    global _option, _generation
    with _state_lock:
        _option = None
        _generation += 1
    _random_pool["items"] = None
    _random_pool["ts"] = 0.0


# ------------------------------------------------------------------ 账号

def remember_enabled() -> bool:
    return db.kv_get(_REMEMBER_KEY) == "1"


def set_remember(enabled: bool) -> bool:
    """开关「记住密码」。

    关掉时**一并删掉已存的密码**，而不是留着不管 —— 用户点关闭就该真的清掉。
    """
    db.kv_set(_REMEMBER_KEY, "1" if enabled else "0")
    if not enabled:
        db.kv_delete(_CRED_KEY)
        secretbox.wipe_key_file()
    return enabled


def _load_credentials() -> dict:
    raw = db.kv_get(_CRED_KEY)
    payload = secretbox.unseal(raw)
    if payload is None:
        return {}
    try:
        data = json.loads(payload.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_credentials(username: str, password: str) -> bool:
    token = secretbox.seal(
        json.dumps({"username": username, "password": password}, ensure_ascii=False).encode("utf-8")
    )
    if token is None:
        logger.warning("当前平台无法安全保存密码，「记住密码」未生效")
        return False
    db.kv_set(_CRED_KEY, token)
    return True


def has_saved_credentials() -> bool:
    creds = _load_credentials()
    return bool(creds.get("username") and creds.get("password"))


def secret_backend_info() -> dict:
    """当前平台用哪套机制保护密码，以及它的真实强度。"""
    return secretbox.backend_info()


def session_state() -> dict:
    """给界面用的登录状态。

    ``logged_in`` 只表示"本地存着一份看起来可用的会话"，**不代表服务端还认它**。
    真正的失效信号来自 ``expired`` —— 那是某次真实请求拿到 401 之后才置上的。
    """
    session = _load_session()
    logged_in = bool(session.get("username") and session.get("cookies"))
    return {
        "logged_in": logged_in,
        "username": session.get("username") or None,
        "login_at": session.get("ts") or None,
        "expired": bool(_relogin_state["invalid_since"]) and logged_in,
        "invalid_since": int(_relogin_state["invalid_since"]) or None,
        "last_error": _relogin_state["last_error"] or None,
        "auto_relogin": remember_enabled() and has_saved_credentials(),
        "relogin_blocked": _relogin_state["failures"] >= _RELOGIN_MAX_ATTEMPTS,
        "secret_backend": secretbox.backend_info(),
    }


def is_logged_in() -> bool:
    session = _load_session()
    return bool(session.get("username") and session.get("cookies"))


def current_user() -> Optional[str]:
    return _load_session().get("username") or None


def _do_login(username: str, password: str) -> dict:
    """真正打一次登录接口。不碰本地存储。"""
    _throttle()
    client = get_client()
    client.login(username, password)
    cookies = client.get_meta_data("cookies") or {}
    if not isinstance(cookies, dict) or not cookies:
        raise RuntimeError("登录返回的 Cookie 为空，登录可能未成功")
    _save_session({"username": username, "cookies": cookies, "ts": int(time.time())})
    _invalidate()
    return {"username": username}


def login(username: str, password: str) -> dict:
    """用户主动登录。成功后按「记住密码」开关决定是否把凭据密封存下来。"""
    result = _do_login(username, password)

    with _login_lock:
        _relogin_state["failures"] = 0
        _relogin_state["invalid_since"] = 0.0
        _relogin_state["last_error"] = ""
        _relogin_state["last_ok"] = time.time()
    db.kv_delete(_FAIL_KEY)

    if remember_enabled():
        _save_credentials(username, password)
    return result


def logout() -> None:
    """退出登录：会话、凭据、失败计数一并清掉。"""
    db.kv_delete(_SESSION_KEY)
    db.kv_delete(_CRED_KEY)
    db.kv_delete(_REMEMBER_KEY)
    db.kv_delete(_FAIL_KEY)
    secretbox.wipe_key_file()
    with _login_lock:
        _relogin_state.update(
            {"failures": 0, "invalid_since": 0.0, "last_error": "", "last_attempt": 0.0}
        )
    _invalidate()


def _is_auth_error(exc: BaseException) -> bool:
    """判断异常是不是"服务端说我没登录"。

    jmcomic 把这类响应包成 ResponseUnexpectedException，消息里带着原始 JSON：
        {"code":401, "errorMsg":"請先登入會員"}
    这里同时认简繁两种写法，也认我们自己抛的 PermissionError。
    """
    if isinstance(exc, PermissionError):
        return True
    text = str(exc)
    if '"code":401' in text.replace(" ", ""):
        return True
    return ("請先登入" in text) or ("请先登录" in text) or ("尚未登入" in text)


def relogin_if_possible() -> bool:
    """会话失效时，用存下来的凭据自动重新登录一次。

    返回 True 表示"现在可以重试原请求了"。带退避与失败上限，
    **失败绝不会连环重试**（那看起来像撞库，可能触发风控）。
    """
    if not remember_enabled():
        return False

    creds = _load_credentials()
    username, password = creds.get("username"), creds.get("password")
    if not username or not password:
        return False

    with _login_lock:
        failures = _relogin_state["failures"]
        if failures >= _RELOGIN_MAX_ATTEMPTS:
            return False

        now = time.time()
        if failures and now - _relogin_state["last_attempt"] < _RELOGIN_BACKOFF[failures - 1]:
            return False           # 还在退避窗口里

        _relogin_state["last_attempt"] = now

    try:
        _do_login(str(username), str(password))
    except Exception as exc:  # noqa: BLE001 —— 重登失败不能让原请求崩掉
        with _login_lock:
            _relogin_state["failures"] += 1
            _relogin_state["invalid_since"] = _relogin_state["invalid_since"] or time.time()
            _relogin_state["last_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
            db.kv_set(_FAIL_KEY, str(_relogin_state["failures"]))
        logger.warning("自动重新登录失败（第 %d 次），已转入退避", _relogin_state["failures"])
        return False

    with _login_lock:
        _relogin_state["failures"] = 0
        _relogin_state["invalid_since"] = 0.0
        _relogin_state["last_error"] = ""
        _relogin_state["last_ok"] = time.time()
    db.kv_delete(_FAIL_KEY)
    logger.info("已用保存的凭据自动重新登录")
    return True


def _require_login() -> None:
    if not is_logged_in():
        raise PermissionError("该功能需要先登录禁漫账号")


def _authed(call):
    """跑一个需要登录的调用；遇到会话失效就自动重登并重试一次。

    放在这里而不是每个接口里各写一遍，是为了保证所有需要登录的路径
    行为一致：失败 → 标记失效 → 尝试重登 → 重试一次 → 还不行就明确报错。
    """
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        if not _is_auth_error(exc):
            raise
        with _login_lock:
            _relogin_state["invalid_since"] = _relogin_state["invalid_since"] or time.time()
        _invalidate()          # 丢掉可能带着旧 cookie 的 client
        if not relogin_if_possible():
            raise PermissionError("登录已失效，请重新登录禁漫账号") from exc
    return call()


# ------------------------------------------------------------------ 数据整形

def _int_or(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str_or(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _cover_url(album_id: str) -> str:
    """封面统一走后端代理，前端不直连图床。"""
    return f"/api/cover/{album_id}"


def _brief_from_dict(item: dict) -> dict:
    aid = _str_or(item.get("id") or item.get("album_id"))
    category = item.get("category") or {}
    if not isinstance(category, dict):
        category = {}
    tags = item.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    updated_at = _int_or(item.get("update_at"))
    # /random_recommend 之类的接口不给 adddate，用更新时间兜个底，
    # 免得卡片第三行空着
    adddate = _str_or(item.get("adddate"))
    if not adddate and updated_at:
        try:
            adddate = time.strftime("%Y-%m-%d", time.localtime(updated_at))
        except (OSError, ValueError, OverflowError):
            adddate = ""
    return {
        "album_id": aid,
        "title": _str_or(item.get("name") or item.get("title")),
        "author": _str_or(item.get("author")),
        "tags": [_str_or(t) for t in tags if t],
        "category": _str_or(category.get("title")),
        "cover": _cover_url(aid) if aid else "",
        "is_favorite": bool(item.get("is_favorite")),
        "updated_at": updated_at,
        "adddate": adddate,
    }


def _brief_from_search_item(pair: Any) -> dict:
    """搜索/分类页的元素是 ``(album_id, dict)`` 元组。"""
    if isinstance(pair, (tuple, list)) and len(pair) == 2:
        aid, data = pair
        data = data if isinstance(data, dict) else {}
        brief = _brief_from_dict(data)
        if not brief["album_id"]:
            brief["album_id"] = _str_or(aid)
            brief["cover"] = _cover_url(brief["album_id"])
        return brief
    if isinstance(pair, dict):
        return _brief_from_dict(pair)
    return _brief_from_dict({})


def _search_page_to_dict(page: Any, page_number: int) -> dict:
    items = [_brief_from_search_item(x) for x in (getattr(page, "content", None) or [])]
    total = _int_or(getattr(page, "total", 0))
    per_page = len(items) or 80
    return {
        "items": items,
        "total": total,
        "page": page_number,
        # 禁漫的 total 常被截断成 10000，所以用「有没有取满一页」判断后续
        "has_next": len(items) >= per_page,
    }


# ------------------------------------------------------------------ 首页 / 发现

# 随机池的来源。官方推荐位固定只有 30 条且本身不随机，光靠它"随机 10 部"
# 最多只能见到那 30 部，所以再掺入最新列表和若干「随机分类 × 随机页码」。
_SEED_CATEGORIES = ("0", "doujin", "single", "short", "hanman", "meiman", "another", "3D")
_SEED_ORDERS = ("mr", "mv", "tf", "mp")
_POOL_WORKERS = 2      # 并发抓池子的线程数。限速闸门之下并发只是为了少等，不会多打站点


def _pool_items_from_list(data: Any) -> list[dict]:
    """``/random_recommend``、``/latest`` 这类直接返回数组的接口。"""
    if not isinstance(data, list):
        return []
    return [_brief_from_dict(x) for x in data if isinstance(x, dict)]


def _pool_items_from_filter(data: Any) -> list[dict]:
    """``/categories/filter`` 返回 ``{content: [...], total, ...}``。"""
    content = None
    if isinstance(data, dict):
        content = data.get("content") or data.get("list")
    elif isinstance(data, list):
        content = data
    if not isinstance(content, list):
        return []
    out = []
    for entry in content:
        item = entry[1] if isinstance(entry, (tuple, list)) and len(entry) == 2 else entry
        if isinstance(item, dict):
            out.append(_brief_from_dict(item))
    return out


def _fetch_api(path: str) -> Any:
    # 注意：必须在工作线程里调 get_client()，让它拿到本线程自己的 client
    _throttle()
    return get_client().req_api(path).res_data


def _grow_random_pool() -> list[dict]:
    """合并多个来源，尽量把随机池做大。单个来源失败不影响整体。"""
    jobs: list[tuple[str, Any]] = [
        ("/random_recommend", _pool_items_from_list),
        ("/latest", _pool_items_from_list),
    ]
    rng = random.Random()
    for _ in range(config.HOME_POOL_SOURCES):
        cat = rng.choice(_SEED_CATEGORIES)
        order = rng.choice(_SEED_ORDERS)
        page = rng.randint(1, config.HOME_POOL_MAX_PAGE)
        jobs.append((f"/categories/filter?c={cat}&o={order}&page={page}", _pool_items_from_filter))

    merged: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=_POOL_WORKERS) as pool:
        futures = [(pool.submit(_fetch_api, path), parser) for path, parser in jobs]
        for future, parser in futures:
            try:
                for brief in parser(future.result()):
                    aid = brief.get("album_id")
                    if aid:
                        merged.setdefault(aid, brief)
            except Exception:  # noqa: BLE001 —— 少一个来源而已，不该拖垮首页
                continue
    return list(merged.values())


_POOL_KV_KEY = "random_pool_v1"


def _pool_from_db() -> Optional[tuple[float, list[dict]]]:
    try:
        raw = db.kv_get(_POOL_KV_KEY)
        if not raw:
            return None
        data = json.loads(raw)
        items = data.get("items")
        if isinstance(items, list) and items:
            return float(data.get("ts") or 0), items
    except Exception:  # noqa: BLE001 —— 缓存坏了就当没有
        return None
    return None


def _pool_to_db(ts: float, items: list[dict]) -> None:
    try:
        db.kv_set(_POOL_KV_KEY, json.dumps({"ts": ts, "items": items}, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def random_pool(force: bool = False) -> list[dict]:
    """随机推荐池。

    实测（2026-09）：``GET /random_recommend`` 固定返回 30 条，且短时间内多次调用
    结果完全相同 —— 它是服务端的一份缓存列表，**不是每次随机**。
    只从这 30 条里抽样的话，刷新再多次也只会在同样这 30 部里打转，
    所以这里把「官方推荐位 + 最新列表 + 若干随机分类的随机页」合并成一个大池子。

    池子还会落盘，**重启进程不会重新抓一遍**。
    """
    now = time.time()
    ttl = config.RANDOM_POOL_TTL

    if not force and _random_pool["items"] is not None and now - _random_pool["ts"] < ttl:
        return _random_pool["items"]

    # 内存里没有（比如刚重启），先看落盘那份还新不新
    if not force:
        stored = _pool_from_db()
        if stored is not None and now - stored[0] < ttl:
            _random_pool["items"], _random_pool["ts"] = stored[1], stored[0]
            return stored[1]

    items = _grow_random_pool()

    # 池子异常缩水时不要覆盖掉原来那个（比如刚好全部来源都超时）
    if not items and _random_pool["items"]:
        return _random_pool["items"]

    _random_pool["items"] = items
    _random_pool["ts"] = now
    _pool_to_db(now, items)
    return items


def home_random(count: Optional[int] = None) -> list[dict]:
    """默认页：从推荐池里随机取 N 部。"""
    n = count or config.HOME_RANDOM_COUNT
    pool = random_pool()
    if not pool:
        return []
    return random.sample(pool, min(n, len(pool)))


def lookup_album(album_id: str) -> dict:
    """按 JM 号（社区里说的"车牌号"）精确定位。

    找不到时返回 ``{"found": False}`` 而**不是抛异常** —— 上层必须能区分
    「查无此号」和「网络故障」：前者该告诉用户车号错了，后者该报 502，
    混在一起用户会以为是自己的车号打错了。

    注意禁漫自己的提示里还有一条：不存在的本子**也可能是仅登录用户可见**。
    所以没命中时要提示用户「车号有误，或该漫画仅登录可见」，而不是断言不存在。
    """
    aid = str(album_id).strip()
    if not aid.isdigit():
        raise ValueError(f"JM 号必须是纯数字，收到：{album_id!r}")

    try:
        return {"found": True, "album_id": aid, "album": album(aid)}
    except MissingAlbumPhotoException:
        return {"found": False, "album_id": aid}


def categories() -> dict:
    """分类树 + 标签区块（筛选 UI 与标签选择器的数据源）。分类基本不变，缓存住。"""
    return _cached("categories", config.CATEGORIES_TTL, _categories_uncached)


def _categories_uncached() -> dict:
    _throttle()
    data = get_client().req_api("/categories").res_data
    cats = []
    if isinstance(data, dict):
        for c in data.get("categories") or []:
            if not isinstance(c, dict):
                continue
            cats.append({
                "id": _str_or(c.get("id")),
                "name": _str_or(c.get("name")),
                "slug": _str_or(c.get("slug")),
                "total": _int_or(c.get("total_albums")),
            })
        blocks = []
        for b in data.get("blocks") or []:
            if not isinstance(b, dict):
                continue
            content = b.get("content") or []
            blocks.append({
                "title": _str_or(b.get("title")),
                "tags": [_str_or(t) for t in content if t] if isinstance(content, list) else [],
            })
    else:
        blocks = []
    return {"categories": cats, "blocks": blocks}


def rankings(category: str = "0", order: str = "mv_w", page: int = 1) -> dict:
    """排行榜。禁漫的排行榜就是分类筛选的一种形式（o=mv_w / mv_m / mv_t）。"""
    if order not in {"mr", "mv", "mp", "tf", "mv_w", "mv_m", "mv_t"}:
        order = "mv_w"

    def produce() -> dict:
        _throttle()
        page_obj = get_client().categories_filter(
            page=page, time="a", category=category or "0", order_by=order
        )
        return _search_page_to_dict(page_obj, page)

    return _cached(("rankings", category, order, page), config.ALBUM_TTL, produce)


# ------------------------------------------------------------------ 搜索

def search(
    keyword: str,
    page: int = 1,
    order: str = "mr",
    time_range: str = "a",
    by: str = "site",
) -> dict:
    """关键词搜索。

    ``by`` 对应禁漫的 ``main_tag``：site=站内 / work=作品 / author=作者 /
    tag=标签 / actor=登场人物。**标签搜索走 tag，不要用关键词去凑。**
    """
    order = order if order in VALID_ORDERS else "mr"
    time_range = time_range if time_range in VALID_TIMES else "a"

    def produce() -> dict:
        _throttle()
        client = get_client()
        funcs = {
            "site": client.search_site,
            "work": client.search_work,
            "author": client.search_author,
            "tag": client.search_tag,
            "actor": client.search_actor,
        }
        func = funcs.get(by, client.search_site)
        page_obj = func(keyword, page=page, order_by=order, time=time_range)
        return _search_page_to_dict(page_obj, page)

    # 翻回上一页、或同一个关键词反复搜，都不该再打一次站点
    return _cached(("search", keyword, page, order, time_range, by), config.ALBUM_TTL, produce)


# ------------------------------------------------------------------ 多标签筛选

def tags() -> dict:
    """供 UI 使用的内置标签表（按禁漫自己的分组）。"""
    data = categories()
    blocks = data.get("blocks") or []
    return {
        "blocks": blocks,
        "all": [t for b in blocks for t in b.get("tags", [])],
    }


def tag_search(
    tag_list: list[str],
    mode: str = "and",
    order: str = "mr",
    time_range: str = "a",
    page: int = 1,
    page_size: int = 80,
) -> dict:
    """多标签筛选。

    禁漫的接口**只支持单个 tag**（``main_tag=3`` + 一个 search_query）。
    实测用空格/逗号拼多个标签并不可靠 —— 那是整串模糊匹配，不是 AND。
    所以多选只能在本地做集合运算：

    - ``mode='and'``：取所有标签结果的**交集**（同时具备这些标签）
    - ``mode='or'`` ：取**并集**（具备任一标签）

    代价是要为每个标签各取若干页。为了对站点温柔，这里做了硬性上限：
    最多 ``TAG_SEARCH_MAX_TAGS`` 个标签 × ``TAG_SEARCH_MAX_PAGES`` 页，
    结果再缓存 ``TAG_SEARCH_CACHE_TTL`` 秒。

    因此结果是「在各自前 N 页里同时出现」的近似 —— 对于按同一规则排序的列表来说，
    这等价于取交集后的头部，实用上足够。
    """
    order = order if order in VALID_ORDERS else "mr"
    time_range = time_range if time_range in VALID_TIMES else "a"

    cleaned: list[str] = []
    for raw in tag_list:
        text = str(raw).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        raise ValueError("至少需要选择一个标签")
    if len(cleaned) > TAG_SEARCH_MAX_TAGS:
        cleaned = cleaned[:TAG_SEARCH_MAX_TAGS]

    # 单个标签没必要绕圈子，直接用原生能力
    if len(cleaned) == 1:
        result = search(cleaned[0], page=page, order=order, time_range=time_range, by="tag")
        result["mode"] = "single"
        result["tags_used"] = cleaned
        return result

    mode = "or" if str(mode).lower() == "or" else "and"
    # 抓取深度跟着请求页码走：要第 N 页，就至少备够 N+1 页的量
    target = (page + 1) * page_size
    cache_key = (tuple(cleaned), mode, order, time_range, page)
    combined = _tag_cache_get(cache_key)
    if combined is None:
        combined = _collect_tag_results(cleaned, mode, order, time_range, target, page_size)
        _tag_cache_put(cache_key, combined)

    start = (page - 1) * page_size
    end = start + page_size
    items = combined[start:end]
    return {
        "items": items,
        "total": len(combined),
        "page": page,
        "has_next": end < len(combined),
        "mode": mode,
        "tags_used": cleaned,
        # 提醒前端：这是在每个标签前 N 页范围内算出来的交集/并集
        "approximate": True,
    }


def _collect_tag_results(
    tags: list[str],
    mode: str,
    order: str,
    time_range: str,
    target: int,
    page_size: int,
) -> list[dict]:
    """按轮次逐页抓取各标签结果，并做交集/并集。

    ``target`` 是希望凑够的条目数，够了就提前收手，避免白抓。
    """
    client = get_client()
    seen: dict[str, dict] = {}          # album_id -> brief
    ranks: dict[str, dict[str, int]] = {t: {} for t in tags}
    exhausted: dict[str, bool] = {t: False for t in tags}
    accumulated: dict[str, list[dict]] = {t: [] for t in tags}

    for page_no in range(1, TAG_SEARCH_MAX_PAGES + 1):
        progressed = False
        for tag in tags:
            if exhausted[tag]:
                continue
            # 这里直接调 client，必须自己过限速闸门（否则多标签筛选会变成一次突发）
            _throttle()
            page_obj = client.search_tag(tag, page=page_no, order_by=order, time=time_range)
            batch = [_brief_from_search_item(x) for x in (getattr(page_obj, "content", None) or [])]
            if len(batch) < page_size:
                exhausted[tag] = True
            if not batch:
                continue
            progressed = True
            base = len(accumulated[tag])
            accumulated[tag].extend(batch)
            for offset, brief in enumerate(batch):
                aid = brief["album_id"]
                if not aid:
                    continue
                ranks[tag][aid] = base + offset
                # 保留字段最全的那一份
                if aid not in seen or len(brief.get("tags") or []) > len(seen[aid].get("tags") or []):
                    seen[aid] = brief

        if not progressed:
            break

        if len(_combined_ids(seen, ranks, tags, mode)) >= target or all(exhausted.values()):
            break

    keep = _combined_ids(seen, ranks, tags, mode)

    def best_rank(aid: str) -> int:
        values = [ranks[t][aid] for t in tags if aid in ranks[t]]
        return min(values) if values else 10 ** 9

    keep.sort(key=best_rank)
    return [seen[aid] for aid in keep]


def _combined_ids(seen: dict, ranks: dict, tags: list[str], mode: str) -> list[str]:
    """AND 取交集，OR 取并集。"""
    if mode == "and":
        return [aid for aid in seen if all(aid in ranks[t] for t in tags)]
    return list(seen)


_tag_cache: dict[tuple, tuple[float, list[dict]]] = {}


def _tag_cache_get(key: tuple) -> Optional[list[dict]]:
    hit = _tag_cache.get(key)
    if not hit:
        return None
    ts, data = hit
    if time.time() - ts > TAG_SEARCH_CACHE_TTL:
        _tag_cache.pop(key, None)
        return None
    return data


def _tag_cache_put(key: tuple, data: list[dict]) -> None:
    if len(_tag_cache) > 64:
        _tag_cache.clear()
    _tag_cache[key] = (time.time(), data)


# ------------------------------------------------------------------ 详情

def album(album_id: str) -> dict:
    """本子详情（含章节列表）。缓存住，详情页来回进出不用反复回源。"""
    return _cached(("album", str(album_id)), config.ALBUM_TTL, lambda: _album_uncached(str(album_id)))


def _album_uncached(album_id: str) -> dict:
    _throttle()
    a = get_client().get_album_detail(str(album_id))

    chapters = []
    for idx, ep in enumerate(getattr(a, "episode_list", None) or [], start=1):
        # episode_list 的元素是 (photo_id:int, 排序标记:str, 标题:str)
        if isinstance(ep, (tuple, list)) and len(ep) >= 3:
            photo_id, _mark, title = ep[0], ep[1], ep[2]
        elif isinstance(ep, (tuple, list)) and len(ep) == 2:
            photo_id, title = ep[0], ep[1]
        else:
            photo_id, title = ep, ""
        photo_id = _str_or(photo_id)
        chapters.append({
            "chapter_id": photo_id,
            "index": idx,
            "title": _str_or(title) or f"第 {idx} 话",
            "cover": _cover_url(photo_id),
        })

    related = []
    for r in getattr(a, "related_list", None) or []:
        if isinstance(r, dict):
            related.append(_brief_from_dict(r))

    return {
        "album_id": _str_or(a.album_id),
        "title": _str_or(a.name),
        "authors": [_str_or(x) for x in (getattr(a, "authors", None) or [])],
        "tags": [_str_or(x) for x in (getattr(a, "tags", None) or [])],
        "actors": [_str_or(x) for x in (getattr(a, "actors", None) or [])],
        "works": [_str_or(x) for x in (getattr(a, "works", None) or [])],
        "description": _str_or(getattr(a, "description", "")),
        "page_count": _int_or(getattr(a, "page_count", 0)),
        "views": _str_or(getattr(a, "views", "")),
        "likes": _str_or(getattr(a, "likes", "")),
        "comment_count": _str_or(getattr(a, "comment_count", "")),
        "pub_date": _str_or(getattr(a, "pub_date", "")),
        "update_date": _str_or(getattr(a, "update_date", "")),
        "is_favorite": bool(getattr(a, "is_favorite", False)),
        "liked": bool(getattr(a, "liked", False)),
        "cover": _cover_url(_str_or(a.album_id)),
        "chapters": chapters,
        "related": related,
    }


def _photo_meta(photo_id: str, force: bool = False) -> dict:
    """章节目录信息（含每页文件名与解扰参数），带内存缓存。"""
    key = str(photo_id)
    now = time.time()
    if not force:
        hit = _photo_mem.get(key)
        if hit and now - hit[0] < config.PHOTO_META_TTL:
            return hit[1]

    _throttle()
    ph = get_client().get_photo_detail(key, fetch_album=False)
    pages = [str(x) for x in (getattr(ph, "page_arr", None) or [])]
    meta = {
        "photo_id": key,
        "title": _str_or(getattr(ph, "name", "")),
        "pages": pages,
        "page_count": len(pages),
        "scramble_id": _int_or(getattr(ph, "scramble_id", 0)),
        "image_domain": _str_or(getattr(ph, "data_original_domain", "")) or None,
    }
    _photo_mem[key] = (now, meta)
    return meta


def chapter(photo_id: str, album_id: Optional[str] = None) -> dict:
    """章节信息 + 每页的取图地址（前端只认我们自己的地址）。"""
    meta = _photo_meta(photo_id)
    return {
        "chapter_id": meta["photo_id"],
        "title": meta["title"],
        "page_count": meta["page_count"],
        "album_id": _str_or(album_id) or None,
        "pages": [f"/api/page/{meta['photo_id']}/{i}" for i in range(1, meta["page_count"] + 1)],
    }


# ------------------------------------------------------------------ 图片

def _content_type(filename: str) -> str:
    return _CONTENT_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")


def _image_url(meta: dict, filename: str) -> str:
    domain = meta.get("image_domain")
    if not domain:
        domain = random.choice(JmModuleConfig.DOMAIN_IMAGE_LIST)
    return f"https://{domain}/media/photos/{meta['photo_id']}/{filename}"


def _prune_page_cache() -> None:
    limit = config.PAGE_CACHE_MAX_FILES
    if limit <= 0:
        return
    root = config.CACHE_DIR / "pages"
    if not root.is_dir():
        return
    files = sorted(root.rglob("*"), key=lambda p: p.stat().st_mtime if p.is_file() else 0)
    files = [f for f in files if f.is_file()]
    for f in files[: max(0, len(files) - limit)]:
        try:
            f.unlink()
        except OSError:
            pass


def page_filename(photo_id: str, index: int) -> str:
    """章节第 ``index`` 页（1 起）的原始文件名。"""
    meta = _photo_meta(photo_id)
    pages = meta["pages"]
    if index < 1 or index > len(pages):
        raise IndexError(f"页码超出范围: {index} / {len(pages)}")
    return pages[index - 1]


def save_page(photo_id: str, index: int, dest_path: str) -> str:
    """把第 ``index`` 页下载并解扰到 ``dest_path``，返回原始文件名。

    下载与解扰全部交给 jmcomic，我们只负责决定存到哪里。

    .. important::
       ``dest_path`` **必须带上正确的图片扩展名**（如 ``.webp``）。
       jmcomic 底层用 ``PIL.Image.save()`` 落盘，而 PIL 是靠扩展名判断格式的，
       文件名结尾不是已知图片后缀会直接抛 ``ValueError: unknown file extension``。
    """
    suffix = Path(dest_path).suffix.lower()
    if suffix not in _CONTENT_TYPES:
        raise ValueError(f"dest_path 必须以图片扩展名结尾，收到：{dest_path!r}")

    meta = _photo_meta(photo_id)
    pages = meta["pages"]
    if index < 1 or index > len(pages):
        raise IndexError(f"页码超出范围: {index} / {len(pages)}")
    filename = pages[index - 1]
    _throttle()
    get_client().download_image(
        img_url=_image_url(meta, filename),
        img_save_path=str(dest_path),
        scramble_id=meta["scramble_id"] or None,
        decode_image=True,
    )
    return filename


def page_bytes(photo_id: str, index: int, use_cache: bool = True) -> tuple[bytes, str]:
    """取章节的第 ``index`` 页（1 起）。返回 (图片字节, content-type)。

    解扰由 jmcomic 完成；结果缓存在本地，避免同一页反复回源。
    """
    filename = page_filename(photo_id, index)
    suffix = Path(filename).suffix or ".webp"
    cache_file = config.CACHE_DIR / "pages" / str(photo_id) / f"{index:05d}{suffix}"

    if use_cache and cache_file.exists() and cache_file.stat().st_size > 0:
        return cache_file.read_bytes(), _content_type(filename)

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件必须保留真实图片后缀，否则 PIL 无法推断格式
    tmp_file = cache_file.with_name(f"{cache_file.stem}.part{cache_file.suffix}")
    try:
        save_page(photo_id, index, str(tmp_file))
    except Exception:
        tmp_file.unlink(missing_ok=True)
        raise
    tmp_file.replace(cache_file)
    _prune_page_cache()
    return cache_file.read_bytes(), _content_type(filename)


def cover_bytes(album_id: str) -> tuple[bytes, str]:
    """封面图（同样走代理）。落盘缓存 —— 同一本封面全站只从禁漫取一次。"""
    cache_file = config.CACHE_DIR / "covers" / f"{album_id}.jpg"
    if cache_file.exists() and cache_file.stat().st_size > 0:
        return cache_file.read_bytes(), "image/jpeg"

    _throttle()
    url = JmcomicText.get_album_cover_url(str(album_id))
    resp = get_client().get_jm_image(url)
    resp.require_success()
    data = resp.content
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(data)
    _prune_cover_cache()
    return data, "image/jpeg"


def _prune_cover_cache() -> None:
    """封面缓存也要有上限，否则浏览得越多磁盘越大。"""
    limit = config.COVER_CACHE_MAX_FILES
    if limit <= 0:
        return
    root = config.CACHE_DIR / "covers"
    if not root.is_dir():
        return
    files = [p for p in root.glob("*.jpg") if p.is_file()]
    if len(files) <= limit:
        return
    files.sort(key=lambda p: p.stat().st_mtime)
    for f in files[: len(files) - limit]:
        try:
            f.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ 收藏夹

def favorites(page: int = 1, folder_id: str = "0", order: str = "mr") -> dict:
    """收藏夹列表。需要登录；会话过期时会尝试自动重登一次。"""
    _require_login()
    order = order if order in {"mr", "mp"} else "mr"

    def produce() -> dict:
        _throttle()
        return get_client().favorite_folder(
            page=page, order_by=order, folder_id=str(folder_id), username=current_user() or ""
        )

    def fetch():
        # 命中缓存就不会真的出网，也就不会遇到 401
        return _cached(("favorites", page, folder_id, order), config.ALBUM_TTL, produce)

    data = _authed(fetch)

    raw_items = getattr(data, "content", None) or []
    items = []
    for it in raw_items:
        if isinstance(it, (tuple, list)) and len(it) == 2:
            aid, payload = it
            payload = payload if isinstance(payload, dict) else {}
            brief = _brief_from_dict(payload)
            if not brief["album_id"]:
                brief["album_id"] = _str_or(aid)
                brief["cover"] = _cover_url(brief["album_id"])
            items.append(brief)
        elif isinstance(it, dict):
            items.append(_brief_from_dict(it))

    folders = []
    for f in getattr(data, "folder_list", None) or []:
        if isinstance(f, dict):
            folders.append({
                "id": _str_or(f.get("FID") or f.get("id")),
                "name": _str_or(f.get("name")),
            })

    total = _int_or(getattr(data, "total", 0))
    return {
        "items": items,
        "folders": folders,
        "total": total,
        "page": page,
        "has_next": len(items) >= 20,
    }
