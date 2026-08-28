from app.agents.registry import AgentRegistry
from app.core.config import settings
from app.schemas.agent import AgentDescriptor, AgentRegistryStatusResponse


# AgentCatalog 是 API 层使用的门面，内部委托给 AgentRegistry 扫描 manifest。
# 这样 chat、workflow 等服务以后仍然依赖 catalog，而不用直接知道目录结构。
_REGISTRY = AgentRegistry(
    builtin_dir=settings.builtin_agents_dir,
    user_dir=settings.user_agents_dir,
)


def list_agents() -> list[AgentDescriptor]:
    return _REGISTRY.list_agents()


def get_agent(agent_id: str) -> AgentDescriptor | None:
    return _REGISTRY.get_agent(agent_id)


def registry_status() -> AgentRegistryStatusResponse:
    agents = _REGISTRY.list_agents()
    return AgentRegistryStatusResponse(
        loaded_total=len(agents),
        builtin_dir=str(settings.builtin_agents_dir),
        user_dir=str(settings.user_agents_dir),
        errors=_REGISTRY.errors(),
    )

