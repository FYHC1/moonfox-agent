"""Write-only credential protection. Windows DPAPI or explicitly injected secure storage."""

from __future__ import annotations

import ctypes
import sys
from typing import Protocol


class SecretStorageError(RuntimeError):
    pass


class SecretProtector(Protocol):
    @property
    def kind(self) -> str: ...

    def protect(self, value: str, *, context: str) -> bytes: ...

    def unprotect(self, value: bytes, *, context: str) -> str: ...


class EnvironmentOnly:
    kind = "env_only"

    def protect(self, value: str, *, context: str) -> bytes:
        raise SecretStorageError("Stored credentials are unavailable; use environment references")

    def unprotect(self, value: bytes, *, context: str) -> str:
        raise SecretStorageError("Credential cannot be unlocked on this host")


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class WindowsDPAPI:
    """Current-user protection, no machine-wide key or plaintext fallback."""

    kind = "dpapi"

    @staticmethod
    def _transform(value: bytes, context: str, *, decrypt: bool) -> bytes:
        if sys.platform != "win32":
            raise SecretStorageError("Windows credential protection is unavailable")
        source_buffer = ctypes.create_string_buffer(value)
        entropy_value = ("moonfox-provider:" + context).encode("utf-8")
        entropy_buffer = ctypes.create_string_buffer(entropy_value)
        source = _Blob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        entropy = _Blob(
            len(entropy_value), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte))
        )
        output = _Blob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        operation.argtypes = [
            ctypes.POINTER(_Blob),
            ctypes.c_void_p,
            ctypes.POINTER(_Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_Blob),
        ]
        operation.restype = ctypes.c_int
        try:
            if not operation(
                ctypes.byref(source),
                None,
                ctypes.byref(entropy),
                None,
                None,
                1,
                ctypes.byref(output),
            ):
                raise SecretStorageError("Credential protection/unlock failed")
            return ctypes.string_at(output.data, output.size)
        finally:
            if output.data:
                kernel32.LocalFree(output.data)
            ctypes.memset(source_buffer, 0, len(source_buffer))

    def protect(self, value: str, *, context: str) -> bytes:
        return self._transform(value.encode("utf-8"), context, decrypt=False)

    def unprotect(self, value: bytes, *, context: str) -> str:
        try:
            return self._transform(value, context, decrypt=True).decode("utf-8")
        except (UnicodeError, OSError):
            raise SecretStorageError("Credential cannot be unlocked on this host") from None


def default_protector() -> SecretProtector:
    return WindowsDPAPI() if sys.platform == "win32" else EnvironmentOnly()
