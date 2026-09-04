"""验证 Commander 的任务优先路由与数据图表交付计划。

不读取客户文件、不调用模型、网络或图表渲染。该回归固定三条容易退化的客户路径：
挂着数据集时制作 PPT、明确要求图表 PNG、以及无材料的普通对话。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
VERIFY_DATA_DIR = Path(tempfile.mkdtemp(prefix="agentflow_commander_intent_"))
os.environ["AGENTFLOW_DATA_DIR"] = str(VERIFY_DATA_DIR)
sys.path.insert(0, str(BACKEND_ROOT))


def _dataset_material():
    from app.schemas.chat import WorkflowMaterialBinding

    return WorkflowMaterialBinding(
        binding_id="verify_dataset",
        kind="dataset",
        ref="sales.csv",
        display_name="销售数据.csv",
        origin="client_selected",
        usage="Commander 路由离线验收材料。",
    )


def main() -> None:
    from app.services.agent_catalog import list_agents
    from app.services.commander import create_commander_plan
    from app.services.commander_intent import parse_commander_intent_candidate
    from app.schemas.chat import CommanderAgentHint

    agents = list_agents()
    dataset = _dataset_material()

    # “做 PPT”必须压过残留数据材料与 @数据工作台偏好，不能错误委派数据 Agent。
    presentation_plan = create_commander_plan(
        "@数据工作台 帮我做一个内马尔生涯数据 PPT，要有 4 种图表。",
        available_agents=agents,
        materials=[dataset],
    )
    presentation_steps = [(step.agent, step.action) for step in presentation_plan.steps]
    assert ("document_agent", "open_presentation_studio") in presentation_steps
    assert not any(agent == "data_agent" for agent, _ in presentation_steps)
    assert presentation_plan.next_action == "open_presentation_studio"
    assert presentation_plan.workspace_scope.read_paths == []

    # 数据交付必须形成“分析 -> PNG 图表”的显式链，并把写入变成权限可见的受控步骤。
    chart_plan = create_commander_plan(
        "请分析当前 CSV 的趋势并生成图表 PNG。",
        available_agents=agents,
        materials=[dataset],
    )
    chart_step = next(step for step in chart_plan.steps if step.action == "export_chart_dashboard")
    analysis_step = next(step for step in chart_plan.steps if step.action == "analyze_dataset")
    assert chart_step.depends_on == [analysis_step.id]
    assert chart_step.required_permissions == ["file_read", "file_write"]
    assert chart_step.requires_confirmation is True
    assert chart_plan.next_action == "review_plan_and_confirm_permissions"
    assert chart_plan.workspace_scope.read_paths == ["data/datasets/sales.csv"]
    assert chart_plan.workspace_scope.write_paths == ["data/outputs/<runtime_task_id>/"]
    assert chart_plan.validation_errors == [], chart_plan.validation_errors

    # 残留的已选材料只是候选上下文。没有处理意图的普通聊天不能被 CSV 反向劫持。
    conversation_plan = create_commander_plan(
        "梅西是不是比 C 罗强一些？",
        available_agents=agents,
        materials=[dataset],
    )
    assert conversation_plan.intent == "direct_answer"
    assert conversation_plan.workspace_scope.read_paths == []
    assert conversation_plan.next_action == "execute_after_confirm"

    # 同一会话的省略式短追问由表达模型依据受控近轮上下文承接，不能被规则层仅因字数短
    # 误改为“你希望完成什么”。新会话的孤立短句仍保留原有澄清，避免脱离上下文乱猜。
    isolated_short_plan = create_commander_plan("思想演变", available_agents=agents)
    assert isolated_short_plan.next_action == "ask_clarifying_questions"
    continued_short_plan = create_commander_plan(
        "思想演变",
        available_agents=agents,
        has_conversation_context=True,
    )
    assert continued_short_plan.intent == "direct_answer"
    assert continued_short_plan.clarifying_questions == []

    # R5.5B：模型候选只能使用固定枚举，Harness 仍根据已绑定材料和 Action Contract
    # 生成最终计划。候选无需出现 CSV/图表关键词，也不能凭空读取未选择的数据。
    semantic_data_candidate = parse_commander_intent_candidate(
        '{"version":"agentflow.commander_intent.v1","intent":"data","is_follow_up":false,'
        '"delivery":"chart_png","preferred_agents":["data_agent"],'
        '"required_material_kinds":["dataset"],"confidence":0.91,"clarifying_question":""}'
    )
    semantic_data_plan = create_commander_plan(
        "帮我把这些结果做得更直观。",
        available_agents=agents,
        materials=[dataset],
        semantic_intent=semantic_data_candidate,
    )
    assert semantic_data_plan.intent_resolution.source == "model"
    assert semantic_data_plan.intent_resolution.applied is True
    assert any(step.action == "analyze_dataset" for step in semantic_data_plan.steps)
    assert any(step.action == "export_chart_dashboard" for step in semantic_data_plan.steps)

    # 明确的客户原句优先于模型候选：即使候选误判为数据，PPT 请求也不能同时委派
    # 数据工作台，避免一次模型分类偏差把任务无端扩展为多 Agent 组合。
    explicit_presentation_plan = create_commander_plan(
        "帮我制作一份产品介绍 PPT。",
        available_agents=agents,
        materials=[dataset],
        semantic_intent=semantic_data_candidate,
    )
    assert any(step.action == "open_presentation_studio" for step in explicit_presentation_plan.steps)
    assert not any(step.agent == "data_agent" for step in explicit_presentation_plan.steps)

    semantic_document_candidate = parse_commander_intent_candidate(
        '{"version":"agentflow.commander_intent.v1","intent":"document","is_follow_up":false,'
        '"delivery":"analysis","preferred_agents":["document_agent"],'
        '"required_material_kinds":["document"],"confidence":0.88,"clarifying_question":""}'
    )
    missing_semantic_document = create_commander_plan(
        "请按刚才的要求处理。",
        available_agents=agents,
        semantic_intent=semantic_document_candidate,
    )
    assert missing_semantic_document.intent_resolution.source == "model"
    assert any("请先导入并选择 TXT、Markdown、PDF 或 DOCX" in item for item in missing_semantic_document.clarifying_questions)
    assert not any("已点名" in item for item in missing_semantic_document.clarifying_questions)

    semantic_fresh_candidate = parse_commander_intent_candidate(
        '{"version":"agentflow.commander_intent.v1","intent":"fresh_external_information",'
        '"is_follow_up":false,"delivery":"none","preferred_agents":[],'
        '"required_material_kinds":[],"confidence":0.93,"clarifying_question":""}'
    )
    semantic_fresh_plan = create_commander_plan(
        "帮我看看外面的最新情况。",
        available_agents=agents,
        semantic_intent=semantic_fresh_candidate,
    )
    assert semantic_fresh_plan.intent == "fresh_external_information"
    assert not any(step.agent in {"document_agent", "data_agent", "knowledge_agent"} for step in semantic_fresh_plan.steps)

    # 从数据页跳转后的自然说法仍然有效；“当前数据”会把该材料显式带入数据 Agent，
    # 因此不需要客户重新找一次文件或重复指定路径。
    contextual_data_plan = create_commander_plan(
        "请分析当前这份数据，并制作折线图。",
        available_agents=agents,
        materials=[dataset],
    )
    assert any(step.action == "analyze_dataset" for step in contextual_data_plan.steps)
    assert any(step.action == "export_chart_dashboard" for step in contextual_data_plan.steps)

    # 客户明确点名数据工作台而没有绑定文件时，仍然要求选择材料，不能猜测目录内容。
    missing_data = create_commander_plan(
        "@数据工作台 帮我分析并制作图表。",
        available_agents=agents,
        agent_hints=[CommanderAgentHint(agent_id="data_agent", source="mention")],
    )
    assert missing_data.next_action == "ask_clarifying_questions"
    assert any("尚未选择数据文件" in item for item in missing_data.clarifying_questions)

    print("Commander intent routing verification passed.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(VERIFY_DATA_DIR, ignore_errors=True)
