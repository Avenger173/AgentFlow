import asyncio
import os
from urllib.parse import unquote

from app.database.task_repository import (
    append_workflow_event,
    list_workflow_plan_versions,
    load_workflow_plan_version,
    list_workflow_artifacts,
    list_workflow_tool_calls,
    load_workflow_plan,
    load_workflow_run,
    load_workflow_step_runs,
    list_runtime_permission_requests,
    list_workflow_runs,
    record_runtime_permission_decision,
)
from app.database.memory_repository import create_long_term_memory, list_long_term_memories
from app.schemas.chat import WorkflowPlan
from app.schemas.events import TaskLogListResponse
from app.schemas.plan_revisions import (
    WorkflowPlanRevisionRequest,
    WorkflowPlanRevisionResponse,
    WorkflowPlanVersionDetailResponse,
    WorkflowPlanVersionListResponse,
)
from app.schemas.memory import (
    LongTermMemoryProposalConfirmRequest,
    LongTermMemoryProposalListResponse,
    LongTermMemoryRecord,
)
from app.schemas.workflow import (
    RiskLevel,
    RuntimePermissionDecision,
    RuntimePermissionDecisionInput,
    RuntimePermissionItem,
    RuntimePermissionListResponse,
    TaskControlResponse,
    WorkflowExecutionResponse,
    WorkflowArtifact,
    WorkflowArtifactListResponse,
    WorkflowArtifactPreviewResponse,
    WorkflowDeliveryCard,
    WorkflowRun,
    WorkflowRunListResponse,
    WorkflowRunMode,
    WorkflowModelRouteAuditResponse,
    WorkflowRuntimeMetricsResponse,
    WorkflowRuntimeStateResponse,
    WorkflowRunStatus,
    WorkflowStepListResponse,
    WorkflowTaskUpdateListResponse,
    WorkflowTaskEvaluationResponse,
    WorkflowToolCallListResponse,
)
from app.core.config import settings
from app.services.data_analysis_delivery import (
    cancel_data_workbook_export_task,
    get_data_workbook_export_task_result,
)
from app.services.data_chart_delivery import (
    cancel_data_chart_export_task,
    get_data_chart_export_task_result,
)
from app.services.data_transformation_delivery import (
    cancel_data_transformation_task,
    get_data_transformation_task_result,
)
from app.services.commander_memory_proposals import (
    build_commander_memory_proposals,
    is_current_memory_proposal,
)
from app.services.long_term_memory import (
    LongTermMemorySafetyError,
    normalize_memory_scope,
    normalize_memory_source_task_id,
    normalize_memory_tags,
    sanitize_memory_text,
)
from app.workflow.dry_run import (
    get_task_log_events,
    get_workflow_run,
    request_cancel,
    retry_workflow_dry_run,
)
from app.workflow.evaluation import evaluate_workflow_task
from app.services.delivery_card import build_delivery_card
from app.workflow.runtime import (
    execute_workflow_runtime,
    request_runtime_cancel,
    request_runtime_pause,
)
from app.workflow.runtime_jobs import start_runtime_job
from app.workflow.plan_revision import WorkflowPlanRevisionError, revise_workflow_plan
from app.workflow.state_machine import describe_runtime_state
from app.workflow.updates import build_task_updates
from fastapi import APIRouter, HTTPException, Query
from pathlib import Path
from pydantic import BaseModel


router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _load_completed_runtime_plan_for_memory(task_id: str) -> tuple[WorkflowRun, WorkflowPlan]:
    """确认记忆候选只能来自已完成的 Runtime 计划，避免预演和失败任务污染长期层。"""

    run = load_workflow_run(task_id)
    plan = load_workflow_plan(task_id)
    if run is None or plan is None:
        raise HTTPException(status_code=404, detail="未找到指定任务或其计划快照。")
    if run.mode != "runtime" or run.status != "completed":
        raise HTTPException(status_code=409, detail="只有已完成的真实任务可以提供长期记忆候选。")
    return run, plan


