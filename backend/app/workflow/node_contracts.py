from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.chat import WorkflowStep


@dataclass(frozen=True)
class NodeContract:
    """内置 Agent 步骤的最小契约。

    这里先只收拢阶段 5 最需要稳定的协议字段：Agent/action 到 Tool 名称的映射、
    输入输出形状、状态写入和典型失败码。Runtime 仍负责真正的安全校验；本模块只提供
    “这个节点应该长什么样”的共享定义，避免 dry-run、runtime、未来 LangGraph 适配层各写一套。
    """

    agent_id: str
    action: str
    tool_name: str
    node_type: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    state_writes: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()
    failure_codes: tuple[str, ...] = ()
    evaluation_signals: tuple[str, ...] = ()


NODE_CONTRACTS: dict[tuple[str, str], NodeContract] = {
    ("commander_agent", "analyze_task"): NodeContract(
        agent_id="commander_agent",
        action="analyze_task",
        tool_name="planner.analyze_task",
        node_type="llm",
        input_schema={"task": "string"},
        output_schema={"summary": "string", "workflow_plan": "object"},
        state_writes=("plan.summary", "plan.steps"),
        evaluation_signals=("plan_valid", "step_total", "risk_level"),
    ),
    ("commander_agent", "direct_answer"): NodeContract(
        agent_id="commander_agent",
        action="direct_answer",
        tool_name="planner.direct_answer",
        node_type="llm",
        input_schema={"task": "string"},
        output_schema={"answer": "string"},
        state_writes=("conversation.answer",),
        evaluation_signals=("answer_generated",),
    ),
    ("commander_agent", "synthesize_results"): NodeContract(
        agent_id="commander_agent",
        action="synthesize_results",
        tool_name="planner.synthesize_results",
        node_type="control",
        input_schema={
            "child_step_ids": "admitted specialist step ids",
            "composition_mode": "native_read_only_c6_4 or guarded future mode",
        },
        output_schema={
            "summary": "bounded synthesis of completed child results",
            "result_scope": "completed child task summaries only",
        },
        state_writes=("composition.summary", "conversation.answer"),
        failure_codes=("composition_runtime_not_available", "child_task_incomplete", "shared_budget_exceeded"),
        evaluation_signals=("dependency_graph_valid", "child_result_scope_verified", "partial_completion_visible"),
    ),
    ("document_agent", "read_text"): NodeContract(
        agent_id="document_agent",
        action="read_text",
        tool_name="document.read_text",
        node_type="data",
        input_schema={"path": "workspace-relative txt/md/markdown path"},
        output_schema={
            "path": "string",
            "relative_path": "string",
            "preview": "string",
            "bytes": "integer",
        },
        state_writes=("document.content", "document.preview", "artifacts"),
        required_permissions=("file_read",),
        failure_codes=(
            "invalid_parameters",
            "path_outside_workspace",
            "unsupported_file_type",
            "file_not_found",
            "file_too_large",
            "tool_timeout",
        ),
        evaluation_signals=("read_success", "bytes", "failure_code"),
    ),
    ("document_agent", "search_text"): NodeContract(
        agent_id="document_agent",
        action="search_text",
        tool_name="document.search_text",
        node_type="data",
        input_schema={
            "query": "string",
            "document_refs": "explicitly selected workspace-relative document paths",
            "limit": "integer",
            "case_sensitive": "boolean",
            "auto_read_if_unique": "boolean",
        },
        output_schema={
            "matches": "array",
            "total": "integer",
            "matched_documents": "array",
            "suggested_read_path": "string|null",
            "auto_read": "object|null",
        },
        state_writes=("document.search.matches", "document.context"),
        required_permissions=("file_read",),
        failure_codes=(
            "empty_query",
            "invalid_parameters",
            "file_too_large",
            "decode_error",
            "tool_timeout",
        ),
        evaluation_signals=("match_count", "searched_documents", "limit_reached"),
    ),
    ("document_agent", "extract_requirements"): NodeContract(
        agent_id="document_agent",
        action="extract_requirements",
        tool_name="document.extract_requirements",
        node_type="data",
        input_schema={
            "task": "string",
            "depends_on": "document.read_text|document.search_text step ids",
        },
        output_schema={
            "requirements": "array",
            "summary": "string",
            "context": "object",
            "context.source_steps": "array",
            "context.search_matches": "array",
            "context.read_previews": "array",
        },
        state_writes=("requirements", "document.summary", "document.context"),
        failure_codes=("missing_document_context", "invalid_parameters", "tool_timeout"),
        evaluation_signals=(
            "requirement_count",
            "summary_generated",
            "source_step_count",
            "search_match_total",
            "read_preview_total",
        ),
    ),
    ("document_agent", "summarize_document"): NodeContract(
        agent_id="document_agent",
        action="summarize_document",
        tool_name="document.summarize_document",
        node_type="llm",
        input_schema={"document": "string"},
        output_schema={"summary": "string"},
        state_writes=("document.summary",),
        evaluation_signals=("summary_generated",),
    ),
    ("document_agent", "analyze_document"): NodeContract(
        agent_id="document_agent",
        action="analyze_document",
        tool_name="agent.document_agent.analyze",
        node_type="llm",
        input_schema={
            "task_goal": "string",
            "document_refs": "workspace-relative txt/md/markdown paths",
            "output_mode": "auto|requirements|summary|qa",
        },
        output_schema={
            "document_context": "agentflow.document_context.v1",
            "reply": "string",
            "sources": "array",
        },
        state_writes=("document.context", "document.summary", "conversation.answer"),
        required_permissions=("file_read",),
        failure_codes=(
            "document_not_selected",
            "ambiguous_document",
            "insufficient_context",
            "model_output_invalid",
            "max_turns_exceeded",
        ),
        evaluation_signals=("source_coverage", "tool_call_total", "turn_total", "output_valid"),
    ),
    ("document_agent", "open_presentation_studio"): NodeContract(
        agent_id="document_agent",
        action="open_presentation_studio",
        tool_name="ui.presentation_studio.open",
        node_type="ui",
        input_schema={"task_goal": "customer presentation topic, maximum 1200 characters"},
        output_schema={"next_action": "open_presentation_studio", "message": "string"},
        state_writes=("routing.presentation_studio",),
        failure_codes=("presentation_studio_unavailable",),
        evaluation_signals=("guided_handoff_presented", "prompt_prefilled"),
    ),
    ("knowledge_agent", "answer_question"): NodeContract(
        agent_id="knowledge_agent",
        action="answer_question",
        tool_name="agent.knowledge_agent.answer",
        node_type="llm",
        input_schema={
            "knowledge_base_id": "explicitly selected knowledge base id",
            "query": "customer question, maximum 800 characters",
        },
        output_schema={
            "delegated_task_id": "knowledge answer task id",
            "agent_status": "completed|blocked|failed",
            "source_count": "integer",
            "retrieval_mode": "keyword|dense|hybrid|unavailable",
        },
        state_writes=("knowledge.answer_task", "knowledge.sources", "conversation.answer"),
        failure_codes=(
            "invalid_parameters",
            "knowledge_base_not_ready",
            "insufficient_evidence",
            "agent_delegate_failed",
            "tool_timeout",
        ),
        evaluation_signals=("source_coverage", "citation_verified", "active_generation_verified"),
    ),
    ("knowledge_agent", "deep_summary"): NodeContract(
        agent_id="knowledge_agent",
        action="deep_summary",
        tool_name="agent.knowledge_agent.deep_summary",
        node_type="subgraph",
        input_schema={
            "knowledge_base_id": "explicitly selected knowledge base id",
            "task_goal": "customer-confirmed whole-library deep-summary goal, maximum 800 characters",
            "task_kind": "summary only; comparison stays in the knowledge workspace",
        },
        output_schema={
            "delegated_task_id": "knowledge deep task id",
            "agent_status": "queued|running|completed|blocked|failed",
            "scope_map_count": "frozen chapter count",
            "handoff_state": "accepted",
        },
        state_writes=("knowledge.deep_task", "knowledge.deep_scope", "delegations"),
        required_permissions=("knowledge_deep_analysis",),
        failure_codes=(
            "invalid_parameters",
            "knowledge_base_not_ready",
            "knowledge_deep_scope_failed",
            "agent_delegate_failed",
        ),
        evaluation_signals=("delegation_accepted", "scope_frozen", "parent_child_auditable"),
    ),
    ("data_agent", "open_workspace"): NodeContract(
        agent_id="data_agent",
        action="open_workspace",
        tool_name="ui.data_workspace.open",
        node_type="ui",
        input_schema={
            "task_goal": "string",
            "dataset_refs": "explicitly selected CSV/XLSX references",
        },
        output_schema={"next_action": "open_data_workspace", "message": "string"},
        state_writes=("routing.data_workspace",),
        failure_codes=("dataset_not_selected", "data_agent_not_admitted"),
        evaluation_signals=("guided_handoff_presented",),
    ),
    ("data_agent", "analyze_dataset"): NodeContract(
        agent_id="data_agent",
        action="analyze_dataset",
        tool_name="agent.data_agent.analyze_preview",
        node_type="data",
        input_schema={
            "task_goal": "customer goal, maximum 1200 characters",
            "dataset_name": "one explicitly selected imported CSV/XLSX reference",
            "dataset_refs": "single-item material binding retained for admission audit",
            "cleaning_policy": "safe only",
            "max_chart_count": "integer 1..4",
        },
        output_schema={
            "delegated_task_id": "data preview child task id",
            "agent_status": "completed|failed",
            "source_sha256": "verified source hash",
            "insight_headline": "short conclusion headline",
            "chart_count": "integer",
            "table_count": "integer",
            "read_only": "true",
        },
        state_writes=("data.analysis_task", "data.source_hash", "data.insight", "delegations"),
        required_permissions=("file_read",),
        failure_codes=(
            "invalid_parameters",
            "data_file_unavailable",
            "data_analysis_failed",
            "tool_timeout",
            "agent_delegate_failed",
        ),
        evaluation_signals=(
            "single_dataset_bound",
            "raw_rows_not_logged",
            "source_hash_recorded",
            "parent_child_auditable",
        ),
    ),
    ("data_agent", "export_chart_dashboard"): NodeContract(
        agent_id="data_agent",
        action="export_chart_dashboard",
        tool_name="agent.data_agent.export_chart_dashboard",
        node_type="action",
        input_schema={
            "task_goal": "customer chart-delivery goal, maximum 1200 characters",
            "dataset_name": "one explicitly selected imported CSV/XLSX reference",
            "dataset_refs": "single-item material binding retained for admission audit",
            "cleaning_policy": "safe only",
            "max_chart_count": "integer 1..4",
        },
        output_schema={
            "delegated_task_id": "data chart export task id",
            "agent_status": "completed|failed",
            "chart_count": "integer 1..4",
            "artifacts": "verified agentflow-output PNG artifact references",
        },
        state_writes=("data.chart_task", "artifacts", "delegations"),
        required_permissions=("file_read", "file_write"),
        failure_codes=(
            "invalid_parameters",
            "data_file_unavailable",
            "data_chart_export_failed",
            "artifact_verification_failed",
            "tool_timeout",
        ),
        evaluation_signals=("single_dataset_bound", "png_count", "png_pixels_verified", "source_unchanged"),
    ),
    ("data_agent", "export_analysis_workbook"): NodeContract(
        agent_id="data_agent",
        action="export_analysis_workbook",
        tool_name="agent.data_agent.export_analysis_workbook",
        node_type="action",
        input_schema={
            "task_goal": "customer workbook-delivery goal, maximum 1200 characters",
            "dataset_name": "one explicitly selected imported CSV/XLSX reference",
            "dataset_refs": "single-item material binding retained for admission audit",
            "cleaning_policy": "safe only",
            "max_chart_count": "integer 1..4",
        },
        output_schema={
            "delegated_task_id": "data workbook export task id",
            "agent_status": "completed|failed",
            "artifact": "verified agentflow-output XLSX artifact reference",
            "verification": "native sheet/table/chart/metric readback summary",
        },
        state_writes=("data.workbook_task", "artifacts", "delegations"),
        required_permissions=("file_read", "file_write"),
        failure_codes=(
            "invalid_parameters",
            "data_file_unavailable",
            "data_workbook_export_failed",
            "artifact_verification_failed",
            "tool_timeout",
        ),
        evaluation_signals=("single_dataset_bound", "xlsx_readback_verified", "source_unchanged"),
    ),
    ("data_agent", "plan_field_transform"): NodeContract(
        agent_id="data_agent",
        action="plan_field_transform",
        tool_name="agent.data_agent.plan_field_transform",
        node_type="data",
        input_schema={
            "task_goal": "customer field-transformation goal, maximum 1200 characters",
            "dataset_name": "one explicitly selected imported CSV/XLSX reference",
            "source_sha256": "verified source hash",
            "operations": "bounded field transformation operation array, maximum 12",
        },
        output_schema={
            "intent_version": "agentflow.data_transform_intent.v1",
            "plans": "validated derived-field plans",
            "row_count": "integer",
            "affected_count": "integer",
            "read_only": "true",
        },
        state_writes=("data.transform.plan", "data.source_hash"),
        required_permissions=("file_read",),
        failure_codes=("invalid_parameters", "data_file_unavailable", "data_transformation_failed"),
        evaluation_signals=("single_dataset_bound", "operation_whitelist_verified", "source_hash_recorded", "preview_returned"),
    ),
    ("data_agent", "export_field_transform"): NodeContract(
        agent_id="data_agent",
        action="export_field_transform",
        tool_name="agent.data_agent.export_field_transform",
        node_type="action",
        input_schema={
            "task_goal": "customer field-transformation goal, maximum 1200 characters",
            "dataset_name": "one explicitly selected imported CSV/XLSX reference",
            "source_sha256": "source hash captured by the planning step",
            "operations": "same validated bounded operation array used by preview",
            "confirmed": "customer confirmation represented by Runtime permission decision",
        },
        output_schema={
            "delegated_task_id": "data transformation child task id",
            "agent_status": "completed|failed",
            "artifact": "verified CSV/XLSX copy artifact reference",
            "plans": "validated appended-field plans",
            "verification": "source hash, row count and new-column readback summary",
        },
        state_writes=("data.transform.artifact", "artifacts", "delegations"),
        required_permissions=("file_read", "file_write"),
        failure_codes=(
            "invalid_parameters",
            "data_file_unavailable",
            "data_transformation_failed",
            "artifact_verification_failed",
            "tool_timeout",
        ),
        evaluation_signals=("single_dataset_bound", "new_columns_appended", "copy_readback_verified", "source_unchanged"),
    ),
    ("code_agent", "generate_code"): NodeContract(
        agent_id="code_agent",
        action="generate_code",
        tool_name="code.generate_code",
        node_type="action",
        input_schema={
            "workflow_plan": "object",
            "document_context": "object",
            "language": "string",
        },
        output_schema={
            "output_file": "string",
            "relative_path": "string",
            "bytes": "integer",
            "document_context": "object",
            "verification": "object",
        },
        state_writes=("artifacts.code",),
        required_permissions=("file_write",),
        failure_codes=("io_error", "artifact_verification_failed", "tool_timeout"),
        evaluation_signals=("artifact_created", "artifact_verified", "bytes"),
    ),
    ("code_agent", "explain_code"): NodeContract(
        agent_id="code_agent",
        action="explain_code",
        tool_name="code.explain_code",
        node_type="llm",
        input_schema={"code": "string"},
        output_schema={"explanation": "string"},
        state_writes=("code.explanation",),
        evaluation_signals=("explanation_generated",),
    ),
    ("code_agent", "create_project_files"): NodeContract(
        agent_id="code_agent",
        action="create_project_files",
        tool_name="artifact.write_project_files",
        node_type="action",
        input_schema={"files": "array"},
        output_schema={"artifacts": "array"},
        state_writes=("artifacts.code",),
        required_permissions=("file_write",),
        failure_codes=("io_error", "tool_timeout"),
        evaluation_signals=("artifact_created", "file_count"),
    ),
    ("report_agent", "generate_report"): NodeContract(
        agent_id="report_agent",
        action="generate_report",
        tool_name="report.compose_markdown",
        node_type="action",
        input_schema={"steps": "array", "artifacts": "array", "document_context": "object"},
        output_schema={
            "output_file": "string",
            "relative_path": "string",
            "bytes": "integer",
            "document_context": "object",
            "verification": "object",
        },
        state_writes=("artifacts.report",),
        required_permissions=("file_write",),
        failure_codes=("io_error", "artifact_verification_failed", "tool_timeout"),
        evaluation_signals=("artifact_created", "artifact_verified", "bytes"),
    ),
    ("report_agent", "generate_markdown_report"): NodeContract(
        agent_id="report_agent",
        action="generate_markdown_report",
        tool_name="report.compose_markdown",
        node_type="action",
        input_schema={"steps": "array", "artifacts": "array", "document_context": "object"},
        output_schema={
            "output_file": "string",
            "relative_path": "string",
            "bytes": "integer",
            "document_context": "object",
            "verification": "object",
        },
        state_writes=("artifacts.report",),
        required_permissions=("file_write",),
        failure_codes=("io_error", "artifact_verification_failed", "tool_timeout"),
        evaluation_signals=("artifact_created", "artifact_verified", "bytes"),
    ),
    ("report_agent", "summarize_artifacts"): NodeContract(
        agent_id="report_agent",
        action="summarize_artifacts",
        tool_name="report.summarize_artifacts",
        node_type="llm",
        input_schema={"artifacts": "array"},
        output_schema={"summary": "string"},
        state_writes=("artifacts.summary",),
        evaluation_signals=("summary_generated",),
    ),
}


