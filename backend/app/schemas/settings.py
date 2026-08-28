from typing import Literal

from pydantic import BaseModel, Field


PermissionPolicy = Literal[
    "always_ask",
    "auto_approve",
    "smart_confirm",
    "full_access",
]
AgentPersonality = Literal[
    "professional",
    "concise",
    "warm",
    "creative",
]


class RuntimePreferenceOption(BaseModel):
    """设置页选项说明。

    后端返回选项说明，Qt 不必把每种权限模式的解释硬编码在页面里。
    """

    value: str
    label: str
    description: str


class RuntimePreferencesUpdateRequest(BaseModel):
    """运行偏好更新请求。

    这些偏好只影响平台确认策略、计划摘要和 Agent 表达风格；真实权限边界仍由 Runtime
    和工具层校验，不能因为用户选择高权限模式就跳过审计记录。
    """

    permission_policy: PermissionPolicy = "smart_confirm"
    personality: AgentPersonality = "professional"
    # 默认关闭。开启后总指挥只读取用户确认、已启用且与当前目标相关的短记忆。
    memory_enabled: bool = False


class RuntimePreferencesResponse(RuntimePreferencesUpdateRequest):
    updated_at: str = ""
    notes: str = ""
    permission_policy_options: list[RuntimePreferenceOption] = Field(default_factory=list)
    personality_options: list[RuntimePreferenceOption] = Field(default_factory=list)