_ARTIFACT_PREVIEW_DEFAULT_BYTES = 64 * 1024
_ARTIFACT_PREVIEW_MAX_BYTES = 256 * 1024
_TEXT_ARTIFACT_KINDS = {"text", "markdown", "code", "report"}
_TEXT_MIME_TYPES = {
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
}
_TEXT_EXTENSIONS = {
    ".cfg",
    ".csv",
    ".ini",
    ".json",
    ".log",
    ".md",
    ".py",
    ".rst",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


def _runtime_control_response(
    response: WorkflowExecutionResponse,
    *,
    action: str,
) -> TaskControlResponse:
    """把 Runtime 的真实控制结果映射到历史页既有的控制响应协议。"""

    return TaskControlResponse(
        task_id=response.runtime_task_id,
        action=action,  # type: ignore[arg-type]
        accepted=response.accepted,
        status=response.status,
        message=response.message,
        workflow_run=response.workflow_run,
    )


def _find_artifact(task_id: str, artifact_id: str) -> WorkflowArtifact | None:
    """按 task_id 和 artifact_id 精确定位产物。

    artifact_id 里会包含冒号，不能拆分推断；统一读仓储里的完整 artifact_json 更安全。
    旧版 Qt 客户端曾在 ``QUrl::setPath`` 前手工 percent-encode 一次，随后 QUrl 又会把 ``%``
    编码一次。FastAPI 因而会收到字面量 ``%3A``，而不是数据库中的冒号。这里只在首次精确
    查找失败后解码一层并再次精确匹配，既兼容已发布客户端，也不会把路径或模糊 ID 当作产物。
    """

    artifacts = list_workflow_artifacts(task_id)
    for artifact in artifacts:
        if artifact.artifact_id == artifact_id:
            return artifact

    decoded_artifact_id = unquote(artifact_id)
    if decoded_artifact_id == artifact_id:
        return None
    for artifact in artifacts:
        if artifact.artifact_id == decoded_artifact_id:
            return artifact
    return None


def _artifacts_with_delegated_children(task_id: str) -> list[WorkflowArtifact]:
    """把父任务登记过的子任务交付并入展示面，不改变父子仓储关系。

    Commander 父任务只保存 ``agentflow-task://`` 关联，真实 PNG/Excel 留在专业子任务中。
    历史和结果卡若只读父表，就会出现“任务完成但没有产物”。这里只展开一层、只信任父
    artifact 的稳定 delegated_task_id，并按任务和产物 ID 去重；不会扫描目录或任意任务。
    """

    roots = list_workflow_artifacts(task_id)
    expanded: list[WorkflowArtifact] = []
    seen: set[tuple[str, str]] = set()
    for root in roots:
        delegated_task_id = str(root.metadata.get("delegated_task_id", "")).strip()
        if delegated_task_id and delegated_task_id != task_id:
            for child in list_workflow_artifacts(delegated_task_id):
                key = (child.task_id, child.artifact_id)
                if key not in seen:
                    expanded.append(child)
                    seen.add(key)
        key = (root.task_id, root.artifact_id)
        if key not in seen:
            expanded.append(root)
            seen.add(key)
    return expanded


def _artifact_preview_metadata(artifact: WorkflowArtifact) -> dict[str, object]:
    """返回给 UI 的元数据去掉本机绝对路径，避免把内部目录结构暴露成产品界面细节。"""

    metadata = dict(artifact.metadata)
    if "output_path" in metadata:
        metadata["output_path"] = "<hidden>"
    return metadata


def _public_artifact(artifact: WorkflowArtifact) -> WorkflowArtifact:
    """从列表响应移除仅供后端 resolver 使用的绝对输出路径。"""

    return artifact.model_copy(update={"metadata": _artifact_preview_metadata(artifact)})


def _artifact_unavailable_response(
    artifact: WorkflowArtifact,
    reason: str,
    *,
    source: str = "unavailable",
) -> WorkflowArtifactPreviewResponse:
    return WorkflowArtifactPreviewResponse(
        task_id=artifact.task_id,
        artifact_id=artifact.artifact_id,
        available=False,
        reason=reason,
        kind=artifact.kind,
        name=artifact.name,
        uri=artifact.uri,
        mime_type=artifact.mime_type,
        source=source,
        metadata=_artifact_preview_metadata(artifact),
    )


def _is_text_preview_candidate(artifact: WorkflowArtifact, path: Path) -> bool:
    """用 MIME、产物类型和扩展名三层信号判断是否适合按文本预览。"""

    mime_type = artifact.mime_type.lower()
    if mime_type.startswith("text/") or mime_type in _TEXT_MIME_TYPES:
        return True
    if artifact.kind in _TEXT_ARTIFACT_KINDS:
        return True
    return path.suffix.lower() in _TEXT_EXTENSIONS


def _resolve_runtime_artifact_path(artifact: WorkflowArtifact) -> Path | None:
    """只允许读取 Runtime 或用户确认写入的受控真实产物。

    这一步是 Runtime Harness 的边界：即使数据库里出现了异常路径，也不能让预览接口读取
    outputs 目录之外的文件。
    """

    if not artifact.uri.startswith("agentflow-output://"):
        return None
    if artifact.metadata.get("runtime") is not True:
        return None

    raw_output_path = artifact.metadata.get("output_path")
    if not isinstance(raw_output_path, str) or not raw_output_path.strip():
        return None

    output_path = Path(raw_output_path).resolve()
    output_scope = str(artifact.metadata.get("output_scope", "runtime"))
    if output_scope == "document_drafts":
        # 文档草稿是用户明确保存的项目交付物，不与 Runtime 临时 outputs 混放；仍必须由
        # 后端根据 metadata 选择固定根目录，不能接受 URI 或 Qt 传入的任意本机路径。
        if not artifact.uri.startswith("agentflow-output://document_drafts/"):
            return None
        outputs_root = settings.document_draft_output_dir
    elif output_scope == "runtime":
        outputs_root = (settings.data_dir / "outputs").resolve()
    elif output_scope == "document_processing":
        if not artifact.uri.startswith("agentflow-output://document_processing/"):
            return None
        outputs_root = settings.document_processing_output_dir
    elif output_scope == "document_presentations":
        # PPTX 不是文本，预览接口会明确拒绝读取二进制；仍需在这里校验其路径与 URI 关系，
        # 让历史页后续可以安全地定位、打开或下载这个用户确认的交付物。
        if not artifact.uri.startswith("agentflow-output://document_presentations/"):
            return None
        outputs_root = settings.document_presentation_output_dir
    elif output_scope == "knowledge_reports":
        # K4 深度报告为客户确认后的 UTF-8 Markdown；历史页仍只使用 artifact 的稳定 URI，
        # 路径必须同时落在知识库报告固定根目录内才允许预览或交给系统打开。
        if not artifact.uri.startswith("agentflow-output://knowledge_reports/"):
            return None
        outputs_root = settings.knowledge_report_output_dir
    elif output_scope == "data_analysis":
        # 数据工作簿与导入数据集隔离。历史预览当前不读取二进制 Excel，但仍要先保持 URI、
        # metadata 路径和固定输出目录三者一致，为后续“打开交付物”动作保留安全边界。
        if not artifact.uri.startswith("agentflow-output://data_analysis/"):
            return None
        outputs_root = settings.data_analysis_output_dir
    elif output_scope == "data_charts":
        # D5.2 图表按 task_id 隔离到固定 data_charts 根；历史列表仍只返回脱敏 URI，图像字节
        # 仅由数据工作台的专用受控接口读取。
        if not artifact.uri.startswith("agentflow-output://data_charts/"):
            return None
        outputs_root = settings.data_chart_output_dir
    elif output_scope == "data_transformations":
        # D5.3 的字段加工副本只能位于自己的固定目录；任务历史可受控打开文件，但列表和
        # Qt 都不接触绝对路径。
        if not artifact.uri.startswith("agentflow-output://data_transformations/"):
            return None
        outputs_root = settings.data_transformation_output_dir
    else:
        return None
    try:
        output_path.relative_to(outputs_root)
    except ValueError:
        return None
    return output_path


class WorkflowPlanDetailResponse(BaseModel):
    """任务历史页按需读取的计划详情。

    列表接口继续保持轻量；只有用户选中某条任务时，前端才读取当时缓存的
    Commander `workflow_plan`，用于复盘计划意图、完成标准、预算和工作区边界。
    """

    task_id: str
    workflow_plan: WorkflowPlan


class WorkflowArtifactOpenResponse(BaseModel):
    """受控产物交给当前系统默认程序打开后的最小回执。

    列表和 Qt 始终拿不到真实绝对路径；只有后端完成 URI、scope 和固定输出根校验后，才允许
    在本机发起打开动作。这让“任务历史一键打开”不需要为了方便而放宽目录边界。
    """

    task_id: str
    artifact_id: str
    opened: bool
    message: str


@router.get("", response_model=WorkflowRunListResponse)
async def list_tasks(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status: WorkflowRunStatus | None = Query(default=None),
    mode: WorkflowRunMode | None = Query(default=None),
    max_risk_level: RiskLevel | None = Query(default=None),
    requires_confirmation: bool | None = Query(default=None),
) -> WorkflowRunListResponse:
    """查询工作流任务历史摘要。

    当前列出 SQLite 中的 dry-run/runtime 任务。列表接口返回轻量摘要，详情仍通过
    `GET /api/tasks/{task_id}` 获取，避免任务多时列表响应过大。
    status/mode/risk/confirmation 筛选用于后续前端历史页、权限审查页快速定位任务。
    """

    total, tasks = list_workflow_runs(
        limit=limit,
        offset=offset,
        status=status,
        mode=mode,
        max_risk_level=max_risk_level,
        requires_confirmation=requires_confirmation,
    )
    return WorkflowRunListResponse(total=total, limit=limit, offset=offset, tasks=tasks)


@router.get("/{task_id}", response_model=WorkflowRun)
async def get_task(task_id: str) -> WorkflowRun:
    """查询工作流任务状态。

    当前优先读开发期内存缓存，缓存丢失时从 SQLite 恢复。steps 也已同步落到
    `workflow_steps`，后续真实 Runtime 可逐步更新单步状态。
    """

    run = get_workflow_run(task_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    return run


@router.get("/{task_id}/plan", response_model=WorkflowPlanDetailResponse)
async def get_task_plan(task_id: str) -> WorkflowPlanDetailResponse:
    """查询任务对应的 Commander 计划。

    `WorkflowRun` 只保存运行结果，计划本体独立落库，避免历史列表和运行态接口被大对象拖重。
    这个接口专门给历史详情“为什么这样安排”的回看视图使用。
    """

    if get_workflow_run(task_id) is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    plan = load_workflow_plan(task_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' plan was not found.")

    return WorkflowPlanDetailResponse(task_id=task_id, workflow_plan=plan)


@router.get("/{task_id}/memory-proposals", response_model=LongTermMemoryProposalListResponse)
async def get_task_memory_proposals(task_id: str) -> LongTermMemoryProposalListResponse:
    """返回任务结束后的可编辑候选，不会自动创建长期记忆。"""

    _, plan = _load_completed_runtime_plan_for_memory(task_id)
    items, note = build_commander_memory_proposals(task_id=task_id, plan=plan)
    return LongTermMemoryProposalListResponse(task_id=task_id, items=items, note=note)


@router.post("/{task_id}/memory-proposals/confirm", response_model=LongTermMemoryRecord)
async def confirm_task_memory_proposal(
    task_id: str,
    request: LongTermMemoryProposalConfirmRequest,
) -> LongTermMemoryRecord:
    """把客户确认的候选写入长期记忆，并将来源稳定绑定到完成任务。"""

    if not request.user_confirmed:
        raise HTTPException(status_code=400, detail="保存长期记忆需要用户明确确认。")
    _, plan = _load_completed_runtime_plan_for_memory(task_id)
    if not is_current_memory_proposal(proposal_id=request.proposal_id, task_id=task_id, plan=plan):
        raise HTTPException(status_code=409, detail="记忆候选已失效或不属于当前任务，请重新查看候选。")

    try:
        scope = normalize_memory_scope(request.scope)
        title = sanitize_memory_text(request.title, field_name="记忆标题", maximum=120)
        summary = sanitize_memory_text(request.summary, field_name="记忆摘要", maximum=1000)
        tags = normalize_memory_tags(request.tags)
        source_task_id = normalize_memory_source_task_id(task_id)
    except LongTermMemorySafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 重复点击确认时复用同一条已保存记录，避免桌面端网络重试制造多份完全相同的记忆。
    for item in list_long_term_memories(scope=scope, include_disabled=True):
        if item.source_task_id == task_id and item.title == title and item.summary == summary:
            return item

    record = create_long_term_memory(
        kind=request.kind,
        scope=scope,
        title=title,
        summary=summary,
        tags=tags,
        source_task_id=source_task_id,
        user_confirmed=True,
    )
    append_workflow_event(
        task_id=task_id,
        event_name="memory_proposal_confirmed",
        agent_id="commander_agent",
        message=f"用户确认保存长期记忆：{record.title}（范围：{record.scope}）。",
    )
    return record


@router.get("/{task_id}/plan-versions", response_model=WorkflowPlanVersionListResponse)
async def get_task_plan_versions(task_id: str) -> WorkflowPlanVersionListResponse:
    """列出总指挥计划的不可变历史版本。"""

    versions = list_workflow_plan_versions(task_id)
    if versions is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' plan was not found.")
    current_plan = load_workflow_plan(task_id)
    return WorkflowPlanVersionListResponse(
        task_id=task_id,
        current_plan_id=current_plan.plan_id if current_plan is not None else None,
        total=len(versions),
        versions=versions,
    )


@router.get(
    "/{task_id}/plan-versions/{plan_version}",
    response_model=WorkflowPlanVersionDetailResponse,
)
async def get_task_plan_version(
    task_id: str,
    plan_version: int,
) -> WorkflowPlanVersionDetailResponse:
    """读取指定计划快照，不会把历史版本设回当前执行计划。"""

    version = load_workflow_plan_version(task_id, plan_version)
    if version is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' plan version {plan_version} was not found.")
    summary, plan = version
    return WorkflowPlanVersionDetailResponse(task_id=task_id, version=summary, workflow_plan=plan)


@router.post(
    "/{task_id}/plan-revisions",
    response_model=WorkflowPlanRevisionResponse,
)
async def revise_task_plan(
    task_id: str,
    request: WorkflowPlanRevisionRequest,
) -> WorkflowPlanRevisionResponse:
    """用户确认后重新生成当前 dry-run 计划的下一版本。"""

    try:
        plan, run = await asyncio.to_thread(revise_workflow_plan, task_id=task_id, request=request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except WorkflowPlanRevisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return WorkflowPlanRevisionResponse(
        task_id=task_id,
        workflow_plan=plan,
        workflow_run=run,
        message=f"计划已更新为 v{plan.plan_version}；请复核范围和权限后再执行。",
    )


@router.get("/{task_id}/steps", response_model=WorkflowStepListResponse)
async def get_task_steps(task_id: str) -> WorkflowStepListResponse:
    """查询任务的 step 级结果。

    这个接口先服务历史详情和后续局部刷新；真实执行器接入后，前端可以只刷新步骤列表，
    不必每次拉完整 workflow_run。
    """

    if get_workflow_run(task_id) is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    steps = load_workflow_step_runs(task_id) or []
    return WorkflowStepListResponse(task_id=task_id, total=len(steps), steps=steps)


@router.get("/{task_id}/metrics", response_model=WorkflowRuntimeMetricsResponse)
async def get_task_metrics(task_id: str) -> WorkflowRuntimeMetricsResponse:
    """查询任务执行预算和运行指标。

    这个接口为后续历史页、评估面板和成本提示服务；真实 Runtime 接入后，前端可用它快速
    刷新耗时、重试次数、工具失败数和 token 估算，而不必拉完整任务详情。
    """

    run = get_workflow_run(task_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    return WorkflowRuntimeMetricsResponse(
        task_id=task_id,
        limits=run.limits,
        metrics=run.metrics,
    )


@router.get("/{task_id}/model-routes", response_model=WorkflowModelRouteAuditResponse)
async def get_task_model_routes(task_id: str) -> WorkflowModelRouteAuditResponse:
    """返回本次任务已经保存的实际模型路由事实。

    这是只读历史审计接口。旧任务为空时明确返回空列表，由客户端说明“历史版本未记录”，
    绝不根据当前模型配置猜测或回填，以免客户把后来改过的模型误当成当时实际使用的模型。
    """

    run = get_workflow_run(task_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    return WorkflowModelRouteAuditResponse(task_id=task_id, model_routes=run.model_routes)


@router.get("/{task_id}/evaluation", response_model=WorkflowTaskEvaluationResponse)
async def get_task_evaluation(task_id: str) -> WorkflowTaskEvaluationResponse:
    """查询任务效果评估摘要。

    metrics 负责给机器读的计数，evaluation 负责给用户和后续离线评估读的判断：
    任务是否完成、工具是否可靠、是否卡在权限、下一步该怎么处理。
    """

    run = get_workflow_run(task_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    return evaluate_workflow_task(
        run=run,
        tool_calls=list_workflow_tool_calls(task_id),
        permissions=list_runtime_permission_requests(task_id=task_id),
    )


@router.get("/{task_id}/runtime-state", response_model=WorkflowRuntimeStateResponse)
async def get_task_runtime_state(task_id: str) -> WorkflowRuntimeStateResponse:
    """查询任务 Runtime 状态机快照。

    当前 dry-run 基本都是 completed，但真实 Runtime 接入前先固定状态语义，前端可以据此
    决定是否显示取消、重试、等待权限等动作，而不是靠散落的字符串判断。
    """

    run = get_workflow_run(task_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    return describe_runtime_state(run)


@router.post("/{task_id}/execute", response_model=WorkflowExecutionResponse)
async def execute_task(task_id: str) -> WorkflowExecutionResponse:
    """显式启动或恢复真实 Runtime。

    `/api/chat` 只负责产出计划和 dry-run，不直接产生文件副作用。用户确认要执行时，
    前端调用这个接口：dry-run 会派生出新的 runtime task；等待权限中的 runtime task
    会在权限批准后继续执行。
    """

    # Runtime 可能等待模型、文件或受控工具；放到工作线程避免把 FastAPI 事件循环卡住。
    # 文档助手的 async Tool loop 会在该线程内建立自己的事件循环，主请求仍可响应其他查询。
    response = await asyncio.to_thread(execute_workflow_runtime, task_id)
    if response is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    return response


@router.post("/{task_id}/start", response_model=WorkflowExecutionResponse)
async def start_task(task_id: str) -> WorkflowExecutionResponse:
    """受理真实 Runtime 并立即返回，不等待模型或 Tool 全部结束。

    新的 Qt 调度台应使用此入口，再通过 WebSocket / history 观察阶段事件。保留 ``/execute``
    仅为现有脚本和兼容客户端提供同步行为，避免在同一轮改动中悄悄改变旧 API 的返回时机。
    """

    response = await start_runtime_job(task_id)
    if response is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")
    return response


@router.post("/{task_id}/resume", response_model=WorkflowExecutionResponse)
async def resume_task(task_id: str) -> WorkflowExecutionResponse:
    """从同一 Runtime task 的安全检查点恢复后台执行。"""

    response = await start_runtime_job(task_id)
    if response is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")
    return response


@router.get("/{task_id}/artifacts", response_model=WorkflowArtifactListResponse)
async def get_task_artifacts(task_id: str) -> WorkflowArtifactListResponse:
    """查询任务产物目录。

    dry-run 返回虚拟产物，不代表后端已经写入文件；runtime 返回受控 outputs 目录里的真实
    文本产物。前端统一通过这个接口展示报告、代码、数据文件等可追踪结果。
    """

    if get_workflow_run(task_id) is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    artifacts = _artifacts_with_delegated_children(task_id)
    return WorkflowArtifactListResponse(
        task_id=task_id,
        total=len(artifacts),
        artifacts=[_public_artifact(artifact) for artifact in artifacts],
    )


@router.post(
    "/{task_id}/artifacts/{artifact_id}/open",
    response_model=WorkflowArtifactOpenResponse,
)
async def open_task_artifact(
    task_id: str,
    artifact_id: str,
) -> WorkflowArtifactOpenResponse:
    """通过后端受控边界打开一个已经登记的真实交付物。

    这是一个本机桌面端动作，不返回绝对路径，也不接受客户端传来的路径。Windows 下由系统默认
    程序处理文件类型；失效、dry-run 或脱离 outputs 根目录的 artifact 一律拒绝。
    """

    if get_workflow_run(task_id) is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    artifact = _find_artifact(task_id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"Artifact '{artifact_id}' was not found.")

    output_path = _resolve_runtime_artifact_path(artifact)
    if output_path is None:
        raise HTTPException(status_code=400, detail="该产物不是可打开的受控 Runtime 交付物。")
    if not output_path.exists() or not output_path.is_file():
        raise HTTPException(status_code=404, detail="产物文件不存在，可能已被移动或清理。")

    start_file = getattr(os, "startfile", None)
    if start_file is None:
        raise HTTPException(status_code=501, detail="当前系统暂不支持通过默认程序打开产物。")

    try:
        # startfile 只负责把已验证的本机文件交给系统，不等待外部应用关闭，避免占用 API 事件循环。
        await asyncio.to_thread(start_file, str(output_path))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"系统无法打开该产物：{exc}") from exc

    return WorkflowArtifactOpenResponse(
        task_id=task_id,
        artifact_id=artifact_id,
        opened=True,
        message="已交给系统默认程序打开。",
    )


@router.get(
    "/{task_id}/artifacts/{artifact_id}/preview",
    response_model=WorkflowArtifactPreviewResponse,
)
async def preview_task_artifact(
    task_id: str,
    artifact_id: str,
    max_bytes: int = Query(
        default=_ARTIFACT_PREVIEW_DEFAULT_BYTES,
        ge=1,
        le=_ARTIFACT_PREVIEW_MAX_BYTES,
    ),
) -> WorkflowArtifactPreviewResponse:
    """读取单个产物的安全文本预览。

    dry-run 产物只是计划占位，不读文件；Runtime 产物或用户确认的 Markdown 草稿必须位于
    各自受控根目录内，并且看起来是文本文件，才会按 UTF-8 读取有限字节。这样 Qt 端不用知道
    本地真实路径，也不会误读二进制或任意系统文件。
    """

    if get_workflow_run(task_id) is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    artifact = _find_artifact(task_id, artifact_id)
    if artifact is None:
        raise HTTPException(
            status_code=404,
            detail=f"Artifact '{artifact_id}' was not found.",
        )

    if artifact.metadata.get("dry_run") is True or artifact.uri.startswith("artifact://dry-run/"):
        return _artifact_unavailable_response(
            artifact,
            "dry-run 产物只是计划占位，真实执行后才会生成可预览文件。",
            source="dry_run",
        )

    output_path = _resolve_runtime_artifact_path(artifact)
    if output_path is None:
        return _artifact_unavailable_response(
            artifact,
            "该产物没有受控 outputs 文件路径，暂不支持预览。",
        )
    if not output_path.exists():
        return _artifact_unavailable_response(
            artifact,
            "产物文件不存在，可能已被移动或清理。",
            source="runtime_output",
        )
    if not output_path.is_file():
        return _artifact_unavailable_response(
            artifact,
            "产物路径不是普通文件，不能预览。",
            source="runtime_output",
        )
    if not _is_text_preview_candidate(artifact, output_path):
        return _artifact_unavailable_response(
            artifact,
            "该产物类型不像文本文件，已拒绝直接预览。",
            source="runtime_output",
        )

    file_size = output_path.stat().st_size
    with output_path.open("rb") as file:
        raw = file.read(max_bytes)
    text = raw.decode("utf-8", errors="replace")
    truncated = file_size > len(raw)
    return WorkflowArtifactPreviewResponse(
        task_id=task_id,
        artifact_id=artifact_id,
        available=True,
        reason="ok",
        kind=artifact.kind,
        name=artifact.name,
        uri=artifact.uri,
        mime_type=artifact.mime_type,
        source="runtime_output",
        text=text,
        bytes_read=len(raw),
        truncated=truncated,
        metadata=_artifact_preview_metadata(artifact),
    )


@router.get("/{task_id}/tool-calls", response_model=WorkflowToolCallListResponse)
async def get_task_tool_calls(task_id: str) -> WorkflowToolCallListResponse:
    """查询任务工具调用审计记录。

    这个接口让 UI 能看到 Runtime 边界：哪些工具被计划调用、是否需要权限、当前状态是什么。
    dry-run 中只会出现 simulated 记录；runtime 会记录 pending/completed/skipped 等真实状态。
    """

    if get_workflow_run(task_id) is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    tool_calls = list_workflow_tool_calls(task_id)
    return WorkflowToolCallListResponse(
        task_id=task_id,
        total=len(tool_calls),
        tool_calls=tool_calls,
    )


@router.get("/{task_id}/logs", response_model=TaskLogListResponse)
async def get_task_logs(task_id: str) -> TaskLogListResponse:
    """查询任务日志。

    这是 WebSocket 的只读兜底接口：前端如果错过实时日志，可以按 task_id 补拉缓存事件。
    """

    events = get_task_log_events(task_id)
    if events is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' logs were not found.")

    return TaskLogListResponse(task_id=task_id, total=len(events), events=events)


@router.get("/{task_id}/updates", response_model=WorkflowTaskUpdateListResponse)
async def get_task_updates(task_id: str) -> WorkflowTaskUpdateListResponse:
    """查询任务 updates 时间线。

    logs 是原始日志兜底；updates 会把日志、步骤、工具调用、权限请求和产物聚合成一条
    结构化时间线，给 Qt 后续事件流面板使用，避免前端重复拼多个接口。
    """

    run = get_workflow_run(task_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    return build_task_updates(
        run=run,
        events=get_task_log_events(task_id) or [],
        tool_calls=list_workflow_tool_calls(task_id),
        artifacts=_artifacts_with_delegated_children(task_id),
        permissions=list_runtime_permission_requests(task_id=task_id),
    )


@router.get("/{task_id}/delivery", response_model=WorkflowDeliveryCard)
async def get_task_delivery(task_id: str) -> WorkflowDeliveryCard:
    """返回结论优先的统一结果卡，供调度台和专业工作台共用。"""

    run = get_workflow_run(task_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")
    return build_delivery_card(
        run=run,
        artifacts=_artifacts_with_delegated_children(task_id),
        tool_calls=list_workflow_tool_calls(task_id),
        permissions=list_runtime_permission_requests(task_id=task_id),
    )


@router.get("/{task_id}/permissions", response_model=RuntimePermissionListResponse)
async def get_task_permissions(
    task_id: str,
    decision: RuntimePermissionDecision | None = Query(default=None),
) -> RuntimePermissionListResponse:
    """查询任务的权限请求。

    dry-run 阶段这些请求来自计划中的敏感步骤；真实执行器接入后，同一接口会用于展示
    pending/approved/denied 审计状态。
    """

    if get_workflow_run(task_id) is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    permissions = list_runtime_permission_requests(task_id=task_id, decision=decision)
    return RuntimePermissionListResponse(
        task_id=task_id,
        total=len(permissions),
        permissions=permissions,
    )


@router.post(
    "/{task_id}/permissions/{request_id}/decision",
    response_model=RuntimePermissionItem,
)
async def decide_task_permission(
    task_id: str,
    request_id: str,
    decision_input: RuntimePermissionDecisionInput,
) -> RuntimePermissionItem:
    """批准或拒绝某个权限请求。

    这个接口只写审计决策，不会触发真实工具执行；后续 Runtime 会读取 approved/denied
    决策，再决定敏感步骤能否继续。
    """

    if get_workflow_run(task_id) is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    item = record_runtime_permission_decision(
        task_id=task_id,
        request_id=request_id,
        decision_input=decision_input,
    )
    if item is None:
        raise HTTPException(
            status_code=404,
            detail=f"Permission request '{request_id}' was not found.",
        )

    return item


@router.post("/{task_id}/cancel", response_model=TaskControlResponse)
async def cancel_task(task_id: str) -> TaskControlResponse:
    """请求取消任务。

    数据工作簿和图表 PNG 导出采用协作式取消：先持久化取消终态，后台线程在安全提交点清理
    未登记文件。其它任务仍沿用既有 dry-run/Runtime 控制语义。
    """

    # 数据交付任务在统一历史中也使用 ``mode=runtime``，但它们有自己的协作式取消协议：
    # 导出线程不能被强杀，专用处理器需要先落 cancelled、清理未登记文件，再让后台线程安全返回。
    # 因此这里必须先尝试专用任务，不能让通用 Runtime 分支提前返回 ``running``。
    response = await cancel_data_transformation_task(task_id)
    if response is None:
        response = await cancel_data_chart_export_task(task_id)
    if response is None:
        response = await cancel_data_workbook_export_task(task_id)
    if response is None:
        runtime_response = await asyncio.to_thread(request_runtime_cancel, task_id)
        if runtime_response is not None:
            return _runtime_control_response(runtime_response, action="cancel")
    if response is None:
        response = request_cancel(task_id)
    if response is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")

    return response


@router.post("/{task_id}/pause", response_model=TaskControlResponse)
async def pause_task(task_id: str) -> TaskControlResponse:
    """请求 Runtime 在当前安全步骤结束后暂停。"""

    response = await asyncio.to_thread(request_runtime_pause, task_id)
    if response is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' was not found.")
    return _runtime_control_response(response, action="pause")


@router.post("/{task_id}/retry", response_model=TaskControlResponse)
async def retry_task(task_id: str) -> TaskControlResponse:
    """基于缓存 workflow_plan 重新生成 dry-run。

    这里不重新调用 LLM，也不执行真实 Agent；只是复用内存中的计划生成新的 dry-run task_id。
    """

    transform_result = get_data_transformation_task_result(task_id)
    if transform_result is not None:
        return TaskControlResponse(
            task_id=task_id,
            action="retry",
            accepted=False,
            status=transform_result.status,
            message="字段加工不会从历史重放；请回到数据工作台确认当前文件和变更预览后重新保存。",
        )

    data_result = get_data_workbook_export_task_result(task_id)
    if data_result is not None:
        # 数据任务只保存脱敏的导出合同，不保存客户原始分析目标，因此不能从历史中伪造一键
        # 重放。客户可在数据工作台保留的预览中复核后再次确认，这也避免旧任务覆盖新产物。
        return TaskControlResponse(
            task_id=task_id,
            action="retry",
            accepted=False,
            status=data_result.status,
            message="数据工作簿不会从历史重放；请回到数据工作台查看当前预览后重新确认导出。",
        )

    chart_result = get_data_chart_export_task_result(task_id)
    if chart_result is not None:
        # 图表任务同样不保存客户原始目标全文；必须重看当前版本预览再确认，避免历史任务按旧
        # 数据版本重放或覆盖新的 PNG。
        return TaskControlResponse(
            task_id=task_id,
            action="retry",
            accepted=False,
            status=chart_result.status,
            message="图表看板不会从历史重放；请回到数据工作台查看当前预览后重新确认保存。",
        )

    response = retry_workflow_dry_run(task_id)
    if response is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' retry data was not found.")

    return response
