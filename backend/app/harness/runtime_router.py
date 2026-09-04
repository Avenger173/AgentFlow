"""ExecutionBackend 的确定性选择器。

Router 不读取客户正文、不调用模型，也不接受 LLM 给出的后端名称。LGM3 只允许它把一类
固定的内部测试图路由到 LangGraph；所有客户任务和任何副作用任务继续落在 Native Runtime。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


RuntimeRouteBackend = Literal["native", "langgraph"]

_LGM3_GRAPH_ID = "lgm3_deterministic_fixture"
_LGM3_GRAPH_VERSION = "v1"


@dataclass(frozen=True)
class RuntimeRoutingRequest:
    """Router 做选择所需的已验证事实，不包含自然语言或模型建议。"""

    task_id: str
    requested_backend: str = "native"
    graph_id: str = ""
    graph_version: str = ""
    internal_test: bool = False
    feature_enabled: bool = False
    graph_admitted: bool = False
    read_only: bool = True
    side_effects_started: bool = False

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("RuntimeRouter 需要非空 task_id。")


@dataclass(frozen=True)
class RuntimeRoute:
    """一次路由选择的可审计摘要。"""

    backend_id: RuntimeRouteBackend
    accepted: bool
    reason: str


class RuntimeRouter:
    """按静态准入规则选择执行后端。

    这里刻意没有“根据模型推荐切 LangGraph”的路径。只有内部夹具同时满足开关、图版本、
    只读和无副作用四项条件时才会返回 LangGraph，避免依赖已安装就扩大客户能力范围。
    """

    def select(self, request: RuntimeRoutingRequest) -> RuntimeRoute:
        if request.requested_backend != "langgraph":
            return RuntimeRoute(
                backend_id="native",
                accepted=True,
                reason="未请求 LangGraph，保持 Native Runtime 默认路径。",
            )
        if not request.internal_test:
            return RuntimeRoute(
                backend_id="native",
                accepted=False,
                reason="LGM3 仅允许内部确定性测试图，客户任务不能路由到 LangGraph。",
            )
        if not request.feature_enabled:
            return RuntimeRoute(
                backend_id="native",
                accepted=False,
                reason="LangGraph 试验开关未启用。",
            )
        if not request.graph_admitted:
            return RuntimeRoute(
                backend_id="native",
                accepted=False,
                reason="测试图尚未通过静态准入。",
            )
        if (
            request.graph_id != _LGM3_GRAPH_ID
            or request.graph_version != _LGM3_GRAPH_VERSION
        ):
            return RuntimeRoute(
                backend_id="native",
                accepted=False,
                reason="请求的 LangGraph 图 ID 或版本不在 LGM3 白名单内。",
            )
        if not request.read_only or request.side_effects_started:
            return RuntimeRoute(
                backend_id="native",
                accepted=False,
                reason="LangGraph 测试图只允许只读且尚未产生副作用的任务。",
            )
        return RuntimeRoute(
            backend_id="langgraph",
            accepted=True,
            reason="已路由到 LGM3 内部确定性测试图。",
        )


def lgm3_graph_identity() -> tuple[str, str]:
    """向测试后端提供唯一允许的图身份，避免散落魔法字符串。"""

    return _LGM3_GRAPH_ID, _LGM3_GRAPH_VERSION
