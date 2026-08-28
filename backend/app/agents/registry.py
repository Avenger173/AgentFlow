from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.schemas.agent import AgentDescriptor


@dataclass(frozen=True)
class ManifestSignature:
    path: str
    mtime_ns: int
    size: int


class AgentRegistry:
    """从 manifest.yaml 加载 Agent 元数据的注册表。

    当前阶段的 Registry 只负责“发现和校验 Agent 描述”，不 import agent.py，
    也不执行插件代码。这样可以先让 Qt 端显示真实注册结果，同时把插件执行、安全确认、
    依赖安装等高风险能力留到后续阶段。
    """

    def __init__(self, builtin_dir: Path, user_dir: Path) -> None:
        self.builtin_dir = builtin_dir
        self.user_dir = user_dir
        self._agents: dict[str, AgentDescriptor] = {}
        self._errors: list[str] = []
        self._loaded = False
        self._signature: tuple[ManifestSignature, ...] = ()

    def reload(self) -> None:
        """重新扫描内置目录和用户目录。

        内置目录优先加载；如果用户目录出现同 id manifest，暂时跳过而不是覆盖。
        这个策略保护内置 Commander/Document/Code/Report 的稳定行为，等插件权限和签名机制
        做好后，再考虑受控覆盖或禁用内置 Agent。
        """

        self._signature = self._collect_signature()
        self._reload_from_disk()

    def refresh_if_changed(self) -> None:
        """按 manifest 文件签名决定是否重新扫描。

        这里的性能取舍是：每次请求只做很轻的目录 glob + stat，不重复解析 YAML。
        当 manifest 路径、mtime 或大小变化时才重新加载，开发期改 manifest 仍会自动生效。
        """

        signature = self._collect_signature()
        if self._loaded and signature == self._signature:
            return

        self._signature = signature
        self._reload_from_disk()

    def _reload_from_disk(self) -> None:
        self._agents = {}
        self._errors = []

        self._load_dir(self.builtin_dir, source="builtin", builtin=True, allow_override=False)
        self._load_dir(self.user_dir, source="user", builtin=False, allow_override=False)
        self._loaded = True

    def list_agents(self) -> list[AgentDescriptor]:
        """返回按来源和 id 排序后的 Agent 列表，保证 API 响应顺序稳定。"""

        self._ensure_loaded()
        return sorted(
            self._agents.values(),
            key=lambda agent: (0 if agent.builtin else 1, agent.sort_order, agent.id),
        )

    def get_agent(self, agent_id: str) -> AgentDescriptor | None:
        self._ensure_loaded()
        return self._agents.get(agent_id)

    def errors(self) -> list[str]:
        # registry_status() 通常会先 list_agents()，此时错误列表已经是最新扫描结果。
        # 如果外部直接查询 errors，再做一次懒加载即可。
        if not self._loaded:
            self.reload()
        return list(self._errors)

    def _ensure_loaded(self) -> None:
        self.refresh_if_changed()

    def _collect_signature(self) -> tuple[ManifestSignature, ...]:
        """收集当前可见 manifest 的轻量签名。

        签名只包含路径、mtime_ns 和 size。它不读取 YAML 内容，因此性能开销稳定；
        极少数文件系统 mtime 粒度异常导致的漏刷新，可以通过显式 reload 兜底。
        """

        signatures: list[ManifestSignature] = []
        for root in (self.builtin_dir, self.user_dir):
            if not root.exists() or not root.is_dir():
                # 把目录缺失也纳入签名，目录创建/删除时能触发重新扫描和错误刷新。
                marker = -1 if not root.exists() else -2
                signatures.append(ManifestSignature(str(root), marker, marker))
                continue

            for manifest_path in sorted(root.glob("*/manifest.yaml")):
                try:
                    stat = manifest_path.stat()
                except OSError:
                    signatures.append(ManifestSignature(str(manifest_path), -3, -3))
                    continue

                signatures.append(
                    ManifestSignature(
                        str(manifest_path),
                        stat.st_mtime_ns,
                        stat.st_size,
                    )
                )

        return tuple(signatures)

    def _load_dir(
        self,
        root: Path,
        *,
        source: str,
        builtin: bool,
        allow_override: bool,
    ) -> None:
        if not root.exists():
            if builtin:
                self._errors.append(f"内置 Agent 目录不存在：{root}")
            return

        if not root.is_dir():
            self._errors.append(f"Agent 路径不是目录：{root}")
            return

        # 只扫描 agents/*/manifest.yaml，不递归进入更深层目录。
        # 这能避免误读 prompts/、data/ 或临时解压残留里的 manifest。
        for manifest_path in sorted(root.glob("*/manifest.yaml")):
            self._load_manifest(
                manifest_path,
                source=source,
                builtin=builtin,
                allow_override=allow_override,
            )

    def _load_manifest(
        self,
        manifest_path: Path,
        *,
        source: str,
        builtin: bool,
        allow_override: bool,
    ) -> None:
        try:
            descriptor = self._read_descriptor(manifest_path, source=source, builtin=builtin)
        except (OSError, ValueError, ValidationError) as exc:
            self._errors.append(f"{manifest_path}: {exc}")
            return

        if descriptor.id in self._agents and not allow_override:
            self._errors.append(
                f"{manifest_path}: Agent id '{descriptor.id}' 已存在，已跳过重复 manifest。"
            )
            return

        self._agents[descriptor.id] = descriptor

    def _read_descriptor(self, manifest_path: Path, *, source: str, builtin: bool) -> AgentDescriptor:
        # UTF-8 是项目约定；这里显式声明，避免中文 name/description 在 Windows 区域设置下乱码。
        with manifest_path.open("r", encoding="utf-8") as file:
            payload = yaml.safe_load(file) or {}

        if not isinstance(payload, dict):
            raise ValueError("manifest.yaml 顶层必须是 YAML object。")

        data = dict(payload)
        # source/builtin 由扫描目录决定，不信任用户 manifest 自己声明的值。
        data["source"] = source
        data["builtin"] = builtin

        descriptor = AgentDescriptor.model_validate(data)
        if not descriptor.id:
            raise ValueError("Agent id 不能为空。")

        return descriptor


def manifest_field_names() -> set[str]:
    """返回当前 manifest 支持的字段名，给后续校验/文档生成复用。

    目前没有强制拒绝额外字段，因为插件规范还在演进；Pydantic 会忽略未知字段。
    """

    return set(AgentDescriptor.model_fields.keys())
