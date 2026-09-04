from __future__ import annotations

import json
from typing import Iterable

from pydantic import ValidationError

from app.schemas.agent import AgentDescriptor
from app.schemas.chat import CommanderAgentHint, WorkflowMaterialBinding
from app.schemas.commander_intent import CommanderIntentCandidate
from app.services.model_gateway import ModelGatewayError, ModelRuntime


class CommanderIntentResolutionError(ValueError):
    """语义意图候选不可用时的受控错误，调用方应回退到确定性规划。"""


_SEMANTIC_INTENT_MARKERS = (
    "@",
    "文档", "文件", "资料", "pdf", "word", "docx", "markdown",
    "数据", "表格", "csv", "excel", "图表", "字段", "工作簿",
    "知识库", "资料库", "索引", "引用", "来源",
    "ppt", "演示", "幻灯片", "制作", "生成", "导出", "审查", "分析",
    "联网", "搜索", "检索", "百科", "新闻", "实时", "行情", "天气", "赛程",
)


def should_resolve_commander_intent(
    message: str,
    *,
    has_conversation_context: bool,
    agent_hints: Iterable[CommanderAgentHint],
    materials: Iterable[WorkflowMaterialBinding],
) -> bool:
    """只为存在路由歧义或连续语义的输入增加短 JSON 回合。"""

    normalized = message.strip().lower()
    if not normalized:
        return False
    if has_conversation_context and len(normalized) <= 80:
        return True
    if any(_marker in normalized for _marker in _SEMANTIC_INTENT_MARKERS):
        return True
    return bool(list(agent_hints) or list(materials)) and any(
        token in normalized for token in ("当前", "这份", "那个", "它", "继续")
    )


async def resolve_commander_intent_candidate(
    *,
    runtime: ModelRuntime,
    message: str,
    conversation_context: str,
    agents: Iterable[AgentDescriptor],
    materials: Iterable[WorkflowMaterialBinding],
    agent_hints: Iterable[CommanderAgentHint],
) -> CommanderIntentCandidate:
    """让模型在固定枚举内理解语义；它不能产生 Tool、路径或权限决定。"""

    system_prompt = _build_intent_system_prompt(agents)
    user_payload = {
        "current_message": message[:1200],
        "conversation_context": conversation_context[:2200],
        "bound_materials": [
            {
                "kind": item.kind,
                "name": (item.display_name or item.ref)[:160],
            }
            for item in list(materials)[:8]
        ],
        "agent_hints": [item.agent_id for item in list(agent_hints)[:3]],
    }
    try:
        content = await runtime.chat_json(
            system_prompt=system_prompt,
            user_message=json.dumps(user_payload, ensure_ascii=False, separators=(",", ":")),
            maximum_tokens=360,
        )
    except ModelGatewayError as exc:
        raise CommanderIntentResolutionError(str(exc)) from exc
    return parse_commander_intent_candidate(content)


def parse_commander_intent_candidate(content: str) -> CommanderIntentCandidate:
    """从兼容供应商偶尔夹带的 Markdown 中提取首个合法 Intent JSON。"""

    decoder = json.JSONDecoder()
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            return CommanderIntentCandidate.model_validate(payload)
        except ValidationError as exc:
            raise CommanderIntentResolutionError("模型意图候选未通过固定契约校验。") from exc
    raise CommanderIntentResolutionError("模型没有返回合法的意图候选 JSON。")


def _build_intent_system_prompt(agents: Iterable[AgentDescriptor]) -> str:
    ready_agents = {
        agent.id
        for agent in agents
        if agent.runtime_ready and agent.id in {"document_agent", "data_agent", "knowledge_agent"}
    }
    catalog = ["direct_answer：普通常识、解释、创作或无需工具的对话。"]
    if "document_agent" in ready_agents:
        catalog.append("document：仅处理客户已选择的 TXT/Markdown/PDF/DOCX 文档。")
    if "data_agent" in ready_agents:
        catalog.append("data：仅处理客户已导入并选择的 CSV/XLSX 数据。")
    if "knowledge_agent" in ready_agents:
        catalog.append("knowledge：仅回答客户已选择且索引完成的资料库。")
    catalog.extend(
        [
            "presentation：制作 PPT 主题，可不依赖材料，但写出文件仍由 Harness 确认。",
            "public_reference：仅指固定百科型公开资料参考，不是通用网页搜索。",
            "fresh_external_information：最近新闻、实时行情、天气、赛程等；当前没有获批连接时只能说明边界。",
            "clarify：当前目标无法从本轮和受控上下文确定。",
        ]
    )
    return (
        "你是 AgentFlow 总指挥的语义意图解析器。只返回一个 JSON 对象，不要 Markdown、解释、"
        "推理过程或额外字段。你不调用 Agent、Tool，不读取文件，不授权联网或写入。\n"
        "任务：结合当前消息、同会话的受控上下文、已绑定材料和弱 @ 偏好，选择最贴切的意图。"
        "短句如“思想演变”“第二种”可能是上一轮的续话，不要因为字数短就选 clarify。\n"
        "能力目录：" + "；".join(catalog) + "\n"
        "JSON 契约："
        '{"version":"agentflow.commander_intent.v1","intent":"direct_answer|document|data|knowledge|presentation|public_reference|fresh_external_information|clarify",'
        '"is_follow_up":true,"delivery":"answer|analysis|chart_png|analysis_workbook|presentation|public_reference_search|none",'
        '"preferred_agents":["document_agent|data_agent|knowledge_agent"],'
        '"required_material_kinds":["document|dataset|knowledge_base"],'
        '"confidence":0.0,"clarifying_question":""}。'
        "@ 只表示偏好；不要把它当作唯一依据，也不要因它扩大权限或虚构材料。"
    )
