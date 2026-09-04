from uuid import uuid4

from app.schemas.chat import ChatRequest, ChatResponse, WorkflowPlan
from app.services.agent_catalog import list_agents
from app.services.commander import COMMANDER_AGENT_ID, build_commander_reply, create_commander_plan
from app.services.commander_memory import retrieve_commander_memory_context
from app.services.conversation_memory import PreparedConversation, build_conversation_plan_summary
from app.services.runtime_preferences_store import load_runtime_preferences
from app.workflow.dry_run import run_workflow_dry_run


def create_mock_chat_response(
    request: ChatRequest,
    agent_id: str,
    message: str,
    conversation: PreparedConversation | None = None,
) -> ChatResponse:
    # 模拟模式只生成稳定、可预测的响应，方便 Qt 端先完成协议和 UI 联调。
    task_id = request.task_id or f"task_{uuid4().hex[:12]}"
    workflow_plan = None
    workflow_run = None
    if agent_id == COMMANDER_AGENT_ID:
        # 同一次请求只读取一次 AgentCatalog。Registry 内部会做 mtime/size 缓存，
        # 这里避免规划和 dry-run 各自触发一轮列表构建。
        agents = list_agents()
        runtime_preferences = load_runtime_preferences().to_workflow_preferences()
        memory_context = retrieve_commander_memory_context(
            user_goal=message,
            preferences=runtime_preferences,
            project_scope=request.project_scope,
        )
        workflow_plan = create_commander_plan(
            message,
            available_agents=agents,
            preferences=runtime_preferences,
            materials=request.materials,
            agent_hints=request.agent_hints,
            memory_context=memory_context,
            project_scope=request.project_scope,
            conversation_id=request.conversation_id or "",
            conversation_context_summary=(build_conversation_plan_summary(conversation) if conversation else []),
            has_conversation_context=bool(
                conversation
                and (
                    conversation.context.recent_messages
                    or conversation.context.session.summary.strip()
                )
            ),
        )
        workflow_run = run_workflow_dry_run(
            task_id=task_id,
            plan=workflow_plan,
            available_agents=agents,
        )
    reply = build_mock_reply(agent_id=agent_id, message=message, has_plan=workflow_plan is not None)

    return ChatResponse(
        task_id=task_id,
        agent_id=agent_id,
        reply=reply,
        conversation_id=request.conversation_id or "",
        workflow_plan=workflow_plan,
        workflow_run=workflow_run,
    )


def build_mock_reply(agent_id: str, message: str, has_plan: bool) -> str:
    if has_plan:
        return build_commander_reply(mode="mock", has_plan=True)

    return f"已收到发给 {agent_id} 的消息，当前后端处于模拟聊天模式。消息摘要：{message[:80]}"


def build_mock_workflow_plan(message: str) -> WorkflowPlan:
    """保留旧函数名，避免脚本或前端验证里还有临时引用。

    实际规划已经交给 Commander 服务。这里传入 `list_agents()`，会走 Registry 的
    mtime/size 缓存：正常请求只做轻量 stat，不重复解析 manifest YAML。
    """

    preferences = load_runtime_preferences().to_workflow_preferences()
    return create_commander_plan(
        message,
        available_agents=list_agents(),
        preferences=preferences,
        memory_context=retrieve_commander_memory_context(
            user_goal=message,
            preferences=preferences,
        ),
    )