def node_contract_for_step(step: WorkflowStep) -> NodeContract | None:
    """按 WorkflowStep 查找内置节点契约，未知插件步骤返回 None。"""

    return NODE_CONTRACTS.get((step.agent, step.action))


def list_node_contracts(
    *,
    agent_id: str | None = None,
    action: str | None = None,
) -> list[NodeContract]:
    """列出内置节点契约。

    API、验证脚本和未来 LangGraph 适配层都应该从这里读取同一份契约，避免 Document /
    Code / Report 的输入输出、权限和失败码在多个地方漂移。筛选只做精确匹配，保持接口
    可预测，也避免前端传入模糊条件时拿到意料之外的高权限节点。
    """

    contracts = sorted(
        NODE_CONTRACTS.values(),
        key=lambda contract: (contract.agent_id, contract.action),
    )
    if agent_id is not None:
        contracts = [
            contract for contract in contracts if contract.agent_id == agent_id
        ]
    if action is not None:
        contracts = [
            contract for contract in contracts if contract.action == action
        ]
    return contracts


def tool_name_for_step(step: WorkflowStep) -> str:
    """返回前端、审计日志和 Runtime 共享的稳定工具名。"""

    contract = node_contract_for_step(step)
    if contract is not None:
        return contract.tool_name
    return f"agentflow.{step.agent}.{step.action}"
