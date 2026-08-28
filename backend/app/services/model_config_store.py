from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from app.core.config import settings
from app.services.secret_store import protect_text, secure_storage_available, unprotect_text


class ModelConfigStoreError(RuntimeError):
    """模型配置仓储错误。

    这个仓储只负责本地持久化和密钥保护，不直接知道某个 provider 的 API 协议。
    provider 是否支持、默认模型是什么，由 ModelGateway 的 profile 负责。
    """


@dataclass(frozen=True)
class StoredModelConfig:
    provider: str = ""
    base_url: str = ""
    model: str = ""
    thinking: str = ""
    # 每个 provider 分别保存 DPAPI 密文。全局当前 provider 只是“默认运行时”，不能成为
    # 密钥所有权边界，否则用户切换模型会意外丢失已经配置好的其它 Key。
    api_key_secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    updated_at: str = ""

    @property
    def api_key_configured(self) -> bool:
        """返回当前默认 provider 是否已配置 Key。"""

        return self.api_key_configured_for(self.provider)

    @property
    def any_api_key_configured(self) -> bool:
        """只做安全存储状态判断，不解密任何 Key。"""

        return any(self._is_valid_secret(secret) for secret in self.api_key_secrets.values())

    @property
    def secure_storage(self) -> str:
        secret = self.api_key_secret_for(self.provider)
        return secret.get("storage", "") if secret else ""

    def api_key_secret_for(self, provider: str) -> dict[str, str] | None:
        secret = self.api_key_secrets.get(_normalize_provider_id(provider))
        return secret if self._is_valid_secret(secret) else None

    def api_key_configured_for(self, provider: str) -> bool:
        return self.api_key_secret_for(provider) is not None

    def decrypt_api_key(self, provider: str | None = None) -> str:
        # 只有真实调用或运行时解析需要明文 Key；状态接口只看 api_key_configured 即可。
        target_provider = _normalize_provider_id(provider or self.provider)
        secret = self.api_key_secret_for(target_provider)
        if secret is None:
            # 不能把 None 传给 DPAPI 解密层，否则调用方只能看到难以定位的类型错误。
            raise ModelConfigStoreError(f"模型供应商 {target_provider or '当前'} 未配置 API Key。")
        return unprotect_text(secret)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "StoredModelConfig":
        provider = _normalize_provider_id(str(data.get("provider") or ""))
        api_key_secrets = _read_api_key_secrets(data.get("api_key_secrets"))

        # 兼容 v1 的单 Key 配置。迁移只在下次保存时落盘为 v2，不读取或输出 Key 明文。
        legacy_secret = data.get("api_key_secret")
        if provider and cls._is_valid_secret(legacy_secret) and provider not in api_key_secrets:
            api_key_secrets[provider] = legacy_secret

        return cls(
            provider=provider,
            base_url=str(data.get("base_url") or "").strip(),
            model=str(data.get("model") or "").strip(),
            thinking=str(data.get("thinking") or "").strip().lower(),
            api_key_secrets=api_key_secrets,
            updated_at=str(data.get("updated_at") or "").strip(),
        )

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "version": 2,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "thinking": self.thinking,
            "updated_at": self.updated_at,
        }
        if self.api_key_secrets:
            data["api_key_secrets"] = self.api_key_secrets
        return data

    @staticmethod
    def _is_valid_secret(value: object) -> bool:
        return isinstance(value, dict) and bool(value.get("ciphertext"))


