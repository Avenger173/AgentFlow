"""把 MCP 调用的脱敏事实映射为 AgentFlow 既有 Tool 审计结构。"""

from __future__ import annotations

from app.mcp.contracts import McpToolResult
from app.mcp.result_guard import tool_result_size_bytes
from app.schemas.workflow import WorkflowToolCall


def project_tool_call_audit(
    *,
    task_id: str,
    step_id: str,
    agent_id: str,
    call_id: str,
    result: McpToolResult,
    duration_ms: int,
    request_bytes: int,
) -> WorkflowToolCall:
    """创建尚未落库的 `WorkflowToolCall` 兼容投影。

    LGM1 只在专项回归构造该对象，不写任务历史、不创建客户 task。后续 LGM2 只有经过
    Action Admission、权限策略和 Result Verifier 后，才能把同一投影写入正式 Runtime。
    """

    return WorkflowToolCall(
        call_id=call_id,
        task_id=task_id,
        step_id=step_id,
        agent_id=agent_id,
        tool_name=result.reference.qualified_name,
        status="failed" if result.is_error else "completed",
        risk_level="low",
        permission_required=False,
        attempt=1,
        max_attempts=1,
        timeout_ms=5_000,
        duration_ms=max(0, duration_ms),
        failure_count=1 if result.is_error else 0,
        request={
            "mcp_server_id": result.reference.server.server_id,
            "mcp_tool_name": result.reference.tool_name,
            "request_bytes": max(0, request_bytes),
        },
        result={
            "is_error": result.is_error,
            "result_bytes": tool_result_size_bytes(result),
            "content_block_types": result.content_block_types,
            "result_truncated": result.result_truncated,
        },
        error="MCP Tool 返回错误状态。" if result.is_error else "",
    )
