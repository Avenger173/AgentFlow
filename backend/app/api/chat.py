from app.schemas.chat import ChatRequest, ChatResponse, WorkflowPlan
from app.schemas.conversation import ConversationContext, ConversationSessionList, ConversationTranscriptPage
from app.database.conversation_repository import (
    get_conversation_context,
    get_conversation_transcript,
    list_conversations,
)
from app.services.agent_catalog import get_agent
from app.services.conversation_memory import (
    ConversationSafetyError,
    normalize_conversation_id,
    persist_successful_conversation_turn,
    prepare_conversation,
)
from app.services.llm_chat import LlmChatError, create_llm_chat_response, is_llm_enabled
from app.services.long_term_memory import LongTermMemorySafetyError, normalize_memory_scope
from app.services.mock_chat import create_mock_chat_response
from fastapi import APIRouter, HTTPException


router = APIRouter(prefix="/api/chat", tags=["chat"])


def _automatic_read_only_activity(plan: WorkflowPlan | None) -> str:
    """返回可自动受理的单材料只读动作，其他计划一律保留既有确认流程。"""

    if (
        plan is None
        or plan.execution_readiness != "ready"
        or plan.requires_confirmation
        or plan.workspace_scope.write_paths
        or plan.workspace_scope.external_services
    ):
        return ""
    specialists = [step for step in plan.steps if step.agent != "commander_agent"]
    if len(specialists) != 1:
        return ""
    step = specialists[0]
    if step.requires_confirmation or step.execution_mode != "execute":
        return ""
    if step.agent == "knowledge_agent" and step.action == "answer_question":
        return "knowledge" if str(step.input.get("knowledge_base_id", "")).strip() else ""
    if step.agent == "data_agent" and step.action == "analyze_dataset":
        return "data" if str(step.input.get("dataset_name", "")).strip() else ""
    return ""


@router.get("/conversations", response_model=ConversationSessionList)
async def get_conversation_list(project_scope: str = "global", limit: int = 40) -> ConversationSessionList:
    """列出当前项目范围可切换的会话，不返回聊天正文。"""

    try:
        normalized_scope = normalize_memory_scope(project_scope)
    except LongTermMemorySafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return list_conversations(project_scope=normalized_scope, limit=limit)


@router.get("/conversations/{conversation_id}/messages", response_model=ConversationTranscriptPage)
async def get_conversation_messages(
    conversation_id: str,
    project_scope: str = "global",
    offset: int = 0,
    limit: int = 40,
) -> ConversationTranscriptPage:
    """按页读取一段会话的完整脱敏归档，并限制在客户选择的项目范围内。"""

    try:
        normalized_id = normalize_conversation_id(conversation_id)
        normalized_scope = normalize_memory_scope(project_scope)
    except (ConversationSafetyError, LongTermMemorySafetyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not normalized_id:
        raise HTTPException(status_code=404, detail="未找到指定会话。")
    try:
        return get_conversation_transcript(
            conversation_id=normalized_id,
            project_scope=normalized_scope,
            offset=offset,
            limit=limit,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="未找到指定会话。") from exc


@router.get("/conversations/{conversation_id}", response_model=ConversationContext)
async def get_conversation(conversation_id: str) -> ConversationContext:
    """恢复一段已脱敏的有限会话，供桌面端重启后重新展示近轮上下文。"""

    try:
        normalized_id = normalize_conversation_id(conversation_id)
    except ConversationSafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not normalized_id:
        raise HTTPException(status_code=404, detail="未找到指定会话。")
    try:
        return get_conversation_context(normalized_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="未找到指定会话。") from exc


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")
    try:
        # 前端传来的只能是 global 或 project:<稳定标识>；不能借“项目”字段夹带磁盘路径。
        request.project_scope = normalize_memory_scope(request.project_scope)
    except LongTermMemorySafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        prepared_conversation = prepare_conversation(
            conversation_id=request.conversation_id,
            project_scope=request.project_scope,
            message=message,
            supplied_materials=request.materials,
        )
    except ConversationSafetyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 之后的 Commander 规划、表达模型和成功后的会话写入都使用同一份确认过的会话快照。
    # 若客户输入“刚才那份资料”之类的明确指代，服务只复用本会话此前的材料引用，不扫描
    # 本机目录，也不会跨 project_scope 带入任何材料。
    request.conversation_id = prepared_conversation.context.session.conversation_id
    request.materials = prepared_conversation.effective_materials

    # 第一阶段默认走 commander_agent，先跑通“用户任务 -> 计划 -> 日志”的闭环。
    # 真实 Agent 路由后续交给 Commander / Agent Registry。
    agent_id = request.agent_id or "commander_agent"
    agent = get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' was not found.")

    if is_llm_enabled():
        try:
            response = await create_llm_chat_response(
                request=request,
                agent=agent,
                message=message,
                conversation=prepared_conversation,
            )
        except LlmChatError as exc:
            # 真实模型失败属于上游服务问题，用 502 让 Qt 端显示明确错误。
            # 不在错误里包含 API Key、请求头或完整供应商响应，避免泄露敏感信息。
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    else:
        response = create_mock_chat_response(
            request=request,
            agent_id=agent_id,
            message=message,
            conversation=prepared_conversation,
        )

    automatic_activity = _automatic_read_only_activity(response.workflow_plan)
    if automatic_activity:
        # 规划模型的长篇解释属于 Inspector，而不是客户会话。先保存一句真实状态，Runtime 完成后
        # 仅由 K3 Gate 已验证的最终答案追加回同一会话，避免重启后恢复一段“计划书”却看不到结论。
        response = response.model_copy(update={
            "reply": (
                "已收到，正在检索已选资料库；完成后会直接给出带来源的回答。"
                if automatic_activity == "knowledge"
                else "已收到，正在分析已选数据；完成后会直接给出趋势、差异和图表建议。"
            )
        })

    # 会话材料只以 Commander 已规范化的 plan 快照为准；因此客户端自填的非法相对引用不会
    # 被存成下一轮可复用范围。无计划的普通聊天才回退到本轮已准备的有限材料列表。
    material_bindings = (
        response.workflow_plan.material_bindings
        if response.workflow_plan is not None
        else prepared_conversation.effective_materials
    )
    persist_successful_conversation_turn(
        prepared=prepared_conversation,
        user_message=message,
        assistant_message=response.reply,
        material_bindings=material_bindings,
        task_id=response.task_id,
        plan_id=response.workflow_plan.plan_id if response.workflow_plan is not None else "",
    )
    # `request.conversation_id` 已由服务端创建或校验，响应显式回传它给 Qt 缓存。
    # response 已携带该字段；这里保留 model_copy 能兼容未来普通 Agent 返回的旧对象。
    return response.model_copy(update={"conversation_id": request.conversation_id})