class ModelConfigRepository:
    """读写本地模型配置。

    配置文件属于低频变更数据，所以用 mtime/size 做轻量缓存；保存时使用临时文件替换，
    避免应用退出或系统中断时留下半截 JSON。缓存里不保存明文 API Key。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = Lock()
        self._cache_signature: tuple[Path, int, int] | None = None
        self._cache_config: StoredModelConfig | None = None

    @property
    def path(self) -> Path:
        return (self._path or settings.data_dir / "model_config.json").resolve()

    def load(self) -> StoredModelConfig:
        path = self.path
        if not path.exists():
            with self._lock:
                self._cache_signature = (path, 0, 0)
                self._cache_config = StoredModelConfig()
            return StoredModelConfig()

        stat = path.stat()
        signature = (path, stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if self._cache_signature == signature and self._cache_config is not None:
                return self._cache_config

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelConfigStoreError(f"模型配置文件读取失败：{exc}") from exc
        if not isinstance(data, dict):
            raise ModelConfigStoreError("模型配置文件顶层必须是 JSON object。")

        config = StoredModelConfig.from_json(data)
        with self._lock:
            self._cache_signature = signature
            self._cache_config = config
        return config

    def save(
        self,
        *,
        provider: str,
        base_url: str,
        model: str,
        thinking: str,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> StoredModelConfig:
        existing = self.load()
        normalized_provider = _normalize_provider_id(provider)

        api_key_secrets = dict(existing.api_key_secrets)
        if clear_api_key or api_key == "":
            api_key_secrets.pop(normalized_provider, None)
        elif api_key is not None:
            # protect_text 内部会拒绝无安全后端的平台，确保不会写出明文 Key。
            api_key_secrets[normalized_provider] = protect_text(api_key.strip()).to_json()

        config = StoredModelConfig(
            provider=normalized_provider,
            base_url=base_url.strip().rstrip("/"),
            model=model.strip(),
            thinking=thinking.strip().lower(),
            api_key_secrets=api_key_secrets,
            updated_at=_utc_now(),
        )
        self._write_atomic(config)
        return config

    def save_provider_api_key(self, *, provider: str, api_key: str) -> StoredModelConfig:
        """只新增或替换某个 provider 的 Key，不切换全局默认运行时。

        这用于客户预先配置图像/视频等专业 Agent 所需模型。配置过程始终使用 DPAPI，
        调用者拿到的仍是脱敏状态，不会从仓储取回明文。
        """

        normalized_provider = _normalize_provider_id(provider)
        value = api_key.strip()
        if not normalized_provider:
            raise ModelConfigStoreError("provider 不能为空。")
        if not value:
            raise ModelConfigStoreError("API Key 不能为空。")

        existing = self.load()
        api_key_secrets = dict(existing.api_key_secrets)
        api_key_secrets[normalized_provider] = protect_text(value).to_json()
        config = StoredModelConfig(
            provider=existing.provider,
            base_url=existing.base_url,
            model=existing.model,
            thinking=existing.thinking,
            api_key_secrets=api_key_secrets,
            updated_at=_utc_now(),
        )
        self._write_atomic(config)
        return config

    def _write_atomic(self, config: StoredModelConfig) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.tmp")
        payload = json.dumps(config.to_json(), ensure_ascii=False, indent=2)
        try:
            temp_path.write_text(payload, encoding="utf-8")
            temp_path.replace(path)
        except OSError as exc:
            raise ModelConfigStoreError(f"模型配置文件保存失败：{exc}") from exc

        stat = path.stat()
        with self._lock:
            self._cache_signature = (path, stat.st_mtime_ns, stat.st_size)
            self._cache_config = config


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


_DEFAULT_REPOSITORY = ModelConfigRepository()


def load_model_config() -> StoredModelConfig:
    return _DEFAULT_REPOSITORY.load()


def save_model_config(
    *,
    provider: str,
    base_url: str,
    model: str,
    thinking: str,
    api_key: str | None = None,
    clear_api_key: bool = False,
) -> StoredModelConfig:
    return _DEFAULT_REPOSITORY.save(
        provider=provider,
        base_url=base_url,
        model=model,
        thinking=thinking,
        api_key=api_key,
        clear_api_key=clear_api_key,
    )


def save_model_provider_api_key(*, provider: str, api_key: str) -> StoredModelConfig:
    """安全保存非当前 provider 的 Key，保留当前默认模型选择。"""

    return _DEFAULT_REPOSITORY.save_provider_api_key(provider=provider, api_key=api_key)


def model_config_path() -> Path:
    return _DEFAULT_REPOSITORY.path


def model_secure_storage_available() -> bool:
    return secure_storage_available()


def _normalize_provider_id(provider: str) -> str:
    return provider.strip().lower()


def _read_api_key_secrets(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    secrets: dict[str, dict[str, str]] = {}
    for provider, secret in value.items():
        normalized_provider = _normalize_provider_id(str(provider))
        if normalized_provider and StoredModelConfig._is_valid_secret(secret):
            secrets[normalized_provider] = secret
    return secrets
