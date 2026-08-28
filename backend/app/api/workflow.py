from dataclasses import asdict

from app.schemas.workflow import (
    WorkflowCommandPolicyCheckRequest,
    WorkflowCommandPolicyCheckResponse,
    WorkflowCommandPolicyRule,
    WorkflowCommandPolicyRuleListResponse,
    WorkflowNodeContract,
    WorkflowNodeContractListResponse,
)
from app.workflow.command_policy import classify_command_policy, list_command_policy_rules
from app.workflow.node_contracts import list_node_contracts
from app.services.runtime_preferences_store import (
    RuntimePreferencesStoreError,
    load_runtime_preferences,
)
from fastapi import APIRouter, Query


router = APIRouter(prefix="/api/workflow", tags=["workflow"])


@router.get("/node-contracts", response_model=WorkflowNodeContractListResponse)
async def get_node_contracts(
    agent_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
) -> WorkflowNodeContractListResponse:
    """查询内置 Agent 节点契约。

    这个接口只暴露静态协议，不执行工具、不读取用户文件、不写产物。它让 Qt、验证脚本和
    后续编排层可以看到同一份 Node Contract：输入输出 schema、权限、失败码和评估信号。
    """

    contracts = [
        WorkflowNodeContract(**asdict(contract))
        for contract in list_node_contracts(agent_id=agent_id, action=action)
    ]
    return WorkflowNodeContractListResponse(
        total=len(contracts),
        contracts=contracts,
    )


@router.get("/command-policy/rules", response_model=WorkflowCommandPolicyRuleListResponse)
async def get_command_policy_rules() -> WorkflowCommandPolicyRuleListResponse:
    """查询命令治理规则目录，不执行命令。

    规则目录服务于产品说明、审计和未来 Runtime 壳的提示文案；真正判断某条命令是否安全，
    仍必须调用 `/command-policy/check` 按当前命令、cwd 和权限偏好实时分类。
    """

    rules = [
        WorkflowCommandPolicyRule(**rule)
        for rule in list_command_policy_rules()
    ]
    return WorkflowCommandPolicyRuleListResponse(total=len(rules), rules=rules)


@router.post("/command-policy/check", response_model=WorkflowCommandPolicyCheckResponse)
async def check_command_policy(
    request: WorkflowCommandPolicyCheckRequest,
) -> WorkflowCommandPolicyCheckResponse:
    """静态检查命令风险，不执行命令。

    这个接口是后续代码工坊 / Runtime Shell 工具的安全前置层：先解释命令属于只读、诊断、
    修改、联网还是高危，再由 UI 和权限策略决定是否允许进入真实执行。
    """

    permission_policy = request.permission_policy.strip().lower()
    if not permission_policy:
        try:
            permission_policy = load_runtime_preferences().permission_policy
        except RuntimePreferencesStoreError:
            # 命令检查是执行前安全解释入口；偏好文件异常时按推荐的 smart_confirm 降级，
            # 避免用户连静态风险都看不到。设置页仍会单独暴露偏好读取错误。
            permission_policy = "smart_confirm"

    return classify_command_policy(
        request.command,
        cwd=request.cwd,
        permission_policy=permission_policy,
    ).to_response()
