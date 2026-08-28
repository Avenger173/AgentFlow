from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass


class SecretStoreError(RuntimeError):
    """密钥安全存储层错误。

    这里专门隔离平台相关的加密/解密逻辑，上层只处理“能否保存”和“是否已保存”，
    不需要知道 Windows DPAPI 的 ctypes 细节，也不能退回到明文存储。
    """


class SecretStoreUnavailable(SecretStoreError):
    """当前平台没有可用的安全密钥存储。"""


@dataclass(frozen=True)
class ProtectedSecret:
    storage: str
    ciphertext: str

    def to_json(self) -> dict[str, str]:
        return {
            "storage": self.storage,
            "ciphertext": self.ciphertext,
        }


def secure_storage_available() -> bool:
    """当前实现优先使用 Windows DPAPI。

    AgentFlow 目前面向 Windows 桌面端开发；后续如果要支持 macOS/Linux，
    再分别接 Keychain / libsecret。没有安全后端时，不保存 API Key 明文。
    """

    return os.name == "nt"


def protect_text(secret: str) -> ProtectedSecret:
    if not secure_storage_available():
        raise SecretStoreUnavailable("当前系统暂不支持安全保存 API Key。")

    ciphertext = _protect_with_windows_dpapi(secret.encode("utf-8"))
    return ProtectedSecret(
        storage="windows_dpapi_current_user",
        ciphertext=base64.b64encode(ciphertext).decode("ascii"),
    )


def unprotect_text(record: dict[str, str] | None) -> str:
    if not record:
        return ""

    storage = record.get("storage", "")
    if storage != "windows_dpapi_current_user":
        raise SecretStoreError(f"不支持的密钥存储类型：{storage}")
    if not secure_storage_available():
        raise SecretStoreUnavailable("当前系统无法读取 Windows DPAPI 保存的 API Key。")

    ciphertext = base64.b64decode(record.get("ciphertext", ""))
    return _unprotect_with_windows_dpapi(ciphertext).decode("utf-8")


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _blob_from_bytes(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    return blob, buffer


def _protect_with_windows_dpapi(data: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    input_blob, _buffer = _blob_from_bytes(data)
    output_blob = _DataBlob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN，避免后台服务弹出系统交互窗口。

    ok = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "AgentFlow model API key",
        None,
        None,
        None,
        flags,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise SecretStoreError(f"Windows DPAPI 加密失败：{ctypes.get_last_error()}")

    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))


def _unprotect_with_windows_dpapi(ciphertext: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    input_blob, _buffer = _blob_from_bytes(ciphertext)
    output_blob = _DataBlob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        flags,
        ctypes.byref(output_blob),
    )
    if not ok:
        raise SecretStoreError(f"Windows DPAPI 解密失败：{ctypes.get_last_error()}")

    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, wintypes.HLOCAL))
