"""凭据的落盘保护。

项目里**不允许出现明文密码或明文账号**。密码要能自动重新登录，就必须能还原
明文 —— 这是"自动登录"的固有限制，做不到哈希那种强度。既然必须可还原，
那就把钥匙交给**操作系统托管**，而不是和锁一起放进同一个抽屉。

按强度从高到低依次尝试，第一个可用的生效：

1. **Windows DPAPI（已在 Windows 实测）**
   ``CryptProtectData``。密钥由 Windows 从当前用户账户派生，**不落盘**。
   密文绑定"这台机器 + 这个 Windows 用户"：数据库被拷到别的机器、
   或被同机其它用户读到，都解不开。通过 ctypes 直接调系统 API，零新依赖。

2. **macOS 钥匙串（未实测）**
   走系统自带的 ``security`` 命令。**密码根本不写进数据库** ——
   jmreader.db 里只有一个标记，真正的密文在登录钥匙串里，由系统按用户保护。

3. **Linux Secret Service（未实测）**
   走 ``secret-tool``（libsecret）。同样**密码不进数据库**。
   需要系统装了 libsecret 且钥匙串守护进程在跑；没有就自动降级到第 4 种。

4. **AES-256-GCM + 机器绑定的本地密钥（兜底）**
   密钥文件在数据目录下（尽力 0600），但用它之前还要再混入**机器身份**
   （主机名 + 用户名 + home 路径）做一次 KDF。所以：

   - 单独把 jmreader.db 交出去 -> 解不开
   - **把整个 data/ 目录拷到另一台机器 -> 同样解不开**（密钥文件也不够）

   仍然不是操作系统级的保护，属于**混淆级别**，界面上会如实标注。
   如果主机名会变（例如容器每次重建都换 hostname），可以用环境变量
   ``JMREADER_SECRET_BIND`` 固定一个字符串，避免反复丢密码。

无论哪种后端，外部看到的都是同一串 ``v1:<backend>:<base64>`` 令牌；
``unseal`` 解不开时返回 ``None``（换了机器、换了用户、钥匙串被清掉、
数据被手改过），调用方按"未登录"处理，而不是崩溃。
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import ctypes.wintypes as wintypes
import getpass
import hashlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .. import config

_PREFIX = "v1"
_KEY_FILE = "secret.key"

# 钥匙串里的条目名。本程序只会保存一份凭据，所以用固定名字，
# 重复保存就是覆盖，不会越攒越多。
_KC_SERVICE = "JMReader"
_KC_ACCOUNT = "credentials"

# 外部命令一律限时，避免某个守护进程卡住导致整个请求挂死。
_CMD_TIMEOUT = 10
# 派生兜底密钥时的 KDF 轮数（结果有缓存，不会每次都算）
_KDF_ROUNDS = 100_000

_derived_cache: dict[bytes, bytes] = {}
_backends_cache: Optional[list[dict]] = None


# ------------------------------------------------------------------ Windows: DPAPI


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_call(func, data: bytes) -> bytes:
    """调用 CryptProtectData / CryptUnprotectData 的公共部分。"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = func(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_available() -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        ctypes.windll.crypt32
        return True
    except (AttributeError, OSError):
        return False


def _dpapi_protect(data: bytes) -> bytes:
    return _dpapi_call(ctypes.windll.crypt32.CryptProtectData, data)


def _dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi_call(ctypes.windll.crypt32.CryptUnprotectData, data)


# ------------------------------------------------------- macOS 钥匙串 / Linux 密钥环
#
# 这两种后端的共同点：**密码不进数据库**。seal() 把密文交给系统钥匙串，
# 数据库里只留一个标记。所以即使 jmreader.db 被完整拿走，也拿不到密码。


def _run(argv: list[str], stdin: Optional[bytes] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        timeout=_CMD_TIMEOUT,
        check=False,
    )


def _macos_tool() -> Optional[str]:
    if sys.platform != "darwin":
        return None
    return shutil.which("security")


def _macos_available() -> bool:
    return _macos_tool() is not None


def _macos_store(data: bytes) -> None:
    tool = _macos_tool()
    if not tool:
        raise OSError("找不到 security 命令")
    # -U：已存在则更新。值走 base64，避免密码里的特殊字符影响命令行。
    proc = _run([
        tool, "add-generic-password", "-U",
        "-s", _KC_SERVICE, "-a", _KC_ACCOUNT,
        "-w", base64.b64encode(data).decode("ascii"),
    ])
    if proc.returncode != 0:
        raise OSError((proc.stderr or b"").decode("utf-8", "replace")[:200] or "写入钥匙串失败")


def _macos_load() -> bytes:
    tool = _macos_tool()
    if not tool:
        raise OSError("找不到 security 命令")
    proc = _run([tool, "find-generic-password", "-s", _KC_SERVICE, "-a", _KC_ACCOUNT, "-w"])
    if proc.returncode != 0:
        raise OSError("钥匙串里没有这条凭据")
    return base64.b64decode(proc.stdout.strip(), validate=True)


