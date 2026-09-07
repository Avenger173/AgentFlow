"""LGM5.7 开发者试点的脱敏准入契约。

这里的记录只证明某个已批准 Runtime 是否具备进入开发者试点的条件。它不保存客户目标、
材料名称、正文、模型名称、凭据或 Graph checkpoint；真实材料与模型的授权只以摘要和
不透明审批引用留痕。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_HEX64_PATTERN = r"^[a-f0-9]{64}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9_.:-]+$"
_REFERENCE_PATTERN = r"^[A-Za-z0-9_.:-]{8,160}$"

LangGraphTrialAdmissionStatus = Literal["admitted", "rejected", "revoked"]
LangGraphTrialEvidenceOrigin = Literal["developer_authorized_live", "fixture"]


class LangGraphCompositionTrialEvidence(BaseModel):
    """一次 Native/Graph 对照与资源基线的最小事实集合。"""

    model_config = ConfigDict(extra="forbid")

    evidence_origin: LangGraphTrialEvidenceOrigin
    approval_reference: str = Field(pattern=_REFERENCE_PATTERN)
    comparison_reference: str = Field(pattern=_REFERENCE_PATTERN)
    plan_digest: str = Field(pattern=_HEX64_PATTERN)
    material_scope_digest: str = Field(pattern=_HEX64_PATTERN)
    model_profile_digest: str = Field(pattern=_HEX64_PATTERN)
    native_reference_id: str = Field(pattern=_REFERENCE_PATTERN)
    graph_reference_id: str = Field(pattern=_REFERENCE_PATTERN)
    composition_comparison_passed: bool
    event_delivery_comparison_passed: bool
    source_artifact_comparison_passed: bool
    recovery_semantics_passed: bool
    native_retry_route_verified: bool
    real_materials_authorized: bool
    real_model_authorized: bool
    native_startup_ms: int = Field(ge=1, le=3_600_000)
    graph_startup_ms: int = Field(ge=1, le=3_600_000)
    native_resident_memory_mib: int = Field(ge=1, le=65_536)
    graph_resident_memory_mib: int = Field(ge=1, le=65_536)

    @field_validator("approval_reference", "comparison_reference", "native_reference_id", "graph_reference_id")
    @classmethod
    def _validate_reference(cls, value: str) -> str:
        if re.fullmatch(_REFERENCE_PATTERN, value) is None:
            raise ValueError("试点证据引用只能使用不透明标识，不能写入客户内容。")
        return value


class LangGraphCompositionTrialAdmissionRecord(BaseModel):
    """与一个 Runtime 任务绑定的试点准入记录，可在启动前撤销。"""

    model_config = ConfigDict(extra="forbid")

    runtime_task_id: str = Field(min_length=1, max_length=160, pattern=_IDENTIFIER_PATTERN)
    plan_digest: str = Field(pattern=_HEX64_PATTERN)
    status: LangGraphTrialAdmissionStatus
    evidence: LangGraphCompositionTrialEvidence
    blockers: tuple[str, ...] = Field(max_length=12)
    created_at: str = Field(min_length=1, max_length=40)
    updated_at: str = Field(min_length=1, max_length=40)

    @field_validator("blockers")
    @classmethod
    def _validate_blockers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("试点准入阻断项不能重复。")
        if any(not value.strip() or len(value) > 180 for value in values):
            raise ValueError("试点准入阻断项无效。")
        return values
