from abc import ABC, abstractmethod
from typing import Any

from app.schemas.agent import AgentDescriptor


class BaseAgent(ABC):
    """所有真实 Agent Runtime 未来要继承的最小接口。

    当前阶段只做 Registry 和 manifest 扫描，不动态 import 或执行第三方 Agent 代码。
    这个抽象先把边界立住：descriptor 是静态注册信息，execute 才是后续 Workflow
    Engine 会调用的运行入口。
    """

    descriptor: AgentDescriptor

    @abstractmethod
    async def execute(
        self,
        action: str,
        input_data: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """执行某个 Agent action。

        这里暂不落实现，原因是执行阶段必须先接入权限确认、任务状态和日志追踪。
        """

