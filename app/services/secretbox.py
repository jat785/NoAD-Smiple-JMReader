"""凭据的落盘保护。

项目里**不允许出现明文密码或明文账号**。密码要能自动重新登录，就必须能还原
明文 —— 这是"自动登录"的固有限制，做不到哈希那种强度。既然必须可还原，
那就用**操作系统托管的密钥**，而不是把钥匙和锁放进同一个抽屉。

各平台的实际强度差别很大，这里如实标注，不含糊：

- **Windows（已验证）**：DPAPI（``CryptProtectData``）。密钥由 Windows 从
  当前用户账户派生，**不落在任何文件里**。密文绑定"这台机器 + 这个 Windows
  用户"：数据库被拷到别的机器、或被同机其它用户读到，都**解不开**。
  通过 ctypes 直接调系统 API，不引入任何新依赖。

- **macOS / Linux（未实测，不保证安全）**：退化为 AES-256-GCM，密钥放在
  数据目录下的 ``secret.key``（尽力设为 0600）。这只能挡住"只拿到
  jmreader.db 一个文件"的情况（备份、贴给别人排查等），**拿到整个数据目录
  的人可以连同密钥一起拿走 —— 属于混淆级别，不是加密级别**。
  这两个平台上正确的做法是 Keychain / Secret Service，本项目没做。

无论哪种后端，外部看到的都是同一串 ``v1:<backend>:<base64>`` 令牌；
``unseal`` 解不开时返回 ``None``（例如数据库换了机器），调用方应当
按"登录已失效、需要重新登录"处理，而不是崩溃。
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import ctypes.wintypes as wintypes
import os
import sys
from pathlib import Path
from typing import Optional

from .. import config

_PREFIX = "v1"
_KEY_FILE = "secret.key"

_win_dpapi_warned = False


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
    ok = func(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
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


# ------------------------------------------------------------- 其它平台: AES 兜底


def _key_path() -> Path:
    return config.DATA_DIR / _KEY_FILE


def _load_or_create_key() -> Optional[bytes]:
    """取 AES 兜底用的密钥；没有就生成一个。

    注意：这把钥匙**和数据库放在同一个数据目录里**，所以它挡不住拿到整个
    目录的人。它的价值只有一个 —— 单独把 jmreader.db 交出去时不会泄露凭据。
    """
    path = _key_path()
    try:
        if path.is_file():
            raw = path.read_bytes()
            if len(raw) >= 32:
                return raw[:32]
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        # 先按 0600 创建，再写内容，避免出现"短暂可被他人读到"的窗口
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


def _aes_protect(data: bytes) -> bytes:
    key = _load_or_create_key()
    if key is None:
        raise OSError("无法创建密钥文件")
    from Crypto.Cipher import AES  # pycryptodome（jmcomic 的依赖，已在 requirements 里）

    cipher = AES.new(key, AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(data)
    return cipher.nonce + tag + ciphertext


def _aes_unprotect(data: bytes) -> bytes:
    key = _load_or_create_key()
    if key is None:
        raise OSError("找不到密钥文件")
    if len(data) < 28:
        raise ValueError("密文长度不对")
    from Crypto.Cipher import AES

    nonce, tag, ciphertext = data[:12], data[12:28], data[28:]
    return AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ciphertext, tag)


# ------------------------------------------------------------------ 对外接口


def backend_name() -> str:
    return "dpapi" if _dpapi_available() else "aes-file"


def backend_info() -> dict:
    """给界面和 /api/settings 用的说明。``secure`` 不为真就别当保险箱用。"""
    if _dpapi_available():
        return {
            "name": "dpapi",
            "label": "Windows DPAPI",
            "secure": True,
            "note": (
                "密码由 Windows 用当前用户账户派生的密钥加密，密钥不落盘。"
                "密文绑定这台机器和这个 Windows 用户：数据库被拷到别的机器、"
                "或被同机其它用户读到，都解不开。"
            ),
        }
    return {
        "name": "aes-file",
        "label": "AES-256-GCM（本地密钥文件）",
        "secure": False,
        "note": (
            "当前系统不是 Windows，没有 DPAPI 可用，退化为本地密钥文件的 AES 加密。"
            "它只能保证「单独把 jmreader.db 交出去」不会泄露密码；"
            "拿到整个数据目录的人可以连同 secret.key 一起拿走。"
            "这属于混淆级别，不保证安全 —— 想稳妥就关掉「记住密码」。"
        ),
    }


def seal(plaintext: bytes) -> Optional[str]:
    """密封一段字节，返回可安全落盘的字符串令牌。失败返回 None。"""
    if _dpapi_available():
        try:
            payload = _dpapi_protect(plaintext)
            backend = "dpapi"
        except OSError:
            return None
    else:
        try:
            payload = _aes_protect(plaintext)
            backend = "aes-file"
        except (OSError, ValueError, ImportError):
            return None
    return f"{_PREFIX}:{backend}:{base64.b64encode(payload).decode('ascii')}"


def unseal(token: Optional[str]) -> Optional[bytes]:
    """还原 ``seal`` 的产物。

    解不开一律返回 ``None``，不抛异常 —— 换机器、换用户、密钥文件丢了、
    数据被手改过，都属于"当作没有凭据"的正常情况。
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
                return None          # 在非 Windows 上读 Windows 写的密文
            return _dpapi_unprotect(payload)
        if backend == "aes-file":
            return _aes_unprotect(payload)
    except Exception:  # noqa: BLE001 —— 任何解不开都等价于"没有凭据"
        return None
    return None


def wipe_key_file() -> None:
    """删掉兜底密钥（退出登录时顺手清理，避免残留）。"""
    try:
        _key_path().unlink(missing_ok=True)
    except OSError:
        pass
