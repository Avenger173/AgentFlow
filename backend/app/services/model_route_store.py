"""低频模型路由 Profile 的本地持久化。

这里刻意与 ``model_config_store`` 分离：后者持有全局默认模型和 Provider 密钥，
本模块只保存“哪个产品作用域继承默认模型，或使用哪份显式非敏感配置”。两者都使用
原子替换，且本文件绝不包含 API Key、模型回复或客户材料。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from app.core.config import settings
from app.schemas.model import ModelRouteScope, ModelRouteSettings


class ModelRouteStoreError(RuntimeError):
    """模型路由配置文件损坏或无法安全写入时抛出。"""


MODEL_ROUTE_DEFINITIONS: dict[str, tuple[str, str, tuple[str, ...], bool]] = {
    "commander_planning": (
        "总指挥规划",
        "生成本轮计划说明与客户可见回复。",
        (),
        True,
    ),
    "commander_synthesis": (
        "总指挥汇总",
        "汇总已完成专业分支；当前 C6.4 使用确定性汇总，配置会保留到模型汇总启用时。",
        (),
        False,
    ),
    "document_analysis": (
        "文档分析",
        "受控读取、结构化分析与来源收束。",
        ("json_output", "tool_calls"),
        True,
    ),
    "document_presentation": (
        "文档与 PPT 制作",
        "项目方案、审查和可编辑 PPT 的结构化创作。",
        ("json_output",),
        True,
    ),
    "data_insight": (
        "数据洞察",
        "基于确定性统计结果生成解释与结论。",
        ("json_output",),
        True,
    ),
    "knowledge_answer": (
        "知识库问答",
        "仅依据活动索引中的证据生成带来源回答。",
        ("json_output",),
        True,
    ),
    "knowledge_deep_analysis": (
        "知识库深度分析",
        "Map-Reduce 的章节小结与递归归并。",
        ("json_output",),
        True,
    ),
    "visual_generation": (
        "视觉生成",
        "图片生成 Provider 尚未接入通用模型 Profile，当前仅保留显式路由位置。",
        ("visual_generation",),
        False,
    ),
}


def list_model_route_ids() -> tuple[str, ...]:
    """返回稳定顺序，供 API 和 Qt 使用同一份低频配置目录。"""

    return tuple(MODEL_ROUTE_DEFINITIONS)


def default_model_route_settings(route_id: ModelRouteScope) -> ModelRouteSettings:
    return ModelRouteSettings(route_id=route_id)


class ModelRouteRepository:
    """带轻量缓存的 Profile 文件仓储。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = Lock()
        self._cache_signature: tuple[Path, int, int] | None = None
        self._cache_routes: dict[str, ModelRouteSettings] | None = None

    @property
    def path(self) -> Path:
        return (self._path or settings.data_dir / "model_route_profiles.json").resolve()

    def load_all(self) -> dict[str, ModelRouteSettings]:
        path = self.path
        if not path.exists():
            return {}
        stat = path.stat()
        signature = (path, stat.st_mtime_ns, stat.st_size)
        with self._lock:
            if self._cache_signature == signature and self._cache_routes is not None:
                return dict(self._cache_routes)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelRouteStoreError(f"模型路由配置读取失败：{exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("routes", {}), dict):
            raise ModelRouteStoreError("模型路由配置必须包含 routes JSON object。")

        routes: dict[str, ModelRouteSettings] = {}
        for route_id, raw in payload["routes"].items():
            if route_id not in MODEL_ROUTE_DEFINITIONS or not isinstance(raw, dict):
                continue
            try:
                settings_value = ModelRouteSettings.model_validate({**raw, "route_id": route_id})
            except ValueError:
                # 无法识别的单条配置按继承全局处理，不能让一个旧字段阻止模型页恢复。
                continue
            routes[route_id] = settings_value
        with self._lock:
            self._cache_signature = signature
            self._cache_routes = dict(routes)
        return routes

    def load(self, route_id: ModelRouteScope) -> ModelRouteSettings:
        return self.load_all().get(route_id, default_model_route_settings(route_id))

    def save(self, settings_value: ModelRouteSettings) -> ModelRouteSettings:
        routes = self.load_all()
        saved = settings_value.model_copy(update={"updated_at": _utc_now()})
        routes[saved.route_id] = saved
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.tmp")
        payload = {
            "version": 1,
            "routes": {route_id: route.model_dump(mode="json") for route_id, route in routes.items()},
        }
        try:
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(path)
        except OSError as exc:
            raise ModelRouteStoreError(f"模型路由配置保存失败：{exc}") from exc
        stat = path.stat()
        with self._lock:
            self._cache_signature = (path, stat.st_mtime_ns, stat.st_size)
            self._cache_routes = dict(routes)
        return saved


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


_DEFAULT_REPOSITORY = ModelRouteRepository()


def load_model_route_settings(route_id: ModelRouteScope) -> ModelRouteSettings:
    return _DEFAULT_REPOSITORY.load(route_id)


def list_stored_model_route_settings() -> dict[str, ModelRouteSettings]:
    return _DEFAULT_REPOSITORY.load_all()


def save_model_route_settings(settings_value: ModelRouteSettings) -> ModelRouteSettings:
    return _DEFAULT_REPOSITORY.save(settings_value)