def _macos_clear() -> None:
    tool = _macos_tool()
    if tool:
        _run([tool, "delete-generic-password", "-s", _KC_SERVICE, "-a", _KC_ACCOUNT])


def _secret_tool() -> Optional[str]:
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return None
    return shutil.which("secret-tool")


def _secret_service_available() -> bool:
    return _secret_tool() is not None


def _secret_service_store(data: bytes) -> None:
    tool = _secret_tool()
    if not tool:
        raise OSError("找不到 secret-tool")
    # 密文走 stdin —— 不进命令行，不会出现在 ps 里
    proc = _run(
        [tool, "store", "--label", _KC_SERVICE, "service", _KC_SERVICE, "account", _KC_ACCOUNT],
        stdin=base64.b64encode(data),
    )
    if proc.returncode != 0:
        raise OSError((proc.stderr or b"").decode("utf-8", "replace")[:200] or "写入密钥环失败")


def _secret_service_load() -> bytes:
    tool = _secret_tool()
    if not tool:
        raise OSError("找不到 secret-tool")
    proc = _run([tool, "lookup", "service", _KC_SERVICE, "account", _KC_ACCOUNT])
    if proc.returncode != 0 or not proc.stdout.strip():
        raise OSError("密钥环里没有这条凭据")
    return base64.b64decode(proc.stdout.strip(), validate=True)


def _secret_service_clear() -> None:
    tool = _secret_tool()
    if tool:
        _run([tool, "clear", "service", _KC_SERVICE, "account", _KC_ACCOUNT])


# ------------------------------------------------------------- 兜底: 机器绑定 AES


def _key_path() -> Path:
    return config.DATA_DIR / _KEY_FILE


def _machine_binding() -> bytes:
    """代表"这台机器的这个用户"的一串信息。

    它本身**不是秘密**（主机名/用户名都是可猜的），作用只有一个：
    让密钥文件离开这台机器就失效 —— 也就是挡住"把整个 data/ 目录拷走"。

    主机名不稳定的场景（容器每次重建都换 hostname）可以用
    ``JMREADER_SECRET_BIND`` 固定一个值，否则每次重建都要重新登录一次。
    """
    override = os.environ.get("JMREADER_SECRET_BIND", "").strip()
    if override:
        return override.encode("utf-8")
    try:
        user = getpass.getuser()
    except (OSError, KeyError):
        user = "unknown"
    parts = [platform.node() or "", user, str(Path.home())]
    return "|".join(parts).encode("utf-8")


def _load_or_create_key_file() -> Optional[bytes]:
    path = _key_path()
    try:
        if path.is_file():
            raw = path.read_bytes()
            if len(raw) >= 32:
                return raw[:32]
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        # 先按 0600 建，再写内容，避免出现"短暂可被他人读到"的窗口
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return key
    except OSError:
        return None


def _aes_key() -> Optional[bytes]:
    """密钥文件 + 机器身份 -> 实际用于 AES 的密钥。结果缓存，KDF 很贵。"""
    file_key = _load_or_create_key_file()
    if file_key is None:
        return None
    cached = _derived_cache.get(file_key)
    if cached is not None:
        return cached
    try:
        derived = hashlib.pbkdf2_hmac("sha256", file_key, _machine_binding(), _KDF_ROUNDS, 32)
    except (ValueError, TypeError):
        return None
    _derived_cache.clear()
    _derived_cache[file_key] = derived
    return derived


_NONCE_LEN = 12   # GCM 推荐 96 bit；显式指定，**不要**用 pycryptodome 的默认值
_TAG_LEN = 16


def _aes_protect(data: bytes) -> bytes:
    key = _aes_key()
    if key is None:
        raise OSError("无法创建密钥文件")
    from Crypto.Cipher import AES  # pycryptodome（jmcomic 的依赖，已在 requirements）

    # 必须显式给 nonce。pycryptodome 在 AES-GCM 下的**默认 nonce 是 16 字节**，
    # 而 GCM 的标准长度是 12；按 12 去解析就会整体错位、永远 MAC 校验失败。
    # 这里统一成 12 字节，存取两侧都用同一组常量，不再靠"记得是 12"。
    nonce = os.urandom(_NONCE_LEN)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(data)
    return nonce + tag + ciphertext


def _aes_unprotect(data: bytes) -> bytes:
    key = _aes_key()
    if key is None:
        raise OSError("找不到密钥文件")
    if len(data) < _NONCE_LEN + _TAG_LEN:
        raise ValueError("密文长度不对")
    from Crypto.Cipher import AES

    nonce = data[:_NONCE_LEN]
    tag = data[_NONCE_LEN:_NONCE_LEN + _TAG_LEN]
    ciphertext = data[_NONCE_LEN + _TAG_LEN:]
    return AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ciphertext, tag)


# ------------------------------------------------------------------ 后端描述


