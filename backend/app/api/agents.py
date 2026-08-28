from app.schemas.agent import (
    AgentActionAdmissionDescriptor,
    AgentActionAdmissionListResponse,
    AgentDescriptor,
    AgentListResponse,
    AgentRegistryStatusResponse,
)
from app.services.agent_catalog import get_agent, list_agents, registry_status
from app.workflow.action_admission import list_action_admissions
from fastapi import APIRouter, HTTPException


router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("", response_model=AgentListResponse)
async def get_agents() -> AgentListResponse:
    agents = list_agents()
    return AgentListResponse(total=len(agents), agents=agents)


@router.get("/registry/status", response_model=AgentRegistryStatusResponse)
async def get_agent_registry_status() -> AgentRegistryStatusResponse:
    # 状态接口只暴露扫描结果和错误，不会执行或导入任何 Agent 插件代码。
    return registry_status()


@router.get("/action-admissions", response_model=AgentActionAdmissionListResponse)
async def get_agent_action_admissions() -> AgentActionAdmissionListResponse:
    """返回当前已确认可由 Commander 使用的具体动作目录。"""

    actions = [
        AgentActionAdmissionDescriptor(
            agent_id=item.agent_id,
            action=item.action,
            execution_mode=item.execution_mode,
            requires_runtime_ready=item.requires_runtime_ready,
            material_kind=(
                item.material_kind
                if item.material_kind in {"document", "dataset", "knowledge_base"}
                else None
            ),
            expected_output=item.expected_output,
            verification_scope=item.verification_scope,
            recovery_hint=item.recovery_hint,
        )
        for item in list_action_admissions()
    ]
    return AgentActionAdmissionListResponse(total=len(actions), actions=actions)


@router.get("/{agent_id}", response_model=AgentDescriptor)
async def get_agent_detail(agent_id: str) -> AgentDescriptor:
    agent = get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' was not found.")
    return agent
