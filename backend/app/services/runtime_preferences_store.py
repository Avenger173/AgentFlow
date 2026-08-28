from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from app.core.config import settings
from app.schemas.chat import WorkflowPlanPreferences


VALID_PERMISSION_POLICIES = {
    "always_ask",
    "auto_approve",
    "smart_confirm",
    "full_access",
}
VALID_PERSONALITIES = {
    "professional",
    "concise",
    "warm",
    "creative",
}


class RuntimePreferencesStoreError(RuntimeError):
    """运行偏好仓储错误。"""


@dataclass(frozen=True)
class StoredRuntimePreferences:
    """本地运行偏好。

    这里保存的是平台级偏好，不保存 API Key、不保存用户任务正文。真实 Runtime 仍要按
    工具权限和审计记录做最终裁决，不能只相信这里的高权限选项。
    """

    permission_policy: str = "smart_confirm"
    personality: str = "professional"
    memory_enabled: bool = False
    updated_at: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "StoredRuntimePreferences":
        permission_policy = str(data.get("permission_policy") or "smart_confirm").strip().lower()
        personality = str(data.get("personality") or "professional").strip().lower()
        if permission_policy not in VALID_PERMISSION_POLICIES:
            permission_policy = "smart_confirm"
        if personality not in VALID_PERSONALITIES:
            personality = "professional"
        return cls(
            permission_policy=permission_policy,
            personality=personality,
            memory_enabled=bool(data.get("memory_enabled", False)),
            updated_at=str(data.get("updated_at") or "").strip(),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 2,
            "permission_policy": self.permission_policy,
            "personality": self.personality,
            "memory_enabled": self.memory_enabled,
            "updated_at": self.updated_at,
        }

    def to_workflow_preferences(self) -> WorkflowPlanPreferences:
        return WorkflowPlanPreferences(
            permission_policy=self.permission_policy,
            personality=self.personality,
            memory_enabled=self.memory_enabled,
        )


class RuntimePreferencesRepository:
    """读写本地运行偏好。

    运行偏好属于低频配置，使用 mtime/size 做轻量缓存；保存时写临时文件再替换，避免
    进程中断留下半截 JSON。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = Lock()
        self._cache_signature: tuple[Path, int, int] | None = None
        self._cache_preferences: StoredRuntimePreferences | None = None

    @property
    def path(self) -> Path:
        return (self._path or settings.data_dir / "runtime_preferences.json").resolve()

    def load(self) -> StoredRuntimePreferences:
        path = self.path
        if not path.exists():
            preferences = StoredRuntimePreferences()
            with self._lock:
                self._cache_signature = (path, 0, 0)
                self._cache_preferences = preferences
            return preferences

        stat = path.stat()
        signature = (path, stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if self._cache_signature == signature and self._cache_preferences is not None:
                return self._cache_preferences

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimePreferencesStoreError(f"运行偏好读取失败：{exc}") from exc
        if not isinstance(data, dict):
            raise RuntimePreferencesStoreError("运行偏好文件顶层必须是 JSON object。")

        preferences = StoredRuntimePreferences.from_json(data)
        with self._lock:
            self._cache_signature = signature
            self._cache_preferences = preferences
        return preferences

    def save(
        self,
        *,
        permission_policy: str,
        personality: str,
        memory_enabled: bool,
    ) -> StoredRuntimePreferences:
        normalized_policy = permission_policy.strip().lower()
        normalized_personality = personality.strip().lower()
        if normalized_policy not in VALID_PERMISSION_POLICIES:
            raise RuntimePreferencesStoreError(f"未知权限策略：{permission_policy}")
        if normalized_personality not in VALID_PERSONALITIES:
            raise RuntimePreferencesStoreError(f"未知 Agent 风格：{personality}")

        preferences = StoredRuntimePreferences(
            permission_policy=normalized_policy,
            personality=normalized_personality,
            memory_enabled=bool(memory_enabled),
            updated_at=_utc_now(),
        )
        self._write_atomic(preferences)
        return preferences

    def _write_atomic(self, preferences: StoredRuntimePreferences) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.tmp")
        payload = json.dumps(preferences.to_json(), ensure_ascii=False, indent=2)
        try:
            temp_path.write_text(payload, encoding="utf-8")
            temp_path.replace(path)
        except OSError as exc:
            raise RuntimePreferencesStoreError(f"运行偏好保存失败：{exc}") from exc

        stat = path.stat()
        with self._lock:
            self._cache_signature = (path, stat.st_mtime_ns, stat.st_size)
            self._cache_preferences = preferences


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


_DEFAULT_REPOSITORY = RuntimePreferencesRepository()


def load_runtime_preferences() -> StoredRuntimePreferences:
    return _DEFAULT_REPOSITORY.load()


def save_runtime_preferences(
    *,
    permission_policy: str,
    personality: str,
    memory_enabled: bool,
) -> StoredRuntimePreferences:
    return _DEFAULT_REPOSITORY.save(
        permission_policy=permission_policy,
        personality=personality,
        memory_enabled=memory_enabled,
    )


def runtime_preferences_path() -> Path:
    return _DEFAULT_REPOSITORY.path