def _backends() -> list[dict]:
    """可用后端，按强度排序。第一个就是当前生效的。

    结果缓存 —— 这里要做 ``shutil.which``（扫 PATH），而平台能力在进程
    生命周期内不会变，没必要每次保存凭据都重扫一遍。
    """
    global _backends_cache
    if _backends_cache is not None:
        return _backends_cache
    out: list[dict] = []
    if _dpapi_available():
        out.append({
            "name": "dpapi",
            "label": "Windows DPAPI",
            "secure": True,
            "external": False,
            "note": (
                "密码由 Windows 用当前用户账户派生的密钥加密，密钥不落盘。"
                "密文绑定这台机器和这个 Windows 用户：数据库被拷到别的机器、"
                "或被同机其它用户读到，都解不开。"
            ),
        })
    if _macos_available():
        out.append({
            "name": "keychain-macos",
            "label": "macOS 登录钥匙串",
            "secure": True,
            "external": True,
            "note": (
                "密码保存在 macOS 登录钥匙串里，由系统按你的账户保护，"
                "**根本不写进 jmreader.db**。数据库被拷走也拿不到密码。"
            ),
        })
    if _secret_service_available():
        out.append({
            "name": "secret-service",
            "label": "Linux Secret Service（libsecret）",
            "secure": True,
            "external": True,
            "note": (
                "密码保存在系统的密钥环里（GNOME Keyring / KWallet 等），"
                "**根本不写进 jmreader.db**。数据库被拷走也拿不到密码。"
            ),
        })
    out.append({
        "name": "aes-file",
        "label": "AES-256-GCM（本地密钥文件 + 机器绑定）",
        "secure": False,
        "external": False,
        "note": (
            "当前系统没有可用的系统钥匙串，退化为本地密钥文件的 AES 加密。"
            "密钥里还混入了本机身份（主机名/用户名/home），所以"
            "**单独拷走 jmreader.db、甚至拷走整个 data/ 目录到别的机器，都解不开**。"
            "但它终究不是操作系统级的保护，属于混淆级别；"
            "拿到这台机器上同一用户权限的人仍可解密 —— 想稳妥就关掉「记住密码」。"
        ),
    })
    _backends_cache = out
    return out


def _active() -> dict:
    return _backends()[0]


def backend_name() -> str:
    return _active()["name"]


def backend_info() -> dict:
    """给界面和 /api/settings 用。``secure`` 不为真就别当保险箱用。"""
    b = _active()
    return {k: b[k] for k in ("name", "label", "secure", "note")}


# ------------------------------------------------------------------ 对外接口


def seal(plaintext: bytes) -> Optional[str]:
    """密封一段字节，返回可安全落盘的字符串令牌。失败返回 None。

    钥匙串类后端下，``plaintext`` 本身会被存进**系统钥匙串**，
    数据库里只留一个标记 —— 所以令牌里不含任何可用信息。
    """
    backend = _active()
    name = backend["name"]

    if name == "dpapi":
        try:
            payload = _dpapi_protect(plaintext)
        except OSError:
            return None
    elif name == "keychain-macos":
        try:
            _macos_store(plaintext)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        payload = b"credentials"
    elif name == "secret-service":
        try:
            _secret_service_store(plaintext)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        payload = b"credentials"
    else:
        try:
            payload = _aes_protect(plaintext)
        except (OSError, ValueError, ImportError):
            return None

    return f"{_PREFIX}:{name}:{base64.b64encode(payload).decode('ascii')}"


def unseal(token: Optional[str]) -> Optional[bytes]:
    """还原 ``seal`` 的产物。

    解不开一律返回 ``None``，不抛异常 —— 换机器、换用户、钥匙串被清掉、
    密钥文件丢了、数据被手改过，都属于"当作没有凭据"的正常情况。
    """
    if not token or not isinstance(token, str):
        return None
    parts = token.split(":", 2)
    if len(parts) != 3 or parts[0] != _PREFIX:
        return None
    _, backend, b64 = parts
    try:
        payload = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None

    try:
        if backend == "dpapi":
            if not _dpapi_available():
                return None      # 在非 Windows 上读 Windows 写的密文
            return _dpapi_unprotect(payload)
        if backend == "keychain-macos":
            return _macos_load()
        if backend == "secret-service":
            return _secret_service_load()
        if backend == "aes-file":
            return _aes_unprotect(payload)
    except Exception:  # noqa: BLE001 —— 任何解不开都等价于"没有凭据"
        return None
    return None


def wipe() -> None:
    """清掉当前后端留下的痕迹（退出登录时调用）。

    钥匙串里那条也会删掉 —— 否则"退出登录"之后系统里还留着密码。
    """
    try:
        _key_path().unlink(missing_ok=True)
    except OSError:
        pass
    _derived_cache.clear()

    for clearer in (_macos_clear, _secret_service_clear):
        try:
            clearer()
        except Exception:  # noqa: BLE001
            pass


# 兼容旧调用名
wipe_key_file = wipe
