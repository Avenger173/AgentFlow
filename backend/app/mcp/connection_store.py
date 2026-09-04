"""LGM2 内置 MCP 连接的低频状态仓储。

第一条客户连接没有 URL、Token 或客户命令：它只对应随 AgentFlow 发布的 Wikimedia
公开资料服务。这里持久化的只有“客户是否启用”与最近工具检测摘要，使用 mtime/size
缓存并原子替换写入，避免启动期和聊天高频路径反复扫描配置文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from app.core.config import settings


PUBLIC_REFERENCE_CONNECTION_ID = "public-reference"


class McpConnectionStoreError(RuntimeError):
    """内置 MCP 连接状态读取或写入失败。"""


@dataclass(frozen=True)
class McpConnectionState:
    connection_id: str = PUBLIC_REFERENCE_CONNECTION_ID
    enabled: bool = False
    updated_at: str = ""
    last_checked_at: str = ""
    last_check_status: str = "not_checked"
    last_tool_count: int = 0
    last_error_code: str = ""

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> "McpConnectionState":
        if str(value.get("connection_id") or PUBLIC_REFERENCE_CONNECTION_ID) != PUBLIC_REFERENCE_CONNECTION_ID:
            return cls()
        try:
            tool_count = int(value.get("last_tool_count") or 0)
        except (TypeError, ValueError):
            tool_count = 0
        return cls(
            enabled=bool(value.get("enabled", False)),
            updated_at=str(value.get("updated_at") or "")[:40],
            last_checked_at=str(value.get("last_checked_at") or "")[:40],
            last_check_status=(str(value.get("last_check_status") or "not_checked")[:32]),
            last_tool_count=max(0, min(tool_count, 32)),
            last_error_code=str(value.get("last_error_code") or "")[:80],
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "connection_id": self.connection_id,
            "enabled": self.enabled,
            "updated_at": self.updated_at,
            "last_checked_at": self.last_checked_at,
            "last_check_status": self.last_check_status,
            "last_tool_count": self.last_tool_count,
            "last_error_code": self.last_error_code,
        }


class McpConnectionStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = Lock()
        self._cache_signature: tuple[Path, int, int] | None = None
        self._cache_state: McpConnectionState | None = None

    @property
    def path(self) -> Path:
        return (self._path or settings.data_dir / "mcp_connections.json").resolve()

    def load_public_reference(self) -> McpConnectionState:
        path = self.path
        if not path.exists():
            state = McpConnectionState()
            with self._lock:
                self._cache_signature = (path, 0, 0)
                self._cache_state = state
            return state
        stat = path.stat()
        signature = (path, stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if self._cache_signature == signature and self._cache_state is not None:
                return self._cache_state
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise McpConnectionStoreError(f"MCP 连接状态读取失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise McpConnectionStoreError("MCP 连接状态文件顶层必须是 JSON object。")
        state = McpConnectionState.from_json(payload)
        with self._lock:
            self._cache_signature = signature
            self._cache_state = state
        return state

    def set_enabled(self, enabled: bool) -> McpConnectionState:
        current = self.load_public_reference()
        return self._write(current.__class__(
            enabled=bool(enabled),
            updated_at=_utc_now(),
            last_checked_at=current.last_checked_at,
            last_check_status=current.last_check_status,
            last_tool_count=current.last_tool_count,
            last_error_code=current.last_error_code,
        ))

    def record_check(self, *, tool_count: int = 0, error_code: str = "") -> McpConnectionState:
        current = self.load_public_reference()
        return self._write(current.__class__(
            enabled=current.enabled,
            updated_at=current.updated_at,
            last_checked_at=_utc_now(),
            last_check_status="failed" if error_code else "ready",
            last_tool_count=max(0, min(tool_count, 32)),
            last_error_code=error_code[:80],
        ))

    def _write(self, state: McpConnectionState) -> McpConnectionState:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        try:
            temporary.write_text(json.dumps(state.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            raise McpConnectionStoreError(f"MCP 连接状态保存失败：{exc}") from exc
        stat = path.stat()
        with self._lock:
            self._cache_signature = (path, stat.st_mtime_ns, stat.st_size)
            self._cache_state = state
        return state


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


_DEFAULT_STORE = McpConnectionStore()


def load_public_reference_connection() -> McpConnectionState:
    return _DEFAULT_STORE.load_public_reference()


def set_public_reference_enabled(enabled: bool) -> McpConnectionState:
    return _DEFAULT_STORE.set_enabled(enabled)


def record_public_reference_check(*, tool_count: int = 0, error_code: str = "") -> McpConnectionState:
    return _DEFAULT_STORE.record_check(tool_count=tool_count, error_code=error_code)
